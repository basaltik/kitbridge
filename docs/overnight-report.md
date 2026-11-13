# Kitbridge Overnight Conversion-Fidelity & Robustness Report

**Branch:** `overnight/antigravity`  
**Exporter Version:** `11` → `12`  
**Blender Version:** `5.3.0` (`D:\Games\steam\steamapps\common\Blender\blender.exe`)  
**Cinema 4D Self-Test:** Cinema 4D 2024 (`c4dpy.exe dev\c4d_selftest.py`, Redshift)  
**Unit Test Suite:** `87 passed` (`py -3 -m pytest -q`)

---

## 1. Summary Table of Tested Assets (20 New Free Assets at `--res 1024`)

None of the 20 assets below appear in `dev/bench/assets.json`, and none of the benchmark packages were modified.

| # | Asset Name | `base_id` | Type | Category | Conv. Time (v11 → v12) | FBX MB | Raw / C4D Polys | Result (v11 → v12) |
|---:|---|---|---|---|---:|---:|---:|---|
| 1 | Teak Sofa by Arne Wahl Iversen | `b45d44a6-3059-4646-b9ca-804bee1b6ab5` | `model` | Furniture | 5.43s | 1.609 | 3,592 / 18,980 (2 SDS) | PASS → **PASS** |
| 2 | Vintage Bicycle | `32783d96-735c-4365-9bf4-818b9fc39727` | `model` | Vehicles | 14.43s → **16.07s** | 6.972 | 127,180 / 139,628 (11 SDS) | **FAIL** (C4D crash + missing tex) → **PASS** |
| 3 | Bonsai tree | `ae0a2d0b-2a1b-41df-bbf1-3c73c0a5a106` | `model` | Plants | 16.41s → **17.52s** | 2.818 | 51,923 / 58,235 (2 SDS) | WARN (missing `base_color` on unused slot) → **PASS** |
| 4 | Coupe Wine Glass | `f6ca75da-0038-4241-851a-bd7302071581` | `model` | Glass | 0.25s | 0.216 | 5,376 / 5,376 (0 SDS) | PASS → **PASS** |
| 5 | Onyx marble | `b55555d2-1fb1-4540-8b26-a676c2cf4a9b` | `material` | Procedural / Stone | 9.21s → **4.43s** | 0.087 | 2,048 / 2,048 | WARN (CPU bake + missing `bump_in_normal`) → **PASS** |
| 6 | Celosia Dining Chair | `6089f275-3f1e-4304-822c-b9ca8403b011` | `model` | Furniture | 4.01s | 0.068 | 971 / 1,099 (1 SDS) | PASS → **PASS** |
| 7 | Robot KUKA AGILUS with Rig animate | `45eeaaf4-55ae-4a19-973f-3eb45fde20aa` | `model` | Characters / Rigs | 80.94s → **60.35s** | 37.721 | 964,379 / 964,379 (0 SDS) | WARN (8 bogus normal maps, 9 redundant bakes) → **PASS** |
| 8 | Modern House | `264b47f4-a1ee-4c31-9ee9-4c0e8dc1a98b` | `model` | Architecture | 36.57s → **36.31s** | 3.882 | 91,010 / 91,010 (0 SDS) | **FAIL** (C4D crash + 4 chrome stucco walls) → **PASS** |
| 9 | Perfume Bottle | `0e486dc1-3a6f-4cd3-bf80-1df96ef270e8` | `model` | Glass | 10.50s → **12.13s** | 0.167 | 2,231 / 2,615 (2 SDS) | WARN (opaque `GLASS` bottle material) → **PASS** |
| 10 | Gold metal (gold) | `5a932113-9a23-422e-8dc9-5466576fe601` | `material` | Metal | 11.49s → **4.83s** | 0.088 | 2,048 / 2,048 | WARN (unconfigured CPU bake) → **PASS** |
| 11 | Luxury Electric Car – Black | `c3c28d91-3547-4b7b-9cc3-68d00d7a6993` | `model` | Vehicles | 17.84s → **15.53s** | 8.362 | 127,484 / 185,936 (45 SDS) | **FAIL** (C4D crash + 4 broken glass mats) → **PASS** |
| 12 | Monstera plant | `65ea3e91-b091-4f2c-97a4-d118c594a537` | `model` | Plants | 8.31s → **9.46s** | 2.618 | 83,230 / 84,190 (1 SDS) | WARN (solid white leaves `Daun`) → **PASS** |
| 13 | Character | `8d13b980-caf4-4efd-869e-44ffd324f16e` | `model` | Characters | 15.27s → **14.81s** | 4.431 | 71,588 / 83,876 (1 SDS) | WARN (lost UDIM & SSS on skin, `alpha=0.027` eye) → **PASS** |
| 14 | Magic Watch | `0c810e6b-8f61-48a5-84a8-22d822e30714` | `model` | Metal | 0.50s | 0.129 | 1,875 / 1,875 (0 SDS) | **FAIL** (C4D crash) → **PASS** |
| 15 | Beige linen fabric | `23c9cd3e-6892-4c29-add4-0cbaf429f451` | `material` | Fabric | 9.46s → **4.66s** | 0.087 | 2,048 / 2,048 | WARN (unconfigured CPU bake) → **PASS** |
| 16 | Apartment Building | `d48241bb-48b0-4883-8415-d4cd991480a4` | `model` | Architecture | 4.02s | 8.719 | 322,592 / 322,592 (0 SDS) | PASS (noted `Generated` coords on `Curtain`) → **PASS** |
| 17 | Bell Portable Moss Table Lamp | `30deac90-8710-4453-a053-d41645e31ea5` | `model` | Metal / Lighting | 5.45s → **6.19s** | 0.175 | 2,514 / 2,514 (0 SDS) | WARN (clipped 8-bit white emission) → **PASS** |
| 18 | Vintage Lace Curtain | `c5529a99-e37c-47a6-a79c-e9b7665bf91b` | `model` | Fabric | 4.52s | 0.279 | 3,671 / 5,719 (5 SDS) | PASS → **PASS** |
| 19 | Pack of 03 Folded Towels | `c9362ddd-103e-495d-a16a-9bdd389bb43e` | `model` | Fabric | 0.31s | 0.934 | 34,478 / 34,478 (0 SDS) | PASS → **PASS** |
| 20 | Terrazzo White | `519889f4-25ea-43aa-8cb2-b9fa961b2fce` | `material` | Procedural / Stone | 0.33s | 0.087 | 2,048 / 2,048 | PASS → **PASS** |

