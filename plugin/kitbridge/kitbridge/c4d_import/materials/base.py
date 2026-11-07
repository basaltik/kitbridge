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

"""Renderer-agnostic material plan builder (Milestone 2b).

Translates each ``kitbridge.json`` material entry into a :class:`MaterialPlan`
consisting of channel operations (constant values or texture lookups with
channel splitting, inversion, and UV transforms) and deduplicated texture nodes.

This module MUST remain free of ``c4d`` and ``maxon`` imports so it can be
unit-tested under plain CPython.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from kitbridge.core.pipeline import UDIM_TOKEN, udim_tiles

# Custom BaseContainer IDs used to tag Kitbridge-created materials and HDR domes
# inside a Cinema 4D document for deduplication and replacement.
KITBRIDGE_MAT_BC_ID = 1059981
KITBRIDGE_MAT_RENDERER_BC_ID = 1059982
KITBRIDGE_HDR_BC_ID = 1059983
KITBRIDGE_HDR_KEY_BC_ID = 1059984

METERS_TO_CM = 100.0

COLOR_CHANNELS = ("base_color", "emission")
SCALAR_CHANNELS = (
    "metallic",
    "roughness",
    "specular",
    "ior",
    "alpha",
    "transmission",
    "coat",
    "coat_roughness",
    "sheen",
    "subsurface",
    "translucency",
)
RELIEF_CHANNELS = ("normal", "bump", "displacement")
ALL_PLAN_CHANNELS = (
    "base_color",
    "metallic",
    "roughness",
    "specular",
    "ior",
    "emission",
    "alpha",
    "transmission",
    "coat",
    "coat_roughness",
    "sheen",
    "subsurface",
    "translucency",
    "normal",
    "bump",
    "displacement",
)


@dataclass(frozen=True)
class UVTransformPlan:
    """Resolved 2D UV transform for a material or texture."""

    map_name: str = "UVMap"
    scale: Tuple[float, float] = (1.0, 1.0)
    offset: Tuple[float, float] = (0.0, 0.0)
    rotation: float = 0.0

    @property
    def is_identity(self) -> bool:
        return (
            abs(self.scale[0] - 1.0) <= 1e-6
            and abs(self.scale[1] - 1.0) <= 1e-6
            and abs(self.offset[0]) <= 1e-6
            and abs(self.offset[1]) <= 1e-6
            and abs(self.rotation) <= 1e-6
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "map": self.map_name,
            "scale": [self.scale[0], self.scale[1]],
            "offset": [self.offset[0], self.offset[1]],
            "rotation": self.rotation,
        }


@dataclass(frozen=True)
class TextureOp:
    """Renderer-agnostic texture operation for a single material channel."""

    path: str
    rel_path: str
    colorspace: str  # "srgb" | "raw"
    channel: str = "rgb"  # "rgb" | "r" | "g" | "b" | "a"
    invert: bool = False
    uv: UVTransformPlan = field(default_factory=UVTransformPlan)
    texture_key: str = ""
    # Extra channel-specific parameters:
    strength: float = 1.0
    flip_green: bool = False
    distance: float = 0.01  # original meters from Blender Bump node
    distance_cm: float = 1.0  # converted to C4D centimeters
    midlevel: float = 0.5
    scale: float = 5.0  # converted to C4D centimeters (meters * 100 * meters_per_unit)
    scale_meters: float = 0.05  # original meters from manifest

    @property
    def is_single_channel(self) -> bool:
        return self.channel in ("r", "g", "b", "a")


@dataclass
class ChannelOp:
    """Operation for one material channel: either a constant value or a texture."""

    channel_name: str
    kind: str  # "value" | "texture"
    value: Optional[Union[float, Tuple[float, float, float]]] = None
    texture: Optional[TextureOp] = None
    strength: float = 1.0

    @property
    def is_texture(self) -> bool:
        return self.kind == "texture" and self.texture is not None

    @property
    def is_value(self) -> bool:
        return self.kind == "value" and self.value is not None


@dataclass
class SharedTextureSpec:
    """Deduplicated texture file node shared by one or more channel ops (e.g. packed ORM)."""

    key: str
    path: str
    rel_path: str
    colorspace: str
    uv: UVTransformPlan
    used_by_channels: List[str] = field(default_factory=list)
    channels_needed: List[str] = field(default_factory=list)

    @property
    def needs_splitter(self) -> bool:
        return any(ch in ("r", "g", "b", "a") for ch in self.channels_needed)


@dataclass
class MaterialPlan:
    """Complete renderer-agnostic plan for constructing a C4D material."""

    name: str
    dedupe_key: str
    blend_method: str = "opaque"
    uv: UVTransformPlan = field(default_factory=UVTransformPlan)
    ops: Dict[str, ChannelOp] = field(default_factory=dict)
    shared_textures: List[SharedTextureSpec] = field(default_factory=list)
    reflection_weight: float = 1.0
    ior: float = 1.5
    emission_strength: float = 0.0
    has_transmission: bool = False
    requires_alpha: bool = False
    is_non_opaque: bool = False
    has_displacement: bool = False  # move geometry (Cycles method DISPLACEMENT or BOTH)
    displacement_as_bump: bool = False  # shade the height as bump (Cycles method BUMP or BOTH)
    baked_channels: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def texture_channels(self) -> List[str]:
        return [name for name, op in self.ops.items() if op.is_texture]

    @property
    def value_channels(self) -> List[str]:
        return [name for name, op in self.ops.items() if op.is_value]


# ── Pure-Python Helpers ──────────────────────────────────────────────────────


def make_material_dedupe_key(base_id: str, material_name: str) -> str:
    """Build the hidden tag/container key used to deduplicate materials on re-import."""
    clean_base = (base_id or "unknown").strip()
    clean_name = (material_name or "Material").strip()
    return f"kitbridge:{clean_base}:{clean_name}"


def unique_material_name(desired_name: str, existing_names: Iterable[str]) -> str:
    """Return *desired_name* if unused in *existing_names*, else append ``_1``, ``_2``, etc."""
    base = (desired_name or "Material").strip() or "Material"
    taken = set(existing_names)
    if base not in taken:
        return base
    idx = 1
    while True:
        candidate = f"{base}_{idx}"
        if candidate not in taken:
            return candidate
        idx += 1


def parse_uv_transform(uv_dict: Optional[Dict[str, Any]]) -> UVTransformPlan:
    """Parse a manifest ``uv`` block into a :class:`UVTransformPlan`."""
    if not isinstance(uv_dict, dict):
        return UVTransformPlan()
    map_name = str(uv_dict.get("map") or "UVMap").strip() or "UVMap"
    raw_scale = uv_dict.get("scale")
    if isinstance(raw_scale, (list, tuple)) and len(raw_scale) >= 2:
        scale = (float(raw_scale[0]), float(raw_scale[1]))
    else:
        scale = (1.0, 1.0)

    raw_offset = uv_dict.get("offset")
    if isinstance(raw_offset, (list, tuple)) and len(raw_offset) >= 2:
        offset = (float(raw_offset[0]), float(raw_offset[1]))
    else:
        offset = (0.0, 0.0)

    raw_rot = uv_dict.get("rotation", 0.0)
    rotation = float(raw_rot) if isinstance(raw_rot, (int, float)) else 0.0
    return UVTransformPlan(map_name=map_name, scale=scale, offset=offset, rotation=rotation)


def resolve_texture_path(package_dir: str, rel_path: str) -> str:
    """Resolve a manifest-relative texture path to an absolute OS path."""
    cleaned = (rel_path or "").replace("\\", "/").lstrip("/")
    if not cleaned:
        return ""
    if os.path.isabs(rel_path):
        return os.path.normpath(rel_path)
    parts = [p for p in cleaned.split("/") if p]
    return os.path.normpath(os.path.abspath(os.path.join(package_dir, *parts)))


# Linear Rec.709 (what Blender stores) to the render spaces Cinema 4D offers. Every row sums to 1,
# so greys, weights and multipliers written through the same path stay unchanged.
_REC709_TO_RENDER_SPACE = {
    "ACEScg": ((0.6131, 0.3395, 0.0474), (0.0702, 0.9164, 0.0134), (0.0206, 0.1096, 0.8698)),
    "ACES2065-1": ((0.4397, 0.3830, 0.1773), (0.0898, 0.8134, 0.0968), (0.0175, 0.1115, 0.8710)),
    "scene-linear DCI-P3 D65": ((0.8225, 0.1774, 0.0001), (0.0332, 0.9669, -0.0001), (0.0171, 0.0724, 0.9105)),
    "scene-linear Rec.2020": ((0.6274, 0.3293, 0.0433), (0.0691, 0.9195, 0.0114), (0.0164, 0.0880, 0.8956)),
}


def rec709_to_render_space(value: Any, render_space: str) -> Any:
    """Convert a literal RGB colour to *render_space*; anything else passes through."""
    matrix = _REC709_TO_RENDER_SPACE.get(render_space or "")
    if matrix is None or not isinstance(value, (list, tuple)) or len(value) != 3:
        return value
    r, g, b = (float(v) for v in value)
    return tuple(row[0] * r + row[1] * g + row[2] * b for row in matrix)


def transmission_tint(value: Any) -> Any:
    """Per-surface glass tint: Blender takes the square root, so entering and leaving give the base colour once."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return value
    return tuple(max(0.0, float(c)) ** 0.5 for c in value)


