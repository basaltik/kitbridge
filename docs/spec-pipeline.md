# Kitbridge — download & convert pipeline spec (milestone 2a)

Goal: given a search result, produce a renderer-agnostic, cached package on disk that the C4D side
(milestone 2b) can import without Blender. Still **no `c4d` imports** in anything here. Same hard
constraints as `spec-core.md` (stdlib only, Python 3.9 floor; the Blender-side script runs in
Blender's own Python, so it may use `bpy` and Blender's Python version, but keep it Blender 4.2+
compatible and check API differences up to 5.x — e.g. Principled BSDF socket names changed in 4.0).

## Package format (the contract between pipeline and C4D importer)

`~/blenderkit_data/kitbridge_cache/<assetBaseId>_<resolution>/`
```
source/            downloaded raw file(s) (.blend / .hdr / .exr)
textures/          every image the package references (unpacked, original or baked)
geometry.fbx       models + material assets (material assets: a 2x2 m UV plane, or a 1 m UV sphere —
                   pick the sphere, it previews better)
kitbridge.json     manifest, see below
```
`kitbridge.json` (schema_version 1):
```jsonc
{
  "schema_version": 1,
  "asset": {"id": "...", "base_id": "...", "name": "...", "type": "model|material|hdr",
            "resolution": "2048", "author": "...", "license": "royalty_free|cc_zero", "source_url": "..."},
  "units": {"meters_per_unit": 1.0, "up_axis": "Y"},   // geometry.fbx is exported Y-up, meters
  "geometry": "geometry.fbx",                          // null for hdr
  "hdr": "textures/xxx.exr",                           // only for hdr
  "objects": [{"name": "...", "materials": ["MatA", "MatB"]}],   // per-object slot order
  "materials": [{
    "name": "MatA",
    "blend_method": "opaque|clip|blend",
    "channels": {
      // every key optional; each value is either {"value": number|[r,g,b]} or a texture ref
      "base_color":   {"texture": "textures/a_col.jpg", "colorspace": "srgb", "channel": "rgb"},
      "metallic":     {"texture": "textures/a_orm.png", "colorspace": "raw", "channel": "b"},
      "roughness":    {"texture": "textures/a_orm.png", "colorspace": "raw", "channel": "g", "invert": false},
      "specular":     {"value": 0.5},
      "ior":          {"value": 1.5},
      "normal":       {"texture": "textures/a_nrm.png", "colorspace": "raw", "strength": 1.0, "flip_green": false},
      "bump":         {"texture": "...", "colorspace": "raw", "strength": 0.2, "distance": 0.01},
      "displacement": {"texture": "...", "colorspace": "raw", "midlevel": 0.5, "scale": 0.05},
      "alpha":        {"texture": "...", "channel": "a"},
      "emission":     {"texture": "...", "colorspace": "srgb", "strength": 1.0},
      "transmission": {"value": 0.0},
      "coat":         {"value": 0.0}, "coat_roughness": {"value": 0.03},
      "sheen":        {"value": 0.0}, "subsurface": {"value": 0.0},
      "ambient_occlusion": {"texture": "..."}
    },
    "uv": {"map": "UVMap", "scale": [1, 1], "offset": [0, 0], "rotation": 0.0},  // from a Mapping node if present
    "baked_channels": ["base_color"],   // channels that were produced by baking
    "warnings": ["..."]                 // human-readable: what could not be represented
  }]
}
```
All paths relative to the package dir. Values are linear floats; colors linear RGB.

## Blender side: `plugin/kitbridge/kitbridge/blender_scripts/kb_export.py`

Run as `blender -b --factory-startup <source.blend> --python kb_export.py -- <params.json>`.
Params: output dir, asset type, bake resolution, max texture size. Progress via stdout lines
`KB_PROGRESS <0..1> <msg>`, `KB_WARNING <msg>`, `KB_DONE <manifest path>`, `KB_ERROR <msg>`
(and non-zero exit on error).

Steps:
1. Open file, make everything visible/selectable, realize geometry-node instances and apply
   modifiers (evaluated mesh), flatten armature rigs like bk_client's `export_usd.py` does
   (`_flatten_rig_to_single_root`, read it). Only objects in the asset (BlenderKit marks the asset
   root via `blenderkit` custom props / asset collection — check how bk_client's export_usd.py
   decides; ignore cameras/lights unless the asset is only lights).