---

## 2. Problems Found & Root Causes Fixed (with Commit Hashes & Before/After Evidence)

### Fix 1 — Cinema 4D Import Crash (`ReferenceError: the object 'c4d.BaseObject' is not alive`) on Partially-Subdivided or Non-Subdivided Meshes
- **Commit:** `380ae40` (`c4d_import: exclude unwrapped SDS generators from imported objects list`)
- **Files Modified:** `plugin/kitbridge/kitbridge/c4d_import/geometry.py`, `tests/test_c4d_import.py`
- **Affected Assets (4/20 failed `c4d_selftest.py` before fix):**
  - `Vintage Bicycle` (`32783d96-735c-4365-9bf4-818b9fc39727`)
  - `Modern House` (`264b47f4-a1ee-4c31-9ee9-4c0e8dc1a98b`)
  - `Luxury Electric Car – Black` (`c3c28d91-3547-4b7b-9cc3-68d00d7a6993`)
  - `Magic Watch` (`0c810e6b-8f61-48a5-84a8-22d822e30714`)
- **Root Cause:**
  `import_geometry()` imports `geometry.fbx` with `FBXIMPORT_SDS = True`, which causes Cinema 4D's FBX importer to wrap *every* mesh in the FBX into a `c4d.Osds` (`Subdivision Surface`) generator. `import_geometry()` collected `new_objs` (including all `c4d.Osds` wrappers and their children) **before** calling `_configure_subdivision(new_objs, manifest)`. Inside `_configure_subdivision()`, any `c4d.Osds` wrapper whose child mesh had no `"subdivision"` entry in `kitbridge.json` was unwrapped and destroyed via `obj.Remove()`. However, `new_objs` still held the dead `c4d.BaseObject` reference and returned it in `imported_objects`, causing downstream material binding and scene traversal (`obj.GetName()`) to crash with `ReferenceError: the object 'c4d.BaseObject' is not alive`.
