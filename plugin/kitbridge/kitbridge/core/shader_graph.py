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

"""Pure-Python shader node-graph classifier (no ``bpy`` or ``c4d`` imports).

Classifies material sockets into ``"direct"`` (constant value or recognised
single-image PBR pattern) vs ``"bake"`` (procedural textures, Mix, Math,
ColorRamp, AO, non-UV coordinates, Mix Shader of two BSDFs, etc.).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

DIRECT = "direct"
BAKE = "bake"

# Canonical node type aliases (maps both Blender bl_idname and node.type strings).
_NODE_TYPE_ALIASES: Dict[str, str] = {
    "SHADERNODETEXIMAGE": "TEX_IMAGE",
    "TEX_IMAGE": "TEX_IMAGE",
    "IMAGE_TEXTURE": "TEX_IMAGE",
    "IMAGE": "TEX_IMAGE",
    "SHADERNODESEPARATECOLOR": "SEPARATE_COLOR",
    "SEPARATE_COLOR": "SEPARATE_COLOR",
    "SHADERNODESEPARATERGB": "SEPARATE_RGB",
    "SEPARATE_RGB": "SEPARATE_RGB",
    "SEP_RGB": "SEPARATE_RGB",
    "SEP_COLOR": "SEPARATE_COLOR",
    "SHADERNODENORMALMAP": "NORMAL_MAP",
    "NORMAL_MAP": "NORMAL_MAP",
    "SHADERNODEBUMP": "BUMP",
    "BUMP": "BUMP",
    "SHADERNODEINVERT": "INVERT",
    "INVERT": "INVERT",
    "SHADERNODEDISPLACEMENT": "DISPLACEMENT",
    "DISPLACEMENT": "DISPLACEMENT",
    "SHADERNODEMAPPING": "MAPPING",
    "MAPPING": "MAPPING",
    "SHADERNODEUVMAP": "UVMAP",
    "UVMAP": "UVMAP",
    "UV_MAP": "UVMAP",
    "SHADERNODETEXCOORD": "TEX_COORD",
    "TEX_COORD": "TEX_COORD",
    "NODEREROUTE": "REROUTE",
    "REROUTE": "REROUTE",
    "SHADERNODEBSDFPRINCIPLED": "BSDF_PRINCIPLED",
    "BSDF_PRINCIPLED": "BSDF_PRINCIPLED",
    "PRINCIPLED_BSDF": "BSDF_PRINCIPLED",
    "SHADERNODEOUTPUTMATERIAL": "OUTPUT_MATERIAL",
    "OUTPUT_MATERIAL": "OUTPUT_MATERIAL",
    "MATERIAL_OUTPUT": "OUTPUT_MATERIAL",
    "SHADERNODEMIXSHADER": "MIX_SHADER",
    "MIX_SHADER": "MIX_SHADER",
    "SHADERNODEADDSHADER": "ADD_SHADER",
    "ADD_SHADER": "ADD_SHADER",
    "SHADERNODEEMISSION": "EMISSION",
    "EMISSION": "EMISSION",
    "SHADERNODEBSDFGLASS": "BSDF_GLASS",
    "BSDF_GLASS": "BSDF_GLASS",
    "GLASS_BSDF": "BSDF_GLASS",
    "SHADERNODEBSDFREFRACTION": "BSDF_REFRACTION",
    "BSDF_REFRACTION": "BSDF_REFRACTION",
    "REFRACTION_BSDF": "BSDF_REFRACTION",
    "SHADERNODEBSDFDIFFUSE": "BSDF_DIFFUSE",
    "BSDF_DIFFUSE": "BSDF_DIFFUSE",
    "DIFFUSE_BSDF": "BSDF_DIFFUSE",
    "SHADERNODEBSDFGLOSSY": "BSDF_GLOSSY",
    "BSDF_GLOSSY": "BSDF_GLOSSY",
    "GLOSSY_BSDF": "BSDF_GLOSSY",
    "SHADERNODEBSDFANISOTROPIC": "BSDF_ANISOTROPIC",
    "BSDF_ANISOTROPIC": "BSDF_ANISOTROPIC",
    "ANISOTROPIC_BSDF": "BSDF_ANISOTROPIC",
    "SHADERNODEBSDFMETALLIC": "BSDF_METALLIC",
    "BSDF_METALLIC": "BSDF_METALLIC",
    "METALLIC_BSDF": "BSDF_METALLIC",
    "SHADERNODEBSDFTRANSPARENT": "BSDF_TRANSPARENT",
    "BSDF_TRANSPARENT": "BSDF_TRANSPARENT",
    "TRANSPARENT_BSDF": "BSDF_TRANSPARENT",
    "SHADERNODESUBSURFACESCATTERING": "SUBSURFACE_SCATTERING",
    "SUBSURFACE_SCATTERING": "SUBSURFACE_SCATTERING",
    "SHADERNODELIGHTPATH": "LIGHT_PATH",
    "LIGHT_PATH": "LIGHT_PATH",
    "LIGHTPATH": "LIGHT_PATH",
    "SHADERNODEVALUE": "VALUE",
    "VALUE": "VALUE",
    "SHADERNODERGB": "RGB",
    "RGB": "RGB",
    "SHADERNODEVALTORGB": "VALTORGB",
    "VALTORGB": "VALTORGB",
    "COLORRAMP": "VALTORGB",
    "COLOR_RAMP": "VALTORGB",
    "SHADERNODEMIX": "MIX",
    "SHADERNODEMIXRGB": "MIX_RGB",
    "MIX": "MIX",
    "MIX_RGB": "MIX_RGB",
    "SHADERNODEMATH": "MATH",
    "MATH": "MATH",
    "SHADERNODEHUESATURATION": "HUE_SAT",
    "HUE_SAT": "HUE_SAT",
    "SHADERNODEGAMMA": "GAMMA",
    "GAMMA": "GAMMA",
    "SHADERNODEBRIGHTCONTRAST": "BRIGHTCONTRAST",
    "BRIGHTCONTRAST": "BRIGHTCONTRAST",
    "SHADERNODEAMBIENTOCCLUSION": "AMBIENT_OCCLUSION",
    "AMBIENT_OCCLUSION": "AMBIENT_OCCLUSION",
    "AO": "AMBIENT_OCCLUSION",
    "SHADERNODELAYERWEIGHT": "LAYER_WEIGHT",
    "LAYER_WEIGHT": "LAYER_WEIGHT",
    "SHADERNODEFRESNEL": "FRESNEL",
    "FRESNEL": "FRESNEL",
    "SHADERNODETEXNOISE": "TEX_NOISE",
    "TEX_NOISE": "TEX_NOISE",
    "SHADERNODETEXVORONOI": "TEX_VORONOI",
    "TEX_VORONOI": "TEX_VORONOI",
    "SHADERNODETEXWAVE": "TEX_WAVE",
    "TEX_WAVE": "TEX_WAVE",
    "SHADERNODETEXMUSGRAVE": "TEX_MUSGRAVE",
    "TEX_MUSGRAVE": "TEX_MUSGRAVE",
    "SHADERNODETEXCHECKER": "TEX_CHECKER",
    "TEX_CHECKER": "TEX_CHECKER",
    "SHADERNODETEXBRICK": "TEX_BRICK",
    "TEX_BRICK": "TEX_BRICK",
    "SHADERNODETEXGRADIENT": "TEX_GRADIENT",
    "TEX_GRADIENT": "TEX_GRADIENT",
    "SHADERNODETEXMAGIC": "TEX_MAGIC",
    "TEX_MAGIC": "TEX_MAGIC",
}

_SEP_CHANNEL_MAP: Dict[str, str] = {
    "r": "r",
    "red": "r",
    "g": "g",
    "green": "g",
    "b": "b",
    "blue": "b",
}

# Principled BSDF socket lookup table:
# (manifest_channel, candidate_blender_socket_names, is_color, default_value)
PRINCIPLED_CHANNEL_SPECS: Tuple[Tuple[str, Tuple[str, ...], bool, Any], ...] = (
    ("base_color", ("Base Color",), True, [0.8, 0.8, 0.8]),
    ("metallic", ("Metallic",), False, 0.0),
    ("roughness", ("Roughness",), False, 0.5),
    ("specular", ("Specular IOR Level", "Specular"), False, 0.5),
    ("ior", ("IOR",), False, 1.5),
    ("alpha", ("Alpha",), False, 1.0),
    ("transmission", ("Transmission Weight", "Transmission"), False, 0.0),
    ("coat", ("Coat Weight", "Clearcoat"), False, 0.0),
    ("coat_roughness", ("Coat Roughness", "Clearcoat Roughness"), False, 0.03),
    ("sheen", ("Sheen Weight", "Sheen"), False, 0.0),
    ("subsurface", ("Subsurface Weight", "Subsurface"), False, 0.0),
)

# Channels whose constant default value can be omitted to keep manifests concise
# unless non-default, while core PBR parameters stay explicit.
_OPTIONAL_ZERO_CHANNELS = {
    "transmission": 0.0,
    "coat": 0.0,
    "sheen": 0.0,
    "subsurface": 0.0,
}


def udim_texture_template(filename: str) -> str:
    """Return a filename with its tile number replaced by the literal UDIM token."""
    name = str(filename or "")
    if "<UDIM>" in name:
        return name
    return re.sub(r"(?<!\d)(?:[._-])\d{4}(?=\.[^.]+$)", ".<UDIM>", name)


def udim_tile_filename(template: str, tile: int) -> str:
    """Expand a UDIM filename template to one tile filename."""
    return str(template).replace("<UDIM>", str(int(tile)))


def blender_matrix_to_fbx(matrix: Sequence[Sequence[float]]) -> List[List[float]]:
    """Convert Blender Z-up coordinates to FBX Y-up/-Z-forward coordinates."""
    m = [[float(x) for x in row[:4]] for row in matrix[:4]]
    c = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))
    ci = ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, -1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0))

    def multiply(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> List[List[float]]:
        return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]

    return multiply(multiply(c, m), ci)


def build_light_manifest_entry(
    *,
    name: str,
    light_type: str,
    color: Sequence[float],
    power: float,
    radius: float,
    matrix: Sequence[Sequence[float]],
    parent: Optional[str] = None,
    shape: Optional[str] = None,
    size: Optional[float] = None,
    size_y: Optional[float] = None,
    spot_size: Optional[float] = None,
    spot_blend: Optional[float] = None,
) -> Dict[str, Any]:
    """Build a Blender-independent light manifest entry."""
    entry: Dict[str, Any] = {
        "name": str(name),
        "type": str(light_type).lower(),
        "colour": [float(x) for x in color[:3]],
        "power": float(power),
        "radius": float(radius),
        "matrix": blender_matrix_to_fbx(matrix),
    }
    if parent:
        entry["parent"] = str(parent)
    if shape:
        entry["shape"] = str(shape).lower()
    if size is not None:
        entry["size"] = float(size)
    if size_y is not None:
        entry["size_y"] = float(size_y)
    if spot_size is not None:
        entry["spot_size"] = float(spot_size)
    if spot_blend is not None:
        entry["spot_blend"] = float(spot_blend)
    return entry


# ── Lightweight Duck-Typed Graph Primitives (for unit tests & adapters) ──────


@dataclass
class GraphLink:
    """Directed connection from ``from_node``'s ``from_socket`` into a target socket."""

    from_node: Any
    from_socket: str = "Color"


