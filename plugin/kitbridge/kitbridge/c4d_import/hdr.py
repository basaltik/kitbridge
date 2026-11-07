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

"""HDR / Dome Light importer per renderer (Milestone 2b).

- Redshift: RS Light (``1036751``) type Dome with the EXR/HDR as texture.
- Arnold: Arnold Light (``1030424``) type skydome + image shader/texture.
- Standard: Sky object + material with luminance channel.

Resolves light type and texture parameters at runtime via ``GetDescription()``
and logs all findings via :mod:`kitbridge.c4d_import.compat`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import compat
from .compat import c4d
from .materials.base import (
    KITBRIDGE_HDR_BC_ID,
    KITBRIDGE_HDR_KEY_BC_ID,
    resolve_texture_path,
    unique_material_name,
)
from .renderer import RENDERER_ARNOLD, RENDERER_REDSHIFT, RENDERER_STANDARD

log = logging.getLogger("kitbridge")

RS_LIGHT_ID = 1036751
ARNOLD_LIGHT_ID = 1030424
ARNOLD_SKY_ID = 1034624
ARNOLD_SKYDOME_TYPE = 2054857832
ARNOLD_SKYDOME_COLOR_ID = 268620635
# From C4DtoA's res/c4dtoa_symbols.h and library/scripts/arnold/custom_gui.py.
C4DTOA_SHADERLINK_CONTAINER_ID = 9988000
C4DTOA_SHADERLINK_TYPE_ID = 101
C4DTOA_SHADERLINK_TEXTURE_ID = 103
C4DTOA_SHADERLINK_TYPE_TEXTURE = 2
C4DTOA_BITMAP_COLOR_SPACE_ID = 10101
# HDRIs hold linear Rec.709, the same thing Blender reads them as.
REDSHIFT_HDR_COLOR_SPACE = "scene-linear Rec.709-sRGB"
ARNOLD_HDR_COLOR_SPACE = "scene-linear Rec.709-sRGB"


@dataclass
class HDRImportOutcome:
    """Result of importing an HDR environment asset."""

    dome_object: Any = None
    objects: List[Any] = field(default_factory=list)
    materials: List[Any] = field(default_factory=list)
    node_count: int = 0
    connected_channels: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _tag_as_kitbridge_hdr(elem: Any, base_id: str) -> None:
    if elem is None or not hasattr(elem, "GetDataInstance"):
        return
    try:
        bc = elem.GetDataInstance()
        if bc is not None:
            bc.SetBool(KITBRIDGE_HDR_BC_ID, True)
            bc.SetString(KITBRIDGE_HDR_KEY_BC_ID, f"kitbridge:hdr:{base_id}")
    except Exception:
        pass


def _is_kitbridge_hdr(elem: Any) -> bool:
    if elem is None or not hasattr(elem, "GetDataInstance"):
        return False
    try:
        bc = elem.GetDataInstance()
        if bc is not None:
            return bool(bc.GetBool(KITBRIDGE_HDR_BC_ID))
    except Exception:
        pass
    return False


def _remove_existing_kitbridge_hdr(doc: Any) -> int:
    """Remove any existing Kitbridge HDR dome objects and companion materials in *doc*."""
    if c4d is None or doc is None:
        return 0

    removed = 0
    # Scan top-level objects
    objs_to_remove: List[Any] = []
    cur = doc.GetFirstObject() if hasattr(doc, "GetFirstObject") else None
    while cur is not None:
        if _is_kitbridge_hdr(cur):
            objs_to_remove.append(cur)
        cur = cur.GetNext()

    for obj in objs_to_remove:
        try:
            if hasattr(doc, "AddUndo"):
                doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, obj)
            obj.Remove()
            removed += 1
        except Exception as exc:
            log.debug("Failed to remove old Kitbridge HDR object: %s", exc)

    # Scan materials for companion HDR materials
    mats_to_remove: List[Any] = []
    for mat in (doc.GetMaterials() if hasattr(doc, "GetMaterials") else ()):
        if _is_kitbridge_hdr(mat):
            mats_to_remove.append(mat)

    for mat in mats_to_remove:
        try:
            if hasattr(doc, "AddUndo"):
                doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, mat)
            mat.Remove()
        except Exception as exc:
            log.debug("Failed to remove old Kitbridge HDR material: %s", exc)

    return removed


def _set_redshift_dome_texture(light: Any, hdr_path: str, warnings: List[str]) -> bool:
    """Resolve and set the Redshift Dome Light texture parameter at runtime."""
    if c4d is None or light is None:
        return False

    # 1. Resolve Light Type parameter (expected 10000 / REDSHIFT_LIGHT_TYPE) and Dome enum (3)
    type_param = compat.resolve_description_param(
        light,
        context="redshift.light.type",
        idents=("REDSHIFT_LIGHT_TYPE",),
        names=("Type",),
        fallback_c4d_symbols=("REDSHIFT_LIGHT_TYPE",),
        fallback_id=10000,
    )
    dome_enum = compat.resolve_cycle_enum(
        type_param,
        context="redshift.light.type_dome_enum",
        symbols=("REDSHIFT_LIGHT_TYPE_DOME", "DOME"),
        labels=("Dome", "Dome Light"),
        fallback_c4d_symbols=("REDSHIFT_LIGHT_TYPE_DOME",),
        fallback_value=3,
    )
    if type_param is not None and dome_enum is not None:
        compat.set_resolved_parameter(light, type_param, dome_enum)

    # 2. Now that Type is Dome, re-walk GetDescription() to inspect Dome texture parameters
    compat.record_resolution(
        "redshift.dome.description_sample",
        {
            "entries": compat.dump_object_description(
                light,
                filter_keywords=("dome", "tex", "path", "file", "map", "env"),
                max_entries=40,
            )
        },
    )

    tex_id = int(getattr(c4d, "REDSHIFT_LIGHT_DOME_TEX0", 12000))
    path_id = int(getattr(c4d, "REDSHIFT_FILE_PATH", 1000))
    space_id = int(getattr(c4d, "REDSHIFT_FILE_COLORSPACE", 1011))
    try:
        light[tex_id, path_id] = str(hdr_path)
        # Cinema 4D hands the path back as a file:/// URL, so only check that something was stored.
        stored = bool(light[tex_id, path_id])
    except Exception as exc:
        log.debug("Setting the Redshift dome texture raised: %s", exc)
        stored = False
    compat.record_resolution("redshift.dome.tex0_set", {"tex_id": tex_id, "sub_id": path_id, "success": stored})
    if not stored:
        warnings.append("Redshift Dome Light created, but its texture could not be set.")
        return False
    try:
        light[tex_id, space_id] = REDSHIFT_HDR_COLOR_SPACE
    except Exception as exc:
        log.debug("Setting the Redshift dome color space raised: %s", exc)
    return True


def _configure_arnold_skydome(light: Any, hdr_path: str, outcome: HDRImportOutcome) -> None:
    """Make *light* a skydome whose color is *hdr_path*, as the Arnold light's texture field does."""
    if c4d is None or light is None:
        return
    color_id = None
    try:
        light[c4d.C4DAI_LIGHT_TYPE] = ARNOLD_SKYDOME_TYPE
        color_id = int(getattr(c4d, "C4DAIP_SKYDOME_LIGHT_COLOR", ARNOLD_SKYDOME_COLOR_ID))
        links = light[C4DTOA_SHADERLINK_CONTAINER_ID]
        if links is None:
            links = c4d.BaseContainer()
        link = links.GetContainer(color_id)
        shader = c4d.BaseShader(c4d.Xbitmap)
        shader[c4d.BITMAPSHADER_FILENAME] = str(hdr_path)
        shader[C4DTOA_BITMAP_COLOR_SPACE_ID] = ARNOLD_HDR_COLOR_SPACE
        light.InsertShader(shader)
        link[C4DTOA_SHADERLINK_TYPE_ID] = C4DTOA_SHADERLINK_TYPE_TEXTURE
        link[C4DTOA_SHADERLINK_TEXTURE_ID] = shader
        links.SetContainer(color_id, link)
        light[C4DTOA_SHADERLINK_CONTAINER_ID] = links
        outcome.connected_channels.append("hdr")
    except Exception as exc:
        log.debug("Arnold skydome setup raised: %s", exc)
        outcome.warnings.append("Arnold Skydome Light created, but its texture could not be set.")