- **Fix & Verification:**
  Recollect `new_objs` from post-import top-level roots after `_configure_subdivision()` finishes removing unneeded `c4d.Osds` generators. All 4 crashing assets now pass `c4d_selftest.py` with `errors: []`.

---

### Fix 2 — `ShaderNodeSubsurfaceScattering` Classification & Identity Color-Adjust Passthrough Preserving UDIM Textures
- **Commit:** `222c11f` (`core/shader_graph: improve SSS, Fresnel glass, Mix Shader, and passthrough classification`)
- **Files Modified:** `plugin/kitbridge/kitbridge/core/shader_graph.py`, `tests/test_pipeline.py`
- **Affected Asset:** `Character` (`8d13b980-caf4-4efd-869e-44ffd324f16e`), material `Skin_Mat_Proxy`
- **Root Cause:**
  1. `Skin_Mat_Proxy` uses a top-level `ShaderNodeSubsurfaceScattering` (`SUBSURFACE_SCATTERING`) node with `Scale = 0.03` and `IOR = 1.4`. `_classify_fallback_shader()` had no `SUBSURFACE_SCATTERING` branch, so it dropped `subsurface` and `ior` completely.
  2. Its 4K UDIM `Skin_Edited_BaseColor.<UDIM>.png` texture was routed through a `ShaderNodeHueSaturation` with identity parameters (`Hue = 0.5, Saturation = 1.0, Value = 1.0, Fac = 1.0`). Because `resolve_upstream()` did not treat identity `HUE_SAT` / `BRIGHTCONTRAST` / `GAMMA` nodes as passthroughs, `base_color` was marked `BAKE`, baking only tile `1001` into a single non-UDIM 1K PNG (`textures/Skin_Mat_Proxy_base_color.png`) and destroying multi-tile UDIM texturing.
  3. `_classify_normal_socket()` did not copy `"udim": True` from `tex_info` onto `normal_data`, omitting `"udim": True` on `Skin_Edited_Normal.<UDIM>.png`.
- **Evidence (`Character` → `Skin_Mat_Proxy` in `kitbridge.json`):**
  - **Before (v11):**
    ```json
    {
      "baked_channels": ["base_color", "roughness"],
      "channels": {
        "metallic": {"value": 0.0},
        "normal": {"texture": "textures/Skin_Edited_Normal.<UDIM>.png", "colorspace": "raw", "strength": 1.0, "flip_green": false},
        "base_color": {"texture": "textures/Skin_Mat_Proxy_base_color.png", "colorspace": "srgb", "channel": "rgb"},
        "roughness": {"texture": "textures/Skin_Mat_Proxy_roughness.png", "colorspace": "raw", "channel": "r", "invert": false}
      }
    }
    ```
  - **After (v12):**
    ```json
    {
      "baked_channels": ["roughness"],
      "channels": {
        "base_color": {"texture": "textures/Skin_Edited_BaseColor.<UDIM>.png", "colorspace": "srgb", "channel": "rgb", "udim": true},
        "metallic": {"value": 0.0},
        "ior": {"value": 1.4},
        "subsurface": {"value": 1.0, "scale": 0.03},
        "normal": {"texture": "textures/Skin_Edited_Normal.<UDIM>.png", "colorspace": "raw", "strength": 1.0, "flip_green": false, "udim": true},
        "roughness": {"texture": "textures/Skin_Mat_Proxy_roughness.png", "colorspace": "raw", "channel": "r", "invert": false}
      }
    }
    ```