@dataclass
class GraphSocket:
    """Duck-typed shader socket with a default value and optional incoming link."""

    name: str
    default_value: Any = None
    link: Optional[GraphLink] = None
    links: List[GraphLink] = field(default_factory=list)

    @property
    def is_linked(self) -> bool:
        return self.link is not None or bool(self.links)


@dataclass
class GraphNode:
    """Duck-typed shader node for pure-Python graph construction and testing."""

    node_type: str
    name: str = ""
    inputs: Dict[str, GraphSocket] = field(default_factory=dict)
    properties: Dict[str, Any] = field(default_factory=dict)

    @property
    def type(self) -> str:
        return self.node_type

    @property
    def bl_idname(self) -> str:
        return self.node_type

    def connect(
        self,
        input_name: str,
        from_node: Any,
        from_socket: str = "Color",
        *,
        default_value: Any = None,
    ) -> GraphNode:
        sock = self.inputs.get(input_name)
        if sock is None:
            sock = GraphSocket(name=input_name, default_value=default_value)
            self.inputs[input_name] = sock
        lnk = GraphLink(from_node=from_node, from_socket=from_socket)
        sock.link = lnk
        sock.links = [lnk]
        return self

    def set_input(self, input_name: str, value: Any) -> GraphNode:
        sock = self.inputs.get(input_name)
        if sock is None:
            self.inputs[input_name] = GraphSocket(name=input_name, default_value=value)
        else:
            sock.default_value = value
        return self


@dataclass(frozen=True)
class UVTransform:
    """Resolved UV map name and 2D Mapping transform."""

    map_name: str = "UVMap"
    scale: Tuple[float, float] = (1.0, 1.0)
    offset: Tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "map": self.map_name or "UVMap",
            "scale": [round(float(self.scale[0]), 6), round(float(self.scale[1]), 6)],
            "offset": [round(float(self.offset[0]), 6), round(float(self.offset[1]), 6)],
            "rotation": round(float(self.rotation), 6),
        }


@dataclass
class SocketClassification:
    """Classification result for a single material channel socket."""

    decision: str  # "direct" | "bake"
    channel_name: str = ""
    is_constant: bool = False
    channel_data: Optional[Dict[str, Any]] = None
    extra_channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    uv: Optional[UVTransform] = None
    reason: str = ""
    warnings: List[str] = field(default_factory=list)
    bake_metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def action(self) -> str:
        return self.decision

    @property
    def mode(self) -> str:
        return self.decision

    def __getitem__(self, key: str) -> Any:
        if key in ("decision", "action", "mode", "kind"):
            return self.decision
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)


@dataclass
class MaterialClassification:
    """Full material node-tree classification before Cycles baking."""

    channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    sockets_to_bake: List[str] = field(default_factory=list)
    bake_metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    uv: Dict[str, Any] = field(
        default_factory=lambda: {
            "map": "UVMap",
            "scale": [1.0, 1.0],
            "offset": [0.0, 0.0],
            "rotation": 0.0,
        }
    )
    warnings: List[str] = field(default_factory=list)
    is_mix_shader: bool = False
    surface_shader_type: str = "BSDF_PRINCIPLED"
    blend_method: str = "opaque"


# ── Duck-Typed Node & Socket Inspection Helpers ──────────────────────────────


def canonical_node_type(node: Any) -> str:
    """Normalise a node's ``node_type``, ``bl_idname``, or ``type`` to a canonical string."""
    if node is None:
        return ""
    for attr in ("node_type", "bl_idname", "type"):
        raw = getattr(node, attr, None)
        if isinstance(raw, str) and raw.strip():
            key = raw.strip().upper().replace(" ", "_")
            if key in _NODE_TYPE_ALIASES:
                return _NODE_TYPE_ALIASES[key]
            cleaned = raw.strip().upper()
            if cleaned in _NODE_TYPE_ALIASES:
                return _NODE_TYPE_ALIASES[cleaned]
            return key
    return ""


def _node_prop(node: Any, name: str, default: Any = None) -> Any:
    props = getattr(node, "properties", None)
    if isinstance(props, dict) and name in props:
        return props[name]
    if hasattr(node, name):
        return getattr(node, name)
    return default


def get_input_socket(node: Any, *names: str) -> Optional[Any]:
    """Find the first matching input socket on *node* by name (case-insensitive fallback)."""
    if node is None:
        return None
    inputs = getattr(node, "inputs", None)
    if inputs is None:
        return None

    # Dict-like (GraphNode or bpy node_tree.nodes[x].inputs)
    if hasattr(inputs, "get"):
        for candidate in names:
            try:
                sock = inputs.get(candidate)
            except Exception:
                sock = None
            if sock is not None:
                return sock

    # Sequence / iterable fallback with case-insensitive match
    lower_targets = {n.lower(): n for n in names}
    try:
        items: Iterable[Any] = inputs.values() if isinstance(inputs, dict) else inputs
        for sock in items:
            sname = str(getattr(sock, "name", "") or "")
            if sname in names or sname.lower() in lower_targets:
                return sock
    except TypeError:
        pass
    return None


def _raw_socket_link(socket: Any) -> Optional[Tuple[Any, str]]:
    if socket is None:
        return None
    is_linked = getattr(socket, "is_linked", None)
    if is_linked is False:
        return None

    link = getattr(socket, "link", None)
    if link is None:
        links = getattr(socket, "links", None)
        if links:
            try:
                link = links[0]
            except (IndexError, TypeError):
                link = None
    if link is None:
        return None

    from_node = getattr(link, "from_node", None)
    if from_node is None:
        return None
    raw_from_sock = getattr(link, "from_socket", None)
    if isinstance(raw_from_sock, str):
        from_sock_name = raw_from_sock
    elif raw_from_sock is not None and hasattr(raw_from_sock, "name"):
        from_sock_name = str(getattr(raw_from_sock, "name") or "Color")
    else:
        from_sock_name = "Color"
    return from_node, from_sock_name


def resolve_upstream(socket: Any, max_hops: int = 32) -> Optional[Tuple[Any, str]]:
    """Return ``(from_node, from_socket_name)`` skipping ``REROUTE`` nodes."""
    current = _raw_socket_link(socket)
    hops = 0
    while current is not None and hops < max_hops:
        node, sock_name = current
        if canonical_node_type(node) != "REROUTE":
            return node, sock_name
        reroute_in = get_input_socket(node, "Input", "")
        if reroute_in is None:
            inputs = getattr(node, "inputs", None)
            if inputs:
                try:
                    reroute_in = list(inputs.values() if isinstance(inputs, dict) else inputs)[0]
                except (IndexError, TypeError):
                    reroute_in = None
        current = _raw_socket_link(reroute_in)
        hops += 1
    return None


def _to_float(val: Any, fallback: float = 0.0) -> float:
    if isinstance(val, (int, float)):
        return round(float(val), 6)
    try:
        seq = list(val)
        if seq:
            return round(float(seq[0]), 6)
    except TypeError:
        pass
    return round(float(fallback), 6)


def _to_rgb(val: Any, fallback: Sequence[float] = (0.8, 0.8, 0.8)) -> List[float]:
    if isinstance(val, (int, float)):
        f = round(float(val), 6)
        return [f, f, f]
    try:
        seq = [round(float(x), 6) for x in list(val)]
        if len(seq) >= 3:
            return seq[:3]
    except TypeError:
        pass
    return [round(float(fallback[0]), 6), round(float(fallback[1]), 6), round(float(fallback[2]), 6)]


def _to_vec2(val: Any, fallback: Tuple[float, float] = (1.0, 1.0)) -> Tuple[float, float]:
    if isinstance(val, (int, float)):
        f = float(val)
        return (f, f)
    try:
        seq = [float(x) for x in list(val)]
        if len(seq) >= 2:
            return (seq[0], seq[1])
    except TypeError:
        pass
    return fallback


def _extract_constant_scalar(socket: Any, fallback: float = 0.0) -> Optional[float]:
    """Return a constant float for *socket* if unlinked or linked to a ``VALUE`` node."""
    if socket is None:
        return round(float(fallback), 6)
    upstream = resolve_upstream(socket)
    if upstream is None:
        return _to_float(getattr(socket, "default_value", fallback), fallback)
    node, _ = upstream
    if canonical_node_type(node) == "VALUE":
        val = _node_prop(node, "value", None)
        if val is not None:
            return _to_float(val, fallback)
        outputs = getattr(node, "outputs", None)
        if outputs:
            try:
                out0 = list(outputs.values() if isinstance(outputs, dict) else outputs)[0]
                return _to_float(getattr(out0, "default_value", fallback), fallback)
            except (IndexError, TypeError):
                pass
        return round(float(fallback), 6)
    return None


def _extract_constant_rgb(
    socket: Any,
    fallback: Sequence[float] = (0.8, 0.8, 0.8),
) -> Optional[List[float]]:
    """Return a constant ``[r, g, b]`` if *socket* is unlinked or linked to ``RGB``/``VALUE``."""
    if socket is None:
        return _to_rgb(fallback, fallback)
    upstream = resolve_upstream(socket)
    if upstream is None:
        return _to_rgb(getattr(socket, "default_value", fallback), fallback)
    node, _ = upstream
    ntype = canonical_node_type(node)
    if ntype == "RGB":
        col = _node_prop(node, "color", None)
        if col is not None:
            return _to_rgb(col, fallback)
        outputs = getattr(node, "outputs", None)
        if outputs:
            try:
                out0 = list(outputs.values() if isinstance(outputs, dict) else outputs)[0]
                return _to_rgb(getattr(out0, "default_value", fallback), fallback)
            except (IndexError, TypeError):
                pass
        return _to_rgb(fallback, fallback)
    if ntype == "VALUE":
        scalar = _extract_constant_scalar(socket, fallback[0])
        if scalar is not None:
            return [scalar, scalar, scalar]
    return None


# ── Image & UV Mapping Inspection ────────────────────────────────────────────


def _image_info(tex_node: Any) -> Tuple[str, bool, List[str]]:
    """Extract ``(relative_texture_path, is_udim, warnings)`` from a ``TEX_IMAGE`` node."""
    warnings: List[str] = []
    is_udim = bool(_node_prop(tex_node, "is_udim", False))
    rel_path = str(_node_prop(tex_node, "image_path", "") or "").strip()

    image_obj = _node_prop(tex_node, "image", None)
    if isinstance(image_obj, str):
        if "<UDIM>" in image_obj:
            is_udim = True
        if not rel_path and image_obj.strip():
            rel_path = image_obj.strip()
        image_obj = None

    if image_obj is not None:
        if getattr(image_obj, "source", "") == "TILED":
            is_udim = True
        raw_fp = str(
            getattr(image_obj, "kitbridge_rel_path", "")
            or getattr(image_obj, "filepath", "")
            or ""
        ).strip()
        if "<UDIM>" in raw_fp:
            is_udim = True
        if not rel_path and raw_fp:
            rel_path = raw_fp.replace("\\", "/")

    return rel_path.replace("\\", "/"), is_udim, warnings


