# Kitbridge — fidelity benchmark (Blender vs Cinema 4D)

Goal: measure how closely an imported asset in Cinema 4D matches the same asset rendered in
Blender, so material/geometry changes can be judged by numbers instead of by eye. Dev tooling only:
everything lives in `dev/bench/`, nothing ships in the plugin. Dev-only Python deps are fine here
(Pillow, numpy) — list them in `dev/bench/requirements.txt`.

## Pipeline
```
dev/bench/assets.json        the asset set (see below)
dev/bench/fetch.py           make sure every asset has a cached Kitbridge package (uses core.pipeline,
                             same as `dev/cli.py get`); prints a table of package dirs; resumable
dev/bench/blender_render.py  runs INSIDE Blender (blender -b --factory-startup <source .blend>
                             --python blender_render.py -- <params.json>), writes ref.png
dev/bench/run_blender.py     drives blender_render.py for every package (direct subprocess, Steam
                             Blender found via core.blender), skips up-to-date results
dev/bench/c4d_render.py      runs INSIDE Cinema 4D (exec'd by the reviewer through MCP); for each
                             package: new document, import via kitbridge.c4d_import.import_package,
                             same lighting/camera, render, write c4d_<renderer>.png, close document
dev/bench/compare.py         scores every pair and writes report.html (side by side + diff heatmap)
out dir: <cache_root>/../kitbridge_bench/<base_id>/   (outside the repo; git-ignore nothing needed)
```

## Fair, identical setup on both sides
- **Resolution** 512×512, square.
- **Lighting**: one HDRI only, the cached Blendkit HDR package
  `2f62aff5-3a19-43e1-9c6b-f52a898bf676_1024` ("Dramatic Twilight Sunset Landscape"); path from its
  `kitbridge.json` (`hdr`). Intensity 1, rotation 0. Hidden from camera: background = flat 18 % grey
  (0.18 linear) so silhouettes compare cleanly — Blender: world shader mix by "Is Camera Ray";
  Redshift: dome "background" off / environment colour; document in the script how it's done.
- **Camera**: perspective, 50 mm on a 36 mm sensor (horizontal FOV ≈ 39.6°). Frame the asset's
  world bounding box (meshes only): target = bbox centre; direction = from the asset's
  front-right-above, i.e. in **Blender** coords the unit vector of (1, −1, 0.6) (Blender front is −Y,
  up +Z); distance so the bbox's bounding sphere fills 90 % of the frame. The C4D side computes the
  same from the imported objects in C4D coords. The FBX export maps Blender (x, y, z) to C4D
  (x, z, −y)·100 (cm) — **verify this mapping** by printing both bboxes for one asset; the reviewer
  will check it in C4D. Put the mapping in one function used everywhere.
- **Colour**: compare display-referred sRGB. Blender: view transform **Standard**, look None,
  exposure 0, gamma 1. C4D/Redshift: make the output sRGB with no tonemapping (document the exact
  RS settings you set: colour management / view transform "sRGB" or "Un-tone-mapped"); if the
  setting differs between C4D versions, read it from the render settings description at runtime.
- **Quality**: Blender Cycles 128 samples + denoise (OpenImageDenoise), GPU if available. Redshift
  progressive/bucket at comparable quality with denoise on.
- **Material assets**: render on a UV sphere 1 m diameter at the origin (Kitbridge already ships a
  preview sphere in material packages — use the package geometry on both sides).
- **HDR assets**: skip.

## Scores (compare.py)
Per pair, on the 512² sRGB images, converted to float:
- `ssim`: mean SSIM on luminance (implement with numpy, 7×7 Gaussian window, standard constants;
  no scikit-image dependency).
- `delta_e`: mean CIE76 ΔE in Lab over pixels where either image's alpha/mask says "object"
  (mask = pixels differing from the flat background by > 2/255 in either image).
- `mask_iou`: IoU of the two object masks (catches framing/scale/missing-geometry errors).
- `mean_rgb` of the object pixels for both images (catches global brightness/colour shifts).
Report: table sorted worst-first by ssim, thumbnails side by side + |diff| heatmap, and a summary
line (median ssim, median ΔE, count of assets with mask_iou < 0.9). Also write `results.json`.

## Asset set (assets.json)
~30 entries `{base_id, name, type, tier}`: ~20 models, ~10 materials, mixed free/Full Plan,
chosen to cover: glass, metal, car paint, fabric, wood, plastic, emissive, alpha-cut foliage,
procedural-only materials, multi-material models, instanced/scattered objects. Pick them with
`dev/cli.py search` queries and record the query used for each in a `why` field.

## Practical rules
- Everything resumable and idempotent; never re-download or re-render without `--force`.
- Timeouts on every subprocess; one asset failing must not stop the run (record the error).
- No changes to `plugin/` code in this task.
- Python 3.9-compatible syntax in files that run inside C4D (`c4d_render.py`); the others may use
  the dev Python.
