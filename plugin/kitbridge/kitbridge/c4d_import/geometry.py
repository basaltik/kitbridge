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

"""FBX geometry loader, material tag relinker, and selection assignment (Milestone 2b)."""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import compat
from .compat import c4d

log = logging.getLogger("kitbridge")

# Cinema 4D FBX plugin IDs (1026369 = FORMAT_FBX_IMPORT, 1026370 = FORMAT_FBX_EXPORT / spec ref)
FBX_IMPORT_PLUGIN_IDS = (1026369, 1026370)


@dataclass
class GeometryImportOutcome:
    """Result of importing geometry or applying a material asset to selection."""

    root_object: Any = None
    objects: List[Any] = field(default_factory=list)
    kept_fbx_materials: List[Any] = field(default_factory=list)
    object_material_names: List[Tuple[Any, str]] = field(default_factory=list)
    applied_to_selection: bool = False
    warnings: List[str] = field(default_factory=list)


def get_selected_objects(doc: Any) -> List[Any]:
    """Return currently selected objects in *doc*."""
    if c4d is None or doc is None:
        return []
    try:
        flags = getattr(c4d, "GETACTIVEOBJECTFLAGS_NONE", 0)
        objs = doc.GetActiveObjects(flags)
        if objs:
            return list(objs)
    except Exception:
        pass
    try:
        active = doc.GetActiveObject()
        if active is not None:
            return [active]
    except Exception:
        pass
    return []


def _find_fbx_importer_plugin() -> Tuple[Any, Optional[int]]:
    if c4d is None:
        return None, None
    plug_type = getattr(c4d, "PLUGINTYPE_SCENELOADER", 10)
    candidate_ids: List[int] = []
    fmt_const = getattr(c4d, "FORMAT_FBX_IMPORT", None)
    if isinstance(fmt_const, int):
        candidate_ids.append(int(fmt_const))
    for pid in FBX_IMPORT_PLUGIN_IDS:
        if pid not in candidate_ids:
            candidate_ids.append(pid)

    for pid in candidate_ids:
        try:
            plug = c4d.plugins.FindPlugin(pid, plug_type)
            if plug is not None:
                compat.record_resolution(
                    "fbx.importer_plugin",
                    {
                        "plugin_id": pid,
                        "name": plug.GetName() if hasattr(plug, "GetName") else "FBX",
                    },
                )
                return plug, pid
        except Exception:
            pass

    # Fallback: scan all scene loader plugins for "fbx"
    try:
        for plug in c4d.plugins.FilterPluginList(plug_type, False):
            pname = str(plug.GetName() or "") if hasattr(plug, "GetName") else ""
            if "fbx" in pname.lower():
                pid = int(plug.GetID()) if hasattr(plug, "GetID") else None
                compat.record_resolution(
                    "fbx.importer_plugin",
                    {"plugin_id": pid, "name": pname, "source": "FilterPluginList"},
                )
                return plug, pid
    except Exception:
        pass

    compat.record_resolution("fbx.importer_plugin", {"found": False, "tried_ids": candidate_ids})
    return None, None