def _extract_mapping_transform(mapping_node: Any) -> Optional[Tuple[Tuple[float, float], Tuple[float, float], float]]:
    """Extract ``(scale, offset, rotation_deg)`` from a ``ShaderNodeMapping`` if constant."""
    loc_sock = get_input_socket(mapping_node, "Location", "Offset")
    rot_sock = get_input_socket(mapping_node, "Rotation")
    scale_sock = get_input_socket(mapping_node, "Scale")

    for s in (loc_sock, rot_sock, scale_sock):
        if s is not None and resolve_upstream(s) is not None:
            return None

    scale_prop = _node_prop(mapping_node, "scale", None)
    offset_prop = _node_prop(mapping_node, "offset", _node_prop(mapping_node, "location", None))
    rot_prop = _node_prop(mapping_node, "rotation", None)

    if scale_prop is not None:
        scale = _to_vec2(scale_prop, (1.0, 1.0))
    elif scale_sock is not None and getattr(scale_sock, "default_value", None) is not None:
        scale = _to_vec2(scale_sock.default_value, (1.0, 1.0))
    else:
        scale = (1.0, 1.0)

    if offset_prop is not None:
        offset = _to_vec2(offset_prop, (0.0, 0.0))
    elif loc_sock is not None and getattr(loc_sock, "default_value", None) is not None:
        offset = _to_vec2(loc_sock.default_value, (0.0, 0.0))
    else:
        offset = (0.0, 0.0)

    if isinstance(rot_prop, (int, float)):
        rotation = float(rot_prop)
    elif rot_prop is not None:
        try:
            seq = [float(x) for x in list(rot_prop)]
            rotation = math.degrees(seq[2]) if len(seq) >= 3 else float(seq[0])
        except (TypeError, IndexError):
            rotation = 0.0
    elif rot_sock is not None and getattr(rot_sock, "default_value", None) is not None:
        dv = rot_sock.default_value
        if isinstance(dv, (int, float)):
            rotation = float(dv)
        else:
            try:
                seq = [float(x) for x in list(dv)]
                rotation = math.degrees(seq[2]) if len(seq) >= 3 else float(seq[0])
            except (TypeError, IndexError):
                rotation = 0.0
    else:
        rotation = 0.0

    return scale, offset, rotation


def resolve_image_uv(
    tex_node: Any,
    default_uv_map: str = "UVMap",
) -> Tuple[Optional[UVTransform], str]:
    """Validate and extract the UV coordinate chain feeding *tex_node*.

    Returns ``(UVTransform, "")`` if *tex_node* uses standard UV coordinates
    (optionally through ``Mapping`` / ``UV Map`` / ``Texture Coordinate(UV)``),
    or ``(None, reason)`` if *tex_node* is driven by non-UV or procedural coordinates.
    """
    projection = str(_node_prop(tex_node, "projection", "FLAT") or "FLAT").upper()
    if projection not in ("FLAT", ""):
        return None, f"Image projection {projection!r} requires baking"

    vec_sock = get_input_socket(tex_node, "Vector")
    if vec_sock is None or resolve_upstream(vec_sock) is None:
        return UVTransform(map_name=default_uv_map), ""

    scale: Tuple[float, float] = (1.0, 1.0)
    offset: Tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0
    uv_map_name = default_uv_map

    cur_sock = vec_sock
    hops = 0
    while hops < 16:
        hops += 1
        upstream = resolve_upstream(cur_sock)
        if upstream is None:
            break
        node, out_sock_name = upstream
        ntype = canonical_node_type(node)

        if ntype == "MAPPING":
            parsed = _extract_mapping_transform(node)
            if parsed is None:
                return None, "Mapping node has procedural inputs"
            m_scale, m_offset, m_rot = parsed
            scale = (scale[0] * m_scale[0], scale[1] * m_scale[1])
            offset = (offset[0] + m_offset[0], offset[1] + m_offset[1])
            rotation += m_rot
            cur_sock = get_input_socket(node, "Vector")
            if cur_sock is None:
                break
            continue

        if ntype == "UVMAP":
            explicit_uv = str(_node_prop(node, "uv_map", "") or "").strip()
            if explicit_uv:
                uv_map_name = explicit_uv
            return UVTransform(map_name=uv_map_name, scale=scale, offset=offset, rotation=rotation), ""

        if ntype == "TEX_COORD":
            if out_sock_name.strip().upper() == "UV":
                return UVTransform(map_name=uv_map_name, scale=scale, offset=offset, rotation=rotation), ""
            return None, f"Non-UV texture coordinate ({out_sock_name}) requires baking"

        return None, f"Procedural vector node ({ntype or type(node).__name__}) before image requires baking"

    return UVTransform(map_name=uv_map_name, scale=scale, offset=offset, rotation=rotation), ""


def _unwrap_identity_color_chain(
    node: Any,
    out_socket_name: str,
    max_hops: int = 8,
) -> Tuple[Any, str]:
    """Skip no-op color pass-through nodes (identity ``Hue/Sat``, ``Gamma(1)``, ``Bright/Contrast(0, 0)``)."""
    cur_node = node
    cur_sock = out_socket_name
    for _ in range(max_hops):
        ntype = canonical_node_type(cur_node)
        if ntype == "HUE_SAT":
            col_in = get_input_socket(cur_node, "Color")
            up = resolve_upstream(col_in)
            if up is None:
                break
            fac_s = get_input_socket(cur_node, "Fac", "Factor")
            fac_v = _extract_constant_scalar(fac_s, -1.0) if fac_s is not None else None
            h_s = get_input_socket(cur_node, "Hue")
            s_s = get_input_socket(cur_node, "Saturation")
            v_s = get_input_socket(cur_node, "Value")
            h_v = _extract_constant_scalar(h_s, -1.0) if h_s is not None else None
            s_v = _extract_constant_scalar(s_s, -1.0) if s_s is not None else None
            v_v = _extract_constant_scalar(v_s, -1.0) if v_s is not None else None
            is_zero_fac = fac_v is not None and abs(fac_v) < 1e-5
            is_identity_hsv = (
                h_v is not None
                and s_v is not None
                and v_v is not None
                and abs(h_v - 0.5) < 1e-5
                and abs(s_v - 1.0) < 1e-5
                and abs(v_v - 1.0) < 1e-5
                and (fac_s is None or fac_v is not None)
            )
            if is_zero_fac or is_identity_hsv:
                cur_node, cur_sock = up
                continue
            break

        if ntype == "GAMMA":
            col_in = get_input_socket(cur_node, "Color")
            up = resolve_upstream(col_in)
            g_s = get_input_socket(cur_node, "Gamma")
            g_v = _extract_constant_scalar(g_s, -1.0) if g_s is not None else None
            if up is not None and g_v is not None and abs(g_v - 1.0) < 1e-5:
                cur_node, cur_sock = up
                continue
            break

        if ntype == "BRIGHTCONTRAST":
            col_in = get_input_socket(cur_node, "Color")
            up = resolve_upstream(col_in)
            b_s = get_input_socket(cur_node, "Bright", "Brightness")
            c_s = get_input_socket(cur_node, "Contrast")
            b_v = _extract_constant_scalar(b_s, -1.0) if b_s is not None else None
            c_v = _extract_constant_scalar(c_s, -1.0) if c_s is not None else None
            if (
                up is not None
                and b_v is not None
                and c_v is not None
                and abs(b_v) < 1e-5
                and abs(c_v) < 1e-5
            ):
                cur_node, cur_sock = up
                continue
            break

        break
    return cur_node, cur_sock


def _match_image_source(
    node: Any,
    out_socket_name: str,
    *,
    colorspace: str,
    default_uv_map: str = "UVMap",
) -> Tuple[Optional[Dict[str, Any]], Optional[UVTransform], List[str], str]:
    """Match ``Image`` or ``Image -> Separate RGB/Color`` at ``(node, out_socket_name)``.

    Returns ``(channel_dict, uv_transform, warnings, fail_reason)``.
    """
    node, out_socket_name = _unwrap_identity_color_chain(node, out_socket_name)
    ntype = canonical_node_type(node)

    if ntype == "TEX_IMAGE":
        rel_path, is_udim, warnings = _image_info(node)
        if not rel_path:
            return None, None, warnings, "Image Texture node has no image file"
        uv_tf, reason = resolve_image_uv(node, default_uv_map=default_uv_map)
        if uv_tf is None:
            return None, None, warnings, reason
        out_lower = out_socket_name.strip().lower()
        ch = "a" if out_lower == "alpha" else "rgb"
        info: Dict[str, Any] = {
            "texture": rel_path,
            "colorspace": colorspace,
            "channel": ch,
        }
        if is_udim:
            info["udim"] = True
        return info, uv_tf, warnings, ""

    if ntype in ("SEPARATE_COLOR", "SEPARATE_RGB"):
        mode = str(_node_prop(node, "mode", "RGB") or "RGB").upper()
        if mode != "RGB":
            return None, None, [], f"Separate Color mode {mode!r} requires baking"
        ch_key = _SEP_CHANNEL_MAP.get(out_socket_name.strip().lower(), "")
        if not ch_key:
            return None, None, [], f"Unknown Separate RGB output {out_socket_name!r}"
        color_in = get_input_socket(node, "Color", "Image")
        upstream = resolve_upstream(color_in)
        if upstream is None:
            return None, None, [], "Separate Color has no linked Image Texture"
        up_node, up_sock = _unwrap_identity_color_chain(upstream[0], upstream[1])
        if canonical_node_type(up_node) != "TEX_IMAGE":
            return None, None, [], f"Separate Color input {canonical_node_type(up_node)} requires baking"
        rel_path, is_udim, warnings = _image_info(up_node)
        if not rel_path:
            return None, None, warnings, "Image Texture node has no image file"
        uv_tf, reason = resolve_image_uv(up_node, default_uv_map=default_uv_map)
        if uv_tf is None:
            return None, None, warnings, reason
        info = {
            "texture": rel_path,
            "colorspace": colorspace,
            "channel": ch_key,
        }
        if is_udim:
            info["udim"] = True
        return info, uv_tf, warnings, ""

    return None, None, [], f"Upstream node {ntype or type(node).__name__} requires baking"


# ── Public Socket & Material Classifiers ─────────────────────────────────────


