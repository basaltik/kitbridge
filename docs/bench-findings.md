# Benchmark findings — run 1 (2026-11-07, 30 assets, C4D 2027 + Redshift)

Median SSIM 0.948, median ΔE 5.4. Wood, stone, fabric, plants, furniture: SSIM ≥ 0.99, ΔE ≈ 1.
Details: `~\blenderkit_data\kitbridge_bench\report.html`. How to re-run: `dev/bench/README.md`.

| # | Problem | Evidence | Likely cause | Where to fix |
|---|---------|----------|--------------|--------------|
| 1 | **Pink/magenta material** | Wood Veneer Walnut (c0390d07…): ΔE 36.9; C4D sphere is magenta with a pattern | Texture files exist and the manifest has base_color/normal/roughness, so the colour is a normal map (or wrong channel) on base colour, or RS shows its "invalid texture" colour | `c4d_import/materials/redshift.py` / `base.py` — check which file lands on which port for this package |
| 2 | **Displacement not rendered** | Wood Fluted Panel (4d6c7a5a…): Blender sphere is deeply fluted, C4D smooth; displacement is baked and in the manifest | RS displacement needs the object's RS Object tag with tessellation + displacement on; the material preview sphere / import path may not add it, or the displacement node isn't connected | `redshift.py: apply_displacement_object_tag`, displacement node wiring, units (m → cm) |
| 3 | **Lights inside assets are dropped** | Opaque glass table lamp (3d83f702…): Blender shade glows, C4D dark; no emission in manifest | The glow is a Blender light object (+ translucent "plastique translucide" Mix Shader). FBX export / import skip lights | `kb_export.py`: export lights into the manifest (type, colour, power, size, transform); importer: create RS/Arnold/Standard lights |
| 4 | **UDIM textures unsupported** | Urban Loft Apartment (0c4f61c0…): building body flat pink instead of brick | Manifest warns "UDIM texture … not supported" → body material has no textures | `kb_export.py` (collect all UDIM tiles), material builders (RS/Arnold both support `<UDIM>` tokens) |
| 5 | Brushed steel too dark | Bosch range (SSIM 0.766) | Metal roughness/anisotropy or coat mapping; needs a closer look | material builders |
| 6 | Glass silhouettes differ (benchmark artefact) | Glass doors/handrails/pots: IoU 0.05–0.74 | Cycles gives refractive glass alpha 1 (refracted world), Redshift alpha 0 | Bench: enable Cycles *Transparent Glass* (`film_transparent_glass`) so both treat glass the same; then re-judge glass colour |

Order: 1 (likely a plain bug), 2, 4, 3, 5; 6 before judging any glass change.
