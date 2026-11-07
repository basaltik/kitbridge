from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import pytest

from kitbridge.core import blender, paths, pipeline
from kitbridge.core.shader_graph import (
    blender_matrix_to_fbx,
    build_light_manifest_entry,
    GraphNode,
    classify_material_output,
    classify_principled_bsdf,
    classify_socket,
    udim_texture_template,
    udim_tile_filename,
)


def _sample_model_manifest() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "asset": {
            "id": "ver-chair-1",
            "base_id": "base-chair-1",
            "name": "Wooden Chair",
            "type": "model",
            "resolution": "1024",
            "author": "Jane Doe",
            "license": "royalty_free",
            "source_url": "https://www.blenderkit.com/asset-gallery-detail/base-chair-1/",
        },
        "units": {"meters_per_unit": 1.0, "up_axis": "Y"},
        "geometry": "geometry.fbx",
        "objects": [{"name": "ChairMesh", "materials": ["WoodMat"]}],
        "materials": [
            {
                "name": "WoodMat",
                "blend_method": "opaque",
                "channels": {
                    "base_color": {
                        "texture": "textures/wood_col.jpg",
                        "colorspace": "srgb",
                        "channel": "rgb",
                    },
                    "metallic": {
                        "texture": "textures/wood_orm.png",
                        "colorspace": "raw",
                        "channel": "b",
                    },
                    "roughness": {
                        "texture": "textures/wood_orm.png",
                        "colorspace": "raw",
                        "channel": "g",
                        "invert": False,
                    },
                    "specular": {"value": 0.5},
                    "ior": {"value": 1.5},
                    "normal": {
                        "texture": "textures/wood_nrm.png",
                        "colorspace": "raw",
                        "strength": 1.0,
                        "flip_green": False,
                    },
                },
                "uv": {
                    "map": "UVMap",
                    "scale": [1.0, 1.0],
                    "offset": [0.0, 0.0],
                    "rotation": 0.0,
                },
                "baked_channels": [],
                "warnings": [],
            }
        ],
    }


