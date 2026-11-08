# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
#  Original code Copyright (C) BlenderKit developers (bk_maya / bk_client).
#  Adapted for Kitbridge.
#
# ##### END GPL LICENSE BLOCK #####

"""Cinema 4D package importer (Milestone 2b).

Consumes an unpacked Kitbridge asset directory containing ``kitbridge.json``,
``geometry.fbx``, and ``textures/*`` and imports it into the active Cinema 4D
document for Redshift, Arnold, or Standard/Physical.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from . import compat, geometry, hdr
from .compat import c4d
from .materials import arnold as arnold_mat
from .materials import redshift as redshift_mat
from .materials import standard as standard_mat
from .materials.base import (
    KITBRIDGE_MAT_BC_ID,
    KITBRIDGE_MAT_RENDERER_BC_ID,
    MaterialPlan,
    build_package_material_plans,
    unique_material_name,
)
from .renderer import (
    RENDERER_ARNOLD,
    RENDERER_REDSHIFT,
    RENDERER_STANDARD,
    detect_renderer,
    normalise_renderer,
    renderer_installed,
)

log =logging.getLogger("kitbridge")


@dataclass
class MaterialImportInfo:
    """Diagnostic summary for a single imported or reused material."""

    name: str
    manifest_name: str
    renderer: str
    node_count: int = 0
    connected_channels: List[str] = field(default_factory=list)
    value_channels: List[str] = field(default_factory=list)
    reused: bool = False


@dataclass
class ImportResult:
    """Result returned by :func:`import_package`."""

    asset_name: str
    asset_type: str
    renderer: str
    root_object: Any = None
    objects: List[Any] = field(default_factory=list)
    materials: List[Any] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    material_details: List[MaterialImportInfo] = field(default_factory=list)
    errors: List[Dict[str, str]] = field(default_factory=list)
    applied_to_selection: bool = False
    has_transmission: bool = False


def _record_error(
    result: ImportResult,
    step: str,
    exc: BaseException,
) -> None:
    tb_str = traceback.format_exc()
    msg = f"[{step}] {type(exc).__name__}: {exc}"
    log.error("Kitbridge import error at %s: %s\n%s", step, exc, tb_str)
    result.warnings.append(msg)
    result.errors.append(
        {
            "step": step,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": tb_str,
        }
    )


def _find_reusable_material(
    doc: Any,
    dedupe_key: str,
    renderer: str,
) -> Optional[Any]:
    """Return an existing material in *doc* matching *dedupe_key* and *renderer*, if any."""
    if doc is None or not hasattr(doc, "GetMaterials"):
        return None
    for mat in doc.GetMaterials():
        try:
            bc = mat.GetDataInstance() if hasattr(mat, "GetDataInstance") else None
            if bc is None:
                continue
            existing_key = bc.GetString(KITBRIDGE_MAT_BC_ID)
            if existing_key != dedupe_key:
                continue
            existing_renderer = bc.GetString(KITBRIDGE_MAT_RENDERER_BC_ID)
            if existing_renderer and existing_renderer != renderer:
                continue
            return mat
        except Exception:
            continue
    return None


def _tag_kitbridge_material(mat: Any, dedupe_key: str, renderer: str) -> None:
    if mat is None or not hasattr(mat, "GetDataInstance"):
        return
    try:
        bc = mat.GetDataInstance()
        if bc is not None:
            bc.SetString(KITBRIDGE_MAT_BC_ID, str(dedupe_key))
            bc.SetString(KITBRIDGE_MAT_RENDERER_BC_ID, str(renderer))
    except Exception:
        pass


def _inspect_existing_material_nodes(mat: Any, renderer: str) -> int:
    """Count nodes on a reused material for reporting."""
    try:
        if renderer == RENDERER_REDSHIFT:
            graph = compat.get_existing_graph(mat, redshift_mat.RS_NODESPACE_ID)
            return len(compat.list_graph_nodes(graph))
        if renderer == RENDERER_ARNOLD:
            graph = compat.get_existing_graph(mat, arnold_mat.ARNOLD_NODESPACE_ID)
            return len(compat.list_graph_nodes(graph))
    except Exception:
        pass
    return 0


def _build_single_material(
    doc: Any,
    plan: MaterialPlan,
    renderer: str,
) -> Tuple[Any, MaterialImportInfo, List[str]]:
    """Build or reuse a C4D material for *plan* under *renderer*."""
    reused_mat = _find_reusable_material(doc, plan.dedupe_key, renderer)
    if reused_mat is not None:
        node_cnt = _inspect_existing_material_nodes(reused_mat, renderer)
        connected = [
            ch
            for ch, op in plan.ops.items()
            if op.is_texture and ch != "specular"
        ]
        val_chs = [
            ch
            for ch, op in plan.ops.items()
            if op.is_value and ch != "specular"
        ]
        info = MaterialImportInfo(
            name=str(reused_mat.GetName()) if hasattr(reused_mat, "GetName") else plan.name,
            manifest_name=plan.name,
            renderer=renderer,
            node_count=node_cnt,
            connected_channels=connected,
            value_channels=val_chs,
            reused=True,
        )
        return reused_mat, info, list(plan.warnings)

    existing_names = [
        str(m.GetName())
        for m in (doc.GetMaterials() if hasattr(doc, "GetMaterials") else ())
        if hasattr(m, "GetName")
    ]
    target_name = unique_material_name(plan.name, existing_names)

    warnings: List[str] = []
    built: Optional[redshift_mat.BuiltMaterialResult] = None

    if renderer == RENDERER_REDSHIFT:
        try:
            built = redshift_mat.build_redshift_material(plan, material_name=target_name)
        except Exception as exc:
            log.warning(
                "Redshift material build failed for %r (%s); falling back to Standard material.",
                plan.name,
                exc,
            )
            warnings.append(
                f"Material '{plan.name}': Redshift node graph build failed ({exc}); fell back to Standard material."
            )
            built = standard_mat.build_standard_material(plan, material_name=target_name)

    elif renderer == RENDERER_ARNOLD:
        try:
            built = arnold_mat.build_arnold_material(plan, material_name=target_name)
        except Exception as exc:
            log.warning(
                "Arnold material build failed for %r (%s); falling back to Standard material.",
                plan.name,
                exc,
            )
            warnings.append(
                f"Material '{plan.name}': Arnold node graph build failed ({exc}); fell back to Standard material."
            )
            built = standard_mat.build_standard_material(plan, material_name=target_name)

    else:
        built = standard_mat.build_standard_material(plan, material_name=target_name)

    mat = built.material
    _tag_kitbridge_material(mat, plan.dedupe_key, renderer)

    doc.InsertMaterial(mat)
    if c4d is not None and hasattr(doc, "AddUndo"):
        try:
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, mat)
        except Exception:
            pass

    all_warnings = list(built.warnings) + warnings
    info = MaterialImportInfo(
        name=str(mat.GetName()) if hasattr(mat, "GetName") else target_name,
        manifest_name=plan.name,
        renderer=renderer,
        node_count=built.node_count,
        connected_channels=list(built.connected_channels),
        value_channels=list(built.value_channels),
        reused=False,
    )
    return mat, info, all_warnings


def import_package(
    doc: Any,
    package_dir: str,
    renderer: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
) -> ImportResult:
    """Import an unpacked Kitbridge asset directory into Cinema 4D document *doc*.

    Parameters
    ----------
    doc:
        Target ``c4d.documents.BaseDocument``. If ``None``, resolves via
        ``c4d.documents.GetActiveDocument()``.
    package_dir:
        Directory containing ``kitbridge.json`` and packed textures/geometry.
    renderer:
        Optional renderer override (``"redshift"``, ``"arnold"``, ``"standard"``).
        When ``None``, auto-detected from *doc*'s active render engine.
    options:
        Optional dictionary of importer flags:
        - ``apply_to_selection`` (bool): For ``material`` assets, assign to
          selected object(s) instead of importing the preview sphere.
        - ``replace_existing_hdr`` (bool): For ``hdr`` assets, replace any
          previous Kitbridge dome light in *doc* (default ``True``).
    """
    opts = options if isinstance(options, dict) else {}

    if doc is None and c4d is not None and hasattr(c4d, "documents"):
        doc = c4d.documents.GetActiveDocument()

    resolved_renderer = (
        normalise_renderer(renderer)
        if renderer is not None
        else detect_renderer(doc)
    )
    fallback_warning: Optional[str] = None
    if resolved_renderer and not renderer_installed(resolved_renderer):
        # e.g. Arnold chosen but not installed in this Cinema 4D: its node space and tags are
        # missing, and creating an Arnold tag aborts the whole import.
        fallback_warning = (
            f"{resolved_renderer.capitalize()} is not installed in this Cinema 4D; "
            "imported with Standard materials."
        )
        resolved_renderer = RENDERER_STANDARD

    result = ImportResult(
        asset_name=os.path.basename(os.path.normpath(package_dir or "")),
        asset_type="unknown",
        renderer=resolved_renderer,
    )
    if fallback_warning:
        result.warnings.append(fallback_warning)

    # 1. Read kitbridge.json manifest
    manifest_path = os.path.join(package_dir, "kitbridge.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception as exc:
        _record_error(result, "read_manifest", exc)
        return result

    asset_meta = manifest.get("asset") if isinstance(manifest.get("asset"), dict) else {}
    result.asset_name = str(asset_meta.get("name") or result.asset_name).strip() or result.asset_name
    result.asset_type = str(asset_meta.get("type") or "model").strip().lower()

    if c4d is None or doc is None:
        _record_error(
            result,
            "check_c4d_document",
            RuntimeError("Cinema 4D Python SDK (c4d) or active BaseDocument is unavailable."),
        )
        return result

    undo_started = False
    try:
        if hasattr(doc, "StartUndo"):
            doc.StartUndo()
            undo_started = True

        # Dispatch by asset type
        if result.asset_type == "hdr":
            try:
                hdr_outcome = hdr.import_hdr(
                    doc,
                    manifest,
                    package_dir,
                    renderer=resolved_renderer,
                    options=opts,
                )
                result.root_object = hdr_outcome.dome_object
                result.objects.extend(hdr_outcome.objects)
                result.materials.extend(hdr_outcome.materials)
                result.warnings.extend(hdr_outcome.warnings)
                for hmat in hdr_outcome.materials:
                    mname = str(hmat.GetName()) if hasattr(hmat, "GetName") else f"{result.asset_name}_HDR"
                    result.material_details.append(
                        MaterialImportInfo(
                            name=mname,
                            manifest_name=mname,
                            renderer=resolved_renderer,
                            node_count=hdr_outcome.node_count,
                            connected_channels=list(hdr_outcome.connected_channels),
                        )
                    )
            except Exception as exc:
                _record_error(result, f"import_hdr:{result.asset_name}", exc)

        elif result.asset_type in ("model", "material"):
            # Build renderer-agnostic MaterialPlans first
            plans: List[MaterialPlan] = []
            try:
                plans = build_package_material_plans(manifest, package_dir)
            except Exception as exc:
                _record_error(result, f"build_material_plans:{result.asset_name}", exc)

            materials_by_manifest_name: Dict[str, Any] = {}
            plans_by_manifest_name: Dict[str, MaterialPlan] = {}

            result.has_transmission = any(plan.has_transmission for plan in plans)
            for plan in plans:
                plans_by_manifest_name[plan.name] = plan
                try:
                    mat_obj, mat_info, mat_warnings = _build_single_material(
                        doc,
                        plan,
                        resolved_renderer,
                    )
                    materials_by_manifest_name[plan.name] = mat_obj
                    result.materials.append(mat_obj)
                    result.material_details.append(mat_info)
                    result.warnings.extend(mat_warnings)
                except Exception as exc:
                    _record_error(result, f"build_material:{plan.name}", exc)

            # Import FBX geometry (or apply material to active selection)
            try:
                geom_outcome = geometry.import_geometry(
                    doc,
                    manifest,
                    package_dir,
                    materials_by_manifest_name,
                    options=opts,
                )
                result.root_object = geom_outcome.root_object
                result.objects.extend(geom_outcome.objects)
                result.materials.extend(geom_outcome.kept_fbx_materials)
                result.warnings.extend(geom_outcome.warnings)
                result.applied_to_selection = geom_outcome.applied_to_selection

                # Attach renderer-specific object tags where required by material plans
                tagged_rs_disp_objs: Set[int] = set()
                tagged_arnold_opaque_objs: Set[int] = set()

                for obj, mat_manifest_name in geom_outcome.object_material_names:
                    plan_for_obj = plans_by_manifest_name.get(mat_manifest_name)
                    if plan_for_obj is None or obj is None:
                        continue

                    if (
                        resolved_renderer == RENDERER_REDSHIFT
                        and plan_for_obj.has_displacement
                        and id(obj) not in tagged_rs_disp_objs
                    ):
                        tagged_rs_disp_objs.add(id(obj))
                        try:
                            redshift_mat.apply_displacement_object_tag(doc, obj, result.warnings)
                        except Exception as exc:
                            _record_error(result, f"rs_displacement_tag:{obj.GetName()}", exc)

                    if (
                        resolved_renderer == RENDERER_ARNOLD
                        and plan_for_obj.is_non_opaque
                        and id(obj) not in tagged_arnold_opaque_objs
                    ):
                        tagged_arnold_opaque_objs.add(id(obj))
                        try:
                            arnold_mat.apply_non_opaque_object_tag(doc, obj, result.warnings)
                        except Exception as exc:
                            _record_error(result, f"arnold_opaque_tag:{obj.GetName()}", exc)

                # Redshift over-brightens glass shells refracted as if solid (a lamp cover: 0.91 against
                # 0.47 in Blender). Arnold handles them and got worse thin-walled, so it stays as it is.
                thin_walled: Set[str] = set()
                for obj, mat_manifest_name in geom_outcome.object_material_names:
                    plan_for_obj = plans_by_manifest_name.get(mat_manifest_name)
                    if (
                        plan_for_obj is None
                        or not plan_for_obj.has_transmission
                        or mat_manifest_name in thin_walled
                        or resolved_renderer != RENDERER_REDSHIFT
                    ):
                        continue
                    try:
                        if geometry.is_thin_shell(obj):
                            thin_walled.add(mat_manifest_name)
                            redshift_mat.set_thin_walled(materials_by_manifest_name[mat_manifest_name])
                    except Exception as exc:
                        result.warnings.append(f"Could not set thin-walled glass on {mat_manifest_name}: {exc}")

            except Exception as exc:
                _record_error(result, f"import_geometry:{result.asset_name}", exc)

        else:
            result.warnings.append(f"Unsupported asset type in manifest: {result.asset_type!r}")

    finally:
        if undo_started and hasattr(doc, "EndUndo"):
            try:
                doc.EndUndo()
            except Exception:
                pass
        if c4d is not None and hasattr(c4d, "EventAdd"):
            try:
                c4d.EventAdd()
            except Exception:
                pass

    return result


__all__ = [
    "ImportResult",
    "MaterialImportInfo",
    "RENDERER_ARNOLD",
    "RENDERER_REDSHIFT",
    "RENDERER_STANDARD",
    "detect_renderer",
    "import_package",
]