def import_hdr(
    doc: Any,
    manifest: Dict[str, Any],
    package_dir: str,
    *,
    renderer: str = RENDERER_STANDARD,
    options: Optional[Dict[str, Any]] = None,
) -> HDRImportOutcome:
    """Import an HDR package into *doc* as a renderer-native dome light / sky."""
    if c4d is None or doc is None:
        raise RuntimeError("Cinema 4D Python SDK (c4d) or active document is unavailable.")

    opts = options if isinstance(options, dict) else {}
    outcome = HDRImportOutcome()

    asset = manifest.get("asset") if isinstance(manifest.get("asset"), dict) else {}
    asset_name = str(asset.get("name") or "Kitbridge HDR").strip() or "Kitbridge HDR"
    base_id = str(asset.get("base_id") or asset.get("id") or "hdr")

    hdr_rel = str(manifest.get("hdr") or "").strip()
    if not hdr_rel:
        raise ValueError(f"HDR manifest for '{asset_name}' is missing 'hdr' file path.")
    hdr_path = resolve_texture_path(package_dir, hdr_rel)
    if not os.path.isfile(hdr_path):
        raise FileNotFoundError(f"HDR image file not found on disk: {hdr_path}")

    replace_existing = bool(opts.get("replace_existing_hdr", True))
    if replace_existing:
        _remove_existing_kitbridge_hdr(doc)

    dome_obj: Any = None

    if renderer == RENDERER_REDSHIFT:
        try:
            dome_obj = c4d.BaseObject(RS_LIGHT_ID)
        except Exception:
            dome_obj = None
        if dome_obj is not None:
            dome_obj.SetName(asset_name)
            _tag_as_kitbridge_hdr(dome_obj, base_id)
            if _set_redshift_dome_texture(dome_obj, hdr_path, outcome.warnings):
                outcome.connected_channels.append("hdr")
        else:
            outcome.warnings.append(
                f"Redshift Light plugin ({RS_LIGHT_ID}) is unavailable; falling back to Standard Sky."
            )

    elif renderer == RENDERER_ARNOLD:
        for plugin_id in (ARNOLD_LIGHT_ID, ARNOLD_SKY_ID):
            try:
                dome_obj = c4d.BaseObject(plugin_id)
                if dome_obj is not None:
                    break
            except Exception:
                dome_obj = None
        if dome_obj is not None:
            dome_obj.SetName(asset_name)
            _tag_as_kitbridge_hdr(dome_obj, base_id)
            _configure_arnold_skydome(dome_obj, hdr_path, outcome)
        else:
            outcome.warnings.append(
                f"Arnold Light ({ARNOLD_LIGHT_ID}) is unavailable; falling back to Standard Sky."
            )

    # Standard / fallback Sky object + Luminance material
    if dome_obj is None:
        osky_id = getattr(c4d, "Osky", 5105)
        dome_obj = c4d.BaseObject(osky_id)
        if dome_obj is None:
            raise RuntimeError("Failed to allocate c4d.BaseObject(c4d.Osky).")
        dome_obj.SetName(asset_name)
        _tag_as_kitbridge_hdr(dome_obj, base_id)

        mat = c4d.BaseMaterial(c4d.Mmaterial)
        if mat is not None:
            existing_names = [m.GetName() for m in (doc.GetMaterials() if hasattr(doc, "GetMaterials") else ())]
            mat.SetName(unique_material_name(f"{asset_name}_HDR", existing_names))
            _tag_as_kitbridge_hdr(mat, base_id)
            try:
                mat[c4d.MATERIAL_USE_COLOR] = False
                mat[c4d.MATERIAL_USE_REFLECTION] = False
                mat[c4d.MATERIAL_USE_LUMINANCE] = True
                shd = c4d.BaseShader(c4d.Xbitmap)
                if shd is not None:
                    shd[c4d.BITMAPSHADER_FILENAME] = str(hdr_path)
                    mat.InsertShader(shd)
                    mat[c4d.MATERIAL_LUMINANCE_SHADER] = shd
                    outcome.node_count = 1
                    outcome.connected_channels.append("hdr")
            except Exception as exc:
                outcome.warnings.append(f"Standard Sky luminance shader setup failed: {exc}")

            doc.InsertMaterial(mat)
            if hasattr(doc, "AddUndo"):
                doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, mat)
            outcome.materials.append(mat)

            ttex_id = getattr(c4d, "Ttexture", 5616)
            ttag = dome_obj.MakeTag(ttex_id) if hasattr(dome_obj, "MakeTag") else None
            if ttag is not None:
                ttag.SetMaterial(mat)

    doc.InsertObject(dome_obj)
    if hasattr(doc, "AddUndo"):
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, dome_obj)

    try:
        sel_new = getattr(c4d, "SELECTION_NEW", 1)
        doc.SetActiveObject(dome_obj, sel_new)
    except Exception:
        pass

    outcome.dome_object = dome_obj
    outcome.objects = [dome_obj]
    return outcome