---

### Fix 3 — Architectural & Automotive Fresnel / Layer-Weight Glass Classification (`Mix Shader` of `Transparent BSDF` + `Glossy`/`Anisotropic`/`Glass BSDF`)
- **Commit:** `222c11f` (`core/shader_graph: improve SSS, Fresnel glass, Mix Shader, and passthrough classification`)
- **Affected Assets:**
  - `Luxury Electric Car – Black` (`c3c28d91-3547-4b7b-9cc3-68d00d7a6993`): `Glass`, `Glass.001`, `Window`, `Window Glass Cycle`
  - `Character` (`8d13b980-caf4-4efd-869e-44ffd324f16e`): `Sclera_Mat.001`
- **Root Cause:**
  In Blender, car windows, architectural glass, and eye corneas (`Sclera_Mat.001`) are commonly authored as `Mix Shader(Transparent BSDF, Anisotropic/Glossy/Glass BSDF)` driven by `Fresnel` or `Layer Weight` (often through a `ColorRamp`). Previously, `shader_graph.py` treated every `Mix Shader` with a `Transparent BSDF` branch as a decal/cutout opacity mask: it classified the other shader (`Anisotropic BSDF` → `metallic: 1.0`!) and baked `Fac` (`Fresnel` / `Layer Weight`) as `"alpha"`. During flat/orthographic Cycles baking, `Layer Weight` evaluated to `0.0` (`alpha: 0.0`, making `Glass` and `Glass.001` completely invisible) or `Fresnel` evaluated to `0.957` (`metallic: 1.0, alpha: 0.957`, turning `Window Glass Cycle` into an opaque chrome windshield), and `Sclera_Mat.001` got `alpha: 0.0272` on top of `transmission: 1.0`.
- **Evidence (`Luxury Electric Car – Black` and `Character` in `kitbridge.json`):**
  - **`Glass` (Before v11):** `{"base_color": {"value": [0.04, 0.04, 0.04]}, "roughness": {"value": 0.0}, "metallic": {"value": 1.0}, "alpha": {"value": 0.0}}`
  - **`Glass` (After v12):** `{"base_color": {"value": [1.0, 1.0, 1.0]}, "roughness": {"value": 0.0}, "metallic": {"value": 0.0}, "transmission": {"value": 1.0}, "ior": {"value": 1.5}}`
  - **`Window Glass Cycle` (Before v11):** `{"base_color": {"value": [1.0, 1.0, 1.0]}, "roughness": {"value": 0.0001}, "metallic": {"value": 1.0}, "alpha": {"value": 0.95742}}`
  - **`Window Glass Cycle` (After v12):** `{"base_color": {"value": [1.0, 1.0, 1.0]}, "roughness": {"value": 0.0001}, "metallic": {"value": 0.0}, "transmission": {"value": 1.0}, "ior": {"value": 1.52}}`
  - **`Sclera_Mat.001` (Before v11):** `{"base_color": {"value": [1.0, 1.0, 1.0]}, "roughness": {"value": 0.0}, "metallic": {"value": 0.0}, "transmission": {"value": 1.0}, "ior": {"value": 1.5}, "alpha": {"value": 0.027202}}`
  - **`Sclera_Mat.001` (After v12):** `{"base_color": {"value": [1.0, 1.0, 1.0]}, "roughness": {"value": 0.0}, "metallic": {"value": 0.0}, "transmission": {"value": 1.0}, "ior": {"value": 1.5}}`

---