def _configure_fbx_importer(meters_per_unit: float = 1.0) -> Tuple[Any, Dict[Any, Any]]:
    """Configure FBX importer settings at runtime via ``GetDescription()`` and return saved state."""
    saved: Dict[Any, Any] = {}
    if c4d is None:
        return None, saved

    plug, _ = _find_fbx_importer_plugin()
    if plug is None:
        return None, saved

    private_data: Dict[str, Any] = {}
    msg_id = getattr(c4d, "MSG_RETRIEVEPRIVATEDATA", 1001081)
    try:
        if not plug.Message(msg_id, private_data):
            return None, saved
    except Exception as exc:
        log.debug("FBX importer MSG_RETRIEVEPRIVATEDATA failed: %s", exc)
        return None, saved

    imexporter = private_data.get("imexporter")
    if imexporter is None:
        return None, saved

    def _save_and_set(resolved: Optional[compat.ResolvedParam], val: Any) -> bool:
        if resolved is None:
            return False
        # c4d.DescID is unhashable, so remember the old value under the plain int id.
        k = resolved.int_id
        if k is not None and k not in saved:
            try:
                saved[k] = imexporter[k]
            except Exception as exc:
                log.debug("Could not read FBX importer pref %s before changing it: %s", k, exc)
        return compat.set_resolved_parameter(imexporter, resolved, val)

    dtype_bool = getattr(c4d, "DTYPE_BOOL", 400006001)
    dtype_long = getattr(c4d, "DTYPE_LONG", 15)

    # 1. Scale: 1 m in the file = 100 cm in C4D
    scale_param = compat.resolve_description_param(
        imexporter,
        context="fbx.import.scale",
        idents=("FBXIMPORT_SCALE", "STR_SCALE"),
        names=("Scale",),
        fallback_c4d_symbols=("FBXIMPORT_SCALE",),
    )
    if scale_param is not None and hasattr(c4d, "UnitScaleData"):
        try:
            unit_scale = c4d.UnitScaleData()
            doc_unit_cm = getattr(c4d, "DOCUMENT_UNIT_CM", 4)
            unit_scale.SetUnitScale(float(meters_per_unit), doc_unit_cm)
            if not _save_and_set(scale_param, unit_scale):
                _save_and_set(scale_param, float(meters_per_unit))
        except Exception as exc:
            log.debug("FBX scale UnitScaleData setup failed: %s", exc)

    # 2. Materials & Textures ON
    for ctx_label, idents, names, fallbacks in (
        (
            "fbx.import.grp_materials",
            ("FBXIMPORT_GRP_MATERIALS", "FBXIMPORT_MATERIALS"),
            ("Materials",),
            ("FBXIMPORT_GRP_MATERIALS", "FBXIMPORT_MATERIALS"),
        ),
        (
            "fbx.import.textures",
            ("FBXIMPORT_TEXTURES",),
            ("Textures", "Import Textures"),
            ("FBXIMPORT_TEXTURES",),
        ),
    ):
        p = compat.resolve_description_param(
            imexporter,
            context=ctx_label,
            idents=idents,
            names=names,
            fallback_c4d_symbols=fallbacks,
            prefer_dtypes=(dtype_bool,),
        )
        _save_and_set(p, True)

    # 3. Cameras & Lights OFF
    for ctx_label, idents, names, fallbacks in (
        (
            "fbx.import.cameras",
            ("FBXIMPORT_GRP_CAMERAS", "FBXIMPORT_CAMERAS"),
            ("Cameras",),
            ("FBXIMPORT_GRP_CAMERAS", "FBXIMPORT_CAMERAS"),
        ),
        (
            "fbx.import.lights",
            ("FBXIMPORT_GRP_LIGHTS", "FBXIMPORT_LIGHTS"),
            ("Lights",),
            ("FBXIMPORT_GRP_LIGHTS", "FBXIMPORT_LIGHTS"),
        ),
    ):
        p = compat.resolve_description_param(
            imexporter,
            context=ctx_label,
            idents=idents,
            names=names,
            fallback_c4d_symbols=fallbacks,
            prefer_dtypes=(dtype_bool,),
        )
        _save_and_set(p, False)

    # 3b. FBX subdivision -> Subdivision Surface generator (with an SDS Weight tag for creases).
    sds_param = compat.resolve_description_param(
        imexporter,
        context="fbx.import.sds",
        idents=("FBXIMPORT_SDS",),
        names=("Subdivision Surface",),
        fallback_c4d_symbols=("FBXIMPORT_SDS",),
        prefer_dtypes=(dtype_bool,),
    )
    _save_and_set(sds_param, True)

    # 4. Normals ON (both boolean group toggle if present and mode dropdown -> Normal Tags)
    norm_bool_param = compat.resolve_description_param(
        imexporter,
        context="fbx.import.grp_normals",
        idents=("FBXIMPORT_GRP_NORMALS",),
        names=("Normals",),
        fallback_c4d_symbols=("FBXIMPORT_GRP_NORMALS",),
        prefer_dtypes=(dtype_bool,),
    )
    if norm_bool_param is not None and norm_bool_param.dtype == dtype_bool:
        _save_and_set(norm_bool_param, True)

    norm_mode_param = compat.resolve_description_param(
        imexporter,
        context="fbx.import.normals_mode",
        idents=("FBXIMPORT_NORMALS",),
        names=("Normals", "Normal Mode"),
        fallback_c4d_symbols=("FBXIMPORT_NORMALS",),
        prefer_dtypes=(dtype_long,),
    )
    if norm_mode_param is not None:
        if norm_mode_param.dtype == dtype_bool and not norm_mode_param.cycle:
            _save_and_set(norm_mode_param, True)
        else:
            nrm_enum = compat.resolve_cycle_enum(
                norm_mode_param,
                context="fbx.import.normals_enum",
                symbols=("FBXIMPORT_NORMALS_NORMALTAGS", "NORMALTAGS", "VERTEX"),
                labels=("Normal Tags", "Vertex Normals", "Normals"),
                fallback_c4d_symbols=("FBXIMPORT_NORMALS_NORMALTAGS",),
                fallback_value=2,
            )
            if nrm_enum is not None:
                _save_and_set(norm_mode_param, nrm_enum)

    return imexporter, saved