def classify_socket(
    socket: Any,
    *,
    channel_name: str = "base_color",
    is_color: Optional[bool] = None,
    default_value: Any = None,
    default_uv_map: str = "UVMap",
) -> SocketClassification:
    """Classify a single shader input socket as ``"direct"`` or ``"bake"``."""
    ch = (channel_name or "base_color").strip().lower()
    if is_color is None:
        is_color = ch in ("base_color", "emission")
    colorspace = "srgb" if ch in ("base_color", "emission") else "raw"

    # Special handling for normal / bump / displacement sockets
    if ch in ("normal", "bump"):
        return _classify_normal_socket(socket, default_uv_map=default_uv_map)
    if ch == "displacement":
        return _classify_displacement_socket(socket, default_uv_map=default_uv_map)

    upstream = resolve_upstream(socket)
    if upstream is None:
        if is_color:
            fallback_rgb = default_value if default_value is not None else [0.8, 0.8, 0.8]
            val: Any = _extract_constant_rgb(socket, fallback_rgb)
        else:
            fallback_f = float(default_value) if default_value is not None else 0.0
            val = _extract_constant_scalar(socket, fallback_f)
        return SocketClassification(
            decision=DIRECT,
            channel_name=ch,
            is_constant=True,
            channel_data={"value": val},
        )

    node, out_sock = upstream
    ntype = canonical_node_type(node)

    # Constant VALUE / RGB node connected to socket
    if ntype in ("VALUE", "RGB"):
        if is_color:
            const_rgb = _extract_constant_rgb(socket)
            if const_rgb is not None:
                return SocketClassification(
                    decision=DIRECT,
                    channel_name=ch,
                    is_constant=True,
                    channel_data={"value": const_rgb},
                )
        else:
            const_f = _extract_constant_scalar(socket)
            if const_f is not None:
                return SocketClassification(
                    decision=DIRECT,
                    channel_name=ch,
                    is_constant=True,
                    channel_data={"value": const_f},
                )

    # Recognised pattern: Image -> Invert -> socket (or Image -> Separate RGB -> Invert -> socket)
    if ntype == "INVERT":
        fac_sock = get_input_socket(node, "Fac", "Factor")
        fac_val = _extract_constant_scalar(fac_sock, 1.0)
        if fac_val is None or fac_val < 0.99:
            return SocketClassification(
                decision=BAKE,
                channel_name=ch,
                reason="Partial or textured Invert factor requires baking",
            )
        col_in = get_input_socket(node, "Color")
        inv_up = resolve_upstream(col_in)
        if inv_up is None:
            return SocketClassification(
                decision=BAKE,
                channel_name=ch,
                reason="Invert node without upstream Image Texture requires baking",
            )
        tex_info, uv_tf, warnings, reason = _match_image_source(
            inv_up[0],
            inv_up[1],
            colorspace=colorspace,
            default_uv_map=default_uv_map,
        )
        if tex_info is not None:
            tex_info["invert"] = True
            return SocketClassification(
                decision=DIRECT,
                channel_name=ch,
                is_constant=False,
                channel_data=tex_info,
                uv=uv_tf,
                warnings=warnings,
            )
        if warnings:
            return SocketClassification(
                decision=DIRECT,
                channel_name=ch,
                is_constant=True,
                channel_data=None,
                warnings=warnings,
                reason=reason,
            )
        return SocketClassification(decision=BAKE, channel_name=ch, reason=reason)

    # Recognised pattern: Image -> socket OR Image -> Separate RGB/Color -> socket
    tex_info, uv_tf, warnings, reason = _match_image_source(
        node,
        out_sock,
        colorspace=colorspace,
        default_uv_map=default_uv_map,
    )
    if tex_info is not None:
        if ch == "roughness" and "invert" not in tex_info:
            tex_info["invert"] = False
        if ch == "alpha":
            tex_info.pop("colorspace", None)
        return SocketClassification(
            decision=DIRECT,
            channel_name=ch,
            is_constant=False,
            channel_data=tex_info,
            uv=uv_tf,
            warnings=warnings,
        )

    if warnings:
        # UDIM texture: warn and skip baking
        return SocketClassification(
            decision=DIRECT,
            channel_name=ch,
            is_constant=True,
            channel_data=None,
            warnings=warnings,
            reason=reason,
        )

    return SocketClassification(
        decision=BAKE,
        channel_name=ch,
        reason=reason or f"Node {ntype} requires baking",
    )


def _classify_normal_socket(
    socket: Any,
    *,
    default_uv_map: str = "UVMap",
) -> SocketClassification:
    """Classify the ``Normal`` input of a BSDF (handling ``Normal Map`` and ``Bump``)."""
    upstream = resolve_upstream(socket)
    if upstream is None:
        return SocketClassification(
            decision=DIRECT,
            channel_name="normal",
            is_constant=True,
            channel_data=None,
        )

    node, _ = upstream
    ntype = canonical_node_type(node)

    if ntype == "NORMAL_MAP":
        space = str(_node_prop(node, "space", "TANGENT") or "TANGENT").upper()
        if space != "TANGENT":
            return SocketClassification(
                decision=BAKE,
                channel_name="normal",
                reason=f"Normal Map space {space!r} requires baking",
            )
        strength_sock = get_input_socket(node, "Strength")
        strength = _extract_constant_scalar(strength_sock, 1.0)
        if strength is None:
            return SocketClassification(
                decision=BAKE,
                channel_name="normal",
                reason="Textured Normal Map strength requires baking",
            )
        color_sock = get_input_socket(node, "Color")
        col_up = resolve_upstream(color_sock)
        if col_up is None:
            return SocketClassification(
                decision=DIRECT,
                channel_name="normal",
                is_constant=True,
                channel_data=None,
            )
        tex_info, uv_tf, warnings, reason = _match_image_source(
            col_up[0],
            col_up[1],
            colorspace="raw",
            default_uv_map=default_uv_map,
        )
        if tex_info is not None:
            normal_data: Dict[str, Any] = {
                "texture": tex_info["texture"],
                "colorspace": "raw",
                "strength": strength,
                "flip_green": False,
            }
            if tex_info.get("udim"):
                normal_data["udim"] = True
            return SocketClassification(
                decision=DIRECT,
                channel_name="normal",
                is_constant=False,
                channel_data=normal_data,
                uv=uv_tf,
                warnings=warnings,
            )
        if warnings:
            return SocketClassification(
                decision=DIRECT,
                channel_name="normal",
                is_constant=True,
                channel_data=None,
                warnings=warnings,
                reason=reason,
            )
        return SocketClassification(
            decision=BAKE,
            channel_name="normal",
            reason=reason or "Procedural input to Normal Map requires baking",
        )

    if ntype == "BUMP":
        strength_sock = get_input_socket(node, "Strength")
        dist_sock = get_input_socket(node, "Distance")
        strength = _extract_constant_scalar(strength_sock, 1.0)
        distance = _extract_constant_scalar(dist_sock, 0.01)
        if strength is None or distance is None:
            return SocketClassification(
                decision=BAKE,
                channel_name="normal",
                reason="Textured Bump strength/distance requires baking",
            )

        extra_channels: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        normal_in = get_input_socket(node, "Normal")
        if normal_in is not None and resolve_upstream(normal_in) is not None:
            nested_nrm = _classify_normal_socket(normal_in, default_uv_map=default_uv_map)
            warnings.extend(nested_nrm.warnings)
            if nested_nrm.decision != DIRECT or nested_nrm.channel_name != "normal":
                return SocketClassification(
                    decision=BAKE,
                    channel_name="normal",
                    reason="Chained Bump/Normal setup requires baking",
                    warnings=warnings,
                )
            if nested_nrm.channel_data is not None:
                extra_channels["normal"] = nested_nrm.channel_data

        height_sock = get_input_socket(node, "Height")
        height_up = resolve_upstream(height_sock)
        if height_up is None:
            if "normal" in extra_channels:
                return SocketClassification(
                    decision=DIRECT,
                    channel_name="normal",
                    is_constant=False,
                    channel_data=extra_channels["normal"],
                    warnings=warnings,
                )
            return SocketClassification(
                decision=DIRECT,
                channel_name="bump",
                is_constant=True,
                channel_data=None,
                warnings=warnings,
            )

        invert_bump = bool(_node_prop(node, "invert", False))
        h_node, h_sock = height_up
        if canonical_node_type(h_node) == "INVERT":
            fac_val = _extract_constant_scalar(get_input_socket(h_node, "Fac", "Factor"), 1.0)
            if fac_val is None or fac_val < 0.99:
                return SocketClassification(
                    decision=BAKE,
                    channel_name="normal",
                    reason="Partial Invert on Bump height requires baking",
                )
            inv_up = resolve_upstream(get_input_socket(h_node, "Color"))
            if inv_up is None:
                return SocketClassification(
                    decision=BAKE,
                    channel_name="normal",
                    reason="Empty Invert on Bump height requires baking",
                )
            h_node, h_sock = inv_up
            invert_bump = not invert_bump

        tex_info, uv_tf, tex_warnings, reason = _match_image_source(
            h_node,
            h_sock,
            colorspace="raw",
            default_uv_map=default_uv_map,
        )
        warnings.extend(tex_warnings)
        if tex_info is not None:
            bump_data: Dict[str, Any] = {
                "texture": tex_info["texture"],
                "colorspace": "raw",
                "strength": strength,
                "distance": distance,
            }
            if tex_info.get("channel") in ("r", "g", "b", "a"):
                bump_data["channel"] = tex_info["channel"]
            if invert_bump:
                bump_data["invert"] = True
            if tex_info.get("udim"):
                bump_data["udim"] = True
            return SocketClassification(
                decision=DIRECT,
                channel_name="bump",
                is_constant=False,
                channel_data=bump_data,
                extra_channels=extra_channels,
                uv=uv_tf,
                warnings=warnings,
            )
        if tex_warnings:
            return SocketClassification(
                decision=DIRECT,
                channel_name="bump",
                is_constant=True,
                channel_data=None,
                warnings=warnings,
                reason=reason,
            )
        return SocketClassification(
            decision=BAKE,
            channel_name="normal",
            reason=reason or "Procedural Bump height requires baking to normal map",
            warnings=warnings,
        )

    return SocketClassification(
        decision=BAKE,
        channel_name="normal",
        reason=f"Node {ntype} connected to Normal requires baking",
    )