### Fix 4 — Direct Non-Shader Nodes (`TEX_IMAGE`) Connected to `Material Output.Surface`
- **Commit:** `222c11f` (`core/shader_graph: improve SSS, Fresnel glass, Mix Shader, and passthrough classification`)
- **Affected Asset:** `Vintage Bicycle` (`32783d96-735c-4365-9bf4-818b9fc39727`), material `Reflektor.001`
- **Root Cause:**
  In Blender, connecting a `ShaderNodeTexImage` directly to `Material Output.Surface` is valid (Cycles implicitly evaluates it as an surface emission/albedo texture). `_classify_fallback_shader()` looked for an input socket named `"Color"` on the `TEX_IMAGE` node itself (which has no `"Color"` *input*), so it silently dropped the image texture and emitted `[0.8, 0.8, 0.8]` clay.
- **Evidence (`Vintage Bicycle` → `Reflektor.001` in `kitbridge.json`):**
  - **Before (v11):** `{"base_color": {"value": [0.8, 0.8, 0.8]}, "roughness": {"value": 0.5}, "metallic": {"value": 0.0}}`
  - **After (v12):** `{"metallic": {"value": 0.0}, "roughness": {"value": 0.5}, "base_color": {"texture": "textures/outro_lightroom_3-198.jpg", "colorspace": "srgb", "channel": "rgb"}}`

---

### Fix 5 — `Mix Shader` Leaf Inspection, Group-Input Pass-Through Defaults, and `blend_method` Resolution
- **Commits:** `222c11f` (`shader_graph.py`) and `62fe186` (`kb_export.py`)
- **Affected Assets:**
  - `Modern House` (`264b47f4-a1ee-4c31-9ee9-4c0e8dc1a98b`): `Sand-Finish`, `plaster`, `mq_WS211_STUCCO-Sand-Finish`, `mq_CC210_ARCH-Ridged`, `Plastic`
  - `Perfume Bottle` (`0e486dc1-3a6f-4cd3-bf80-1df96ef270e8`): `GLASS`
  - `Robot KUKA AGILUS with Rig animate` (`45eeaaf4-55ae-4a19-973f-3eb45fde20aa`): `Kette`, `Ketten-verbinder`, `Kabel-gelb`, `Kabel-Orange`, `Kabel-Schwarz`, `Kabel-Grün`, `Kabel-Blau`, `Gumi.001`
- **Root Causes:**
  1. In `Modern House`, 4 stucco/plaster/concrete materials use a node group (`Concrete_PBR`) with an internal pass-through link `NodeGroupInput.Metallic -> NodeGroupOutput.Metallic`. On the group node, `Metallic` was unlinked with `default_value = 0.0`. `_manual_inline()` ignored `NodeGroupInput -> NodeGroupOutput` links when `ext_in.is_linked` was `False`, leaving `Principled BSDF.Metallic` at `1.0` (from the inner template) instead of copying `ext_in.default_value = 0.0`. Furthermore, `_resolve_blend_method()` kept `blend_method = "blend"` even after the baked `alpha` channel resolved to constant `1.0`.
  2. In `Perfume Bottle`, `GLASS` mixes a `Metallic BSDF` (opaque gold label) with a `Glass BSDF` (clear glass bottle). `MIX_SHADER` classification previously never included `"transmission"` in `sockets_to_bake`, making the entire perfume bottle opaque.
  3. In `Robot KUKA AGILUS` and `Modern House` (`Plastic`), `MIX_SHADER` unconditionally added `"normal"` and `"metallic"` to `sockets_to_bake` even when all leaf BSDFs had unlinked `Normal` sockets and identical constant `Metallic = 0.0`. Because curved geometry without flat faces has slight tangent-normal variation across UV islands, `_detect_constant_bake` (`tol = 0.0005`) saved 8 bogus `*_normal.png` files and performed 17 unnecessary Cycles bakes.