def _restore_fbx_importer(imexporter: Any, saved: Dict[Any, Any]) -> None:
    if imexporter is None or not saved:
        return
    for k, v in saved.items():
        try:
            imexporter[k] = v
        except Exception:
            pass


def _iter_hierarchy(root_obj: Any) -> List[Any]:
    """Depth-first traversal of *root_obj* and all its siblings/descendants."""
    out: List[Any] = []
    stack: List[Any] = []
    cur = root_obj
    while cur is not None:
        stack.append(cur)
        cur = cur.GetNext()
    stack.reverse()

    visited_ids = set()
    while stack:
        node = stack.pop()
        if node is None or id(node) in visited_ids:
            continue
        visited_ids.add(id(node))
        out.append(node)
        children: List[Any] = []
        child = node.GetDown() if hasattr(node, "GetDown") else None
        while child is not None:
            children.append(child)
            child = child.GetNext()
        for c in reversed(children):
            stack.append(c)
    return out


def has_open_edges(obj: Any) -> bool:
    """True when a polygon object is an open shell (some edge belongs to one polygon only)."""
    polygons = obj.GetAllPolygons()
    if not polygons:
        return False
    neighbor = c4d.utils.Neighbor()
    neighbor.Init(obj)
    for index, poly in enumerate(polygons):
        edges = [(poly.a, poly.b), (poly.b, poly.c)]
        edges += [(poly.c, poly.a)] if poly.IsTriangle() else [(poly.c, poly.d), (poly.d, poly.a)]
        if any(neighbor.GetNeighbor(a, b, index) == -1 for a, b in edges):
            return True
    return False


def shell_thickness_ratio(points: Sequence[Sequence[float]], triangles: Sequence[Sequence[int]]) -> float:
    """Rough thickness (3 x volume / area) over the size of the mesh: a sphere is 0.29, a thin cover near 0."""
    volume = area = 0.0
    for ia, ib, ic in triangles:
        ax, ay, az = points[ia]
        bx, by, bz = points[ib]
        cx, cy, cz = points[ic]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area += 0.5 * (nx * nx + ny * ny + nz * nz) ** 0.5
        volume += (ax * (by * cz - bz * cy) + ay * (bz * cx - bx * cz) + az * (bx * cy - by * cx)) / 6.0
    if area <= 0.0 or not points:
        return 0.0
    size = sum((max(p[i] for p in points) - min(p[i] for p in points)) ** 2 for i in range(3)) ** 0.5
    return 3.0 * abs(volume) / area / size if size > 0.0 else 0.0


def is_thin_shell(obj: Any, threshold: float = 0.05) -> bool:
    """Glass with no real volume: an open sheet, or a closed skin only a little thick."""
    if not obj.CheckType(getattr(c4d, "Opolygon", 5100)):
        return False
    if has_open_edges(obj):
        return True
    points = [(p.x, p.y, p.z) for p in obj.GetAllPoints()]
    triangles = []
    for poly in obj.GetAllPolygons():
        triangles.append((poly.a, poly.b, poly.c))
        if not poly.IsTriangle():
            triangles.append((poly.a, poly.c, poly.d))
    return shell_thickness_ratio(points, triangles) < threshold