def _populate_package_files(pkg_dir: Path, manifest: Dict[str, Any]) -> None:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "textures").mkdir(parents=True, exist_ok=True)
    geom = manifest.get("geometry")
    if isinstance(geom, str) and geom:
        (pkg_dir / geom).write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"\x00" * 128)
    hdr = manifest.get("hdr")
    if isinstance(hdr, str) and hdr:
        hdr_path = pkg_dir.joinpath(*hdr.split("/"))
        hdr_path.parent.mkdir(parents=True, exist_ok=True)
        hdr_path.write_bytes(b"v/2f\x01" + b"\x00" * 64)
    for mat in manifest.get("materials") or []:
        for ch_entry in (mat.get("channels") or {}).values():
            if isinstance(ch_entry, dict) and "texture" in ch_entry:
                tex_path = pkg_dir.joinpath(*str(ch_entry["texture"]).split("/"))
                tex_path.parent.mkdir(parents=True, exist_ok=True)
                tex_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    (pkg_dir / "kitbridge.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


# ── 1. Manifest Schema Validation ────────────────────────────────────────────


def test_validate_manifest_valid_model_and_hdr(tmp_path: Path) -> None:
    model_manifest = _sample_model_manifest()
    assert pipeline.validate_manifest(model_manifest) == []
    assert pipeline.is_valid_manifest(model_manifest) is True

    pkg_dir = tmp_path / "base-chair-1_1024"
    _populate_package_files(pkg_dir, model_manifest)
    assert pipeline.validate_manifest(model_manifest, package_dir=str(pkg_dir)) == []

    hdr_manifest: Dict[str, Any] = {
        "schema_version": 1,
        "asset": {
            "id": "hdr-1",
            "base_id": "base-hdr-1",
            "name": "Sunset Sky",
            "type": "hdr",
            "resolution": "1024",
            "author": "Env Artist",
            "license": "cc_zero",
            "source_url": "https://www.blenderkit.com/asset-gallery-detail/base-hdr-1/",
        },
        "units": {"meters_per_unit": 1.0, "up_axis": "Y"},
        "geometry": None,
        "hdr": "textures/sunset_1k.exr",
        "objects": [],
        "materials": [],
    }
    hdr_dir = tmp_path / "base-hdr-1_1024"
    _populate_package_files(hdr_dir, hdr_manifest)
    assert pipeline.validate_manifest(hdr_manifest, package_dir=str(hdr_dir)) == []


def test_validate_manifest_catches_invalid_fields_and_missing_files(tmp_path: Path) -> None:
    bad = _sample_model_manifest()
    bad["schema_version"] = 2
    bad["units"]["up_axis"] = "Z"
    bad["materials"][0]["blend_method"] = "additive"
    bad["materials"][0]["channels"]["base_color"]["colorspace"] = "acescg"
    bad["materials"][0]["uv"]["scale"] = [1.0]

    errs = pipeline.validate_manifest(bad)
    assert any("schema_version" in e for e in errs)
    assert any("up_axis" in e for e in errs)
    assert any("blend_method" in e for e in errs)
    assert any("colorspace" in e for e in errs)
    assert any("uv.scale" in e for e in errs)
    assert pipeline.is_valid_manifest(bad) is False

    # Missing files on disk when package_dir is supplied
    valid_struct = _sample_model_manifest()
    empty_dir = tmp_path / "empty_pkg"
    empty_dir.mkdir(parents=True, exist_ok=True)
    disk_errs = pipeline.validate_manifest(valid_struct, package_dir=str(empty_dir))
    assert any("geometry: file not found" in e for e in disk_errs)
    assert any("wood_col.jpg" in e for e in disk_errs)


# ── 2. Cache-Hit Path & Cache Maintenance Helpers ────────────────────────────


def test_fetch_asset_cache_hit_does_not_call_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KITBRIDGE_GLOBAL_DIR", str(tmp_path))

    manifest = _sample_model_manifest()
    pkg_dir = Path(paths.asset_cache_dir("base-chair-1", "1024", create=True))
    _populate_package_files(pkg_dir, manifest)

    mock_client = mock.MagicMock()
    progress_events: List[Any] = []

    with mock.patch.object(blender, "find_blender_with_version") as mock_find_blender:
        pkg = pipeline.fetch_asset(
            mock_client,
            {
                "id": "ver-chair-1",
                "assetBaseId": "base-chair-1",
                "name": "Wooden Chair",
                "assetType": "model",
                "isFree": True,
            },
            resolution="1024",
            api_key="",
            on_progress=lambda stage, frac, msg: progress_events.append((stage, frac, msg)),
        )
        mock_find_blender.assert_not_called()

    assert pkg.is_cached is True
    assert os.path.normcase(pkg.path) == os.path.normcase(str(pkg_dir))
    assert pkg.manifest["asset"]["base_id"] == "base-chair-1"
    mock_client.ensure_running.assert_not_called()
    mock_client.asset_download.assert_not_called()
    mock_client.report.assert_not_called()
    assert progress_events and progress_events[0][0] == "cache"

    # Verify cache maintenance helpers
    cached_list = pipeline.list_cached()
    assert len(cached_list) == 1
    assert cached_list[0].manifest["asset"]["name"] == "Wooden Chair"
    assert pipeline.cache_size() > 0

    removed = pipeline.remove("base-chair-1")
    assert removed == 1
    assert pipeline.list_cached() == []
    assert pipeline.cache_size() == 0


# ── 3. KB_* Stdout Line Parser ───────────────────────────────────────────────


def test_parse_kb_line_protocol() -> None:
    p1 = pipeline.parse_kb_line("KB_PROGRESS 0.35 Unpacking textures/wood.png")
    assert p1 is not None
    assert p1.kind == "progress"
    assert p1.tag == "KB_PROGRESS"
    assert p1.fraction == pytest.approx(0.35)
    assert p1.message == "Unpacking textures/wood.png"

    # Clamping out-of-range floats
    p_low = pipeline.parse_kb_line("KB_PROGRESS -0.5 Starting")
    assert p_low is not None and p_low.fraction == 0.0
    p_high = pipeline.parse_kb_line("KB_PROGRESS 2.4 Done")
    assert p_high is not None and p_high.fraction == 1.0

    w1 = pipeline.parse_kb_line("KB_WARNING UDIM tiled image skipped: tiles.<UDIM>.png")
    assert w1 is not None
    assert w1.kind == "warning"
    assert w1.message == "UDIM tiled image skipped: tiles.<UDIM>.png"

    d1 = pipeline.parse_kb_line("KB_DONE E:/cache/base-1_1024/kitbridge.json")
    assert d1 is not None
    assert d1.kind == "done"
    assert d1.path == "E:/cache/base-1_1024/kitbridge.json"

    e1 = pipeline.parse_kb_line("KB_ERROR Failed to evaluate Geometry Nodes")
    assert e1 is not None
    assert e1.kind == "error"
    assert "Geometry Nodes" in e1.message

    assert pipeline.parse_kb_line("Fra:1 Mem:42.10M (Peak 50.00M) | Time:00:00.12") is None
    assert pipeline.parse_kb_line("") is None
    assert pipeline.parse_kb_line("KB_PROGRESS not_a_float msg") is None
    assert pipeline.parse_kb_line("KB_DONE   ") is None


# ── 4. Pure-Python Socket-Chain Classifier (No bpy) ──────────────────────────


def test_classifier_direct_patterns_and_uv_extraction() -> None:
    bsdf = GraphNode("BSDF_PRINCIPLED", name="Principled BSDF")
    bsdf.set_input("Base Color", [0.8, 0.8, 0.8, 1.0])
    bsdf.set_input("Metallic", 0.0)
    bsdf.set_input("Roughness", 0.5)
    bsdf.set_input("IOR", 1.5)
    bsdf.set_input("Alpha", 1.0)

    out_node = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out_node.connect("Surface", bsdf, "BSDF")

    # UV Map -> Mapping -> Image Texture (Base Color + Alpha)
    import math

    uv_node = GraphNode("UVMAP", name="UV Map", properties={"uv_map": "CustomUV"})
    mapping = GraphNode("MAPPING", name="Mapping")
    mapping.set_input("Location", [0.1, 0.2, 0.0])
    mapping.set_input("Rotation", [0.0, 0.0, math.radians(45.0)])
    mapping.set_input("Scale", [2.0, 3.0, 1.0])
    mapping.connect("Vector", uv_node, "UV")

    img_col = GraphNode(
        "TEX_IMAGE",
        name="ColTex",
        properties={"image": "textures/albedo.png"},
    )
    img_col.connect("Vector", mapping, "Vector")
    bsdf.connect("Base Color", img_col, "Color")
    bsdf.connect("Alpha", img_col, "Alpha")

    # Image -> Separate Color (Blue -> Metallic, Green -> Invert -> Roughness)
    img_orm = GraphNode(
        "TEX_IMAGE",
        name="OrmTex",
        properties={"image": "textures/orm.png"},
    )
    img_orm.connect("Vector", mapping, "Vector")
    sep = GraphNode("SEPARATE_COLOR", name="Separate Color")
    sep.connect("Color", img_orm, "Color")
    bsdf.connect("Metallic", sep, "Blue")

    inv = GraphNode("INVERT", name="Invert")
    inv.set_input("Fac", 1.0)
    inv.connect("Color", sep, "Green")
    bsdf.connect("Roughness", inv, "Color")

    # Image -> Normal Map -> Normal
    img_nrm = GraphNode(
        "TEX_IMAGE",
        name="NrmTex",
        properties={"image": "textures/normal.png"},
    )
    img_nrm.connect("Vector", mapping, "Vector")
    nrm_node = GraphNode("NORMAL_MAP", name="Normal Map")
    nrm_node.set_input("Strength", 0.75)
    nrm_node.connect("Color", img_nrm, "Color")
    bsdf.connect("Normal", nrm_node, "Normal")

    # Image -> Displacement -> Material Output Displacement
    img_disp = GraphNode(
        "TEX_IMAGE",
        name="DispTex",
        properties={"image": "textures/height.png"},
    )
    img_disp.connect("Vector", mapping, "Vector")
    disp_node = GraphNode("DISPLACEMENT", name="Displacement")
    disp_node.set_input("Midlevel", 0.5)
    disp_node.set_input("Scale", 0.02)
    disp_node.connect("Height", img_disp, "Color")
    out_node.connect("Displacement", disp_node, "Displacement")

    mat_cls = classify_material_output(out_node)
    assert mat_cls.sockets_to_bake == []

    assert mat_cls.channels["base_color"] == {
        "texture": "textures/albedo.png",
        "colorspace": "srgb",
        "channel": "rgb",
    }
    assert mat_cls.channels["alpha"] == {
        "texture": "textures/albedo.png",
        "channel": "a",
    }
    assert mat_cls.channels["metallic"] == {
        "texture": "textures/orm.png",
        "colorspace": "raw",
        "channel": "b",
    }
    assert mat_cls.channels["roughness"] == {
        "texture": "textures/orm.png",
        "colorspace": "raw",
        "channel": "g",
        "invert": True,
    }
    assert mat_cls.channels["normal"] == {
        "texture": "textures/normal.png",
        "colorspace": "raw",
        "strength": 0.75,
        "flip_green": False,
    }
    assert mat_cls.channels["displacement"] == {
        "texture": "textures/height.png",
        "colorspace": "raw",
        "midlevel": 0.5,
        "scale": 0.02,
    }
    assert mat_cls.uv == {
        "map": "CustomUV",
        "scale": [2.0, 3.0],
        "offset": [0.1, 0.2],
        "rotation": 45.0,
    }


def test_classifier_bump_and_multiple_uv_warning() -> None:
    bsdf = GraphNode("BSDF_PRINCIPLED", name="Principled BSDF")
    uv1 = GraphNode("UVMAP", name="UV1", properties={"uv_map": "UVMap"})
    uv2 = GraphNode("UVMAP", name="UV2", properties={"uv_map": "AltUV"})

    img1 = GraphNode("TEX_IMAGE", name="Img1", properties={"image": "textures/c.png"})
    img1.connect("Vector", uv1, "UV")
    bsdf.connect("Base Color", img1, "Color")

    img2 = GraphNode("TEX_IMAGE", name="Img2", properties={"image": "textures/r.png"})
    img2.connect("Vector", uv1, "UV")
    bsdf.connect("Roughness", img2, "Color")

    img_bump = GraphNode("TEX_IMAGE", name="ImgBump", properties={"image": "textures/b.png"})
    img_bump.connect("Vector", uv2, "UV")
    bump = GraphNode("BUMP", name="Bump")
    bump.set_input("Strength", 0.3)
    bump.set_input("Distance", 0.01)
    bump.connect("Height", img_bump, "Color")
    bsdf.connect("Normal", bump, "Normal")

    res = classify_principled_bsdf(bsdf)
    assert "bump" in res.channels
    assert "normal" not in res.channels
    assert res.channels["bump"] == {
        "texture": "textures/b.png",
        "colorspace": "raw",
        "strength": 0.3,
        "distance": 0.01,
    }
    assert res.uv["map"] == "UVMap"
    assert any("different UV/Mapping transforms" in w for w in res.warnings)


def test_classifier_bake_patterns() -> None:
    # 1. Procedural Noise -> ColorRamp -> Base Color, Object coords -> Image -> Roughness
    bsdf = GraphNode("BSDF_PRINCIPLED", name="Principled BSDF")
    bsdf.set_input("Metallic", 0.0)

    noise = GraphNode("TEX_NOISE", name="Noise Texture")
    ramp = GraphNode("VALTORGB", name="ColorRamp")
    ramp.connect("Fac", noise, "Fac")
    bsdf.connect("Base Color", ramp, "Color")

    tex_coord = GraphNode("TEX_COORD", name="TexCoord")
    img_obj = GraphNode("TEX_IMAGE", name="ObjProjImg", properties={"image": "textures/wood.png"})
    img_obj.connect("Vector", tex_coord, "Object")
    bsdf.connect("Roughness", img_obj, "Color")

    bc_cls = classify_socket(bsdf.inputs["Base Color"], channel_name="base_color")
    assert bc_cls.decision == "bake"
    r_cls = classify_socket(bsdf.inputs["Roughness"], channel_name="roughness")
    assert r_cls.decision == "bake"
    m_cls = classify_socket(bsdf.inputs["Metallic"], channel_name="metallic")
    assert m_cls.decision == "direct"
    assert m_cls.channel_data == {"value": 0.0}

    res = classify_principled_bsdf(bsdf)
    assert set(res.sockets_to_bake) == {"base_color", "roughness"}
    assert res.channels["metallic"] == {"value": 0.0}

    # 2. Mix Shader of two BSDFs -> bakes all primary channels and warns
    bsdf_a = GraphNode("BSDF_PRINCIPLED", name="BSDF_A")
    bsdf_b = GraphNode("BSDF_PRINCIPLED", name="BSDF_B")
    mix_shader = GraphNode("MIX_SHADER", name="Mix Shader")
    mix_shader.connect("Shader", bsdf_a, "BSDF")
    mix_shader.connect("Shader_001", bsdf_b, "BSDF")
    out_node = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out_node.connect("Surface", mix_shader, "Shader")

    mix_res = classify_material_output(out_node)
    assert mix_res.is_mix_shader is True
    assert "base_color" in mix_res.sockets_to_bake
    assert "roughness" in mix_res.sockets_to_bake
    assert "normal" in mix_res.sockets_to_bake
    assert any("MIX_SHADER" in w for w in mix_res.warnings)


def test_classifier_principled_transmission_and_alpha() -> None:
    # Principled with Transmission Weight > 0 (value) -> transmission + ior + roughness, blend
    bsdf = GraphNode("BSDF_PRINCIPLED", name="Principled Glass")
    bsdf.set_input("Base Color", [0.95, 0.98, 1.0, 1.0])
    bsdf.set_input("Roughness", 0.05)
    bsdf.set_input("IOR", 1.52)
    bsdf.set_input("Transmission Weight", 0.92)
    out = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out.connect("Surface", bsdf, "BSDF")

    res = classify_material_output(out)
    assert res.sockets_to_bake == []
    assert res.blend_method == "blend"
    assert res.channels["transmission"] == {"value": 0.92}
    assert res.channels["ior"] == {"value": 1.52}
    assert res.channels["roughness"] == {"value": 0.05}
    assert res.channels["base_color"] == {"value": [0.95, 0.98, 1.0]}

    # Principled with textured Transmission Weight and minimal sockets -> still populates ior & roughness
    bsdf_tex = GraphNode("BSDF_PRINCIPLED", name="Principled Tex Trans")
    img_tr = GraphNode("TEX_IMAGE", name="TransTex", properties={"image": "textures/trans.png"})
    bsdf_tex.connect("Transmission Weight", img_tr, "Color")
    res_tex = classify_principled_bsdf(bsdf_tex)
    assert res_tex.blend_method == "blend"
    assert res_tex.channels["transmission"] == {
        "texture": "textures/trans.png",
        "colorspace": "raw",
        "channel": "rgb",
    }
    assert res_tex.channels["ior"] == {"value": 1.5}
    assert res_tex.channels["roughness"] == {"value": 0.5}

    # Principled with Alpha < 1 (and CLIP material blend_method)
    bsdf_alpha = GraphNode("BSDF_PRINCIPLED", name="Principled Alpha")
    bsdf_alpha.set_input("Alpha", 0.4)
    out_clip = GraphNode(
        "OUTPUT_MATERIAL",
        name="Material Output",
        properties={"blend_method": "CLIP"},
    )
    out_clip.connect("Surface", bsdf_alpha, "BSDF")
    res_clip = classify_material_output(out_clip)
    assert res_clip.channels["alpha"] == {"value": 0.4}
    assert res_clip.blend_method == "clip"


def test_classifier_glass_and_refraction_bsdf_direct_and_dominant_mix() -> None:
    # 1. Direct Glass BSDF
    glass = GraphNode("ShaderNodeBsdfGlass", name="Glass BSDF")
    glass.set_input("Color", [0.85, 0.95, 1.0, 1.0])
    glass.set_input("Roughness", 0.02)
    glass.set_input("IOR", 1.5)
    out = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out.connect("Surface", glass, "BSDF")

    res_glass = classify_material_output(out)
    assert res_glass.sockets_to_bake == []
    assert res_glass.blend_method == "blend"
    assert res_glass.channels["transmission"] == {"value": 1.0}
    assert res_glass.channels["base_color"] == {"value": [0.85, 0.95, 1.0]}
    assert res_glass.channels["roughness"] == {"value": 0.02}
    assert res_glass.channels["ior"] == {"value": 1.5}

    # 2. Direct Refraction BSDF
    refr = GraphNode("ShaderNodeBsdfRefraction", name="Refraction BSDF")
    refr.set_input("Color", [1.0, 0.9, 0.2, 1.0])
    refr.set_input("Roughness", 0.1)
    refr.set_input("IOR", 1.45)
    out_refr = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out_refr.connect("Surface", refr, "BSDF")

    res_refr = classify_material_output(out_refr)
    assert res_refr.sockets_to_bake == []
    assert res_refr.blend_method == "blend"
    assert res_refr.channels["transmission"] == {"value": 1.0}
    assert res_refr.channels["base_color"] == {"value": [1.0, 0.9, 0.2]}
    assert res_refr.channels["roughness"] == {"value": 0.1}
    assert res_refr.channels["ior"] == {"value": 1.45}

    # 3. Dominant Glass BSDF in a Mix Shader (e.g. mixed with Glossy via Fresnel or factor >= 0.5)
    glossy = GraphNode("ShaderNodeBsdfGlossy", name="Glossy BSDF")
    mix = GraphNode("ShaderNodeMixShader", name="Mix Shader")
    mix.set_input("Factor", 0.8)
    mix.connect("Shader", glossy, "BSDF")
    mix.connect("Shader_001", glass, "BSDF")
    out_mix = GraphNode("OUTPUT_MATERIAL", name="Material Output")
    out_mix.connect("Surface", mix, "Shader")

    res_dom = classify_material_output(out_mix)
    assert res_dom.sockets_to_bake == []
    assert res_dom.blend_method == "blend"
    assert res_dom.channels["transmission"] == {"value": 1.0}
    assert res_dom.channels["base_color"] == {"value": [0.85, 0.95, 1.0]}
    assert res_dom.channels["roughness"] == {"value": 0.02}
    assert res_dom.channels["ior"] == {"value": 1.5}


def test_classifier_mix_transparent_and_shadow_ray_tricks() -> None:
    # 1. Mix Shader(Transparent BSDF, Principled) with constant factor
    transp = GraphNode("ShaderNodeBsdfTransparent", name="Transparent BSDF")
    bsdf = GraphNode("ShaderNodeBsdfPrincipled", name="Principled BSDF")
    bsdf.set_input("Base Color", [0.1, 0.1, 0.1, 1.0])
    bsdf.set_input("Metallic", 0.0)
    bsdf.set_input("Roughness", 0.0)
    bsdf.set_input("IOR", 1.45)

    mix_const = GraphNode("ShaderNodeMixShader", name="Mix Shader")
    mix_const.set_input("Factor", 0.35)
    mix_const.connect("Shader", transp, "BSDF")
    mix_const.connect("Shader_001", bsdf, "BSDF")
    out_const = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_const.connect("Surface", mix_const, "Shader")

    res_const = classify_material_output(out_const)
    assert res_const.sockets_to_bake == []
    assert res_const.blend_method == "blend"
    assert res_const.channels["alpha"] == {"value": 0.35}
    assert res_const.channels["base_color"] == {"value": [0.1, 0.1, 0.1]}
    assert res_const.channels["roughness"] == {"value": 0.0}

    # 2. Mix Shader(Transparent BSDF, Principled) with textured factor
    img_mask = GraphNode("TEX_IMAGE", name="MaskTex", properties={"image": "textures/mask.png"})
    mix_tex = GraphNode("ShaderNodeMixShader", name="Mix Shader")
    mix_tex.connect("Factor", img_mask, "Color")
    mix_tex.connect("Shader", transp, "BSDF")
    mix_tex.connect("Shader_001", bsdf, "BSDF")
    out_tex = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_tex.connect("Surface", mix_tex, "Shader")

    res_tex = classify_material_output(out_tex)
    assert res_tex.sockets_to_bake == []
    assert res_tex.blend_method == "blend"
    assert res_tex.channels["alpha"] == {
        "texture": "textures/mask.png",
        "channel": "rgb",
    }

    # 3. Ferrari SF90 Glass pattern: Light Path("Is Shadow Ray") -> Math(ADD, 0.5) -> MixShader.Factor
    lp = GraphNode("ShaderNodeLightPath", name="Light Path")
    math_add = GraphNode("ShaderNodeMath", name="Math", properties={"operation": "ADD"})
    math_add.connect("Value", lp, "Is Shadow Ray", default_value=0.5)
    math_add.set_input("Value_001", 0.5)

    mix_sf90 = GraphNode("ShaderNodeMixShader", name="Mix Shader")
    mix_sf90.connect("Factor", math_add, "Value", default_value=0.5)
    mix_sf90.connect("Shader", transp, "BSDF")
    mix_sf90.connect("Shader_001", bsdf, "BSDF")
    out_sf90 = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_sf90.connect("Surface", mix_sf90, "Shader")

    res_sf90 = classify_material_output(out_sf90)
    assert res_sf90.sockets_to_bake == []
    assert res_sf90.blend_method == "blend"
    assert res_sf90.channels["alpha"] == {"value": 0.5}
    assert res_sf90.channels["base_color"] == {"value": [0.1, 0.1, 0.1]}
    assert res_sf90.channels["roughness"] == {"value": 0.0}
    assert res_sf90.channels["ior"] == {"value": 1.45}
    assert "shadow-ray trick ignored" in res_sf90.warnings

    # 4. Direct Light Path("Is Camera Ray") trick on Mix Shader(Transparent BSDF, Glass BSDF)
    glass = GraphNode("ShaderNodeBsdfGlass", name="Glass BSDF")
    glass.set_input("Color", [0.9, 0.95, 1.0, 1.0])
    glass.set_input("Roughness", 0.0)
    glass.set_input("IOR", 1.5)
    mix_cam = GraphNode("ShaderNodeMixShader", name="Mix Shader")
    mix_cam.connect("Factor", lp, "Is Camera Ray")
    mix_cam.connect("Shader", transp, "BSDF")
    mix_cam.connect("Shader_001", glass, "BSDF")
    out_cam = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_cam.connect("Surface", mix_cam, "Shader")

    res_cam = classify_material_output(out_cam)
    assert res_cam.sockets_to_bake == []
    assert res_cam.blend_method == "blend"
    assert res_cam.channels["transmission"] == {"value": 1.0}
    assert res_cam.channels["base_color"] == {"value": [0.9, 0.95, 1.0]}
    assert "shadow-ray trick ignored" in res_cam.warnings


def test_classifier_glossy_metallic_and_emission_shaders() -> None:
    # 1. Glossy BSDF as surface -> metallic 1, base_color, roughness
    glossy = GraphNode("ShaderNodeBsdfGlossy", name="Mirror Glossy")
    glossy.set_input("Color", [0.9, 0.9, 0.92, 1.0])
    glossy.set_input("Roughness", 0.01)
    out_g = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_g.connect("Surface", glossy, "BSDF")

    res_g = classify_material_output(out_g)
    assert res_g.sockets_to_bake == []
    assert res_g.channels["metallic"] == {"value": 1.0}
    assert res_g.channels["base_color"] == {"value": [0.9, 0.9, 0.92]}
    assert res_g.channels["roughness"] == {"value": 0.01}

    # 2. Metallic BSDF (Blender 4.3+/5.x) as surface -> metallic 1, base_color, roughness
    metal = GraphNode("ShaderNodeBsdfMetallic", name="Metallic BSDF")
    metal.set_input("Base Color", [0.8, 0.75, 0.7, 1.0])
    metal.set_input("Roughness", 0.15)
    out_m = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_m.connect("Surface", metal, "BSDF")

    res_m = classify_material_output(out_m)
    assert res_m.sockets_to_bake == []
    assert res_m.channels["metallic"] == {"value": 1.0}
    assert res_m.channels["base_color"] == {"value": [0.8, 0.75, 0.7]}
    assert res_m.channels["roughness"] == {"value": 0.15}

    # 3. Emission shader directly as surface -> emission channel + strength
    em = GraphNode("ShaderNodeEmission", name="Emission")
    em.set_input("Color", [1.0, 0.5, 0.1, 1.0])
    em.set_input("Strength", 4.5)
    out_em = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_em.connect("Surface", em, "Emission")

    res_em = classify_material_output(out_em)
    assert res_em.sockets_to_bake == []
    assert res_em.channels["emission"] == {
        "value": [1.0, 0.5, 0.1],
        "strength": 4.5,
    }

    # 4. Emission shader mixed in with Principled BSDF via Add Shader -> keeps surface + emission
    bsdf = GraphNode("ShaderNodeBsdfPrincipled", name="Principled BSDF")
    bsdf.set_input("Base Color", [0.2, 0.2, 0.2, 1.0])
    bsdf.set_input("Metallic", 0.0)
    bsdf.set_input("Roughness", 0.4)
    add_sh = GraphNode("ShaderNodeAddShader", name="Add Shader")
    add_sh.connect("Shader", bsdf, "BSDF")
    add_sh.connect("Shader_001", em, "Emission")
    out_add = GraphNode("ShaderNodeOutputMaterial", name="Material Output")
    out_add.connect("Surface", add_sh, "Shader")

    res_add = classify_material_output(out_add)
    assert res_add.sockets_to_bake == []
    assert res_add.channels["base_color"] == {"value": [0.2, 0.2, 0.2]}
    assert res_add.channels["roughness"] == {"value": 0.4}
    assert res_add.channels["emission"] == {
        "value": [1.0, 0.5, 0.1],
        "strength": 4.5,
    }

def _make_package(path, name="asset"):
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "kitbridge.json"), "w", encoding="utf-8") as fh:
        json.dump({"asset": {"name": name}}, fh)


