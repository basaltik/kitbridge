# Kitbridge fidelity benchmark

Renders the same Blendkit assets in Blender (reference, original `.blend` materials) and in
Cinema 4D (imported with Kitbridge) under identical camera and lighting, then scores how alike
they are. Spec: `docs/spec-bench.md`. Dev-only deps: `py -3 -m pip install -r dev\bench\requirements.txt`.
Output goes outside the repo: `~\blenderkit_data\kitbridge_bench\<base_id>\`.

1. `py -3 dev\bench\fetch.py` — downloads/converts every asset in `assets.json` at 512 px
   (resumable). The bench HDRI must be cached too:
   `py -3 dev\cli.py get 2f62aff5-3a19-43e1-9c6b-f52a898bf676 --type hdr --res 1024`.
2. `py -3 dev\bench\run_blender.py` — Cycles reference renders → `ref.png`.
3. Inside Cinema 4D (Script Manager or MCP):
   ```python
   import sys; sys.path[:0] = [r"<repo>\plugin\kitbridge", r"<repo>\dev\bench"]
   import c4d_render
   c4d_render.main([<package dirs>], r"~\blenderkit_data\kitbridge_bench", <bench HDRI package dir>)
   ```
   → `c4d_redshift.png`. Each render blocks Cinema 4D for a few seconds.
4. `py -3 dev\bench\compare.py --root "%USERPROFILE%\blenderkit_data\kitbridge_bench"` →
   `results.json` + `report.html` (worst first).

## Decisions that keep the comparison fair
- **Background**: both sides render with a transparent background (HDRI lights the scene but is
  invisible to the camera: Blender `film_transparent`, Redshift dome "Camera" contribution 0).
  `compare.py` composites both onto the same 18 % grey and takes the object mask from alpha.
- **Colour**: Blender view transform *Standard*. Cinema 4D project OCIO: render space
  *scene-linear Rec.709-sRGB*, view *Un-tone-mapped*, display *sRGB*. `RenderDocument` returns
  scene-linear float pixels and `BaseBitmap.Save` writes them without the display transform, so
  `c4d_render.py` encodes sRGB itself (same formula as Blender) and writes the PNG with zlib.
  Calibration: a plain wood material on a sphere scores SSIM 0.998 / ΔE 0.5.
- **Axes**: Kitbridge's FBX import maps Blender (x, y, z) to C4D (x, z, y) — Blender's front (−Y)
  is C4D's front (−Z). `(x, z, −y)` showed the back of an asymmetric asset (IoU 0.39 → 1.0 after
  the fix). Camera direction: Blender (1, −1, 0.6), 50 mm / 36 mm, bounding-box half diagonal fills
  90 % of the frame, on both sides.
- **HDRI orientation**: dome heading 0° matches Blender's world mapping (checked 0/90/180/270°).
- **Material assets**: the `.blend` has no mesh, so the reference uses the package's preview
  sphere with the original material re-assigned (the FBX importer makes simplified copies).