def ior_for_specular_level(ior: float, level: float) -> float:
    """IOR whose reflectance equals Blender's Principled ``ior`` at ``Specular IOR Level`` *level*.

    Blender multiplies F0 by ``2 * level``, so 0.5 keeps the IOR unchanged.
    """
    f0 = ((ior - 1.0) / (ior + 1.0)) ** 2 * 2.0 * max(0.0, level)
    root = min(f0, 0.95) ** 0.5
    return max(1.0001, (1.0 + root) / (1.0 - root))


def _coerce_rgb(val: Any, default: Tuple[float, float, float] = (0.8, 0.8, 0.8)) -> Tuple[float, float, float]:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
        return (f, f, f)
    if isinstance(val, (list, tuple)) and len(val) >= 3:
        return (float(val[0]), float(val[1]), float(val[2]))
    return default


def _coerce_scalar(val: Any, default: float = 0.0) -> float:
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return float(val)
    if isinstance(val, (list, tuple)) and len(val) > 0:
        first = val[0]
        if isinstance(first, (int, float)) and not isinstance(first, bool):
            return float(first)
    return default


def _make_texture_key(abs_path: str, colorspace: str, uv: UVTransformPlan) -> str:
    norm_path = os.path.normcase(os.path.normpath(abs_path))
    return (
        f"{norm_path}|{colorspace}|"
        f"{uv.scale[0]:.6f},{uv.scale[1]:.6f}|"
        f"{uv.offset[0]:.6f},{uv.offset[1]:.6f}|"
        f"{uv.rotation:.6f}"
    )