@pytest.fixture
def chosen_folder(tmp_path):
    paths.set_cache_root(str(tmp_path))
    yield tmp_path
    paths.set_cache_root("")


def test_downloads_go_to_a_subfolder_per_type(chosen_folder):
    for asset_type, folder in (("model", "models"), ("material", "materials"), ("hdr", "hdris"), ("brush", "other")):
        got = paths.asset_cache_dir("id", "2048", asset_type=asset_type, create=False)
        assert got == os.path.join(str(chosen_folder), folder, "id_2048")


def test_find_covers_subfolders_and_the_old_flat_layout(chosen_folder):
    assert paths.find_asset_cache_dir("a", "2048") == ""
    typed = paths.asset_cache_dir("a", "2048", asset_type="hdr", create=True)
    flat = paths.asset_cache_dir("b", "2048", create=True)
    assert paths.find_asset_cache_dir("a", "2048") == typed
    assert paths.find_asset_cache_dir("b", "2048") == flat


def test_clear_cache_never_touches_the_users_other_files(chosen_folder):
    ours = [
        paths.asset_cache_dir("a", "2048", asset_type="model", create=False),
        paths.asset_cache_dir("b", "1024", asset_type="hdr", create=False),
        paths.asset_cache_dir("c", "ORIGINAL", create=False),
    ]
    for path in ours:
        _make_package(path)
    partial = paths.asset_cache_dir("d", "2048", asset_type="model", create=False)
    os.makedirs(os.path.join(partial, "source"))
    foreign = [
        chosen_folder / "holiday_photos_2024",            # looks like a package name, has no manifest
        chosen_folder / "models" / "blenderkit_chair",    # BlenderKit's own layout
        chosen_folder / "Documents",
    ]
    for path in foreign:
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("keep", encoding="utf-8")

    assert pipeline.clear_cache() == 4

    for path in ours + [partial]:
        assert not os.path.exists(path)
    for path in foreign:
        assert (path / "keep.txt").is_file()