def _bake_object_scale(root: Any) -> None:
    """Move the FBX importer's uniform scale into the points.

    Meshes arrive in metres with an object scale of 100. Displacement is applied in object space,
    so a 1 mm height in centimetres would become 10 cm in the render.
    """
    # Only the imported asset: _iter_hierarchy also walks siblings, which would be the user's objects.
    items = [(root, root.GetMg())] + [(obj, obj.GetMg()) for obj in _iter_hierarchy(root.GetDown())]  # parents first
    polygon_type = getattr(c4d, "Opolygon", 5100)
    null_type = getattr(c4d, "Onull", 5140)
    for obj, mg in items:
        is_polygon = obj.CheckType(polygon_type)
        if not is_polygon and not obj.CheckType(null_type):
            obj.SetMg(mg)
            continue
        sx, sy, sz = mg.v1.GetLength(), mg.v2.GetLength(), mg.v3.GetLength()
        if min(sx, sy, sz) <= 0.0 or max(sx, sy, sz) - min(sx, sy, sz) > 1e-4 * sx:
            obj.SetMg(mg)  # non-uniform: keep the transform, only re-apply it under the new parent
            continue
        if is_polygon and abs(sx - 1.0) > 1e-6:
            obj.SetAllPoints([point * sx for point in obj.GetAllPoints()])
            obj.Message(c4d.MSG_UPDATE)
        obj.SetMg(c4d.Matrix(mg.off, mg.v1 / sx, mg.v2 / sy, mg.v3 / sz))


# Blender's Subdivision Surface settings -> Cinema 4D's OpenSubdiv ones.
_UV_SMOOTH_TO_FVAR = {
    "SMOOTH_ALL": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_NONE",
    "PRESERVE_BOUNDARIES": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_BOUNDARIES",
    "PRESERVE_CORNERS": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_CORNERSONLY",
    "PRESERVE_CORNERS_AND_JUNCTIONS": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_CORNERSONLY",
    "PRESERVE_CORNERS_JUNCTIONS_AND_CONCAVE": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_CORNERSONLY",
    "NONE": "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_ALL",
}
_BOUNDARY_TO_OSD = {
    "ALL": "SDSOBJECT_OSD_BOUNDARY_METHOD_EDGEONLY",
    "PRESERVE_CORNERS": "SDSOBJECT_OSD_BOUNDARY_METHOD_EDGEANDCORNER",
}


def subdivision_settings(info: Dict[str, Any]) -> Dict[str, Any]:
    """Cinema 4D parameter names and values for a manifest ``subdivision`` entry."""
    levels = max(0, min(6, int(info.get("levels") or 0)))
    raw_render = info.get("render_levels")
    render_levels = levels if raw_render is None else max(0, min(6, int(raw_render)))
    # Creases come as an SDS Weight tag, and OpenSubdiv with that tag crashes Cinema 4D (2024
    # and 2027) when the scene is evaluated: creased objects keep Cinema 4D's own Catmull-Clark.
    sds_type = "SDSOBJECT_TYPE_CM_N" if info.get("creases") else "SDSOBJECT_TYPE_OSD_CATMARK"
    return {
        "SDSOBJECT_TYPE": sds_type,
        "SDSOBJECT_SUBEDITOR_CM": levels,
        "SDSOBJECT_SUBRAY_CM": render_levels,
        "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD": _UV_SMOOTH_TO_FVAR.get(
            str(info.get("uv_smooth") or ""), "SDSOBJECT_OSD_FVAR_BOUNDARY_METHOD_BOUNDARIES"
        ),
        "SDSOBJECT_OSD_BOUNDARY_METHOD": _BOUNDARY_TO_OSD.get(
            str(info.get("boundary_smooth") or ""), "SDSOBJECT_OSD_BOUNDARY_METHOD_EDGEONLY"
        ),
    }


def _configure_subdivision(sds: Any, obj: Any, info: Dict[str, Any]) -> None:
    """Match a Subdivision Surface generator to Blender's modifier; name it after its object."""
    for param, value in subdivision_settings(info).items():
        pid = getattr(c4d, param, None)
        val = getattr(c4d, value, None) if isinstance(value, str) else value
        if pid is None or val is None:
            continue
        try:
            sds[pid] = val
        except Exception as exc:
            log.debug("Subdivision Surface: %s not set: %s", param, exc)
    sds.SetName(obj.GetName())
    if not info.get("creases"):
        # The FBX importer adds an (empty) SDS Weight tag to every subdivided mesh; with
        # OpenSubdiv it crashes Cinema 4D, so it goes when there is nothing to weight.
        weight_tag_type = getattr(c4d, "Tsds", 1007579)
        for tag in list(obj.GetTags()):
            if tag.CheckType(weight_tag_type):
                tag.Remove()
    # The cage's normal tag does not describe the subdivided surface; let Phong smooth it.
    normal_tag_type = getattr(c4d, "Tnormal", 5711)
    phong_type = getattr(c4d, "Tphong", 5612)
    for tag in list(obj.GetTags()):
        if tag.CheckType(normal_tag_type):
            tag.Remove()
    if info.get("smooth", True):
        phong = obj.GetTag(phong_type) or obj.MakeTag(phong_type)
        if phong is not None:
            angle = info.get("smooth_angle")
            try:
                phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = angle is not None
                if angle is not None:
                    phong[c4d.PHONGTAG_PHONG_ANGLE] = math.radians(float(angle))
            except Exception:
                pass


