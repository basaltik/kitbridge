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

"""Material plan and renderer-specific material builders (Milestone 2b)."""

from __future__ import annotations

from .base import (
    KITBRIDGE_HDR_BC_ID,
    KITBRIDGE_HDR_KEY_BC_ID,
    KITBRIDGE_MAT_BC_ID,
    KITBRIDGE_MAT_RENDERER_BC_ID,
    ChannelOp,
    MaterialPlan,
    SharedTextureSpec,
    TextureOp,
    UVTransformPlan,
    build_material_plan,
    build_package_material_plans,
    make_material_dedupe_key,
    parse_uv_transform,
    resolve_texture_path,
    unique_material_name,
)

__all__ = [
    "KITBRIDGE_HDR_BC_ID",
    "KITBRIDGE_HDR_KEY_BC_ID",
    "KITBRIDGE_MAT_BC_ID",
    "KITBRIDGE_MAT_RENDERER_BC_ID",
    "ChannelOp",
    "MaterialPlan",
    "SharedTextureSpec",
    "TextureOp",
    "UVTransformPlan",
    "build_material_plan",
    "build_package_material_plans",
    "make_material_dedupe_key",
    "parse_uv_transform",
    "resolve_texture_path",
    "unique_material_name",
]