def test_cache_summary_counts_each_folder(chosen_folder):
    _make_package(paths.asset_cache_dir("a", "2048", asset_type="model", create=False))
    _make_package(paths.asset_cache_dir("b", "2048", asset_type="model", create=False))
    _make_package(paths.asset_cache_dir("c", "2048", asset_type="hdr", create=False))
    summary = pipeline.cache_summary()
    assert summary["count"] == 3
    assert summary["folders"] == {"models": 2, "hdris": 1}
    assert summary["size"] > 0

class _ScriptedClient:
    """bk_client stand-in: *progress(call_number)* gives the next task state."""

    def __init__(self, progress):
        self.progress = progress
        self.calls = 0
        self.cancelled = False

    def ensure_running(self):
        pass

    def asset_download(self, **_kwargs):
        return "t1"

    def cancel_download(self, _task_id):
        self.cancelled = True

    def report(self, api_key=""):
        self.calls += 1
        return [dict(task_id="t1", **self.progress(self.calls))]


def test_slow_download_outlives_the_limit_while_it_progresses(tmp_path):
    def progress(call):
        if call >= 14:  # about 2 s at the 0.15 s poll, twice the limit below
            return {"status": "finished", "progress": 100, "result": {"file_paths": []}}
        return {"status": "created", "progress": call}

    client = _ScriptedClient(progress)
    (tmp_path / "asset.blend").write_bytes(b"x")
    pipeline._download_via_client(client, {"isFree": True}, "resolution_2K", str(tmp_path), "", timeout=1.0)
    assert not client.cancelled