def _wrap_in_subdivision(obj: Any, info: Dict[str, Any]) -> Optional[Any]:
    """Put *obj* under a Subdivision Surface generator at its place in the hierarchy.

    Fallback for when the FBX importer did not make one itself.
    """
    sds = c4d.BaseObject(getattr(c4d, "Osds", 1007455))
    if sds is None:
        return None
    _configure_subdivision(sds, obj, info)
    # A generator subdivides all its children: the object's own children stay outside.
    children = []
    child = obj.GetDown()
    while child is not None:
        children.append((child, child.GetMg()))
        child = child.GetNext()
    mg = obj.GetMg()
    sds.InsertBefore(obj)
    sds.SetMg(mg)
    obj.Remove()
    obj.InsertUnder(sds)
    obj.SetMl(c4d.Matrix())
    previous = sds
    for child, child_mg in children:
        child.Remove()
        child.InsertAfter(previous)
        child.SetMg(child_mg)
        previous = child
    return sds


def _unwrap(generator: Any) -> None:
    """Replace *generator* by its children, keeping their world placement."""
    children = []
    child = generator.GetDown()
    while child is not None:
        children.append((child, child.GetMg()))
        child = child.GetNext()
    for child, mg in children:
        child.Remove()
        child.InsertBefore(generator)
        child.SetMg(mg)
    generator.Remove()


def _settle_subdivision(temp_doc: Any, subdivided: Dict[str, Dict[str, Any]], warnings: List[str]) -> None:
    """Leave exactly one Subdivision Surface on each object Blender subdivided, none elsewhere.

    With its Subdivision Surface option on, Cinema 4D's FBX importer wraps every mesh in a
    generator, subdivided or not; objects Blender subdivided get theirs set up to match.
    """
    polygon_type = getattr(c4d, "Opolygon", 5100)
    sds_type = getattr(c4d, "Osds", 1007455)
    weight_tag_type = getattr(c4d, "Tsds", 1007579)
    for obj in _iter_hierarchy(temp_doc.GetFirstObject()):
        if not obj.CheckType(polygon_type):
            continue
        # Generators that hold only this object (C4D objects compare with ==, not "is").
        chain = []
        inner = obj
        parent = obj.GetUp()
        while (
            parent is not None and parent.CheckType(sds_type)
            and parent.GetDown() == inner and inner.GetNext() is None
        ):
            chain.append(parent)
            inner = parent
            parent = parent.GetUp()
        info = subdivided.get(str(obj.GetName() or ""))
        try:
            keep = chain[0] if (info is not None and chain) else None
            for generator in chain:
                if generator != keep:
                    _unwrap(generator)
            if info is None:
                for tag in list(obj.GetTags()):
                    if tag.CheckType(weight_tag_type):
                        tag.Remove()
            elif keep is not None:
                _configure_subdivision(keep, obj, info)
            else:
                _wrap_in_subdivision(obj, info)
        except Exception as exc:
            warnings.append(f"Subdivision Surface on '{obj.GetName()}': {exc}")


def _loose_material_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _match_kitbridge_material(
    fbx_mat_name: str,
    materials_by_name: Dict[str, Any],
) -> Tuple[Optional[str], Optional[Any]]:
    """Match an FBX material name to a Kitbridge material (exact, stripped, or case-insensitive)."""
    if not fbx_mat_name:
        return None, None
    if fbx_mat_name in materials_by_name:
        return fbx_mat_name, materials_by_name[fbx_mat_name]

    stripped = fbx_mat_name.strip()
    for m_name, m_obj in materials_by_name.items():
        if m_name.strip() == stripped:
            return m_name, m_obj

    lower = stripped.lower()
    for m_name, m_obj in materials_by_name.items():
        if m_name.strip().lower() == lower:
            return m_name, m_obj

    # The FBX exporter rewrites punctuation: "Window.glass" arrives as "Window glass".
    loose = _loose_material_name(stripped)
    if loose:
        for m_name, m_obj in materials_by_name.items():
            if _loose_material_name(m_name) == loose:
                return m_name, m_obj

    return None, None