def _classify_displacement_socket(
    socket: Any,
    *,
    default_uv_map: str = "UVMap",
) -> SocketClassification:
    """Classify the ``Displacement`` input on ``Material Output``."""
    upstream = resolve_upstream(socket)
    if upstream is None:
        return SocketClassification(
            decision=DIRECT,
            channel_name="displacement",
            is_constant=True,
            channel_data=None,
        )

    node, out_sock = upstream
    ntype = canonical_node_type(node)

    if ntype == "DISPLACEMENT":
        mid_sock = get_input_socket(node, "Midlevel")
        scale_sock = get_input_socket(node, "Scale")
        midlevel = _extract_constant_scalar(mid_sock, 0.5)
        scale = _extract_constant_scalar(scale_sock, 0.05)
        meta = {
            "midlevel": midlevel if midlevel is not None else 0.5,
            "scale": scale if scale is not None else 0.05,
        }
        if midlevel is None or scale is None:
            return SocketClassification(
                decision=BAKE,
                channel_name="displacement",
                reason="Textured Displacement midlevel/scale requires baking",
                bake_metadata=meta,
            )

        height_sock = get_input_socket(node, "Height")
        height_up = resolve_upstream(height_sock)
        if height_up is None:
            return SocketClassification(
                decision=DIRECT,
                channel_name="displacement",
                is_constant=True,
                channel_data=None,
            )

        tex_info, uv_tf, warnings, reason = _match_image_source(
            height_up[0],
            height_up[1],
            colorspace="raw",
            default_uv_map=default_uv_map,
        )
        if tex_info is not None:
            disp_data: Dict[str, Any] = {
                "texture": tex_info["texture"],
                "colorspace": "raw",
                "midlevel": meta["midlevel"],
                "scale": meta["scale"],
            }
            if tex_info.get("channel") in ("r", "g", "b", "a"):
                disp_data["channel"] = tex_info["channel"]
            if tex_info.get("udim"):
                disp_data["udim"] = True
            return SocketClassification(
                decision=DIRECT,
                channel_name="displacement",
                is_constant=False,
                channel_data=disp_data,
                uv=uv_tf,
                warnings=warnings,
            )
        if warnings:
            return SocketClassification(
                decision=DIRECT,
                channel_name="displacement",
                is_constant=True,
                channel_data=None,
                warnings=warnings,
                reason=reason,
            )
        return SocketClassification(
            decision=BAKE,
            channel_name="displacement",
            reason=reason or "Procedural Displacement height requires baking",
            bake_metadata=meta,
        )

    # Direct image into Material Output Displacement
    tex_info, uv_tf, warnings, reason = _match_image_source(
        node,
        out_sock,
        colorspace="raw",
        default_uv_map=default_uv_map,
    )
    if tex_info is not None:
        disp_direct: Dict[str, Any] = {
            "texture": tex_info["texture"],
            "colorspace": "raw",
            "midlevel": 0.5,
            "scale": 0.05,
        }
        if tex_info.get("udim"):
            disp_direct["udim"] = True
        return SocketClassification(
            decision=DIRECT,
            channel_name="displacement",
            is_constant=False,
            channel_data=disp_direct,
            uv=uv_tf,
            warnings=warnings,
        )

    return SocketClassification(
        decision=BAKE,
        channel_name="displacement",
        reason=reason or f"Displacement node {ntype} requires baking",
        bake_metadata={"midlevel": 0.5, "scale": 0.05},
    )