2. Unpack/collect every image into `textures/` (handle packed, relative and UDIM-less only; UDIM
   → warning). Respect `max texture size` by downscaling copies (export_glb.py has code for it).
3. For each material, walk the node tree from the Material Output surface → (flatten node groups
   first, reuse bk_client's approach) → Principled BSDF. For each socket resolve the upstream chain.
   **Recognised direct patterns** (no bake): Image Texture → socket; Image → Separate RGB/Color →
   socket (channel); Image → Normal Map → Normal; Image → Bump → Normal; Image → Invert → socket
   (invert=true); Image → Displacement node → Material Output displacement; Mapping/UV Map/Texture
   Coordinate(UV) nodes before images (→ `uv` block; if different images use different mappings,
   keep the most common and warn); Image Alpha → Alpha; constant socket values.
   **Anything else** (procedural textures, ColorRamp, Mix, Math, AO, layer weights, Mix Shader of two
   BSDFs, non-UV coordinates, Glass/Emission-only shaders mapped to the nearest equivalent…) →
   that socket is **baked**.
4. Baking (Cycles, `bake_type='EMIT'` trick: temporarily route the socket's upstream value into an
   Emission shader; for normal use `bake_type='NORMAL'`): bake on a representative object that uses
   the material (the largest by area); if it has no UV map, create one with Smart UV Project and
   apply that UV to every object using the material (write a warning). Material assets bake on
   the preview sphere. Image size = bake resolution param. Colorspace: base color/emission sRGB,
   everything else Non-Color, saved as PNG 16-bit for non-color, 8-bit sRGB for color. Use GPU if
   available (`cycles` device prefs) else CPU; samples low (e.g. 16) since it's emission-only.
   Mix Shader of two Principled BSDFs → bake every channel (warn).
5. Export `geometry.fbx`: Y up, -Z forward, apply unit scale, mesh smoothing = face, tangents
   off, embed textures off, path_mode 'STRIP' (materials come from the manifest, not FBX).
   Keep object names and material slot names identical to the manifest.
6. Write `kitbridge.json`, print `KB_DONE`.

## Python side: `plugin/kitbridge/kitbridge/core/pipeline.py`

- `fetch_asset(client, asset, resolution, api_key, on_progress) -> Package` orchestrates:
  cache check (valid `kitbridge.json` with same schema_version → return immediately, no network,
  no Blender) → download via bk_client (`blender/asset_download`, follow how blendkit-rhino
  waits for it via report tasks; or the `wrappers/*` blocking calls bk_maya uses — pick whichever
  works for paid assets, test both if unsure) → for `hdr`: copy/unpack the image (bk_maya has an
  HDR-in-.blend unpack script), write a minimal manifest → for model/material: run kb_export.py
  through `blender.py` (direct subprocess from a worker thread, parse KB_* lines, kill on cancel,
  timeout configurable default 600 s; Blender exits by itself when done).
- Runs in a worker thread; progress callback receives (stage, fraction, message). Cancel support.
- Errors raise a `PipelineError` with a user-facing message (no subscription / not logged in /
  Blender not found / conversion failed + last 30 lines of Blender log saved to the package dir as
  `convert.log`).
- `Package` = small dataclass: path, manifest dict, `is_cached`.
- Cache maintenance helpers: `list_cached()`, `cache_size()`, `remove(base_id)`.

## dev/cli.py additions
- `get <query or asset_base_id> [--type model|material|hdr] [--index N] [--res 2048]` → search,
  pick result N (default 0), run the pipeline, print the package path, manifest summary
  (objects, materials, which channels textured / baked / values, warnings) and timings.
- `cache list|size|clear`.

## Tests (no network, no Blender)
- Manifest schema validation helper + test with a sample manifest.
- Cache-hit path returns without calling the client (mock client).
- KB_* stdout line parser.
- Socket-chain classifier: factor the pure logic (pattern → "direct"/"bake") into a module that
  does not import bpy, operating on a tiny duck-typed node graph, and unit-test it.