def import_geometry(
    doc: Any,
    manifest: Dict[str, Any],
    package_dir: str,
    materials_by_name: Dict[str, Any],
    *,
    options: Optional[Dict[str, Any]] = None,
) -> GeometryImportOutcome:
    """Import ``geometry.fbx`` into *doc* (or assign material to selection for material assets)."""
    if c4d is None or doc is None:
        raise RuntimeError("Cinema 4D Python SDK (c4d) or active document is unavailable.")

    opts = options if isinstance(options, dict) else {}
    outcome = GeometryImportOutcome()

    asset = manifest.get("asset") if isinstance(manifest.get("asset"), dict) else {}
    asset_name = str(asset.get("name") or "Kitbridge Asset").strip() or "Kitbridge Asset"
    asset_type = str(asset.get("type") or "model").strip().lower()

    # Check material asset `apply_to_selection` option (default True when user has selection)
    selected_objs = get_selected_objects(doc)
    apply_opt = opts.get("apply_to_selection")
    if apply_opt is None:
        apply_to_selection = bool(selected_objs)
    else:
        apply_to_selection = bool(apply_opt)

    if asset_type == "material" and apply_to_selection and selected_objs and materials_by_name:
        primary_mat_name, primary_mat = next(iter(materials_by_name.items()))
        ttex_id = getattr(c4d, "Ttexture", 5616)
        proj_id = getattr(c4d, "TEXTURETAG_PROJECTION", None)
        proj_uvw = getattr(c4d, "TEXTURETAG_PROJECTION_UVW", 6)

        for sel_obj in selected_objs:
            try:
                ttag = sel_obj.MakeTag(ttex_id)
                if ttag is not None:
                    ttag.SetMaterial(primary_mat)
                    if proj_id is not None:
                        try:
                            ttag[proj_id] = proj_uvw
                        except Exception:
                            pass
                    if hasattr(doc, "AddUndo"):
                        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, ttag)
                    outcome.object_material_names.append((sel_obj, primary_mat_name))
            except Exception as exc:
                outcome.warnings.append(
                    f"Failed to assign material '{primary_mat_name}' to '{sel_obj.GetName()}': {exc}"
                )

        outcome.applied_to_selection = True
        outcome.objects = list(selected_objs)
        return outcome

    # Load geometry.fbx into a temporary document
    geom_rel = str(manifest.get("geometry") or "geometry.fbx").strip()
    fbx_path = os.path.normpath(os.path.abspath(os.path.join(package_dir, *geom_rel.replace("\\", "/").split("/"))))
    if not os.path.isfile(fbx_path):
        raise FileNotFoundError(f"Geometry FBX file not found: {fbx_path}")

    units = manifest.get("units") if isinstance(manifest.get("units"), dict) else {}
    raw_mpu = units.get("meters_per_unit", 1.0)
    mpu = float(raw_mpu) if isinstance(raw_mpu, (int, float)) and float(raw_mpu) > 0 else 1.0

    imexporter, saved_prefs = _configure_fbx_importer(meters_per_unit=mpu)
    temp_doc = None
    try:
        load_flags = (
            getattr(c4d, "SCENEFILTER_OBJECTS", 1)
            | getattr(c4d, "SCENEFILTER_MATERIALS", 2)
        )
        temp_doc = c4d.documents.LoadDocument(fbx_path, load_flags)
    finally:
        _restore_fbx_importer(imexporter, saved_prefs)

    if temp_doc is None:
        raise RuntimeError(f"c4d.documents.LoadDocument failed for {fbx_path!r}.")

    try:
        # Build lookup table for fallback material slots from manifest["objects"]
        manifest_obj_mats: Dict[str, List[str]] = {}
        for obj_entry in manifest.get("objects") or []:
            if isinstance(obj_entry, dict) and isinstance(obj_entry.get("name"), str):
                mats_list = [str(m) for m in (obj_entry.get("materials") or []) if isinstance(m, str)]
                manifest_obj_mats[obj_entry["name"]] = mats_list

        all_temp_objs = _iter_hierarchy(temp_doc.GetFirstObject())
        ttex_id = getattr(c4d, "Ttexture", 5616)
        kept_fbx_mats_by_id: Dict[int, Any] = {}

        for obj in all_temp_objs:
            obj_name = str(obj.GetName() or "") if hasattr(obj, "GetName") else ""
            tags = list(obj.GetTags()) if hasattr(obj, "GetTags") else []
            tex_tags = [t for t in tags if hasattr(t, "CheckType") and t.CheckType(ttex_id)]

            if tex_tags:
                for ttag in tex_tags:
                    fbx_mat = ttag.GetMaterial() if hasattr(ttag, "GetMaterial") else None
                    fbx_mat_name = str(fbx_mat.GetName() or "") if fbx_mat is not None else ""
                    matched_name, kb_mat = _match_kitbridge_material(fbx_mat_name, materials_by_name)
                    if kb_mat is not None and matched_name is not None:
                        ttag.SetMaterial(kb_mat)
                        outcome.object_material_names.append((obj, matched_name))
                    elif fbx_mat is not None:
                        outcome.warnings.append(
                            f"Object '{obj_name}': FBX material '{fbx_mat_name}' did not match "
                            "any manifest material; keeping FBX material."
                        )
                        if id(fbx_mat) not in kept_fbx_mats_by_id:
                            kept_fbx_mats_by_id[id(fbx_mat)] = fbx_mat
                            try:
                                fbx_mat.Remove()
                                doc.InsertMaterial(fbx_mat)
                                if hasattr(doc, "AddUndo"):
                                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, fbx_mat)
                                outcome.kept_fbx_materials.append(fbx_mat)
                            except Exception as exc:
                                log.debug("Failed to move unmatched FBX material %r: %s", fbx_mat_name, exc)
            else:
                # Fallback if FBX importer created no TextureTag on an object that has one in manifest
                slot_names = manifest_obj_mats.get(obj_name) or []
                for s_name in slot_names:
                    matched_name, kb_mat = _match_kitbridge_material(s_name, materials_by_name)
                    if kb_mat is not None and matched_name is not None and hasattr(obj, "MakeTag"):
                        try:
                            new_ttag = obj.MakeTag(ttex_id)
                            if new_ttag is not None:
                                new_ttag.SetMaterial(kb_mat)
                                outcome.object_material_names.append((obj, matched_name))
                        except Exception:
                            pass

        # Move all top-level objects into a new Null named after the asset at the origin
        # Still in the temporary document: the user's document and its undo block stay out of this.
        for top_obj in temp_doc.GetObjects():
            try:
                _bake_object_scale(top_obj)
            except Exception as exc:
                outcome.warnings.append(f"Could not bake the object scale: {exc}")

        subdivided = {
            str(entry.get("name")): entry["subdivision"]
            for entry in manifest.get("objects") or []
            if isinstance(entry, dict) and isinstance(entry.get("subdivision"), dict)
        }
        _settle_subdivision(temp_doc, subdivided, outcome.warnings)

        top_level_objs: List[Any] = []
        cur = temp_doc.GetFirstObject()
        while cur is not None:
            nxt = cur.GetNext()
            cur.Remove()
            top_level_objs.append(cur)
            cur = nxt

        onull_id = getattr(c4d, "Onull", 5140)
        root_null = c4d.BaseObject(onull_id)
        if root_null is None:
            raise RuntimeError("Failed to allocate c4d.BaseObject(c4d.Onull).")
        root_null.SetName(asset_name)
        if hasattr(root_null, "SetAbsPos") and hasattr(c4d, "Vector"):
            try:
                root_null.SetAbsPos(c4d.Vector(0.0, 0.0, 0.0))
            except Exception:
                pass

        for top_obj in top_level_objs:
            if hasattr(top_obj, "InsertUnderLast"):
                top_obj.InsertUnderLast(root_null)
            else:
                top_obj.InsertUnder(root_null)

        doc.InsertObject(root_null)
        if hasattr(doc, "AddUndo"):
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, root_null)

        try:
            sel_new = getattr(c4d, "SELECTION_NEW", 1)
            doc.SetActiveObject(root_null, sel_new)
        except Exception:
            pass

        outcome.root_object = root_null
        outcome.objects = [root_null] + _iter_hierarchy(root_null.GetDown())
        return outcome
    finally:
        if temp_doc is not None and hasattr(c4d, "documents") and hasattr(c4d.documents, "KillDocument"):
            try:
                c4d.documents.KillDocument(temp_doc)
            except Exception:
                pass