def select_dominant_uv(
    uv_transforms: Sequence[UVTransform],
    default_uv_map: str = "UVMap",
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Select the most common UV transform across images and return ``(uv_dict, warning)``."""
    if not uv_transforms:
        return UVTransform(map_name=default_uv_map).to_dict(), None

    counts: Counter[UVTransform] = Counter(uv_transforms)
    dominant, _ = counts.most_common(1)[0]
    warning: Optional[str] = None
    if len(counts) > 1:
        warning = (
            f"Material uses {len(counts)} different UV/Mapping transforms across textures; "
            f"keeping the most common (map={dominant.map_name!r}, scale={list(dominant.scale)})."
        )
    return dominant.to_dict(), warning


def resolve_blend_method(
    channels: Dict[str, Dict[str, Any]],
    sockets_to_bake: Sequence[str] = (),
    material_blend_method: str = "",
) -> str:
    """Resolve manifest ``blend_method`` (``'opaque'|'clip'|'blend'``) from classified channels."""
    raw_bm = str(material_blend_method or "").strip().upper()
    if raw_bm == "CLIP":
        return "clip"

    has_non_opaque_alpha = "alpha" in sockets_to_bake
    alpha_ch = channels.get("alpha")
    if isinstance(alpha_ch, dict):
        if "texture" in alpha_ch:
            has_non_opaque_alpha = True
        else:
            val = alpha_ch.get("value")
            if isinstance(val, (int, float)) and float(val) < 0.999:
                has_non_opaque_alpha = True

    has_transmission = "transmission" in sockets_to_bake
    trans_ch = channels.get("transmission")
    if isinstance(trans_ch, dict):
        if "texture" in trans_ch:
            has_transmission = True
        else:
            val = trans_ch.get("value")
            if isinstance(val, (int, float)) and float(val) > 0.001:
                has_transmission = True

    if has_non_opaque_alpha or has_transmission:
        return "blend"

    return "opaque"


def _get_ordered_inputs(node: Any) -> List[Any]:
    inputs = getattr(node, "inputs", None)
    if inputs is None:
        return []
    if isinstance(inputs, dict):
        return list(inputs.values())
    try:
        return list(inputs)
    except TypeError:
        return []


def _get_shader_branch_sockets(node: Any) -> Tuple[Optional[Any], Optional[Any]]:
    """Return ``(shader_slot_1, shader_slot_2)`` on a ``MIX_SHADER`` or ``ADD_SHADER`` node."""
    if node is None:
        return None, None
    inputs = getattr(node, "inputs", None)
    if isinstance(inputs, dict):
        s1 = inputs.get("Shader") or inputs.get("Shader1") or inputs.get("Shader_0")
        s2 = (
            inputs.get("Shader_001")
            or inputs.get("Shader_1")
            or inputs.get("Shader2")
            or inputs.get("Shader_002")
        )
        if s1 is not None and s2 is not None and s1 is not s2:
            return s1, s2
        non_fac = [
            s
            for k, s in inputs.items()
            if str(k).strip().lower() not in ("fac", "factor", "weight")
            and str(getattr(s, "name", "")).strip().lower() not in ("fac", "factor", "weight")
        ]
        return (
            non_fac[0] if len(non_fac) > 0 else s1,
            non_fac[1] if len(non_fac) > 1 else s2,
        )

    ordered = _get_ordered_inputs(node)
    shader_socks = [
        s
        for s in ordered
        if str(getattr(s, "type", "")).upper() == "SHADER"
        or str(getattr(s, "name", "")).strip().lower().startswith("shader")
    ]
    if len(shader_socks) >= 2:
        return shader_socks[0], shader_socks[1]
    non_fac = [
        s
        for s in ordered
        if str(getattr(s, "name", "")).strip().lower() not in ("fac", "factor", "weight")
    ]
    return (
        non_fac[0] if len(non_fac) > 0 else None,
        non_fac[1] if len(non_fac) > 1 else None,
    )


def _has_upstream_light_path(socket: Any, max_depth: int = 16) -> bool:
    """Return ``True`` if *socket* depends on a ``Light Path`` node (e.g. shadow/camera ray tricks)."""
    if socket is None or max_depth <= 0:
        return False
    upstream = resolve_upstream(socket)
    if upstream is None:
        return False
    node, _ = upstream
    if canonical_node_type(node) == "LIGHT_PATH":
        return True
    for inp in _get_ordered_inputs(node):
        if _has_upstream_light_path(inp, max_depth=max_depth - 1):
            return True
    return False


def _evaluate_camera_ray_scalar(
    socket: Any,
    default: float = 0.5,
    depth: int = 0,
) -> Optional[float]:
    """Evaluate a scalar socket tree assuming a camera ray (``Is Camera Ray = 1``, ``Is Shadow Ray = 0``)."""
    if socket is None:
        return round(float(default), 6)
    if depth > 12:
        return None
    upstream = resolve_upstream(socket)
    if upstream is None:
        dv = getattr(socket, "default_value", None)
        return _to_float(dv, default) if dv is not None else round(float(default), 6)

    node, out_sock = upstream
    ntype = canonical_node_type(node)

    if ntype == "LIGHT_PATH":
        s_clean = out_sock.strip().lower().replace("_", " ")
        return 1.0 if "camera" in s_clean else 0.0

    if ntype == "VALUE":
        return _extract_constant_scalar(socket, default)

    if ntype == "INVERT":
        col_in = get_input_socket(node, "Color")
        val = _evaluate_camera_ray_scalar(col_in, default=0.0, depth=depth + 1)
        if val is None:
            return None
        fac_in = get_input_socket(node, "Fac", "Factor")
        fac = _evaluate_camera_ray_scalar(fac_in, default=1.0, depth=depth + 1)
        if fac is None:
            return None
        return round(val * (1.0 - fac) + (1.0 - val) * fac, 6)

    if ntype == "MATH":
        op = str(_node_prop(node, "operation", "ADD") or "ADD").strip().upper()
        use_clamp = bool(_node_prop(node, "use_clamp", False))
        inputs = _get_ordered_inputs(node)
        s0 = inputs[0] if len(inputs) > 0 else get_input_socket(node, "Value")
        s1 = (
            inputs[1]
            if len(inputs) > 1
            else get_input_socket(node, "Value_001", "Value_1", "Value2")
        )
        v0 = _evaluate_camera_ray_scalar(s0, default=0.5, depth=depth + 1)
        if v0 is None:
            return None
        v1 = _evaluate_camera_ray_scalar(s1, default=0.5, depth=depth + 1) if s1 is not None else 0.5
        if v1 is None:
            return None

        if op == "ADD":
            res_val = v0 + v1
        elif op == "SUBTRACT":
            res_val = v0 - v1
        elif op == "MULTIPLY":
            res_val = v0 * v1
        elif op == "DIVIDE":
            res_val = (v0 / v1) if abs(v1) > 1e-9 else 0.0
        elif op == "MAXIMUM":
            res_val = max(v0, v1)
        elif op == "MINIMUM":
            res_val = min(v0, v1)
        elif op == "GREATER_THAN":
            res_val = 1.0 if v0 > v1 else 0.0
        elif op == "LESS_THAN":
            res_val = 1.0 if v0 < v1 else 0.0
        elif op == "MULTIPLY_ADD":
            s2 = (
                inputs[2]
                if len(inputs) > 2
                else get_input_socket(node, "Value_002", "Value_2", "Value3")
            )
            v2 = (
                _evaluate_camera_ray_scalar(s2, default=0.0, depth=depth + 1)
                if s2 is not None
                else 0.0
            )
            if v2 is None:
                return None
            res_val = v0 * v1 + v2
        else:
            res_val = v0

        if use_clamp:
            res_val = max(0.0, min(1.0, res_val))
        return round(float(res_val), 6)

    if ntype in ("MIX", "MIX_RGB"):
        fac_s = get_input_socket(node, "Factor", "Fac")
        a_s = get_input_socket(node, "A", "Color1")
        b_s = get_input_socket(node, "B", "Color2")
        fv = _evaluate_camera_ray_scalar(fac_s, default=0.5, depth=depth + 1)
        av = _evaluate_camera_ray_scalar(a_s, default=0.0, depth=depth + 1)
        bv = _evaluate_camera_ray_scalar(b_s, default=1.0, depth=depth + 1)
        if fv is not None and av is not None and bv is not None:
            fv_c = max(0.0, min(1.0, fv))
            return round(av * (1.0 - fv_c) + bv * fv_c, 6)

    return None


def classify_principled_bsdf(
    bsdf_node: Any,
    *,
    displacement_socket: Optional[Any] = None,
    default_uv_map: str = "UVMap",
    material_blend_method: str = "",
) -> MaterialClassification:
    """Classify all sockets of a ``Principled BSDF`` node and optional ``Displacement`` socket."""
    result = MaterialClassification(surface_shader_type="BSDF_PRINCIPLED")
    collected_uvs: List[UVTransform] = []

    for ch_name, sock_names, is_color, default_val in PRINCIPLED_CHANNEL_SPECS:
        sock = get_input_socket(bsdf_node, *sock_names)
        if sock is None:
            continue
        if ch_name == "alpha" and _has_upstream_light_path(sock):
            if "shadow-ray trick ignored" not in result.warnings:
                result.warnings.append("shadow-ray trick ignored")
            cam_alpha = _evaluate_camera_ray_scalar(sock, default=1.0)
            if cam_alpha is not None and 0.0 < cam_alpha < 1.0:
                result.channels["alpha"] = {"value": round(cam_alpha, 6)}
            else:
                result.channels["alpha"] = {"value": 1.0}
            continue

        cls = classify_socket(
            sock,
            channel_name=ch_name,
            is_color=is_color,
            default_value=default_val,
            default_uv_map=default_uv_map,
        )
        for w in cls.warnings:
            if w not in result.warnings:
                result.warnings.append(w)

        if cls.decision == BAKE:
            if ch_name not in result.sockets_to_bake:
                result.sockets_to_bake.append(ch_name)
            if cls.bake_metadata:
                result.bake_metadata[ch_name] = dict(cls.bake_metadata)
        elif cls.channel_data is not None:
            # Omit optional zero-valued scalar channels to keep manifest clean
            if (
                cls.is_constant
                and ch_name in _OPTIONAL_ZERO_CHANNELS
                and cls.channel_data.get("value") == _OPTIONAL_ZERO_CHANNELS[ch_name]
            ):
                continue
            result.channels[ch_name] = cls.channel_data
            if cls.uv is not None:
                collected_uvs.append(cls.uv)

    # Ensure transmission materials always include ior and roughness even on minimal test nodes
    if "transmission" in result.channels or "transmission" in result.sockets_to_bake:
        if "ior" not in result.channels and "ior" not in result.sockets_to_bake:
            result.channels["ior"] = {"value": 1.5}
        if "roughness" not in result.channels and "roughness" not in result.sockets_to_bake:
            result.channels["roughness"] = {"value": 0.5}

    # Emission Color + Emission Strength
    em_color_sock = get_input_socket(bsdf_node, "Emission Color", "Emission")
    em_str_sock = get_input_socket(bsdf_node, "Emission Strength")
    em_strength = _extract_constant_scalar(em_str_sock, 0.0) if em_str_sock is not None else 1.0
    if em_color_sock is not None:
        em_cls = classify_socket(
            em_color_sock,
            channel_name="emission",
            is_color=True,
            default_value=[0.0, 0.0, 0.0],
            default_uv_map=default_uv_map,
        )
        for w in em_cls.warnings:
            if w not in result.warnings:
                result.warnings.append(w)

        if em_strength is None:
            # Textured/procedural emission strength -> bake combined emission
            if "emission" not in result.sockets_to_bake:
                result.sockets_to_bake.append("emission")
            result.bake_metadata["emission"] = {"strength": 1.0}
        elif em_strength > 0.0:
            if em_cls.decision == BAKE:
                if "emission" not in result.sockets_to_bake:
                    result.sockets_to_bake.append("emission")
                result.bake_metadata["emission"] = {"strength": em_strength}
            elif em_cls.channel_data is not None:
                if em_cls.is_constant:
                    rgb = em_cls.channel_data.get("value") or [0.0, 0.0, 0.0]
                    if any(float(c) > 1e-5 for c in rgb):
                        result.channels["emission"] = {
                            "value": rgb,
                            "strength": em_strength,
                        }
                else:
                    em_data = dict(em_cls.channel_data)
                    em_data["strength"] = em_strength
                    result.channels["emission"] = em_data
                    if em_cls.uv is not None:
                        collected_uvs.append(em_cls.uv)

    # Normal / Bump
    nrm_sock = get_input_socket(bsdf_node, "Normal")
    if nrm_sock is not None:
        nrm_cls = _classify_normal_socket(nrm_sock, default_uv_map=default_uv_map)
        for w in nrm_cls.warnings:
            if w not in result.warnings:
                result.warnings.append(w)
        if nrm_cls.decision == BAKE:
            if "normal" not in result.sockets_to_bake:
                result.sockets_to_bake.append("normal")
        else:
            if nrm_cls.channel_data is not None:
                result.channels[nrm_cls.channel_name] = nrm_cls.channel_data
            for extra_k, extra_v in nrm_cls.extra_channels.items():
                result.channels[extra_k] = extra_v
            if nrm_cls.uv is not None:
                collected_uvs.append(nrm_cls.uv)

    # Displacement (on Material Output)
    if displacement_socket is not None:
        disp_cls = _classify_displacement_socket(displacement_socket, default_uv_map=default_uv_map)
        for w in disp_cls.warnings:
            if w not in result.warnings:
                result.warnings.append(w)
        if disp_cls.decision == BAKE:
            if "displacement" not in result.sockets_to_bake:
                result.sockets_to_bake.append("displacement")
            if disp_cls.bake_metadata:
                result.bake_metadata["displacement"] = dict(disp_cls.bake_metadata)
        elif disp_cls.channel_data is not None:
            result.channels["displacement"] = disp_cls.channel_data
            if disp_cls.uv is not None:
                collected_uvs.append(disp_cls.uv)

    uv_dict, uv_warn = select_dominant_uv(collected_uvs, default_uv_map=default_uv_map)
    result.uv = uv_dict
    if uv_warn and uv_warn not in result.warnings:
        result.warnings.append(uv_warn)

    mat_bm = material_blend_method or str(_node_prop(bsdf_node, "blend_method", "") or "")
    result.blend_method = resolve_blend_method(
        result.channels,
        result.sockets_to_bake,
        material_blend_method=mat_bm,
    )
    return result


_SPATIAL_TEXTURE_TYPES = {
    "TEX_IMAGE",
    "TEX_NOISE",
    "TEX_VORONOI",
    "TEX_WAVE",
    "TEX_MUSGRAVE",
    "TEX_CHECKER",
    "TEX_BRICK",
    "TEX_GRADIENT",
    "TEX_MAGIC",
    "AMBIENT_OCCLUSION",
}

_NON_SHADER_SURFACE_TYPES = {
    "TEX_IMAGE",
    "RGB",
    "VALUE",
    "VALTORGB",
    "MIX",
    "MIX_RGB",
    "HUE_SAT",
    "GAMMA",
    "BRIGHTCONTRAST",
    "INVERT",
    "SEPARATE_COLOR",
    "SEPARATE_RGB",
    "MATH",
}

_KNOWN_LEAF_SHADERS = {
    "BSDF_PRINCIPLED",
    "BSDF_DIFFUSE",
    "BSDF_GLOSSY",
    "BSDF_ANISOTROPIC",
    "BSDF_METALLIC",
    "BSDF_GLASS",
    "BSDF_REFRACTION",
    "BSDF_TRANSPARENT",
    "EMISSION",
    "SUBSURFACE_SCATTERING",
}


def _has_upstream_view_angle(socket: Any, max_depth: int = 16) -> bool:
    """Return ``True`` if *socket* depends on ``Fresnel`` or ``Layer Weight`` with no spatial texture."""
    has_view = False
    has_spatial = False

    def _walk(sock: Any, depth: int) -> None:
        nonlocal has_view, has_spatial
        if sock is None or depth <= 0:
            return
        up = resolve_upstream(sock)
        if up is None:
            return
        node, _ = up
        ntype = canonical_node_type(node)
        if ntype in ("FRESNEL", "LAYER_WEIGHT"):
            has_view = True
        if ntype in _SPATIAL_TEXTURE_TYPES:
            has_spatial = True
        for inp in _get_ordered_inputs(node):
            _walk(inp, depth - 1)

    _walk(socket, max_depth)
    return has_view and not has_spatial


def _extract_upstream_fresnel_ior(socket: Any, max_depth: int = 16) -> Optional[float]:
    """Extract constant ``IOR`` from an upstream ``Fresnel`` node if present."""
    if socket is None or max_depth <= 0:
        return None
    up = resolve_upstream(socket)
    if up is None:
        return None
    node, _ = up
    if canonical_node_type(node) == "FRESNEL":
        ior_s = get_input_socket(node, "IOR")
        return _extract_constant_scalar(ior_s, 1.45) if ior_s is not None else 1.45
    for inp in _get_ordered_inputs(node):
        found = _extract_upstream_fresnel_ior(inp, max_depth - 1)
        if found is not None:
            return found
    return None


def _collect_leaf_shaders(
    node: Any,
    max_depth: int = 8,
) -> Tuple[List[Any], bool]:
    """Return ``(leaf_shader_nodes, has_add_shader)`` under a ``MIX_SHADER``/``ADD_SHADER`` tree."""
    leaves: List[Any] = []
    has_add = False

    def _walk(n: Any, d: int) -> None:
        nonlocal has_add
        if n is None or d <= 0:
            return
        nt = canonical_node_type(n)
        if nt in ("MIX_SHADER", "ADD_SHADER"):
            if nt == "ADD_SHADER":
                has_add = True
            s1, s2 = _get_shader_branch_sockets(n)
            u1 = resolve_upstream(s1)
            u2 = resolve_upstream(s2)
            if u1 is not None:
                _walk(u1[0], d - 1)
            if u2 is not None:
                _walk(u2[0], d - 1)
        else:
            leaves.append(n)

    _walk(node, max_depth)
    return leaves, has_add


def _leaf_needs_normal_bake(leaf: Any) -> bool:
    ltype = canonical_node_type(leaf)
    if ltype not in _KNOWN_LEAF_SHADERS:
        return True
    if not getattr(leaf, "inputs", None):
        # Bare stub GraphNode in unit tests with no sockets populated
        return True
    nrm_s = get_input_socket(leaf, "Normal")
    if nrm_s is not None and resolve_upstream(nrm_s) is not None:
        return True
    coat_nrm_s = get_input_socket(leaf, "Coat Normal", "Clearcoat Normal")
    if coat_nrm_s is not None and resolve_upstream(coat_nrm_s) is not None:
        return True
    return False


def _leaf_has_transmission(leaf: Any) -> bool:
    ltype = canonical_node_type(leaf)
    if ltype in ("BSDF_GLASS", "BSDF_REFRACTION"):
        return True
    if ltype == "BSDF_PRINCIPLED":
        tr_s = get_input_socket(leaf, "Transmission Weight", "Transmission")
        if tr_s is not None:
            if resolve_upstream(tr_s) is not None:
                return True
            val = _extract_constant_scalar(tr_s, 0.0)
            if val is not None and val > 1e-4:
                return True
    return False


def _leaf_constant_scalar_channel(leaf: Any, channel_name: str) -> Optional[float]:
    ltype = canonical_node_type(leaf)
    if ltype not in _KNOWN_LEAF_SHADERS or not getattr(leaf, "inputs", None):
        return None
    if channel_name == "metallic":
        if ltype == "BSDF_PRINCIPLED":
            ms = get_input_socket(leaf, "Metallic")
            return _extract_constant_scalar(ms, 0.0) if ms is not None else None
        if ltype in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_METALLIC"):
            return 1.0
        return 0.0
    if channel_name == "roughness":
        if ltype in ("BSDF_TRANSPARENT", "EMISSION"):
            return None
        rs = get_input_socket(leaf, "Roughness")
        return _extract_constant_scalar(rs, 0.5) if rs is not None else None
    return None


def _is_dominant_glass_branch(
    fac_sock: Optional[Any],
    t1: str,
    t2: str,
) -> Optional[int]:
    """Return ``1`` or ``2`` if a ``BSDF_GLASS``/``BSDF_REFRACTION`` branch is dominant in ``Mix Shader``."""
    g1 = t1 in ("BSDF_GLASS", "BSDF_REFRACTION")
    g2 = t2 in ("BSDF_GLASS", "BSDF_REFRACTION")
    if not g1 and not g2:
        return None
    fac_const = _extract_constant_scalar(fac_sock, 0.5)
    if g1 and g2:
        if fac_const is not None and fac_const > 0.5:
            return 2
        return 1
    if fac_const is None:
        # Driven by Fresnel / Layer Weight / Light Path / procedural factor -> Glass is dominant
        return 1 if g1 else 2
    if g1 and fac_const <= 0.5:
        return 1
    if g2 and fac_const >= 0.5:
        return 2
    return None


def _classify_shader_node(
    surf_node: Any,
    *,
    displacement_socket: Optional[Any] = None,
    default_uv_map: str = "UVMap",
    material_blend_method: str = "",
    depth: int = 0,
) -> MaterialClassification:
    """Classify a surface shader node (handling ``Principled``, ``Mix/Add Shader``, and fallback BSDFs)."""
    stype = canonical_node_type(surf_node)

    if stype == "BSDF_PRINCIPLED":
        return classify_principled_bsdf(
            surf_node,
            displacement_socket=displacement_socket,
            default_uv_map=default_uv_map,
            material_blend_method=material_blend_method,
        )

    if stype in ("MIX_SHADER", "ADD_SHADER") and depth < 8:
        s1_sock, s2_sock = _get_shader_branch_sockets(surf_node)
        up1 = resolve_upstream(s1_sock)
        up2 = resolve_upstream(s2_sock)
        n1 = up1[0] if up1 is not None else None
        n2 = up2[0] if up2 is not None else None
        t1 = canonical_node_type(n1) if n1 is not None else ""
        t2 = canonical_node_type(n2) if n2 is not None else ""
        fac_sock = get_input_socket(surf_node, "Factor", "Fac")

        # 1. Mix Shader(Transparent BSDF, X) or Mix Shader(X, Transparent BSDF)
        if stype == "MIX_SHADER" and (
            (t1 == "BSDF_TRANSPARENT" and n2 is not None)
            or (t2 == "BSDF_TRANSPARENT" and n1 is not None)
        ):
            transparent_slot = 1 if t1 == "BSDF_TRANSPARENT" else 2
            transp_node = n1 if transparent_slot == 1 else n2
            x_node = n2 if transparent_slot == 1 else n1
            x_type = canonical_node_type(x_node)
            res = _classify_shader_node(
                x_node,
                displacement_socket=displacement_socket,
                default_uv_map=default_uv_map,
                material_blend_method=material_blend_method,
                depth=depth + 1,
            )
            if _has_upstream_light_path(fac_sock):
                if "shadow-ray trick ignored" not in res.warnings:
                    res.warnings.append("shadow-ray trick ignored")
                cam_fac = _evaluate_camera_ray_scalar(fac_sock, default=0.5)
                if cam_fac is not None:
                    alpha_val = cam_fac if transparent_slot == 1 else (1.0 - cam_fac)
                    alpha_val = max(0.0, min(1.0, alpha_val))
                    if 0.0 < alpha_val < 1.0:
                        existing_a = res.channels.get("alpha", {}).get("value")
                        if (
                            isinstance(existing_a, (int, float))
                            and "texture" not in res.channels.get("alpha", {})
                        ):
                            alpha_val = alpha_val * float(existing_a)
                        res.channels["alpha"] = {"value": round(alpha_val, 6)}
                    elif (
                        "alpha" not in res.channels
                        and "transmission" not in res.channels
                        and "alpha" not in res.sockets_to_bake
                    ):
                        res.channels["alpha"] = {"value": 1.0}
                elif (
                    "alpha" not in res.channels
                    and "transmission" not in res.channels
                    and "alpha" not in res.sockets_to_bake
                ):
                    res.channels["alpha"] = {"value": 1.0}
            elif _has_upstream_view_angle(fac_sock) and (
                x_type in ("BSDF_GLASS", "BSDF_REFRACTION", "BSDF_GLOSSY", "BSDF_ANISOTROPIC")
                or "transmission" in res.channels
                or "transmission" in res.sockets_to_bake
            ):
                if x_type in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC") and "transmission" not in res.channels:
                    # Cycles fake architectural/window glass: Transparent + Glossy/Anisotropic via Fresnel/Layer Weight
                    t_col = _extract_constant_rgb(get_input_socket(transp_node, "Color"), [1.0, 1.0, 1.0])
                    g_col = res.channels.get("base_color", {}).get("value")
                    if "base_color" in res.sockets_to_bake:
                        res.sockets_to_bake.remove("base_color")
                    if t_col is not None and max(t_col) > 0.1:
                        glass_col = t_col
                    elif isinstance(g_col, list) and len(g_col) >= 3 and max(g_col) > 0.1:
                        glass_col = g_col
                    else:
                        glass_col = [1.0, 1.0, 1.0]
                    res.channels["base_color"] = {"value": glass_col}
                    res.channels["metallic"] = {"value": 0.0}
                    res.channels["transmission"] = {"value": 1.0}
                    res.channels["ior"] = {"value": _extract_upstream_fresnel_ior(fac_sock) or 1.5}
                res.channels.pop("alpha", None)
                if "alpha" in res.sockets_to_bake:
                    res.sockets_to_bake.remove("alpha")
            else:
                fac_cls = classify_socket(
                    fac_sock,
                    channel_name="alpha",
                    is_color=False,
                    default_value=0.5,
                    default_uv_map=default_uv_map,
                )
                for w in fac_cls.warnings:
                    if w not in res.warnings:
                        res.warnings.append(w)
                if fac_cls.decision == BAKE:
                    if "alpha" not in res.sockets_to_bake:
                        res.sockets_to_bake.append("alpha")
                    res.channels.pop("alpha", None)
                elif fac_cls.channel_data is not None:
                    if fac_cls.is_constant:
                        f_val = float(fac_cls.channel_data.get("value", 0.5))
                        alpha_val = f_val if transparent_slot == 1 else (1.0 - f_val)
                        alpha_val = max(0.0, min(1.0, alpha_val))
                        existing_a = res.channels.get("alpha", {}).get("value")
                        if (
                            isinstance(existing_a, (int, float))
                            and "texture" not in res.channels.get("alpha", {})
                        ):
                            alpha_val = alpha_val * float(existing_a)
                        res.channels["alpha"] = {"value": round(alpha_val, 6)}
                    else:
                        tex_data = dict(fac_cls.channel_data)
                        tex_data.pop("colorspace", None)
                        if transparent_slot == 2:
                            inv = not bool(tex_data.get("invert", False))
                            if inv:
                                tex_data["invert"] = True
                            else:
                                tex_data.pop("invert", None)
                        res.channels["alpha"] = tex_data
                        if (
                            fac_cls.uv is not None
                            and res.uv == UVTransform(map_name=default_uv_map).to_dict()
                        ):
                            res.uv = fac_cls.uv.to_dict()

            res.blend_method = (
                "clip" if str(material_blend_method or "").strip().upper() == "CLIP" else "blend"
            )
            return res

        # 2. Emission shader mixed in (Mix Shader or Add Shader of Emission and X)
        if (t1 == "EMISSION" and n2 is not None and t2 != "EMISSION") or (
            t2 == "EMISSION" and n1 is not None and t1 != "EMISSION"
        ):
            em_node = n1 if t1 == "EMISSION" else n2
            other_node = n2 if t1 == "EMISSION" else n1
            res = _classify_shader_node(
                other_node,
                displacement_socket=displacement_socket,
                default_uv_map=default_uv_map,
                material_blend_method=material_blend_method,
                depth=depth + 1,
            )
            em_res = _classify_fallback_shader(
                em_node,
                "EMISSION",
                default_uv_map=default_uv_map,
                material_blend_method=material_blend_method,
            )
            for w in em_res.warnings:
                if w not in res.warnings:
                    res.warnings.append(w)
            if "emission" in em_res.sockets_to_bake:
                if "emission" not in res.sockets_to_bake:
                    res.sockets_to_bake.append("emission")
                res.bake_metadata["emission"] = dict(
                    em_res.bake_metadata.get("emission") or {"strength": 1.0}
                )
            elif "emission" in em_res.channels:
                res.channels["emission"] = dict(em_res.channels["emission"])
                if (
                    em_res.uv != UVTransform(map_name=default_uv_map).to_dict()
                    and res.uv == UVTransform(map_name=default_uv_map).to_dict()
                ):
                    res.uv = dict(em_res.uv)
            return res

        # 3. Glass BSDF / Refraction BSDF as dominant branch of a Mix Shader
        if stype == "MIX_SHADER":
            dom_slot = _is_dominant_glass_branch(fac_sock, t1, t2)
            if dom_slot is not None:
                glass_node = n1 if dom_slot == 1 else n2
                glass_type = t1 if dom_slot == 1 else t2
                res = _classify_fallback_shader(
                    glass_node,
                    glass_type,
                    displacement_socket=displacement_socket,
                    default_uv_map=default_uv_map,
                    material_blend_method=material_blend_method,
                )
                if _has_upstream_light_path(fac_sock) and "shadow-ray trick ignored" not in res.warnings:
                    res.warnings.append("shadow-ray trick ignored")
                return res

        # 4. General Mix/Add Shader fallback (bakes procedural combined channels)
        leaves, has_add = _collect_leaf_shaders(surf_node)
        bake_list: List[str] = ["base_color"]
        channels_init: Dict[str, Dict[str, Any]] = {
            "specular": {"value": 0.5},
            "ior": {"value": 1.5},
        }
        for scalar_ch in ("metallic", "roughness"):
            if not has_add and leaves:
                leaf_vals = [_leaf_constant_scalar_channel(lf, scalar_ch) for lf in leaves]
                if all(v is not None for v in leaf_vals):
                    v0 = float(leaf_vals[0])  # type: ignore[arg-type]
                    if all(abs(float(v) - v0) < 1e-5 for v in leaf_vals if v is not None):
                        channels_init[scalar_ch] = {"value": round(v0, 6)}
                        continue
            bake_list.append(scalar_ch)

        if not leaves or any(_leaf_needs_normal_bake(lf) for lf in leaves):
            bake_list.append("normal")

        if any(_leaf_has_transmission(lf) for lf in leaves):
            bake_list.append("transmission")

        res = MaterialClassification(
            channels=channels_init,
            is_mix_shader=True,
            surface_shader_type=stype,
            sockets_to_bake=bake_list,
            uv=UVTransform(map_name=default_uv_map).to_dict(),
            warnings=[
                f"Material uses {stype}; channels are baked into textures."
            ],
        )
        if displacement_socket is not None and resolve_upstream(displacement_socket) is not None:
            disp_cls = _classify_displacement_socket(displacement_socket, default_uv_map=default_uv_map)
            res.warnings.extend(w for w in disp_cls.warnings if w not in res.warnings)
            if disp_cls.decision == BAKE:
                res.sockets_to_bake.append("displacement")
                res.bake_metadata["displacement"] = dict(
                    disp_cls.bake_metadata or {"midlevel": 0.5, "scale": 0.05}
                )
            elif disp_cls.channel_data is not None:
                res.channels["displacement"] = disp_cls.channel_data
        res.blend_method = resolve_blend_method(
            res.channels,
            res.sockets_to_bake,
            material_blend_method=material_blend_method,
        )
        return res

    # Fallback non-Principled shaders (Glass, Refraction, Emission, Diffuse, Glossy, Metallic, Transparent, Subsurface)
    return _classify_fallback_shader(
        surf_node,
        stype,
        displacement_socket=displacement_socket,
        default_uv_map=default_uv_map,
        material_blend_method=material_blend_method,
    )


def classify_material_output(
    output_node: Any,
    *,
    default_uv_map: str = "UVMap",
) -> MaterialClassification:
    """Walk from *output_node* (``Material Output``) and classify the material."""
    disp_sock = get_input_socket(output_node, "Displacement")
    surf_sock = get_input_socket(output_node, "Surface")
    upstream = resolve_upstream(surf_sock)

    if upstream is None:
        res = MaterialClassification(surface_shader_type="NONE")
        res.channels["base_color"] = {"value": [0.8, 0.8, 0.8]}
        res.channels["metallic"] = {"value": 0.0}
        res.channels["roughness"] = {"value": 0.5}
        res.warnings.append("Material Output has no connected surface shader; using default clay.")
        return res

    surf_node, _ = upstream
    mat_bm = str(_node_prop(output_node, "blend_method", "") or "")
    stype = canonical_node_type(surf_node)
    if stype in _NON_SHADER_SURFACE_TYPES:
        res = MaterialClassification(
            surface_shader_type=stype,
            uv=UVTransform(map_name=default_uv_map).to_dict(),
        )
        res.warnings.append(
            f"Mapped {stype} connected directly to Material Output Surface as base_color."
        )
        res.channels["metallic"] = {"value": 0.0}
        res.channels["roughness"] = {"value": 0.5}
        col_cls = classify_socket(
            surf_sock,
            channel_name="base_color",
            is_color=True,
            default_value=[0.8, 0.8, 0.8],
            default_uv_map=default_uv_map,
        )
        res.warnings.extend(w for w in col_cls.warnings if w not in res.warnings)
        if col_cls.decision == BAKE:
            res.sockets_to_bake.append("base_color")
        elif col_cls.channel_data is not None:
            res.channels["base_color"] = col_cls.channel_data
            if col_cls.uv is not None:
                res.uv = col_cls.uv.to_dict()
        else:
            res.channels["base_color"] = {"value": [0.8, 0.8, 0.8]}
        if disp_sock is not None and resolve_upstream(disp_sock) is not None:
            disp_cls = _classify_displacement_socket(disp_sock, default_uv_map=default_uv_map)
            res.warnings.extend(w for w in disp_cls.warnings if w not in res.warnings)
            if disp_cls.decision == BAKE:
                res.sockets_to_bake.append("displacement")
                res.bake_metadata["displacement"] = dict(
                    disp_cls.bake_metadata or {"midlevel": 0.5, "scale": 0.05}
                )
            elif disp_cls.channel_data is not None:
                res.channels["displacement"] = disp_cls.channel_data
        res.blend_method = resolve_blend_method(
            res.channels,
            res.sockets_to_bake,
            material_blend_method=mat_bm,
        )
        return res

    return _classify_shader_node(
        surf_node,
        displacement_socket=disp_sock,
        default_uv_map=default_uv_map,
        material_blend_method=mat_bm,
    )


def _classify_fallback_shader(
    shader_node: Any,
    stype: str,
    *,
    displacement_socket: Optional[Any] = None,
    default_uv_map: str = "UVMap",
    material_blend_method: str = "",
) -> MaterialClassification:
    res = MaterialClassification(
        surface_shader_type=stype,
        uv=UVTransform(map_name=default_uv_map).to_dict(),
    )
    collected_uvs: List[UVTransform] = []

    if stype == "EMISSION":
        res.warnings.append("Mapped Emission shader to Principled BSDF emission channel.")
        res.channels["base_color"] = {"value": [0.0, 0.0, 0.0]}
        res.channels["metallic"] = {"value": 0.0}
        res.channels["roughness"] = {"value": 1.0}
        col_sock = get_input_socket(shader_node, "Color", "Emission Color", "Emission")
        str_sock = get_input_socket(shader_node, "Strength", "Emission Strength")
        strength = _extract_constant_scalar(str_sock, 1.0)
        col_cls = classify_socket(
            col_sock,
            channel_name="emission",
            is_color=True,
            default_value=[1.0, 1.0, 1.0],
            default_uv_map=default_uv_map,
        )
        res.warnings.extend(w for w in col_cls.warnings if w not in res.warnings)
        if strength is None or col_cls.decision == BAKE:
            res.sockets_to_bake.append("emission")
            res.bake_metadata["emission"] = {"strength": strength if strength is not None else 1.0}
        elif col_cls.channel_data is not None:
            em_entry = dict(col_cls.channel_data)
            em_entry["strength"] = strength
            res.channels["emission"] = em_entry
            if col_cls.uv is not None:
                collected_uvs.append(col_cls.uv)
    else:
        res.warnings.append(
            f"Mapped {stype or 'non-Principled'} shader to nearest Principled BSDF equivalent."
        )
        is_glass = stype in ("BSDF_GLASS", "BSDF_REFRACTION")
        is_glossy = stype in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_METALLIC")
        is_sss = stype == "SUBSURFACE_SCATTERING"
        default_col = [1.0, 1.0, 1.0] if is_glass else [0.8, 0.8, 0.8]

        col_sock = get_input_socket(shader_node, "Color", "Base Color")
        if col_sock is not None:
            col_cls = classify_socket(
                col_sock,
                channel_name="base_color",
                is_color=True,
                default_value=default_col,
                default_uv_map=default_uv_map,
            )
            res.warnings.extend(w for w in col_cls.warnings if w not in res.warnings)
            if col_cls.decision == BAKE:
                res.sockets_to_bake.append("base_color")
            elif col_cls.channel_data is not None:
                res.channels["base_color"] = col_cls.channel_data
                if col_cls.uv is not None:
                    collected_uvs.append(col_cls.uv)
        else:
            res.channels["base_color"] = {"value": list(default_col)}

        default_rough = 0.0 if is_glass else (0.2 if is_glossy else 0.5)
        rough_sock = get_input_socket(shader_node, "Roughness")
        if rough_sock is not None:
            r_cls = classify_socket(
                rough_sock,
                channel_name="roughness",
                is_color=False,
                default_value=default_rough,
                default_uv_map=default_uv_map,
            )
            if r_cls.decision == BAKE:
                res.sockets_to_bake.append("roughness")
            elif r_cls.channel_data is not None:
                res.channels["roughness"] = r_cls.channel_data
                if r_cls.uv is not None:
                    collected_uvs.append(r_cls.uv)
        else:
            res.channels["roughness"] = {"value": default_rough}

        if is_glass:
            res.channels["metallic"] = {"value": 0.0}
            res.channels["transmission"] = {"value": 1.0}
            default_ior = 1.45 if stype == "BSDF_REFRACTION" else 1.5
            ior_sock = get_input_socket(shader_node, "IOR")
            if ior_sock is not None:
                ior_cls = classify_socket(
                    ior_sock,
                    channel_name="ior",
                    is_color=False,
                    default_value=default_ior,
                    default_uv_map=default_uv_map,
                )
                if ior_cls.decision == BAKE:
                    res.sockets_to_bake.append("ior")
                elif ior_cls.channel_data is not None:
                    res.channels["ior"] = ior_cls.channel_data
                    if ior_cls.uv is not None:
                        collected_uvs.append(ior_cls.uv)
            else:
                res.channels["ior"] = {"value": default_ior}
        elif is_glossy:
            res.channels["metallic"] = {"value": 1.0}
        elif is_sss:
            res.channels["metallic"] = {"value": 0.0}
            ior_sock = get_input_socket(shader_node, "IOR")
            ior_val = _extract_constant_scalar(ior_sock, 1.4) if ior_sock is not None else 1.4
            res.channels["ior"] = {"value": ior_val if ior_val is not None else 1.4}
            scale_sock = get_input_socket(shader_node, "Scale")
            sss_scale = _extract_constant_scalar(scale_sock, 0.05) if scale_sock is not None else 0.05
            sss_entry: Dict[str, Any] = {"value": 1.0}
            if sss_scale is not None:
                sss_entry["scale"] = sss_scale
            res.channels["subsurface"] = sss_entry
        elif stype == "BSDF_TRANSPARENT":
            res.channels["metallic"] = {"value": 0.0}
            res.channels["alpha"] = {"value": 0.0}
        else:
            res.channels["metallic"] = {"value": 0.0}

        nrm_sock = get_input_socket(shader_node, "Normal")
        if nrm_sock is not None:
            nrm_cls = _classify_normal_socket(nrm_sock, default_uv_map=default_uv_map)
            for w in nrm_cls.warnings:
                if w not in res.warnings:
                    res.warnings.append(w)
            if nrm_cls.decision == BAKE:
                res.sockets_to_bake.append("normal")
            elif nrm_cls.channel_data is not None:
                res.channels[nrm_cls.channel_name] = nrm_cls.channel_data
                if nrm_cls.uv is not None:
                    collected_uvs.append(nrm_cls.uv)
            for extra_k, extra_v in nrm_cls.extra_channels.items():
                res.channels[extra_k] = extra_v

    if displacement_socket is not None and resolve_upstream(displacement_socket) is not None:
        disp_cls = _classify_displacement_socket(displacement_socket, default_uv_map=default_uv_map)
        for w in disp_cls.warnings:
            if w not in res.warnings:
                res.warnings.append(w)
        if disp_cls.decision == BAKE:
            res.sockets_to_bake.append("displacement")
            res.bake_metadata["displacement"] = dict(
                disp_cls.bake_metadata or {"midlevel": 0.5, "scale": 0.05}
            )
        elif disp_cls.channel_data is not None:
            res.channels["displacement"] = disp_cls.channel_data

    uv_dict, uv_warn = select_dominant_uv(collected_uvs, default_uv_map=default_uv_map)
    res.uv = uv_dict
    if uv_warn and uv_warn not in res.warnings:
        res.warnings.append(uv_warn)
    res.blend_method = resolve_blend_method(
        res.channels,
        res.sockets_to_bake,
        material_blend_method=material_blend_method,
    )
    return res