def test_download_with_no_progress_is_given_up_on(tmp_path):
    client = _ScriptedClient(lambda _call: {"status": "created", "progress": 15})
    with pytest.raises(pipeline.PipelineError, match="no progress"):
        pipeline._download_via_client(client, {"isFree": True}, "resolution_2K", str(tmp_path), "", timeout=1.0)
    assert client.cancelled



def test_cancelling_a_fetch_leaves_nothing_behind(chosen_folder, monkeypatch):
    cancel = threading.Event()
    seen = {}

    def fake_download(**kwargs):
        seen["source"] = kwargs["source_dir"]
        Path(kwargs["source_dir"], "partial.blend").write_bytes(b"x")
        cancel.set()
        raise pipeline.PipelineError("Download cancelled.", stage="download")

    monkeypatch.setattr(pipeline, "_download_via_client", fake_download)
    asset = {"assetBaseId": "aaaaaaaa-0000-0000-0000-000000000000", "id": "1", "name": "A", "assetType": "model"}
    with pytest.raises(pipeline.PipelineError, match="^Cancelled") as err:
        pipeline.fetch_asset(mock.Mock(), asset, "1024", cancel_event=cancel)
    assert err.value.stage == "download"
    assert not os.path.exists(os.path.dirname(seen["source"]))