- **Evidence (`Modern House`, `Perfume Bottle`, `Robot KUKA AGILUS` in `kitbridge.json`):**
  - **`Modern House` → `Sand-Finish` (and `plaster`, `mq_WS211_STUCCO-Sand-Finish`, `mq_CC210_ARCH-Ridged`):**
    - **Before (v11):** `blend_method: "blend"`, `channels.metallic: {"value": 1.0}`
    - **After (v12):** `blend_method: "opaque"`, `channels.metallic: {"value": 0.0}`
  - **`Perfume Bottle` → `GLASS`:**
    - **Before (v11):** `blend_method: "opaque"`, `baked_channels: ["base_color", "metallic", "roughness", "normal"]` (no `transmission`)
    - **After (v12):** `blend_method: "blend"`, `baked_channels: ["base_color", "metallic", "roughness", "normal", "transmission"]`, `channels.transmission: {"texture": "textures/GLASS_transmission.png", "colorspace": "raw", "channel": "r"}`
  - **`Robot KUKA AGILUS` (conversion time **80.94s → 60.35s**):**
    - **Before (v11):** `Kette`, `Kabel-gelb`, `Kabel-Orange`, `Kabel-Schwarz`, `Kabel-Grün`, `Kabel-Blau`, `Gumi.001` baked `['base_color', 'roughness', 'normal']`; `Ketten-verbinder` baked `['base_color', 'metallic', 'roughness', 'normal']`.
    - **After (v12):** All 8 materials skip the bogus `normal` bake (`baked_channels: ['base_color', 'roughness']`) and extract `metallic: {"value": 0.0}` directly.

---

### Fix 6 — Nested `ShaderNodeGroup` Inside `Add Shader` / `Mix Shader` Baking Solid White (`Monstera plant`)
- **Commit:** `62fe186` (`blender_scripts/kb_export: fix nested group bakes, HDR emission, and disp_as_bump GPU setup`)
- **Affected Asset:** `Monstera plant` (`65ea3e91-b091-4f2c-97a4-d118c594a537`), material `Daun` (78,765 of 84,190 polygons)
- **Root Cause:**
  In `Monstera plant`, the leaf material `Daun` has top-level `Add Shader(Translucent BSDF, Mix Shader(ShaderNodeGroup, Transparent BSDF))`. In `_bake_material_channels()`, `bake_mat = pristine` only checked whether the *immediate* node connected to `Material Output.Surface` was a `ShaderNodeGroup` (`p_surf.links[0].from_node.bl_idname != "ShaderNodeGroup"`). Because the top node was `ShaderNodeAddShader`, it used the un-flattened `pristine` material for baking; `_build_channel_emission_branch()` then encountered the nested `ShaderNodeGroup`, could not find Principled BSDF sockets on it, and wired fallback constants (`base_color = [0.8, 0.8, 0.8]` + `Translucent BSDF` `[0.8, 0.8, 0.8]` summed to `[1.0, 1.0, 1.0]`, and `roughness = 0.5 + 0.5 = 1.0`), turning the entire plant solid white!
- **Fix & Evidence (`Monstera plant` → `Daun` in `kitbridge.json`):**
  Added `_surface_shaders_have_no_groups()` so `pristine` is only used when no `ShaderNodeGroup` sits anywhere in the `Mix/Add Shader` surface hierarchy, and updated `_build_channel_emission_branch()` to ignore `BSDF_TRANSLUCENT` on non-emission channels in `ADD_SHADER` and average scalar channels instead of summing `> 1.0`.
  - **Before (v11):**
    ```json
    {
      "baked_channels": ["normal"],
      "channels": {
        "base_color": {"value": [1.0, 1.0, 1.0]},
        "metallic": {"value": 0.0},
        "roughness": {"value": 1.0},
        "normal": {"texture": "textures/Daun_normal.png", "colorspace": "raw", "strength": 1.0, "flip_green": false},
        "translucency": {"value": 1.0}
      }
    }
    ```
  - **After (v12):**
    ```json
    {
      "baked_channels": ["base_color", "roughness", "normal"],
      "channels": {
        "base_color": {"texture": "textures/Daun_base_color.png", "colorspace": "srgb", "channel": "rgb"},
        "metallic": {"value": 0.0},
        "roughness": {"texture": "textures/Daun_roughness.png", "colorspace": "raw", "channel": "r", "invert": false},
        "normal": {"texture": "textures/Daun_normal.png", "colorspace": "raw", "strength": 1.0, "flip_green": false},
        "translucency": {"value": 1.0}
      }
    }
    ```

