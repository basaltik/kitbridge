# Kitbridge — Cinema 4D importer spec (milestone 2b)

Input: a package produced by `core/pipeline.py` (see `spec-pipeline.md`, manifest `kitbridge.json`).
Output: the asset in the active C4D document with native materials for the document's renderer.
This is the first code that imports `c4d` / `maxon`; keep it in `plugin/kitbridge/kitbridge/c4d_import/`
and keep `core/` free of it.

Real node/port ids from C4D 2027 are in `docs/c4d-node-ports-2027.json` — use them, do not guess.
Redshift default material graph = `node.output` + `standardmaterial`; Arnold default graph = only
`com.autodesk.arnold.material` (end node; inputs `shader`, `displacement`, `alpha`).
Known plugin ids (2027): RS Light 1036751 (param Type 10000, dome texture params 11001/12000 range —
verify), RS Object tag 1036222, Arnold Light 1030424, Arnold Sky 1034624, Arnold Parameters tag 1029989.

## Constraints
- Python 3.9 floor (C4D 2023.0). Only the low-level maxon graph API that exists since 2023:
  `GetNodeMaterialReference()`, `CreateDefaultGraph/CreateEmptyGraph`, `BeginTransaction`,
  `AddChild`, `GetInputs().FindChild`, `SetDefaultValue`, `Connect`, `Commit`. **No
  `maxon.GraphDescription`** (2024.0+ only). Wrap version-specific bits in one small compat module.
- Everything runs on the main thread (the C4D side calls it from a timer / command). One undo step
  per import (`doc.StartUndo()` / `AddUndo` / `EndUndo`), then `c4d.EventAdd()`.
- Never raise into C4D with a traceback for expected failures: return an `ImportResult`
  (created objects, materials, warnings, errors) — the UI shows it.

## Modules
```
c4d_import/
  __init__.py          import_package(doc, package_dir, renderer=None, options=None) -> ImportResult
  renderer.py          detect doc renderer: redshift / arnold / standard (fallback, legacy channels)
  geometry.py          FBX → objects
  materials/
    base.py            renderer-agnostic plan: manifest material → list of "channel ops"
    redshift.py        build RS node material from the plan
    arnold.py          build Arnold node material from the plan
    standard.py        legacy c4d.Mmaterial fallback (base color, roughness via reflectance, normal, alpha)
  hdr.py               dome light per renderer
  compat.py            version helpers (C4D version, Python, maxon API differences)
dev/c4d_selftest.py    run inside C4D (exec'd by a tool, prints one JSON report, see below)
```

## geometry.py
- Load `geometry.fbx` into a **temporary** document (`c4d.documents.LoadDocument` with FBX import
  prefs set via the FBX importer plugin's container: scale so 1 m in the file = 100 cm, import
  materials ON so we get its texture/selection tags, import cameras/lights OFF, normals ON).
  Check the FBX importer plugin id (1026370) and its pref ids in the 2023–2027 SDK; if a pref id is
  version-specific, guard it.
- Move all top-level objects into a new Null named after the asset (`asset.name`), insert into the
  active doc at the origin, select it. Keep object names.
- FBX importer creates texture tags (+ polygon selection tags for multi-material objects) pointing at
  its own materials named like the manifest materials. Keep those tags but relink each to our
  Kitbridge material with the same name; then drop the FBX materials. If a name doesn't match,
  warn and keep the FBX material.
- Material assets: geometry is a preview sphere — option `apply_to_selection` (default True when
  the user had objects selected): assign the material to the selected objects instead of
  inserting the sphere.

## materials/base.py — the plan
Translate each manifest material into a renderer-agnostic plan: for each channel either a value or
a texture op `{path(absolute), colorspace, channel r/g/b/a/rgb, invert, uv transform}`.
Mapping notes:
- base_color, metallic, roughness, emission (+strength), alpha/opacity, transmission (+ior, roughness
  shared), coat (+coat_roughness), sheen, subsurface (weight only), normal (strength, flip_green),
  bump (strength, distance), displacement (midlevel, scale; meters → convert to cm for C4D!),
  ambient_occlusion → ignore with a warning (both renderers compute AO).
- Blender "specular" (IOR level, default 0.5) → leave reflection weight at 1 and use `ior`.
- Same texture file used for several channels (packed ORM) → one texture node, split channels.
- Material dedupe: same manifest material name + same package → reuse existing material in the doc
  (match by a hidden tag/BaseContainer id like `kitbridge:<base_id>:<name>`), don't duplicate on
  re-import.
- Material names in C4D: `<material name>` ; keep unique within doc.

## materials/redshift.py
- `com.redshift3d.redshift4c4d.class.nodespace`, start from CreateDefaultGraph (output + standard
  material), set values on the standardmaterial ports, add `texturesampler` nodes (`tex0` is a
  bundle with path/colorspace sub-ports — see the json), `rscolorsplitter` for single channels,
  `rsmathinv` for invert, `bumpmap` (inputtype: tangent-space normal vs height) into `bump_input`,
  `displacement` node into `output.displacement` and add/enable displacement on an RS Object tag on
  the imported objects (warn that tessellation is on).
- Transmission → `refr_weight`, `refr_color` = base color, `refr_roughness`; opacity → `opacity_color`.
- Colorspace values: sRGB for color, "RS_INPUT_COLORSPACE_RAW" (verify exact string) for data.

## materials/arnold.py
- `com.autodesk.arnold.nodespace`, CreateDefaultGraph gives `com.autodesk.arnold.material`; add
  `standard_surface` → material.shader. `image` nodes (`filename`, `color_space` "sRGB"/"Raw",
  `output.r/g/b` for single channel), `complement` for invert, `normal_map` → standard_surface.normal,
  `bump2d` for height bump, displacement → material.displacement.
- Opacity in Arnold also needs the object non-opaque: add an Arnold Parameters tag (1029989) with
  opaque OFF on objects that use a material with alpha/transmission (find the param id; if not
  found, warn).

## materials/standard.py
Legacy fallback for Standard/Physical: color, reflectance layer roughness, normal, alpha. Minimal.

## hdr.py
- Redshift: RS Light (1036751) type Dome with the EXR as texture. Arnold: Arnold Light (1030424)
  type skydome + image. Standard: Sky object + material with luminance. One per import, named after
  the asset; if a previous Kitbridge dome exists, ask via option `replace_existing_hdr` (default True).

## dev/c4d_selftest.py
Executed inside C4D via `exec(open(path).read())` with `doc` available. It:
1. adds `plugin/kitbridge` to `sys.path`, reloads the kitbridge modules (importlib.reload),
2. creates a NEW document (never touches the user's), sets its renderer to the one requested by a
   variable `KB_RENDERER` ("redshift"/"arnold"),
3. imports every package found under `~/blenderkit_data/kitbridge_cache/` (or the list in
   `KB_PACKAGES`),
4. prints ONE JSON line `KB_SELFTEST {...}` with per package: objects created, materials created
   with per-material node count + which channels got connected, warnings, errors with tracebacks.
Keep the document open so it can be rendered/inspected.