def test_a_failed_fetch_keeps_its_files(chosen_folder, monkeypatch):
    seen = {}

    def fake_download(**kwargs):
        seen["source"] = kwargs["source_dir"]
        raise pipeline.PipelineError("boom", stage="download")

    monkeypatch.setattr(pipeline, "_download_via_client", fake_download)
    asset = {"assetBaseId": "bbbbbbbb-0000-0000-0000-000000000000", "id": "1", "name": "B", "assetType": "model"}
    with pytest.raises(pipeline.PipelineError, match="boom"):
        pipeline.fetch_asset(mock.Mock(), asset, "1024", cancel_event=threading.Event())
    assert os.path.isdir(seen["source"])


def test_both_sides_agree_on_the_exporter_version():
    script = Path(pipeline.__file__).parent.parent / "blender_scripts" / "kb_export.py"
    assert f"EXPORTER_VERSION = {pipeline.EXPORTER_VERSION}" in script.read_text(encoding="utf-8")


def test_udim_filename_helpers_preserve_literal_manifest_token() -> None:
    template = udim_texture_template("body_basecolor.<UDIM>.webp")
    assert template == "body_basecolor.<UDIM>.webp"
    assert udim_tile_filename(template, 1001) == "body_basecolor.1001.webp"
    assert udim_texture_template("body_basecolor.1001.png") == "body_basecolor.<UDIM>.png"


