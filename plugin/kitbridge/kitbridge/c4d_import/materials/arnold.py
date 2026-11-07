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

"""Arnold (C4DtoA) node material builder for Cinema 4D (Milestone 2b).

Uses the exact node and port identifiers from ``docs/c4d-node-ports-2027.json``
and only the low-level ``maxon`` graph API (C4D 2023.0+).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .. import compat
from ..compat import c4d
from .base import MaterialPlan, TextureOp, rec709_to_render_space, transmission_tint
from .redshift import BuiltMaterialResult

log = logging.getLogger("kitbridge")

ARNOLD_NODESPACE_ID = "com.autodesk.arnold.nodespace"
ARNOLD_PARAMS_TAG_ID = 1029989

# Exact node IDs from docs/c4d-node-ports-2027.json
NODE_MATERIAL = "com.autodesk.arnold.material"
NODE_STANDARD_SURFACE = "com.autodesk.arnold.shader.standard_surface"
NODE_IMAGE = "com.autodesk.arnold.shader.image"
NODE_NORMAL_MAP = "com.autodesk.arnold.shader.normal_map"
NODE_BUMP2D = "com.autodesk.arnold.shader.bump2d"
NODE_COMPLEMENT = "com.autodesk.arnold.shader.complement"
NODE_UV_TRANSFORM = "com.autodesk.arnold.shader.uv_transform"

# Material end-node input ports
PORT_MAT_SHADER = "shader"
PORT_MAT_DISPLACEMENT = "displacement"
PORT_MAT_ALPHA = "alpha"

# Standard Surface ports
PORT_SS_OUT = "output"
PORT_SS_BASE = "base"
PORT_SS_BASE_COLOR = "base_color"
PORT_SS_METALNESS = "metalness"
PORT_SS_SPECULAR = "specular"
PORT_SS_SPECULAR_ROUGHNESS = "specular_roughness"
PORT_SS_SPECULAR_IOR = "specular_IOR"
PORT_SS_EMISSION = "emission"
PORT_SS_EMISSION_COLOR = "emission_color"
PORT_SS_OPACITY = "opacity"
PORT_SS_TRANSMISSION = "transmission"
PORT_SS_TRANSMISSION_COLOR = "transmission_color"
PORT_SS_COAT = "coat"
PORT_SS_COAT_ROUGHNESS = "coat_roughness"
PORT_SS_SHEEN = "sheen"
PORT_SS_SUBSURFACE = "subsurface"
PORT_SS_SUBSURFACE_COLOR = "subsurface_color"
PORT_SS_NORMAL = "normal"

# Image node ports
PORT_IMG_FILENAME = "filename"
PORT_IMG_COLOR_SPACE = "color_space"
PORT_IMG_SSCALE = "sscale"
PORT_IMG_TSCALE = "tscale"
PORT_IMG_SOFFSET = "soffset"
PORT_IMG_TOFFSET = "toffset"
PORT_IMG_MULTIPLY = "multiply"
PORT_IMG_OFFSET = "offset"
PORT_IMG_OUT_MAP = {
    "rgb": "output",
    "r": "output.r",
    "g": "output.g",
    "b": "output.b",
    "a": "output.a",
}

# Complement node ports
PORT_COMP_INPUT = "input"
PORT_COMP_OUT = "output"
PORT_COMP_OUT_R = "output.r"

# Normal map node ports
PORT_NM_INPUT = "input"
PORT_NM_STRENGTH = "strength"
PORT_NM_INVERT_Y = "invert_y"
PORT_NM_OUT = "output"

# Bump2d node ports
PORT_B2D_BUMP_MAP = "bump_map"
PORT_B2D_BUMP_HEIGHT = "bump_height"
PORT_B2D_NORMAL = "normal"
PORT_B2D_OUT = "output"


def _set_node_input(node: Any, port_id: str, value: Any) -> bool:
    port = compat.find_port(node, port_id, is_input=True)
    return compat.set_port_value(port, rec709_to_render_space(value, compat.document_render_space()))


def _connect_to_node_input(src_out_port: Any, dst_node: Any, port_id: str) -> bool:
    dst_port = compat.find_port(dst_node, port_id, is_input=True)
    return compat.connect_ports(src_out_port, dst_port)


def build_arnold_material(plan: MaterialPlan, material_name: Optional[str] = None) -> BuiltMaterialResult:
    """Construct a native Arnold (C4DtoA) node material from *plan*."""
    if c4d is None:
        raise RuntimeError("Cinema 4D Python SDK (c4d) is unavailable.")

    mat = c4d.BaseMaterial(c4d.Mmaterial)
    if mat is None:
        raise RuntimeError("Failed to allocate c4d.BaseMaterial(c4d.Mmaterial).")
    mat.SetName(material_name or plan.name)

    graph = compat.create_or_get_default_graph(mat, ARNOLD_NODESPACE_ID)
    warnings: List[str] = list(plan.warnings)
    connected_channels: List[str] = []
    value_channels: List[str] = []

    with graph.BeginTransaction() as transaction:
        mat_nodes = compat.find_nodes_by_asset_id(graph, NODE_MATERIAL)
        if mat_nodes:
            end_node = mat_nodes[0]
        else:
            end_node = compat.add_shader_node(graph, NODE_MATERIAL)

        ss_nodes = compat.find_nodes_by_asset_id(graph, NODE_STANDARD_SURFACE)
        if ss_nodes:
            surf_node = ss_nodes[0]
        else:
            surf_node = compat.add_shader_node(graph, NODE_STANDARD_SURFACE)

        ss_out = compat.find_port(surf_node, PORT_SS_OUT, is_input=False)
        mat_shader_in = compat.find_port(end_node, PORT_MAT_SHADER, is_input=True)
        compat.connect_ports(ss_out, mat_shader_in)

        # Base weight = 1.0, Specular weight = 1.0, Specular IOR = plan.ior
        _set_node_input(surf_node, PORT_SS_BASE, 1.0)
        _set_node_input(surf_node, PORT_SS_SPECULAR, float(plan.reflection_weight))
        _set_node_input(surf_node, PORT_SS_SPECULAR_IOR, float(plan.ior))

        # 1. Create shared Arnold image nodes (deduplicated across channels, e.g. ORM)
        img_nodes_by_key: Dict[str, Any] = {}
        for shared_spec in plan.shared_textures:
            img_node = compat.add_shader_node(graph, NODE_IMAGE)
            img_nodes_by_key[shared_spec.key] = img_node

            fn_port = compat.find_port(img_node, PORT_IMG_FILENAME, is_input=True)
            # Arnold's tile token is lower case; Redshift reads the manifest's <UDIM> as is.
            if not compat.set_port_file_path(fn_port, shared_spec.path.replace("<UDIM>", "<udim>")):
                warnings.append(
                    f"Material '{plan.name}': failed to set Arnold image.filename for {shared_spec.rel_path!r}."
                )

            cs_port = compat.find_port(img_node, PORT_IMG_COLOR_SPACE, is_input=True)
            cs_val = "sRGB" if shared_spec.colorspace == "srgb" else "Raw"
            compat.set_port_value(cs_port, cs_val)

            if not shared_spec.uv.is_identity:
                _set_node_input(img_node, PORT_IMG_SSCALE, float(shared_spec.uv.scale[0]))
                _set_node_input(img_node, PORT_IMG_TSCALE, float(shared_spec.uv.scale[1]))
                _set_node_input(img_node, PORT_IMG_SOFFSET, float(shared_spec.uv.offset[0]))
                _set_node_input(img_node, PORT_IMG_TOFFSET, float(shared_spec.uv.offset[1]))

        def _resolve_tex_output_port(tex_op: TextureOp, *, prefer_scalar_on_invert: bool = False) -> Any:
            img_node = img_nodes_by_key.get(tex_op.texture_key)
            if img_node is None:
                return None

            out_port_name = PORT_IMG_OUT_MAP.get(tex_op.channel, "output")
            src_port = compat.find_port(img_node, out_port_name, is_input=False)
            if src_port is None:
                src_port = compat.find_port(img_node, "output", is_input=False)

            if tex_op.invert and src_port is not None:
                comp_node = compat.add_shader_node(graph, NODE_COMPLEMENT)
                comp_in = compat.find_port(comp_node, PORT_COMP_INPUT, is_input=True)
                compat.connect_ports(src_port, comp_in)
                if prefer_scalar_on_invert or tex_op.is_single_channel:
                    r_port = compat.find_port(comp_node, PORT_COMP_OUT_R, is_input=False)
                    src_port = r_port if r_port is not None else compat.find_port(comp_node, PORT_COMP_OUT, is_input=False)
                else:
                    src_port = compat.find_port(comp_node, PORT_COMP_OUT, is_input=False)

            return src_port

        def _apply_channel(
            ch_name: str,
            target_port_id: str,
            *,
            scalar_as_rgb: bool = False,
            scalar_port: bool = False,
        ) -> Optional[Any]:
            op = plan.ops.get(ch_name)
            if op is None:
                return None
            if op.is_texture and op.texture is not None:
                out_port = _resolve_tex_output_port(op.texture, prefer_scalar_on_invert=scalar_port)
                if out_port is not None and _connect_to_node_input(out_port, surf_node, target_port_id):
                    if ch_name not in connected_channels:
                        connected_channels.append(ch_name)
                    return out_port
                warnings.append(f"Material '{plan.name}': failed to connect Arnold texture for '{ch_name}'.")
                return None
            if op.is_value and op.value is not None:
                val: Any = op.value
                if scalar_as_rgb and isinstance(val, (int, float)):
                    f = float(val)
                    val = (f, f, f)
                if _set_node_input(surf_node, target_port_id, val):
                    if ch_name not in value_channels:
                        value_channels.append(ch_name)
                return None
            return None

        # 2. Wire standard PBR channels
        base_color_out_port = _apply_channel("base_color", PORT_SS_BASE_COLOR)
        _apply_channel("metallic", PORT_SS_METALNESS, scalar_port=True)
        _apply_channel("roughness", PORT_SS_SPECULAR_ROUGHNESS, scalar_port=True)
        _apply_channel("ior", PORT_SS_SPECULAR_IOR, scalar_port=True)
        _apply_channel("coat", PORT_SS_COAT, scalar_port=True)
        _apply_channel("coat_roughness", PORT_SS_COAT_ROUGHNESS, scalar_port=True)
        _apply_channel("sheen", PORT_SS_SHEEN, scalar_port=True)

        # Subsurface
        ss_op = plan.ops.get("subsurface")
        if ss_op is not None:
            _apply_channel("subsurface", PORT_SS_SUBSURFACE, scalar_port=True)
            bc_op = plan.ops.get("base_color")
            if base_color_out_port is not None:
                _connect_to_node_input(base_color_out_port, surf_node, PORT_SS_SUBSURFACE_COLOR)
            elif bc_op is not None and bc_op.is_value and bc_op.value is not None:
                _set_node_input(surf_node, PORT_SS_SUBSURFACE_COLOR, bc_op.value)

        # Emission
        em_op = plan.ops.get("emission")
        if em_op is not None and plan.emission_strength > 0.0:
            _set_node_input(surf_node, PORT_SS_EMISSION, float(plan.emission_strength))
            _apply_channel("emission", PORT_SS_EMISSION_COLOR)

        # Opacity / Alpha (only when non-opaque)
        if plan.requires_alpha and "alpha" in plan.ops:
            _apply_channel("alpha", PORT_SS_OPACITY, scalar_as_rgb=True)

        # Translucency is not applied here: thin_walled + subsurface moved a test lamp further from Blender

        # Transmission
        if plan.has_transmission and "transmission" in plan.ops:
            _apply_channel("transmission", PORT_SS_TRANSMISSION, scalar_port=True)
            bc_op = plan.ops.get("base_color")
            if base_color_out_port is not None:
                _connect_to_node_input(base_color_out_port, surf_node, PORT_SS_TRANSMISSION_COLOR)
            elif bc_op is not None and bc_op.is_value and bc_op.value is not None:
                _set_node_input(surf_node, PORT_SS_TRANSMISSION_COLOR, transmission_tint(bc_op.value))

        # 3. Normal & Bump -> normal_map and/or bump2d -> standard_surface.normal
        nrm_op = plan.ops.get("normal")
        bump_op = plan.ops.get("bump")
        nrm_node_out_port: Any = None

        if nrm_op is not None and nrm_op.is_texture and nrm_op.texture is not None:
            img_node = img_nodes_by_key.get(nrm_op.texture.texture_key)
            nrm_tex_out = compat.find_port(img_node, "output", is_input=False) if img_node is not None else None
            if nrm_tex_out is not None:
                nm_node = compat.add_shader_node(graph, NODE_NORMAL_MAP)
                _connect_to_node_input(nrm_tex_out, nm_node, PORT_NM_INPUT)
                _set_node_input(nm_node, PORT_NM_STRENGTH, float(nrm_op.texture.strength))
                if nrm_op.texture.flip_green:
                    _set_node_input(nm_node, PORT_NM_INVERT_Y, True)
                nrm_node_out_port = compat.find_port(nm_node, PORT_NM_OUT, is_input=False)

        if bump_op is not None and bump_op.is_texture and bump_op.texture is not None:
            bump_tex_out = _resolve_tex_output_port(bump_op.texture, prefer_scalar_on_invert=True)
            if bump_tex_out is not None:
                b2d_node = compat.add_shader_node(graph, NODE_BUMP2D)
                _connect_to_node_input(bump_tex_out, b2d_node, PORT_B2D_BUMP_MAP)
                height_val = float(bump_op.texture.strength) * float(bump_op.texture.distance_cm or 1.0)
                if abs(height_val) <= 1e-6:
                    height_val = float(bump_op.texture.strength)
                _set_node_input(b2d_node, PORT_B2D_BUMP_HEIGHT, height_val)

                # Chain normal_map -> bump2d.normal if both normal and bump are present
                if nrm_node_out_port is not None:
                    if _connect_to_node_input(nrm_node_out_port, b2d_node, PORT_B2D_NORMAL):
                        connected_channels.append("normal")

                b2d_out = compat.find_port(b2d_node, PORT_B2D_OUT, is_input=False)
                if _connect_to_node_input(b2d_out, surf_node, PORT_SS_NORMAL):
                    connected_channels.append("bump")
        elif nrm_node_out_port is not None:
            if _connect_to_node_input(nrm_node_out_port, surf_node, PORT_SS_NORMAL):
                connected_channels.append("normal")

        # 4. Displacement -> material.displacement
        disp_op = plan.ops.get("displacement")
        # Cycles' BUMP method never moves geometry (Arnold height-as-bump: TODO).
        if plan.has_displacement and disp_op is not None and disp_op.is_texture and disp_op.texture is not None:
            disp_img_node = img_nodes_by_key.get(disp_op.texture.texture_key)
            if disp_img_node is not None:
                scale_cm = float(disp_op.texture.scale)
                mid = float(disp_op.texture.midlevel)
                _set_node_input(disp_img_node, PORT_IMG_MULTIPLY, (scale_cm, scale_cm, scale_cm))
                off_val = -mid * scale_cm
                _set_node_input(disp_img_node, PORT_IMG_OFFSET, (off_val, off_val, off_val))

            disp_out = _resolve_tex_output_port(disp_op.texture, prefer_scalar_on_invert=True)
            if disp_out is not None and _connect_to_node_input(disp_out, end_node, PORT_MAT_DISPLACEMENT):
                connected_channels.append("displacement")

        transaction.Commit()

    all_nodes = compat.list_graph_nodes(graph)
    return BuiltMaterialResult(
        material=mat,
        node_count=len(all_nodes),
        connected_channels=connected_channels,
        value_channels=value_channels,
        warnings=warnings,
    )


def apply_non_opaque_object_tag(doc: Any, obj: Any, warnings: List[str]) -> Optional[Any]:
    """Add an Arnold Parameters tag (``1029989``) with ``opaque = OFF`` on *obj*.

    Resolves the ``opaque`` parameter ID at runtime by walking ``tag.GetDescription()``.
    If the parameter cannot be found, logs and appends a warning.
    """
    if c4d is None or obj is None:
        return None

    tag = obj.GetTag(ARNOLD_PARAMS_TAG_ID) if hasattr(obj, "GetTag") else None
    created_new = False
    if tag is None and hasattr(obj, "MakeTag"):
        try:
            tag = obj.MakeTag(ARNOLD_PARAMS_TAG_ID)
            created_new = tag is not None
        except Exception as exc:
            log.debug("MakeTag(%d) failed on %r: %s", ARNOLD_PARAMS_TAG_ID, obj, exc)
            tag = None

    if tag is None:
        warnings.append(
            f"Object '{obj.GetName()}': could not create Arnold Parameters tag ({ARNOLD_PARAMS_TAG_ID}) "
            "to disable opaque."
        )
        return None

    if created_new and doc is not None and hasattr(doc, "AddUndo"):
        try:
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, tag)
        except Exception:
            pass

    dtype_bool = getattr(c4d, "DTYPE_BOOL", 400006001)
    opaque_param = compat.resolve_description_param(
        tag,
        context="arnold.parameters_tag.opaque",
        idents=("C4DAIP_POLYMESH_OPAQUE", "polymesh.opaque", "opaque"),
        names=("Opaque", "opaque"),
        fallback_c4d_symbols=("C4DAIP_POLYMESH_OPAQUE",),
        prefer_dtypes=(dtype_bool,),
    )

    if opaque_param is None:
        warnings.append(
            f"Object '{obj.GetName()}': Arnold Parameters tag ({ARNOLD_PARAMS_TAG_ID}) attached, "
            "but 'opaque' parameter could not be found in GetDescription()."
        )
        return tag

    if not compat.set_resolved_parameter(tag, opaque_param, False):
        warnings.append(
            f"Object '{obj.GetName()}': resolved Arnold 'opaque' parameter ({opaque_param.ident or opaque_param.int_id}), "
            "but SetParameter(False) failed."
        )
    return tag