def build_material_plan(
    mat_dict: Dict[str, Any],
    package_dir: str,
    *,
    base_id: str = "",
    meters_per_unit: float = 1.0,
) -> MaterialPlan:
    """Translate a single manifest material dictionary into a :class:`MaterialPlan`."""
    raw_name = str(mat_dict.get("name") or "Material")
    mat_name = raw_name.strip() or "Material"
    blend_method = str(mat_dict.get("blend_method") or "opaque").strip().lower()
    if blend_method not in ("opaque", "clip", "blend"):
        blend_method = "opaque"

    uv_plan = parse_uv_transform(mat_dict.get("uv"))
    dedupe_key = make_material_dedupe_key(base_id, raw_name)
    warnings: List[str] = [str(w) for w in (mat_dict.get("warnings") or []) if w]
    baked_channels: List[str] = [str(c) for c in (mat_dict.get("baked_channels") or []) if c]

    channels_raw = mat_dict.get("channels")
    channels: Dict[str, Any] = channels_raw if isinstance(channels_raw, dict) else {}

    mpu = float(meters_per_unit) if meters_per_unit and float(meters_per_unit) > 0 else 1.0
    cm_per_unit = METERS_TO_CM * mpu

    # Pre-scan texture colorspaces so an alpha channel pulling from the same file as an
    # sRGB base_color texture (with no explicit colorspace on alpha) shares the sRGB node.
    file_srgb_paths: Dict[str, str] = {}
    for ch_key, ch_entry in channels.items():
        if not isinstance(ch_entry, dict) or "texture" not in ch_entry:
            continue
        rel_t = str(ch_entry.get("texture") or "").strip()
        if not rel_t:
            continue
        abs_t = resolve_texture_path(package_dir, rel_t)
        norm_t = os.path.normcase(os.path.normpath(abs_t))
        cs = str(ch_entry.get("colorspace") or "").strip().lower()
        if cs == "srgb" or (not cs and ch_key in COLOR_CHANNELS):
            file_srgb_paths[norm_t] = "srgb"

    ops: Dict[str, ChannelOp] = {}
    shared_by_key: Dict[str, SharedTextureSpec] = {}
    shared_list: List[SharedTextureSpec] = []

    def _register_texture_op(
        ch_name: str,
        entry: Dict[str, Any],
        *,
        default_cs: str,
        default_channel: str = "rgb",
    ) -> Optional[TextureOp]:
        rel_tex = str(entry.get("texture") or "").strip()
        if not rel_tex:
            warnings.append(f"Material '{mat_name}': channel '{ch_name}' has empty texture path.")
            return None
        abs_tex = resolve_texture_path(package_dir, rel_tex)
        if UDIM_TOKEN in rel_tex:
            # Tiled texture: the path keeps the token for the renderer; it only has to exist as tiles.
            if not udim_tiles(abs_tex):
                warnings.append(f"Material '{mat_name}': no UDIM tiles on disk for '{ch_name}': {rel_tex}")
                return None
        elif not os.path.isfile(abs_tex):
            warnings.append(
                f"Material '{mat_name}': missing texture file on disk for '{ch_name}': {rel_tex}"
            )
            return None
        norm_tex = os.path.normcase(os.path.normpath(abs_tex))

        raw_cs = str(entry.get("colorspace") or "").strip().lower()
        if raw_cs in ("srgb", "raw"):
            cs = raw_cs
        elif ch_name == "alpha" and norm_tex in file_srgb_paths:
            # Share the sRGB texture node when alpha reads the alpha channel of the albedo map.
            cs = file_srgb_paths[norm_tex]
        else:
            cs = default_cs

        raw_ch = str(entry.get("channel") or default_channel).strip().lower()
        if raw_ch not in ("rgb", "r", "g", "b", "a"):
            raw_ch = default_channel

        invert = bool(entry.get("invert", False))
        strength = _coerce_scalar(entry.get("strength"), 1.0)
        flip_green = bool(entry.get("flip_green", False))
        distance_m = _coerce_scalar(entry.get("distance"), 0.01)
        distance_cm = round(distance_m * cm_per_unit, 6)
        midlevel = _coerce_scalar(entry.get("midlevel"), 0.5)
        scale_m = _coerce_scalar(entry.get("scale"), 0.05)
        scale_cm = round(scale_m * cm_per_unit, 6)

        # A baked texture already contains the original Mapping (tiling, offset, rotation):
        # applying the material's UV transform again would tile it twice (Bosch steel: 4x4 -> 16x).
        tex_uv = UVTransformPlan(map_name=uv_plan.map_name) if ch_name in baked_channels else uv_plan

        tex_key = _make_texture_key(abs_tex, cs, tex_uv)
        spec = shared_by_key.get(tex_key)
        if spec is None:
            spec = SharedTextureSpec(
                key=tex_key,
                path=abs_tex,
                rel_path=rel_tex.replace("\\", "/"),
                colorspace=cs,
                uv=tex_uv,
            )
            shared_by_key[tex_key] = spec
            shared_list.append(spec)

        if ch_name not in spec.used_by_channels:
            spec.used_by_channels.append(ch_name)
        if raw_ch not in spec.channels_needed:
            spec.channels_needed.append(raw_ch)

        return TextureOp(
            path=abs_tex,
            rel_path=rel_tex.replace("\\", "/"),
            colorspace=cs,
            channel=raw_ch,
            invert=invert,
            uv=tex_uv,
            texture_key=tex_key,
            strength=strength,
            flip_green=flip_green,
            distance=distance_m,
            distance_cm=distance_cm,
            midlevel=midlevel,
            scale=scale_cm,
            scale_meters=scale_m,
        )

    # 1. Ambient occlusion -> ignore with a warning (both renderers compute AO)
    if "ambient_occlusion" in channels or "ao" in channels:
        warnings.append(
            f"Material '{mat_name}': ambient_occlusion channel ignored (renderer computes AO natively)."
        )

    # 2. Specular (Blender IOR level, default 0.5) -> keep reflection_weight = 1.0 and use ior
    reflection_weight = 1.0
    ior_val = 1.5
    if "ior" in channels and isinstance(channels["ior"], dict):
        ior_entry = channels["ior"]
        if "texture" in ior_entry:
            tex_op = _register_texture_op("ior", ior_entry, default_cs="raw", default_channel="r")
            if tex_op is not None:
                ops["ior"] = ChannelOp(channel_name="ior", kind="texture", texture=tex_op)
            else:
                ops["ior"] = ChannelOp(channel_name="ior", kind="value", value=ior_val)
        elif "value" in ior_entry:
            ior_val = _coerce_scalar(ior_entry["value"], 1.5)
            ops["ior"] = ChannelOp(channel_name="ior", kind="value", value=ior_val)
    else:
        ops["ior"] = ChannelOp(channel_name="ior", kind="value", value=ior_val)

    # Blender's Specular IOR Level only scales reflectance. Glass keeps its IOR: it also bends light.
    spec_entry = channels.get("specular")
    trans_entry = channels.get("transmission")
    is_glass = isinstance(trans_entry, dict) and ("texture" in trans_entry or _coerce_scalar(trans_entry.get("value"), 0.0) > 0.0)
    if isinstance(spec_entry, dict) and "value" in spec_entry and ops["ior"].kind == "value" and not is_glass:
        level = _coerce_scalar(spec_entry["value"], 0.5)
        if level <= 0.0:
            reflection_weight = 0.0
        else:
            ior_val = ior_for_specular_level(ior_val, level)
            ops["ior"] = ChannelOp(channel_name="ior", kind="value", value=ior_val)

    # 3. Color channels: base_color, emission
    if "base_color" in channels and isinstance(channels["base_color"], dict):
        bc_entry = channels["base_color"]
        if "texture" in bc_entry:
            tex_op = _register_texture_op("base_color", bc_entry, default_cs="srgb", default_channel="rgb")
            if tex_op is not None:
                ops["base_color"] = ChannelOp(channel_name="base_color", kind="texture", texture=tex_op)
            else:
                ops["base_color"] = ChannelOp(channel_name="base_color", kind="value", value=(0.8, 0.8, 0.8))
        elif "value" in bc_entry:
            rgb = _coerce_rgb(bc_entry["value"], (0.8, 0.8, 0.8))
            ops["base_color"] = ChannelOp(channel_name="base_color", kind="value", value=rgb)
    else:
        ops["base_color"] = ChannelOp(channel_name="base_color", kind="value", value=(0.8, 0.8, 0.8))

    emission_strength = 0.0
    if "emission" in channels and isinstance(channels["emission"], dict):
        em_entry = channels["emission"]
        em_str = _coerce_scalar(em_entry.get("strength"), 1.0)
        if "texture" in em_entry and em_str > 0.0:
            tex_op = _register_texture_op("emission", em_entry, default_cs="srgb", default_channel="rgb")
            if tex_op is not None:
                emission_strength = em_str
                ops["emission"] = ChannelOp(
                    channel_name="emission",
                    kind="texture",
                    texture=tex_op,
                    strength=em_str,
                )
        elif "value" in em_entry and em_str > 0.0:
            em_rgb = _coerce_rgb(em_entry["value"], (0.0, 0.0, 0.0))
            if any(c > 1e-6 for c in em_rgb):
                emission_strength = em_str
                ops["emission"] = ChannelOp(
                    channel_name="emission",
                    kind="value",
                    value=em_rgb,
                    strength=em_str,
                )

    # 4. Standard scalar channels
    scalar_defaults: Dict[str, float] = {
        "metallic": 0.0,
        "roughness": 0.5,
        "alpha": 1.0,
        "transmission": 0.0,
        "coat": 0.0,
        "coat_roughness": 0.03,
        "sheen": 0.0,
        "subsurface": 0.0,
        "translucency": 0.0,
    }
    for ch_name, def_val in scalar_defaults.items():
        if ch_name not in channels or not isinstance(channels[ch_name], dict):
            continue
        entry = channels[ch_name]
        def_chan = "a" if ch_name == "alpha" else "r"
        if "texture" in entry:
            tex_op = _register_texture_op(ch_name, entry, default_cs="raw", default_channel=def_chan)
            if tex_op is not None:
                ops[ch_name] = ChannelOp(channel_name=ch_name, kind="texture", texture=tex_op)
            else:
                ops[ch_name] = ChannelOp(channel_name=ch_name, kind="value", value=def_val)
        elif "value" in entry:
            s_val = _coerce_scalar(entry["value"], def_val)
            ops[ch_name] = ChannelOp(channel_name=ch_name, kind="value", value=s_val)

    # 5. Relief channels: normal, bump, displacement
    if "normal" in channels and isinstance(channels["normal"], dict):
        nrm_entry = channels["normal"]
        if "texture" in nrm_entry:
            tex_op = _register_texture_op("normal", nrm_entry, default_cs="raw", default_channel="rgb")
            if tex_op is not None:
                ops["normal"] = ChannelOp(
                    channel_name="normal",
                    kind="texture",
                    texture=tex_op,
                    strength=tex_op.strength,
                )

    if "bump" in channels and isinstance(channels["bump"], dict):
        bump_entry = channels["bump"]
        if "texture" in bump_entry:
            tex_op = _register_texture_op("bump", bump_entry, default_cs="raw", default_channel="r")
            if tex_op is not None:
                ops["bump"] = ChannelOp(
                    channel_name="bump",
                    kind="texture",
                    texture=tex_op,
                    strength=tex_op.strength,
                )

    has_displacement = False
    displacement_as_bump = False
    if "displacement" in channels and isinstance(channels["displacement"], dict):
        disp_entry = channels["displacement"]
        # Packages before exporter 7 have no method: keep their old behaviour (true displacement).
        disp_method = str(disp_entry.get("method") or "DISPLACEMENT").upper()
        if "texture" in disp_entry:
            tex_op = _register_texture_op(
                "displacement",
                disp_entry,
                default_cs="raw",
                default_channel="r",
            )
            if tex_op is not None:
                has_displacement = disp_method in ("DISPLACEMENT", "BOTH")
                displacement_as_bump = disp_method in ("BUMP", "BOTH")
                ops["displacement"] = ChannelOp(
                    channel_name="displacement",
                    kind="texture",
                    texture=tex_op,
                    strength=tex_op.scale,
                )

    # 6. Derive transmission & opacity flags
    has_transmission = False
    trans_op = ops.get("transmission")
    if trans_op is not None:
        if trans_op.is_texture:
            has_transmission = True
        elif isinstance(trans_op.value, (int, float)) and float(trans_op.value) > 1e-4:
            has_transmission = True

    requires_alpha = False
    alpha_op = ops.get("alpha")
    if alpha_op is not None:
        if alpha_op.is_texture:
            requires_alpha = True
        elif isinstance(alpha_op.value, (int, float)) and float(alpha_op.value) < 0.999:
            requires_alpha = True
    elif blend_method in ("blend", "clip") and not has_transmission:
        requires_alpha = True

    is_non_opaque = requires_alpha or has_transmission

    return MaterialPlan(
        name=raw_name,
        dedupe_key=dedupe_key,
        blend_method=blend_method,
        uv=uv_plan,
        ops=ops,
        shared_textures=shared_list,
        reflection_weight=reflection_weight,
        ior=ior_val,
        emission_strength=emission_strength,
        has_transmission=has_transmission,
        requires_alpha=requires_alpha,
        is_non_opaque=is_non_opaque,
        has_displacement=has_displacement,
        displacement_as_bump=displacement_as_bump,
        baked_channels=baked_channels,
        warnings=warnings,
    )


def build_package_material_plans(
    manifest: Dict[str, Any],
    package_dir: str,
) -> List[MaterialPlan]:
    """Build :class:`MaterialPlan`s for all materials in a ``kitbridge.json`` manifest."""
    asset = manifest.get("asset") if isinstance(manifest.get("asset"), dict) else {}
    base_id = str(asset.get("base_id") or asset.get("id") or "")
    units = manifest.get("units") if isinstance(manifest.get("units"), dict) else {}
    raw_mpu = units.get("meters_per_unit", 1.0)
    mpu = float(raw_mpu) if isinstance(raw_mpu, (int, float)) and float(raw_mpu) > 0 else 1.0

    materials = manifest.get("materials")
    if not isinstance(materials, list):
        return []

    plans: List[MaterialPlan] = []
    for mat_dict in materials:
        if isinstance(mat_dict, dict):
            plans.append(
                build_material_plan(
                    mat_dict,
                    package_dir,
                    base_id=base_id,
                    meters_per_unit=mpu,
                )
            )
    return plans