def test_light_manifest_helper_uses_plain_values() -> None:
    entry = build_light_manifest_entry(
        name="Key",
        light_type="AREA",
        color=(0.2, 0.3, 0.4),
        power=250.0,
        radius=0.1,
        matrix=((1, 0, 0, 2), (0, 1, 0, 3), (0, 0, 1, 4), (0, 0, 0, 1)),
        parent="Rig",
        shape="DISK",
        size=1.5,
        size_y=2.0,
    )
    assert entry["type"] == "area"
    assert entry["colour"] == [0.2, 0.3, 0.4]
    assert entry["matrix"][0][3] == 2.0
    assert entry["shape"] == "disk"
    assert entry["parent"] == "Rig"


def test_blender_to_fbx_matrix_swaps_up_axis() -> None:
    matrix = blender_matrix_to_fbx(
        ((1, 0, 0, 2), (0, 1, 0, 3), (0, 0, 1, 4), (0, 0, 0, 1))
    )
    assert [matrix[0][3], matrix[1][3], matrix[2][3]] == [2.0, 4.0, -3.0]


def _old_package(folder: Path, base_id: str = "cccccccc-0000-0000-0000-000000000000") -> Path:
    pkg = Path(paths.asset_cache_dir(base_id, "1024", asset_type="model", create=True))
    manifest = _sample_model_manifest()
    _populate_package_files(pkg, manifest)
    (pkg / "source").mkdir()
    (pkg / "source" / "a.blend").write_bytes(b"x")
    return pkg


