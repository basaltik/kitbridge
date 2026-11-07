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

"""Standard / Physical legacy C4D material fallback builder (Milestone 2b).

Builds a classic ``c4d.BaseMaterial(c4d.Mmaterial)`` setting Color, Reflectance
roughness, Normal, and Alpha.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from kitbridge.core.pipeline import UDIM_TOKEN, udim_tiles

from ..compat import c4d
from .base import MaterialPlan
from .redshift import BuiltMaterialResult

log = logging.getLogger("kitbridge")


def _create_bitmap_shader(mat: Any, path: str) -> Any:
    if c4d is None or not path:
        return None
    if UDIM_TOKEN in path:
        # The legacy Bitmap shader can't tile; use the first tile so the model is not left blank.
        tiles = udim_tiles(path)
        if not tiles:
            return None
        log.warning("Standard materials have no UDIM support; using tile %s only", tiles[0])
        path = tiles[0]
    try:
        shd = c4d.BaseShader(c4d.Xbitmap)
        if shd is None:
            return None
        shd[c4d.BITMAPSHADER_FILENAME] = str(path)
        mat.InsertShader(shd)
        return shd
    except Exception as exc:
        log.debug("Failed to create Xbitmap shader for %s: %s", path, exc)
        return None


def build_standard_material(plan: MaterialPlan, material_name: Optional[str] = None) -> BuiltMaterialResult:
    """Construct a legacy Cinema 4D ``Mmaterial`` from *plan*."""
    if c4d is None:
        raise RuntimeError("Cinema 4D Python SDK (c4d) is unavailable.")

    mat = c4d.BaseMaterial(c4d.Mmaterial)
    if mat is None:
        raise RuntimeError("Failed to allocate c4d.BaseMaterial(c4d.Mmaterial).")
    mat.SetName(material_name or plan.name)

    warnings: List[str] = list(plan.warnings)
    connected_channels: List[str] = []
    value_channels: List[str] = []
    shader_count = 0

    # 1. Base Color
    bc_op = plan.ops.get("base_color")
    if bc_op is not None:
        try:
            mat[c4d.MATERIAL_USE_COLOR] = True
            if bc_op.is_texture and bc_op.texture is not None:
                shd = _create_bitmap_shader(mat, bc_op.texture.path)
                if shd is not None:
                    mat[c4d.MATERIAL_COLOR_SHADER] = shd
                    connected_channels.append("base_color")
                    shader_count += 1
            elif bc_op.is_value and isinstance(bc_op.value, (list, tuple)) and len(bc_op.value) >= 3:
                r, g, b = float(bc_op.value[0]), float(bc_op.value[1]), float(bc_op.value[2])
                mat[c4d.MATERIAL_COLOR_COLOR] = c4d.Vector(r, g, b)
                value_channels.append("base_color")
        except Exception as exc:
            log.debug("Standard material base_color setup failed: %s", exc)

    # 2. Roughness via Reflectance layer
    rough_op = plan.ops.get("roughness")
    if rough_op is not None:
        try:
            mat[c4d.MATERIAL_USE_REFLECTION] = True
            layer = None
            if hasattr(mat, "GetReflectionLayerCount") and mat.GetReflectionLayerCount() > 0:
                layer = mat.GetReflectionLayerIndex(0)
            elif hasattr(mat, "AddReflectionLayer"):
                layer = mat.AddReflectionLayer()
            if layer is not None and hasattr(layer, "GetDataID"):
                base_id = layer.GetDataID()
                if rough_op.is_texture and rough_op.texture is not None:
                    shd = _create_bitmap_shader(mat, rough_op.texture.path)
                    if shd is not None:
                        mat[base_id + c4d.REFLECTION_LAYER_MAIN_SHADER_ROUGHNESS] = shd
                        connected_channels.append("roughness")
                        shader_count += 1
                elif rough_op.is_value and isinstance(rough_op.value, (int, float)):
                    mat[base_id + c4d.REFLECTION_LAYER_MAIN_VALUE_ROUGHNESS] = float(rough_op.value)
                    value_channels.append("roughness")
        except Exception as exc:
            log.debug("Standard material reflectance roughness setup failed: %s", exc)

    # 3. Normal
    nrm_op = plan.ops.get("normal")
    if nrm_op is not None and nrm_op.is_texture and nrm_op.texture is not None:
        try:
            mat[c4d.MATERIAL_USE_NORMAL] = True
            shd = _create_bitmap_shader(mat, nrm_op.texture.path)
            if shd is not None:
                mat[c4d.MATERIAL_NORMAL_SHADER] = shd
                connected_channels.append("normal")
                shader_count += 1
            if hasattr(c4d, "MATERIAL_NORMAL_STRENGTH"):
                mat[c4d.MATERIAL_NORMAL_STRENGTH] = float(nrm_op.texture.strength)
            if nrm_op.texture.flip_green and hasattr(c4d, "MATERIAL_NORMAL_REVERSE_Y"):
                mat[c4d.MATERIAL_NORMAL_REVERSE_Y] = True
        except Exception as exc:
            log.debug("Standard material normal setup failed: %s", exc)

    # 4. Alpha
    if plan.requires_alpha and "alpha" in plan.ops:
        alpha_op = plan.ops["alpha"]
        try:
            mat[c4d.MATERIAL_USE_ALPHA] = True
            if alpha_op.is_texture and alpha_op.texture is not None:
                shd = _create_bitmap_shader(mat, alpha_op.texture.path)
                if shd is not None:
                    mat[c4d.MATERIAL_ALPHA_SHADER] = shd
                    connected_channels.append("alpha")
                    shader_count += 1
        except Exception as exc:
            log.debug("Standard material alpha setup failed: %s", exc)

    return BuiltMaterialResult(
        material=mat,
        node_count=shader_count,
        connected_channels=connected_channels,
        value_channels=value_channels,
        warnings=warnings,
    )
