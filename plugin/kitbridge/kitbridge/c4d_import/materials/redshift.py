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

"""Redshift node material builder for Cinema 4D (Milestone 2b).

Uses the exact node and port identifiers from ``docs/c4d-node-ports-2027.json``
and only the low-level ``maxon`` graph API (C4D 2023.0+).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .. import compat
from ..compat import c4d
from .base import ChannelOp, MaterialPlan, SharedTextureSpec, TextureOp, rec709_to_render_space, transmission_tint

log = logging.getLogger("kitbridge")

RS_NODESPACE_ID = "com.redshift3d.redshift4c4d.class.nodespace"
RS_OBJECT_TAG_ID = 1036222

# Exact node IDs from docs/c4d-node-ports-2027.json
NODE_OUTPUT = "com.redshift3d.redshift4c4d.node.output"
NODE_STANDARD_MAT = "com.redshift3d.redshift4c4d.nodes.core.standardmaterial"
NODE_TEX_SAMPLER = "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
NODE_COLOR_SPLITTER = "com.redshift3d.redshift4c4d.nodes.core.rscolorsplitter"
NODE_MATH_INV = "com.redshift3d.redshift4c4d.nodes.core.rsmathinv"
NODE_BUMP_MAP = "com.redshift3d.redshift4c4d.nodes.core.bumpmap"
NODE_DISPLACEMENT = "com.redshift3d.redshift4c4d.nodes.core.displacement"
NODE_BUMP_BLENDER = "com.redshift3d.redshift4c4d.nodes.core.bumpblender"

# Output node ports
PORT_OUT_SURFACE = "com.redshift3d.redshift4c4d.node.output.surface"
PORT_OUT_DISPLACEMENT = "com.redshift3d.redshift4c4d.node.output.displacement"

# Standard Material ports
_SM = "com.redshift3d.redshift4c4d.nodes.core.standardmaterial."
PORT_SM_OUTCOLOR = _SM + "outcolor"
PORT_SM_BASE_COLOR = _SM + "base_color"
PORT_SM_BASE_WEIGHT = _SM + "base_color_weight"
PORT_SM_METALNESS = _SM + "metalness"
PORT_SM_REFL_WEIGHT = _SM + "refl_weight"
PORT_SM_REFL_ROUGHNESS = _SM + "refl_roughness"
PORT_SM_REFL_IOR = _SM + "refl_ior"
PORT_SM_EMISSION_COLOR = _SM + "emission_color"
PORT_SM_EMISSION_WEIGHT = _SM + "emission_weight"
PORT_SM_OPACITY_COLOR = _SM + "opacity_color"
PORT_SM_REFR_WEIGHT = _SM + "refr_weight"
PORT_SM_REFR_COLOR = _SM + "refr_color"
PORT_SM_REFR_ROUGHNESS = _SM + "refr_roughness"
PORT_SM_COAT_WEIGHT = _SM + "coat_weight"
PORT_SM_COAT_ROUGHNESS = _SM + "coat_roughness"
PORT_SM_SHEEN_WEIGHT = _SM + "sheen_weight"
PORT_SM_MS_AMOUNT = _SM + "ms_amount"
PORT_SM_MS_COLOR = _SM + "ms_color"
PORT_SM_BUMP_INPUT = _SM + "bump_input"

# Texture Sampler ports
_TS = "com.redshift3d.redshift4c4d.nodes.core.texturesampler."
PORT_TS_TEX0 = _TS + "tex0"
PORT_TS_TEX0_PATH = (PORT_TS_TEX0, "path")
PORT_TS_TEX0_COLORSPACE = (PORT_TS_TEX0, "colorspace")
PORT_TS_SCALE = _TS + "scale"
PORT_TS_OFFSET = _TS + "offset"
PORT_TS_ROTATE = _TS + "rotate"
PORT_TS_OUTCOLOR = _TS + "outcolor"

# Color Splitter ports
_CS = "com.redshift3d.redshift4c4d.nodes.core.rscolorsplitter."
PORT_CS_INPUT = _CS + "input"
PORT_CS_OUT_MAP = {
    "r": _CS + "outr",
    "g": _CS + "outg",
    "b": _CS + "outb",
    "a": _CS + "outa",
}

# Math Invert ports
_INV = "com.redshift3d.redshift4c4d.nodes.core.rsmathinv."
PORT_INV_INPUT = _INV + "input"
PORT_INV_OUT = _INV + "out"

# Bump Map ports
_BM = "com.redshift3d.redshift4c4d.nodes.core.bumpmap."
PORT_BM_INPUT = _BM + "input"
PORT_BM_SCALE = _BM + "scale"
PORT_BM_INPUTTYPE = _BM + "inputtype"
PORT_BM_FLIPY = _BM + "flipy"
PORT_BM_OUT = _BM + "out"

# Bump Blender ports (stacks several bump sources)
_BB = "com.redshift3d.redshift4c4d.nodes.core.bumpblender."
PORT_BB_BASE = _BB + "baseinput"
PORT_BB_INPUT = _BB + "bumpinput%d"
PORT_BB_WEIGHT = _BB + "bumpweight%d"
PORT_BB_ADDITIVE = _BB + "additive"
PORT_BB_OUT = _BB + "outdisplacementvector"

# Displacement ports
_DP = "com.redshift3d.redshift4c4d.nodes.core.displacement."
PORT_DP_TEXMAP = _DP + "texmap"
PORT_DP_SCALE = _DP + "scale"
PORT_DP_NEWRANGE_MIN = _DP + "newrange_min"
PORT_DP_NEWRANGE_MAX = _DP + "newrange_max"
PORT_DP_OUT = _DP + "out"

BUMP_INPUTTYPE_HEIGHT = 0
BUMP_INPUTTYPE_TANGENT_NORMAL = 1


@dataclass
class BuiltMaterialResult:
    """Result returned by a renderer material builder."""

    material: Any
    node_count: int = 0
    connected_channels: List[str] = field(default_factory=list)
    value_channels: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _set_sm_input(std_node: Any, port_id: str, value: Any) -> bool:
    port = compat.find_port(std_node, port_id, is_input=True)
    return compat.set_port_value(port, rec709_to_render_space(value, compat.document_render_space()))


def _connect_to_sm_input(src_out_port: Any, std_node: Any, port_id: str) -> bool:
    dst_port = compat.find_port(std_node, port_id, is_input=True)
    return compat.connect_ports(src_out_port, dst_port)


def build_redshift_material(plan: MaterialPlan, material_name: Optional[str] = None) -> BuiltMaterialResult:
    """Construct a native Redshift node material from *plan*."""
    if c4d is None:
        raise RuntimeError("Cinema 4D Python SDK (c4d) is unavailable.")

    mat = c4d.BaseMaterial(c4d.Mmaterial)
    if mat is None:
        raise RuntimeError("Failed to allocate c4d.BaseMaterial(c4d.Mmaterial).")
    mat.SetName(material_name or plan.name)

    graph = compat.create_or_get_default_graph(mat, RS_NODESPACE_ID)
    warnings: List[str] = list(plan.warnings)
    connected_channels: List[str] = []
    value_channels: List[str] = []

    with graph.BeginTransaction() as transaction:
        out_nodes = compat.find_nodes_by_asset_id(graph, NODE_OUTPUT)
        if out_nodes:
            output_node = out_nodes[0]
        else:
            output_node = compat.add_shader_node(graph, NODE_OUTPUT)

        sm_nodes = compat.find_nodes_by_asset_id(graph, NODE_STANDARD_MAT)
        if sm_nodes:
            std_node = sm_nodes[0]
        else:
            std_node = compat.add_shader_node(graph, NODE_STANDARD_MAT)
            sm_out = compat.find_port(std_node, PORT_SM_OUTCOLOR, is_input=False)
            out_surf = compat.find_port(output_node, PORT_OUT_SURFACE, is_input=True)
            compat.connect_ports(sm_out, out_surf)

        # Keep reflection weight at 1.0 and set IOR per spec
        _set_sm_input(std_node, PORT_SM_REFL_WEIGHT, float(plan.reflection_weight))
        _set_sm_input(std_node, PORT_SM_REFL_IOR, float(plan.ior))

        # 1. Create shared texture sampler nodes (deduplicated across channels, e.g. ORM)
        tex_nodes_by_key: Dict[str, Any] = {}
        splitter_nodes_by_key: Dict[str, Any] = {}

        for shared_spec in plan.shared_textures:
            ts_node = compat.add_shader_node(graph, NODE_TEX_SAMPLER)
            tex_nodes_by_key[shared_spec.key] = ts_node

            path_port = compat.find_port(ts_node, PORT_TS_TEX0_PATH, is_input=True)
            if not compat.set_port_file_path(path_port, shared_spec.path):
                warnings.append(
                    f"Material '{plan.name}': failed to set Redshift tex0.path for {shared_spec.rel_path!r}."
                )

            cs_port = compat.find_port(ts_node, PORT_TS_TEX0_COLORSPACE, is_input=True)
            cs_str = compat.resolve_redshift_colorspace(cs_port, shared_spec.colorspace)
            compat.set_port_value(cs_port, cs_str)

            # UV transform on texturesampler when non-identity
            if not shared_spec.uv.is_identity:
                scale_port = compat.find_port(ts_node, PORT_TS_SCALE, is_input=True)
                offset_port = compat.find_port(ts_node, PORT_TS_OFFSET, is_input=True)
                rot_port = compat.find_port(ts_node, PORT_TS_ROTATE, is_input=True)
                compat.set_port_value(scale_port, list(shared_spec.uv.scale))
                compat.set_port_value(offset_port, list(shared_spec.uv.offset))
                compat.set_port_value(rot_port, float(shared_spec.uv.rotation))

            # Add one rscolorsplitter per shared texture that needs single-channel extraction
            if shared_spec.needs_splitter:
                sp_node = compat.add_shader_node(graph, NODE_COLOR_SPLITTER)
                ts_out = compat.find_port(ts_node, PORT_TS_OUTCOLOR, is_input=False)
                sp_in = compat.find_port(sp_node, PORT_CS_INPUT, is_input=True)
                compat.connect_ports(ts_out, sp_in)
                splitter_nodes_by_key[shared_spec.key] = sp_node

        def _resolve_tex_output_port(tex_op: TextureOp) -> Any:
            ts_node = tex_nodes_by_key.get(tex_op.texture_key)
            if ts_node is None:
                return None

            if tex_op.is_single_channel:
                sp_node = splitter_nodes_by_key.get(tex_op.texture_key)
                if sp_node is None:
                    sp_node = compat.add_shader_node(graph, NODE_COLOR_SPLITTER)
                    ts_out = compat.find_port(ts_node, PORT_TS_OUTCOLOR, is_input=False)
                    sp_in = compat.find_port(sp_node, PORT_CS_INPUT, is_input=True)
                    compat.connect_ports(ts_out, sp_in)
                    splitter_nodes_by_key[tex_op.texture_key] = sp_node
                out_port_id = PORT_CS_OUT_MAP.get(tex_op.channel, PORT_CS_OUT_MAP["r"])
                src_port = compat.find_port(sp_node, out_port_id, is_input=False)
            else:
                src_port = compat.find_port(ts_node, PORT_TS_OUTCOLOR, is_input=False)

            if tex_op.invert and src_port is not None:
                inv_node = compat.add_shader_node(graph, NODE_MATH_INV)
                inv_in = compat.find_port(inv_node, PORT_INV_INPUT, is_input=True)
                compat.connect_ports(src_port, inv_in)
                src_port = compat.find_port(inv_node, PORT_INV_OUT, is_input=False)

            return src_port

        def _apply_channel(ch_name: str, target_port_id: str, *, scalar_as_rgb: bool = False) -> Optional[Any]:
            op = plan.ops.get(ch_name)
            if op is None:
                return None
            if op.is_texture and op.texture is not None:
                out_port = _resolve_tex_output_port(op.texture)
                if out_port is not None and _connect_to_sm_input(out_port, std_node, target_port_id):
                    if ch_name not in connected_channels:
                        connected_channels.append(ch_name)
                    return out_port
                warnings.append(f"Material '{plan.name}': failed to connect texture for '{ch_name}'.")
                return None
            if op.is_value and op.value is not None:
                val: Any = op.value
                if scalar_as_rgb and isinstance(val, (int, float)):
                    f = float(val)
                    val = (f, f, f)
                if _set_sm_input(std_node, target_port_id, val):
                    if ch_name not in value_channels:
                        value_channels.append(ch_name)
                return None
            return None

        # 2. Wire standard PBR channels
        base_color_out_port = _apply_channel("base_color", PORT_SM_BASE_COLOR)
        _apply_channel("metallic", PORT_SM_METALNESS)
        roughness_out_port = _apply_channel("roughness", PORT_SM_REFL_ROUGHNESS)
        _apply_channel("ior", PORT_SM_REFL_IOR)
        _apply_channel("coat", PORT_SM_COAT_WEIGHT)
        _apply_channel("coat_roughness", PORT_SM_COAT_ROUGHNESS)
        _apply_channel("sheen", PORT_SM_SHEEN_WEIGHT)

        # Subsurface (weight -> ms_amount, plus ms_color = base_color when active)
        ss_op = plan.ops.get("subsurface")
        if ss_op is not None:
            _apply_channel("subsurface", PORT_SM_MS_AMOUNT)
            bc_op = plan.ops.get("base_color")
            if base_color_out_port is not None:
                _connect_to_sm_input(base_color_out_port, std_node, PORT_SM_MS_COLOR)
            elif bc_op is not None and bc_op.is_value and bc_op.value is not None:
                _set_sm_input(std_node, PORT_SM_MS_COLOR, bc_op.value)

        # Emission (+ emission_weight)
        em_op = plan.ops.get("emission")
        if em_op is not None and plan.emission_strength > 0.0:
            _set_sm_input(std_node, PORT_SM_EMISSION_WEIGHT, float(plan.emission_strength))
            _apply_channel("emission", PORT_SM_EMISSION_COLOR)

        # Opacity / Alpha (only when non-opaque)
        if plan.requires_alpha and "alpha" in plan.ops:
            _apply_channel("alpha", PORT_SM_OPACITY_COLOR, scalar_as_rgb=True)

        # Translucency: Standard Material has no such lobe, a rough thin sheet lets light through the same way
        tl_op = plan.ops.get("translucency")
        if tl_op is not None and tl_op.is_value and float(tl_op.value or 0.0) > 0.0 and not plan.has_transmission:
            _set_sm_input(std_node, PORT_SM_REFR_WEIGHT, float(tl_op.value))
            _set_sm_input(std_node, _SM + "refr_thin_walled", True)
            _set_sm_input(std_node, PORT_SM_REFR_ROUGHNESS, 0.8)
            bc_op = plan.ops.get("base_color")
            if base_color_out_port is not None:
                _connect_to_sm_input(base_color_out_port, std_node, PORT_SM_REFR_COLOR)
            elif bc_op is not None and bc_op.is_value and bc_op.value is not None:
                _set_sm_input(std_node, PORT_SM_REFR_COLOR, bc_op.value)

        # Transmission -> refr_weight, refr_color = base_color, refr_roughness = roughness
        if plan.has_transmission and "transmission" in plan.ops:
            _apply_channel("transmission", PORT_SM_REFR_WEIGHT)
            bc_op = plan.ops.get("base_color")
            if base_color_out_port is not None:
                _connect_to_sm_input(base_color_out_port, std_node, PORT_SM_REFR_COLOR)
            elif bc_op is not None and bc_op.is_value and bc_op.value is not None:
                _set_sm_input(std_node, PORT_SM_REFR_COLOR, transmission_tint(bc_op.value))

            rough_op = plan.ops.get("roughness")
            if roughness_out_port is not None:
                _connect_to_sm_input(roughness_out_port, std_node, PORT_SM_REFR_ROUGHNESS)
            elif rough_op is not None and rough_op.is_value and rough_op.value is not None:
                _set_sm_input(std_node, PORT_SM_REFR_ROUGHNESS, rough_op.value)

        # 3. Normal / Bump / displacement-as-bump -> bumpmap nodes -> standardmaterial.bump_input
        #    (several sources are stacked with an additive Bump Blender, like Cycles chains them)
        bump_sources: List[Tuple[str, Any]] = []

        def _add_bump_map(tex_out: Any, input_type: int, scale: float, flip_y: bool = False) -> Any:
            bm_node = compat.add_shader_node(graph, NODE_BUMP_MAP)
            compat.connect_ports(tex_out, compat.find_port(bm_node, PORT_BM_INPUT, is_input=True))
            compat.set_port_value(compat.find_port(bm_node, PORT_BM_INPUTTYPE, is_input=True), input_type)
            compat.set_port_value(compat.find_port(bm_node, PORT_BM_SCALE, is_input=True), float(scale))
            if flip_y:
                compat.set_port_value(compat.find_port(bm_node, PORT_BM_FLIPY, is_input=True), True)
            return compat.find_port(bm_node, PORT_BM_OUT, is_input=False)

        nrm_op = plan.ops.get("normal")
        if nrm_op is not None and nrm_op.is_texture and nrm_op.texture is not None:
            nrm_tex_out = _resolve_tex_output_port(nrm_op.texture)
            if nrm_tex_out is not None:
                bump_sources.append(("normal", _add_bump_map(
                    nrm_tex_out, BUMP_INPUTTYPE_TANGENT_NORMAL, nrm_op.texture.strength,
                    flip_y=bool(nrm_op.texture.flip_green))))
        bump_op = plan.ops.get("bump")
        if bump_op is not None and bump_op.is_texture and bump_op.texture is not None:
            bump_tex_out = _resolve_tex_output_port(bump_op.texture)
            if bump_tex_out is not None:
                bump_sources.append(("bump", _add_bump_map(
                    bump_tex_out, BUMP_INPUTTYPE_HEIGHT, bump_op.texture.strength)))
        disp_op = plan.ops.get("displacement")
        if (plan.displacement_as_bump and disp_op is not None and disp_op.is_texture
                and disp_op.texture is not None):
            # Cycles shades the height as bump (method BUMP, or BOTH on top of the moved geometry).
            disp_bump_out = _resolve_tex_output_port(disp_op.texture)
            if disp_bump_out is not None:
                bump_sources.append(("displacement_bump", _add_bump_map(
                    disp_bump_out, BUMP_INPUTTYPE_HEIGHT, disp_op.texture.scale)))

        bump_final = bump_sources[0][1] if len(bump_sources) == 1 else None
        if len(bump_sources) > 1:
            blender = compat.add_shader_node(graph, NODE_BUMP_BLENDER)
            compat.set_port_value(compat.find_port(blender, PORT_BB_ADDITIVE, is_input=True), True)
            compat.connect_ports(bump_sources[0][1], compat.find_port(blender, PORT_BB_BASE, is_input=True))
            for i, (_, out_port) in enumerate(bump_sources[1:4]):
                compat.connect_ports(out_port, compat.find_port(blender, PORT_BB_INPUT % i, is_input=True))
                compat.set_port_value(compat.find_port(blender, PORT_BB_WEIGHT % i, is_input=True), 1.0)
            bump_final = compat.find_port(blender, PORT_BB_OUT, is_input=False)
        if bump_final is not None and _connect_to_sm_input(bump_final, std_node, PORT_SM_BUMP_INPUT):
            connected_channels.extend(name for name, _ in bump_sources)

        # 4. Displacement -> displacement node -> output.displacement (only if Cycles moves geometry)
        if plan.has_displacement and disp_op is not None and disp_op.is_texture and disp_op.texture is not None:
            disp_tex_out = _resolve_tex_output_port(disp_op.texture)
            if disp_tex_out is not None:
                dp_node = compat.add_shader_node(graph, NODE_DISPLACEMENT)
                dp_texmap = compat.find_port(dp_node, PORT_DP_TEXMAP, is_input=True)
                dp_scale = compat.find_port(dp_node, PORT_DP_SCALE, is_input=True)
                dp_min = compat.find_port(dp_node, PORT_DP_NEWRANGE_MIN, is_input=True)
                dp_max = compat.find_port(dp_node, PORT_DP_NEWRANGE_MAX, is_input=True)
                dp_out = compat.find_port(dp_node, PORT_DP_OUT, is_input=False)
                out_disp = compat.find_port(output_node, PORT_OUT_DISPLACEMENT, is_input=True)

                compat.connect_ports(disp_tex_out, dp_texmap)
                compat.set_port_value(dp_scale, float(disp_op.texture.scale))
                mid = float(disp_op.texture.midlevel)
                compat.set_port_value(dp_min, -mid)
                compat.set_port_value(dp_max, 1.0 - mid)

                if compat.connect_ports(dp_out, out_disp):
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


def set_thin_walled(mat: Any) -> bool:
    """Treat this glass as a sheet: an open shell has no inside for refraction to work with."""
    graph = compat.create_or_get_default_graph(mat, RS_NODESPACE_ID)
    with graph.BeginTransaction() as transaction:
        nodes = compat.find_nodes_by_asset_id(graph, NODE_STANDARD_MAT)
        done = bool(nodes) and _set_sm_input(nodes[0], _SM + "refr_thin_walled", True)
        transaction.Commit()
    return done


def _enable_tag_displacement(tag: Any, dtype_bool: int) -> bool:
    """Turn displacement on in a Redshift Object tag; True only if a real switch was set.

    Newer Redshift (seen in C4D 2027) has a Mode cycle Off / Render Settings / On, default
    "Render Settings"; older builds have a bool. Match by identifier only: the "Displacement" label
    also names the parameter group, and writing True into a group silently does nothing.
    """
    for bc, did, _ in tag.GetDescription(c4d.DESCFLAGS_DESC_NONE):
        if str(bc[c4d.DESC_IDENT] or "") == "REDSHIFT_OBJECT_GEOMETRY_DISPLACEMENT_MODE":
            for value, label in (bc[c4d.DESC_CYCLE] or []):
                if str(label).strip().lower() == "on":
                    tag[did] = value
                    return True
    legacy = compat.resolve_description_param(
        tag,
        context="redshift.object_tag.displacement_enabled",
        idents=("REDSHIFT_OBJECT_GEOMETRY_DISPLACEMENTENABLED",),
        names=(),
        fallback_c4d_symbols=("REDSHIFT_OBJECT_GEOMETRY_DISPLACEMENTENABLED",),
        prefer_dtypes=(dtype_bool,),
    )
    return legacy is not None and bool(compat.set_resolved_parameter(tag, legacy, True))


def apply_displacement_object_tag(doc: Any, obj: Any, warnings: List[str]) -> Optional[Any]:
    """Add/enable displacement & tessellation on a Redshift Object tag (``1036222``) on *obj*.

    Resolves the geometry override, subdivision/tessellation, and displacement
    enable parameters at runtime via ``tag.GetDescription()``.
    """
    if c4d is None or obj is None:
        return None

    tag = obj.GetTag(RS_OBJECT_TAG_ID) if hasattr(obj, "GetTag") else None
    created_new = False
    if tag is None and hasattr(obj, "MakeTag"):
        try:
            tag = obj.MakeTag(RS_OBJECT_TAG_ID)
            created_new = tag is not None
        except Exception as exc:
            log.debug("MakeTag(%d) failed on %r: %s", RS_OBJECT_TAG_ID, obj, exc)
            tag = None

    if tag is None:
        warnings.append(
            f"Object '{obj.GetName()}': could not create Redshift Object tag ({RS_OBJECT_TAG_ID}) for displacement."
        )
        return None

    if created_new and doc is not None and hasattr(doc, "AddUndo"):
        try:
            doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, tag)
        except Exception:
            pass

    dtype_bool = getattr(c4d, "DTYPE_BOOL", 400006001)

    override_param = compat.resolve_description_param(
        tag,
        context="redshift.object_tag.geometry_override",
        idents=("REDSHIFT_OBJECT_GEOMETRY_OVERRIDE", "geometry_override"),
        names=("Override", "Geometry Override"),
        fallback_c4d_symbols=("REDSHIFT_OBJECT_GEOMETRY_OVERRIDE",),
        prefer_dtypes=(dtype_bool,),
    )
    tess_param = compat.resolve_description_param(
        tag,
        context="redshift.object_tag.subdivision_enabled",
        idents=("REDSHIFT_OBJECT_GEOMETRY_SUBDIVISIONENABLED", "subdivisionenabled", "tessellation"),
        names=("Enabled", "Enable Tessellation", "Subdivision"),
        fallback_c4d_symbols=("REDSHIFT_OBJECT_GEOMETRY_SUBDIVISIONENABLED",),
        prefer_dtypes=(dtype_bool,),
    )
    compat.set_resolved_parameter(tag, override_param, True)
    compat.set_resolved_parameter(tag, tess_param, True)
    disp_ok = _enable_tag_displacement(tag, dtype_bool)

    if disp_ok:
        warnings.append(
            f"Object '{obj.GetName()}': enabled Redshift Object tag displacement and tessellation."
        )
    else:
        warnings.append(
            f"Object '{obj.GetName()}': attached Redshift Object tag ({RS_OBJECT_TAG_ID}), "
            "but displacement enable parameter could not be resolved."
        )
    return tag