def test_needs_reconvert_only_for_old_packages_with_a_source(chosen_folder):
    pkg = _old_package(chosen_folder)
    assert pipeline.needs_reconvert(str(pkg))
    manifest = json.loads((pkg / "kitbridge.json").read_text(encoding="utf-8"))
    manifest["exporter_version"] = pipeline.EXPORTER_VERSION
    (pkg / "kitbridge.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert not pipeline.needs_reconvert(str(pkg))
    manifest.pop("exporter_version")
    (pkg / "kitbridge.json").write_text(json.dumps(manifest), encoding="utf-8")
    (pkg / "source" / "a.blend").unlink()
    assert not pipeline.needs_reconvert(str(pkg))


def test_reconvert_swaps_in_the_new_package(chosen_folder, monkeypatch):
    pkg = _old_package(chosen_folder)
    old = json.loads((pkg / "kitbridge.json").read_text(encoding="utf-8"))
    new = dict(old, exporter_version=pipeline.EXPORTER_VERSION)

    def fake_convert(pkg_dir, **kwargs):
        (Path(pkg_dir) / old["geometry"]).write_bytes(b"new fbx")
        (Path(pkg_dir) / "kitbridge.json").write_text(json.dumps(new), encoding="utf-8")
        return pipeline.Package(path=pkg_dir, manifest=new, is_cached=False)

    monkeypatch.setattr(pipeline, "_convert_model_or_material", fake_convert)
    assert pipeline.reconvert(str(pkg)).path == str(pkg)
    assert (pkg / old["geometry"]).read_bytes() == b"new fbx"
    assert not pipeline.needs_reconvert(str(pkg))
    assert not (pkg / ".reconvert").exists()
    assert (pkg / "source" / "a.blend").is_file()


def test_reconvert_keeps_the_old_package_on_failure_and_reports_a_cancel(chosen_folder, monkeypatch):
    pkg = _old_package(chosen_folder)
    before = (pkg / "kitbridge.json").read_bytes()

    def boom(*_a, **_k):
        raise pipeline.PipelineError("no blender", stage="convert")

    monkeypatch.setattr(pipeline, "_convert_model_or_material", boom)
    assert pipeline.reconvert(str(pkg)).is_cached
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(pipeline.PipelineError, match="^Cancelled"):
        pipeline.reconvert(str(pkg), cancel_event=cancel)
    assert (pkg / "kitbridge.json").read_bytes() == before
    assert not (pkg / ".reconvert").exists()


def test_geometry_size_mb(chosen_folder):
    pkg = _old_package(chosen_folder)
    manifest = json.loads((pkg / "kitbridge.json").read_text(encoding="utf-8"))
    with open(pkg / manifest["geometry"], "wb") as fh:
        fh.truncate(3 * 1024 * 1024)
    assert pipeline.geometry_size_mb(str(pkg)) == 3
    assert pipeline.geometry_size_mb(str(chosen_folder / "nothing")) == 0