---

### Fix 7 — Unconfigured CPU Cycles Bake & Missing `bump_in_normal` on `material` Assets with `disp_as_bump`
- **Commit:** `62fe186` (`blender_scripts/kb_export: fix nested group bakes, HDR emission, and disp_as_bump GPU setup`)
- **Affected Assets:**
  - `Onyx marble` (`b55555d2-1fb1-4540-8b26-a676c2cf4a9b`): **9.21s → 4.43s** (-52%)
  - `Gold metal (gold)` (`5a932113-9a23-422e-8dc9-5466576fe601`): **11.49s → 4.83s** (-58%)
  - `Beige linen fabric` (`23c9cd3e-6892-4c29-add4-0cbaf429f451`): **9.46s → 4.66s** (-51%)
- **Root Cause:**
  In `run_export()`, `any_bakes_needed` was only set when `cls.sockets_to_bake` was non-empty. For `material` assets whose channels were all classified as `DIRECT` (so `cls.sockets_to_bake == []`) but had a `displacement` channel with `displacement_method in ("BUMP", "BOTH")`, `_bake_material_channels(..., on_plane=True)` dynamically appended `"normal"` to `channels_to_bake` without `_configure_cycles_for_baking()` ever having been called! Consequently, Blender baked at the `.blend` file's default CPU settings (128 samples on CPU instead of 16 samples on OptiX/CUDA GPU). Additionally, on `Onyx marble`, the displacement texture was constant across the plane, so `_detect_constant_bake` skipped writing `normal.png` and left `bump_in_normal` unset on `displacement`, causing C4D to wire a flat displacement texture as bump.
- **Evidence (`Onyx marble` in `kitbridge.json`):**
  - **Before (v11, 9.21s):** `"displacement": {"texture": "textures/onyx_Displacement.jpg", "colorspace": "raw", "midlevel": 0.5, "scale": 0.01, "method": "BUMP"}`
  - **After (v12, 4.43s):** `"displacement": {"texture": "textures/onyx_Displacement.jpg", "colorspace": "raw", "midlevel": 0.5, "scale": 0.01, "bump_in_normal": true, "method": "BUMP"}`

---

### Fix 8 — HDR Emission Clipped to `[1.0, 1.0, 1.0]` (`strength: 1.0`) During 8-bit Bake (`Bell Portable Moss Table Lamp`)
- **Commit:** `62fe186` (`blender_scripts/kb_export: fix nested group bakes, HDR emission, and disp_as_bump GPU setup`)
- **Affected Asset:** `Bell Portable Moss Table Lamp` (`30deac90-8710-4453-a053-d41645e31ea5`), material `Material.001`
- **Root Cause:**
  `Material.001` is an `Emission` shader with `Blackbody(4500K)` color and a `ColorRamp` on `Strength`. During `EMIT` baking, `_bake_material_channels()` used an 8-bit image buffer (`float_buffer=False`), which clamped all linear RGB values `> 1.0` to `[1.0, 1.0, 1.0]` and lost both the warm 4500K color tint and the HDR emission strength.
- **Evidence (`Bell Portable Moss Table Lamp` → `Material.001` in `kitbridge.json`):**
  - **Before (v11):** `"emission": {"value": [1.0, 1.0, 1.0], "strength": 1.0}`
  - **After (v12):** `"emission": {"value": [1.0, 0.872686, 0.744538], "strength": 2.42857}`

---

