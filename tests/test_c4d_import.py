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

"""Pure-Python unit tests for ``kitbridge.c4d_import.materials.base`` (Milestone 2b)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kitbridge.c4d_import.materials import base

CACHE_ROOT = Path(r"C:\Users\user\blenderkit_data\kitbridge_cache")


def test_base_module_is_free_of_c4d_imports() -> None:
    """``materials/base.py`` must not import ``c4d`` or ``maxon``."""
    assert "c4d" not in base.__dict__
    assert "maxon" not in base.__dict__
    assert "c4d" not in sys.modules


def test_uv_transform_identity_and_non_identity() -> None:
    uv_default = base.parse_uv_transform(None)
    assert uv_default.is_identity is True
    assert uv_default.scale == (1.0, 1.0)
    assert uv_default.offset == (0.0, 0.0)
    assert uv_default.rotation == 0.0

    uv_explicit_id = base.parse_uv_transform(
        {"scale": [1.0, 1.0], "offset": [0.0, 0.0], "rotation": 0.0}
    )
    assert uv_explicit_id.is_identity is True

    uv_scaled = base.parse_uv_transform(
        {"scale": [10.0, 10.0], "offset": [0.0, 0.0], "rotation": 0.0}
    )
    assert uv_scaled.is_identity is False
    assert uv_scaled.scale == (10.0, 10.0)

    uv_rotated = base.parse_uv_transform(
        {"scale": [55.0, 55.0], "offset": [0.0, 0.0], "rotation": -125.9}
    )
    assert uv_rotated.is_identity is False
    assert uv_rotated.scale == (55.0, 55.0)
    assert pytest.approx(uv_rotated.rotation) == -125.9


def test_packed_orm_texture_deduplication_and_splitting(tmp_path: Path) -> None:
    tex_dir = tmp_path / "textures"
    tex_dir.mkdir()
    orm_file = tex_dir / "orm.png"
    orm_file.write_bytes(b"fake_png")
    albedo_file = tex_dir / "albedo.png"
    albedo_file.write_bytes(b"fake_png")

    mat_dict = {
        "name": "PackedMetal",
        "blend_method": "opaque",
        "channels": {
            "base_color": {
                "texture": "textures/albedo.png",
                "colorspace": "srgb",
                "channel": "rgb",
            },
            "ambient_occlusion": {
                "texture": "textures/orm.png",
                "colorspace": "raw",
                "channel": "r",
            },
            "roughness": {
                "texture": "textures/orm.png",
                "colorspace": "raw",
                "channel": "g",
                "invert": True,
            },
            "metallic": {
                "texture": "textures/orm.png",
                "colorspace": "raw",
                "channel": "b",
            },
        },
    }

    plan = base.build_material_plan(
        mat_dict,
        str(tmp_path),
        base_id="asset-123",
    )

    assert plan.dedupe_key == "kitbridge:asset-123:PackedMetal"
    # 2 unique texture files: albedo.png and orm.png (shared by roughness & metallic)
    assert len(plan.shared_textures) == 2
    shared_by_key = {s.key: s for s in plan.shared_textures}

    rough_tex = plan.ops["roughness"].texture
    metal_tex = plan.ops["metallic"].texture
    assert rough_tex is not None
    assert metal_tex is not None
    assert rough_tex.texture_key == metal_tex.texture_key
    assert rough_tex.channel == "g"
    assert rough_tex.invert is True
    assert metal_tex.channel == "b"

    orm_spec = shared_by_key[rough_tex.texture_key]
    assert orm_spec.needs_splitter is True
    assert set(orm_spec.channels_needed) == {"g", "b"}
    assert set(orm_spec.used_by_channels) == {"roughness", "metallic"}
    assert orm_spec.colorspace == "raw"

    albedo_tex = plan.ops["base_color"].texture
    assert albedo_tex is not None
    albedo_spec = shared_by_key[albedo_tex.texture_key]
    assert albedo_spec.needs_splitter is False
    assert albedo_spec.colorspace == "srgb"

    # AO channel should record a warning and not be in active shader ops
    assert "ambient_occlusion" not in plan.ops
    assert any("ambient_occlusion" in w for w in plan.warnings)


def test_displacement_and_bump_cm_conversion_and_non_opaque(tmp_path: Path) -> None:
    tex_dir = tmp_path / "textures"
    tex_dir.mkdir()
    for fname in ("disp.exr", "bump.png", "norm.png", "alpha.png"):
        (tex_dir / fname).write_bytes(b"data")

    mat_dict = {
        "name": "DispMat",
        "blend_method": "blend",
        "channels": {
            "displacement": {
                "texture": "textures/disp.exr",
                "colorspace": "raw",
                "scale": 0.025,
                "midlevel": 0.5,
            },
            "bump": {
                "texture": "textures/bump.png",
                "colorspace": "raw",
                "strength": 0.3,
                "distance": 0.01,
            },
            "normal": {
                "texture": "textures/norm.png",
                "colorspace": "raw",
                "space": "tangent",
                "strength": 1.2,
                "flip_green": True,
            },
            "alpha": {
                "texture": "textures/alpha.png",
            },
            "specular": {
                "value": 0.5,
            },
        },
    }

    plan = base.build_material_plan(
        mat_dict,
        str(tmp_path),
        base_id="disp-id",
        meters_per_unit=1.0,
    )

    assert plan.has_displacement is True
    assert plan.requires_alpha is True
    assert plan.is_non_opaque is True

    disp_tex = plan.ops["displacement"].texture
    assert disp_tex is not None
    assert pytest.approx(disp_tex.scale) == 2.5  # 0.025 m * 100 cm/m
    assert pytest.approx(disp_tex.midlevel) == 0.5

    bump_tex = plan.ops["bump"].texture
    assert bump_tex is not None
    assert pytest.approx(bump_tex.strength) == 0.3
    assert pytest.approx(bump_tex.distance_cm) == 1.0  # 0.01 m * 100 cm/m

    norm_tex = plan.ops["normal"].texture
    assert norm_tex is not None
    assert norm_tex.flip_green is True
    assert pytest.approx(norm_tex.strength) == 1.2

    # Alpha without explicit colorspace defaults to raw
    alpha_tex = plan.ops["alpha"].texture
    assert alpha_tex is not None
    assert alpha_tex.colorspace == "raw"

    # Specular without explicit IOR keeps reflection_weight=1.0 and default IOR 1.5
    assert pytest.approx(plan.reflection_weight) == 1.0
    assert "ior" in plan.ops
    assert plan.ops["ior"].is_value is True
    assert pytest.approx(plan.ops["ior"].value) == 1.5


def test_missing_texture_falls_back_to_default_with_warning(tmp_path: Path) -> None:
    mat_dict = {
        "name": "MissingTexMat",
        "channels": {
            "base_color": {
                "texture": "textures/does_not_exist.png",
                "colorspace": "srgb",
            },
            "roughness": {
                "texture": "textures/missing_rough.png",
                "colorspace": "raw",
            },
        },
    }
    plan = base.build_material_plan(mat_dict, str(tmp_path), base_id="missing-id")
    assert plan.ops["base_color"].is_value is True
    assert plan.ops["base_color"].value == (0.8, 0.8, 0.8)
    assert plan.ops["roughness"].is_value is True
    assert pytest.approx(plan.ops["roughness"].value) == 0.5
    assert len(plan.shared_textures) == 0
    assert len(plan.warnings) == 2


def test_alpha_and_transmission_value_flags(tmp_path: Path) -> None:
    opaque_plan = base.build_material_plan(
        {
            "name": "Opaque",
            "channels": {
                "base_color": {"value": [0.8, 0.8, 0.8, 1.0]},
                "alpha": {"value": 1.0},
                "transmission": {"value": 0.0},
            },
        },
        str(tmp_path),
    )
    assert opaque_plan.requires_alpha is False
    assert opaque_plan.is_non_opaque is False

    glass_plan = base.build_material_plan(
        {
            "name": "Glass",
            "channels": {
                "base_color": {"value": [1.0, 1.0, 1.0, 1.0]},
                "transmission": {"value": 0.95},
            },
        },
        str(tmp_path),
    )
    assert glass_plan.requires_alpha is False
    assert glass_plan.has_transmission is True
    assert glass_plan.is_non_opaque is True

    semi_alpha_plan = base.build_material_plan(
        {
            "name": "SemiAlpha",
            "channels": {
                "alpha": {"value": 0.5},
            },
        },
        str(tmp_path),
    )
    assert semi_alpha_plan.requires_alpha is True
    assert semi_alpha_plan.is_non_opaque is True


def test_unique_material_name_generation() -> None:
    existing = ["Wood", "Wood_1", "Metal"]
    assert base.unique_material_name("Glass", existing) == "Glass"
    assert base.unique_material_name("Wood", existing) == "Wood_2"
    assert base.unique_material_name("Metal", existing) == "Metal_1"


@pytest.mark.skipif(not CACHE_ROOT.is_dir(), reason="Local kitbridge_cache directory not present")
def test_real_cached_manifests_plan_building() -> None:
    """Verify ``build_package_material_plans`` against all real cached packages on disk."""
    pkg_dirs = sorted(
        p for p in CACHE_ROOT.iterdir() if p.is_dir() and (p / "kitbridge.json").is_file()
    )
    assert len(pkg_dirs) >= 4

    by_id = {}
    for pkg_dir in pkg_dirs:
        manifest = json.loads((pkg_dir / "kitbridge.json").read_text(encoding="utf-8"))
        asset_id = manifest["asset"]["base_id"]
        plans = base.build_package_material_plans(manifest, str(pkg_dir))
        by_id[asset_id] = (manifest, plans, pkg_dir)

    # 1. Sunset HDR (2f62aff5-3a19-43e1-9c6b-f52a898bf676): 0 materials
    hdr_manifest, hdr_plans, _ = by_id["2f62aff5-3a19-43e1-9c6b-f52a898bf676"]
    assert hdr_manifest["asset"]["type"] == "hdr"
    assert hdr_plans == []

    # 2. Procedural Wood material (4087f4db-87a1-4f27-8035-40d0877b3d6c): 1 baked material
    wood_manifest, wood_plans, _ = by_id["4087f4db-87a1-4f27-8035-40d0877b3d6c"]
    assert wood_manifest["asset"]["type"] == "material"
    assert len(wood_plans) == 1
    wood_plan = wood_plans[0]
    assert wood_plan.name == "Procedural Wood"
    for ch in ("base_color", "roughness", "normal"):
        assert wood_plan.ops[ch].is_texture is True
        assert os.path.isfile(wood_plan.ops[ch].texture.path)
    assert not any("missing" in w.lower() for w in wood_plan.warnings)

    # 3. Dining table and chairs set (7bea2b93-30ac-4ee2-8414-c5e1f712911e): 2 materials
    chair_manifest, chair_plans, _ = by_id["7bea2b93-30ac-4ee2-8414-c5e1f712911e"]
    assert len(chair_plans) == 2
    chair_by_name = {p.name: p for p in chair_plans}
    fabric_plan = chair_by_name["mtl_fabric_beige"]
    assert fabric_plan.ops["sheen"].is_value is True
    assert fabric_plan.ops["subsurface"].is_value is True
    # base_color is baked, so its 10x tiling is already in the image; direct maps keep the Mapping.
    assert "base_color" in fabric_plan.baked_channels
    assert fabric_plan.ops["base_color"].texture.uv.scale == (1.0, 1.0)
    assert fabric_plan.ops["roughness"].texture.uv.scale == (10.0, 10.0)
    assert fabric_plan.ops["normal"].texture.path.endswith(".exr")
    assert os.path.isfile(fabric_plan.ops["normal"].texture.path)

    # 4. Ferrari SF90 (5d319c50-df55-4c88-94e5-0a9e9f5a3a56): 35 materials
    car_manifest, car_plans, _ = by_id["5d319c50-df55-4c88-94e5-0a9e9f5a3a56"]
    assert len(car_plans) == len(car_manifest["materials"]) == 35
    car_by_name = {p.name: p for p in car_plans}

    # Air_grill has base_color, alpha, bump, normal with non-trivial UV transform
    air_grill = car_by_name["Air_grill"]
    assert air_grill.requires_alpha is True
    assert air_grill.is_non_opaque is True
    assert air_grill.ops["alpha"].is_texture is True
    assert air_grill.ops["bump"].is_texture is True
    assert air_grill.ops["normal"].is_texture is True
    assert air_grill.ops["alpha"].texture.uv.is_identity is False

    # Glass has alpha = 0.5 value
    glass = car_by_name["Glass"]
    assert glass.requires_alpha is True
    assert glass.is_non_opaque is True
    assert glass.ops["alpha"].is_value is True
    assert pytest.approx(glass.ops["alpha"].value) == 0.5

    # "Roll " has trailing space preserved in manifest name
    assert "Roll " in car_by_name
    roll = car_by_name["Roll "]
    assert roll.ops["normal"].is_texture is True
    assert pytest.approx(roll.ops["normal"].texture.strength) == -0.5

def test_fbx_material_names_match_despite_rewritten_punctuation():
    from kitbridge.c4d_import.geometry import _match_kitbridge_material

    materials = {"Window.glass": "glass", "Button.decals_2": "decal2", "Button.decals": "decal", "Body": "body"}
    assert _match_kitbridge_material("Window glass", materials) == ("Window.glass", "glass")
    assert _match_kitbridge_material("Button decals_2", materials) == ("Button.decals_2", "decal2")
    assert _match_kitbridge_material("Button decals", materials) == ("Button.decals", "decal")
    assert _match_kitbridge_material("Body", materials) == ("Body", "body")
    assert _match_kitbridge_material("Paint", materials) == (None, None)

def test_literal_colours_follow_the_render_space():
    from kitbridge.c4d_import.materials.base import rec709_to_render_space as convert

    red = convert((0.2588, 0.0, 0.0), "ACEScg")
    assert red == pytest.approx((0.1587, 0.0182, 0.0053), abs=1e-3)
    for space in ("ACEScg", "ACES2065-1", "scene-linear DCI-P3 D65", "scene-linear Rec.2020"):
        assert convert((0.5, 0.5, 0.5), space) == pytest.approx((0.5, 0.5, 0.5), abs=1e-3), space
    assert convert((0.2, 0.3, 0.4), "scene-linear Rec.709-sRGB") == (0.2, 0.3, 0.4)
    assert convert((0.2, 0.3, 0.4), "") == (0.2, 0.3, 0.4)
    assert convert(0.75, "ACEScg") == 0.75

def test_specular_level_becomes_an_equivalent_ior():
    from kitbridge.c4d_import.materials.base import ior_for_specular_level as to_ior

    assert to_ior(1.5, 0.5) == pytest.approx(1.5)
    reflectance = ((to_ior(1.5, 0.25) - 1) / (to_ior(1.5, 0.25) + 1)) ** 2
    assert reflectance == pytest.approx(0.02)


def _plan(channels):
    from kitbridge.c4d_import.materials.base import build_material_plan

    return build_material_plan({"name": "m", "channels": channels}, ".")


def test_plan_applies_specular_except_on_glass():
    assert _plan({"specular": {"value": 0.5}, "ior": {"value": 1.5}}).ior == pytest.approx(1.5)
    assert _plan({"specular": {"value": 0.25}, "ior": {"value": 1.5}}).ior < 1.4
    assert _plan({"specular": {"value": 0.0}}).reflection_weight == 0.0
    assert _plan({"specular": {"value": 0.25}, "ior": {"value": 1.5}, "transmission": {"value": 1.0}}).ior == pytest.approx(1.5)

def test_glass_tint_is_applied_once_over_two_surfaces():
    from kitbridge.c4d_import.materials.base import transmission_tint

    r, g, b = transmission_tint((0.81, 0.64, 0.0))
    assert (r * r, g * g, b * b) == pytest.approx((0.81, 0.64, 0.0))

def _box(sx, sy, sz):
    pts = [(x * sx, y * sy, z * sz) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    return pts, faces


def test_thickness_ratio_separates_solid_from_shell():
    from kitbridge.c4d_import.geometry import shell_thickness_ratio

    assert shell_thickness_ratio(*_box(10, 10, 10)) > 0.2       # a block of glass keeps refracting
    assert shell_thickness_ratio(*_box(30, 30, 0.2)) < 0.05     # a thin cover
    assert shell_thickness_ratio([], []) == 0.0

def test_translucency_reaches_the_plan():
    plan = _plan({"translucency": {"value": 0.5}})
    assert plan.ops["translucency"].value == pytest.approx(0.5)
    assert not plan.has_transmission