### Fix 9 — Missing Fallback Channels on Unused Material Slots
- **Commit:** `62fe186` (`blender_scripts/kb_export: fix nested group bakes, HDR emission, and disp_as_bump GPU setup`)
- **Affected Assets:**
  - `Bonsai tree` (`ae0a2d0b-2a1b-41df-bbf1-3c73c0a5a106`): `MOSS_hair`
  - `Character` (`8d13b980-caf4-4efd-869e-44ffd324f16e`): `Skin_nail_Proxy.001`
  - `Robot KUKA AGILUS with Rig animate` (`45eeaaf4-55ae-4a19-973f-3eb45fde20aa`): `Material.007`
- **Root Cause:**
  When a material sits in an object's material slot but 0 polygons are assigned to that slot index, `_bake_material_channels()` returns early (`no faces use it`). Any channel that had been queued in `cls.sockets_to_bake` (`base_color`, `roughness`, `metallic`) was left completely absent from `cls.channels` in `kitbridge.json`.
- **Evidence (`Robot KUKA AGILUS` → `Material.007` in `kitbridge.json`):**
  - **Before (v11):** `"channels": {"specular": {"value": 0.5}, "ior": {"value": 1.5}}`
  - **After (v12):** `"channels": {"specular": {"value": 0.5}, "ior": {"value": 1.5}, "base_color": {"value": [0.8, 0.8, 0.8]}, "metallic": {"value": 0.0}, "roughness": {"value": 0.5}}`

---

## 3. Problems Found Strategy / Items Not Fixed (with Evidence)

1. **Direct Texture Extraction Using Non-UV Coordinates (`Generated` / `Object`) When Mesh `UVMap` Has Non-Normalized Tiling (`Apartment Building` — `d48241bb-48b0-4883-8415-d4cd991480a4`)**
   - **Evidence:** In `Apartment Building`, the `Curtain` material uses `Texture Coordinate (Generated) -> Mapping (scale=[1,1,1]) -> Image Texture (curtain_diff.jpg)`. Meanwhile, the curtain mesh's `UVMap` layer has UV coordinates spanning `[-14.3, 12.1]` (authored for tiled cloth weaves rather than 0..1 normalized space). Because `shader_graph._extract_uv_chain()` accepts `TEX_COORD` regardless of whether its output socket is `"UV"`, `"Generated"`, or `"Object"`, `Curtain` is classified as `DIRECT` using `UVMap` instead of baking `Generated` coordinates into `UVMap` (or setting a projection mode).
   - **Why Not Changed Tonight:** Forcing `BAKE` whenever `TEX_COORD` uses `"Generated"` or `"Object"` would trigger full Cycles bakes on many architectural/furniture box-mapped textures whose meshes have overlapping or multi-tile UVs (`[-14, 12]`), which cannot be baked cleanly into the `0..1` UV tile without re-unwrapping the mesh and breaking other materials on the same object. Proper handling requires creating a dedicated C4D cubic/spatial projection on the Texture Tag rather than baking into `0..1` UVs.

2. **Multi-UV Scale Conflict on Single Material (`Vintage Lace Curtain` — `c5529a99-e37c-47a6-a79c-e9b7665bf91b`)**
   - **Evidence:** Material `Lace` uses `UVMap` with `Mapping(scale=[0.8, 0.8, 0.8])` on its base color/alpha/normal textures (`lace_fabric_curtain_*.png`) and `Mapping(scale=[28.0, 28.0, 28.0])` on a fine micro-detail bump texture. `select_dominant_uv()` emits the warning:
     `Material uses 2 different UV/Mapping transforms across textures; keeping the most common (map='UVMap', scale=[0.8, 0.8]).`
   - **Why Not Changed Tonight:** The manifest schema (`SCHEMA_VERSION = 1`) stores a single material-level `uv` dictionary (`scale`, `offset`, `rotation`) shared across channels rather than per-channel UV transforms. Changing this would require a schema & C4D material-builder change across Redshift, Arnold, and Standard importers.
