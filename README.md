<p align="center"><img src="assets/logo/kitbridge-icon.svg" width="96" alt="Kitbridge"></p>

# Kitbridge

Kitbridge is a free, open-source Cinema 4D plugin that brings
[BlenderKit](https://www.blenderkit.com/) models, materials and HDRIs into Cinema 4D. You search
and download inside Cinema 4D; Kitbridge converts the `.blend` files with your own copy of
Blender running in the background and builds native Redshift or Arnold materials.

> **Unofficial.** Kitbridge is a community project. It is not affiliated with or endorsed by
> BlenderKit, Maxon or the Blender Foundation. Assets keep their BlenderKit license — see
> [Asset usage](#asset-usage).

![Kitbridge inside Cinema 4D: models, materials, HDRIs and settings](docs/img/ui.png)

![The same assets rendered in Blender and, after Kitbridge, in Cinema 4D](docs/img/fidelity.png)

*The same BlenderKit assets under the same light: Blender's own render on the left, the Kitbridge
import rendered by Redshift on the right.*

**Status:** early public release. It works, it is tested, and it still has rough edges — see
[Known limitations](#known-limitations). Bug reports are welcome.

## Why

BlenderKit is one of the largest asset libraries around, with tens of thousands of free models,
materials and HDRIs — but its assets are `.blend` files with Cycles shader networks, and its
add-ons exist for Blender, Maya and Rhino only. Getting one asset into Cinema 4D by hand means
opening Blender, exporting FBX, and rebuilding every material node by node. Kitbridge does that
whole trip for you in one click and keeps as much of the original look as the target renderer
allows.

## What it does

- Search BlenderKit for models, materials and HDRIs, filter by free / Full Plan, and log in with
  your BlenderKit account.
- Paste an `asset_base_id:…` code copied from an asset's page on blenderkit.com to jump straight
  to it.
- Import at 512, 1K, 2K, 4K, 8K or original texture resolution; downloads are cached, so an asset
  converts once.
- Materials are rebuilt as node materials for **Redshift** or **Arnold**. If neither is installed,
  Kitbridge falls back to Standard materials.
- Shader networks that have no direct counterpart (procedural textures, mixed shaders, layered
  setups) are baked to textures with Cycles, on your GPU when one is available.
- Displacement, bump, normal maps, UDIM tiles, subsurface, translucency, emission and lights are
  carried over.

## How it works

1. **Search and download** go through BlenderKit's own background client (`bk_client`), the same
   one their Blender add-on uses, with your BlenderKit login.
2. **Conversion** runs your Blender without a window. A script applies modifiers, reads every
   material's node tree and sorts each channel into one of three cases: a plain value, an image
   texture that can be reused as is, or something only Cycles can evaluate. The last group is
   baked to textures. Geometry goes out as FBX, and everything Kitbridge learned about the
   materials goes into a manifest next to it.
3. **Import** loads the FBX into the open Cinema 4D document and builds node materials for the
   renderer in use from the manifest: base colour, metalness, roughness, normal and bump,
   displacement, transmission, subsurface, emission, opacity.

The converted package is cached, so the same asset imports instantly the next time. When a newer
Kitbridge converts better, cached assets are converted again by themselves on their next import.

## Using it

- Open **Kitbridge** from the main menu bar and log in (the button opens blenderkit.com in your
  browser). Search works without logging in; downloading needs an account.
- Pick **Models**, **Materials** or **HDRIs**, type what you need, click a thumbnail. Progress is
  shown on the thumbnail: download, conversion, import.
- Right-click a thumbnail for *Import*, *Open on blenderkit.com* and *Remove from cache*.
- The **Downloaded** tab lists what is already on disk and works offline.
- The gear opens the settings: texture resolution, which renderer to build materials for
  (follows the document by default), the download folder, the Blender Kitbridge found, and the log.
- An HDRI becomes a dome light for the current renderer.

## Requirements

- **Windows 10 or 11.** The code is written to be portable, but macOS and Linux are untested and
  there is no installer for them yet.
- **Cinema 4D 2023 or newer.** This version is tested on 2024 and 2027. Earlier builds also ran
  on 2023; reports from 2023, 2025 and 2026 are welcome.
- **Redshift or Arnold** for full-quality materials. Arnold has had less testing than Redshift.
- **Blender 4.2 or newer**, installed anywhere (blender.org installer, Steam, …). Kitbridge finds
  it by itself and only ever starts it in the background; you can also point to it in the plugin
  settings.
- A BlenderKit account for downloads. Paid assets need your own BlenderKit subscription.

## Install

1. Download `KitbridgeSetup-<version>.exe` from the
   [Releases](../../releases) page and run it. It finds every Cinema 4D on the machine and
   installs into its user plugins folder; no administrator rights are needed.
2. Start Cinema 4D and open **Kitbridge** from the main menu bar (also under **Extensions**).

The installer is not code-signed, so Windows SmartScreen may warn about an unknown publisher:
choose *More info → Run anyway*. To remove Kitbridge, use *Apps → Installed apps* in Windows
settings, or run the installer with `/uninstall` (add `/purge` to also delete downloaded assets,
settings and the login).

Downloaded and converted assets live in `%USERPROFILE%\blenderkit_data\kitbridge_cache`.

## Known limitations

- Conversion is a translation between two renderers, not a copy. On the project's benchmark of 30
  assets the Cinema 4D render matches the Blender one closely (median SSIM 0.987), but glass with
  caustics, light fixtures enclosed in glass and volumetric effects still differ visibly.
- Procedural materials become textures: they tile like an image and no longer follow the object's
  shape the way a 3D procedural does in Blender.
- Baking uses the UV map: scene-dependent nodes (Ambient Occlusion, Bevel) are left to the
  renderer.
- Rigs, animation, particles, hair and geometry-nodes setups that cannot be applied are not
  transferred. Scenes, brushes and add-ons from BlenderKit are not supported.
- The first import of a heavy asset can take a minute or two while Blender converts it.

## Troubleshooting

- **"Blender not found".** Install Blender 4.2 or newer, or set its path in the Kitbridge
  settings.
- **An import fails or looks wrong.** The log is at
  `%USERPROFILE%\blenderkit_data\kitbridge\kitbridge.log` (also reachable from the settings).
  Please attach it to a bug report together with the asset's link on blenderkit.com and your
  Cinema 4D and renderer versions.
- **A material looks different from the BlenderKit preview.** Previews are rendered in Cycles
  with BlenderKit's own lighting. Compare under the same HDRI before judging; if it still differs,
  that is worth an issue.
- **The first import of an asset is slow.** Blender starts, converts and bakes; a model with
  dozens of procedural materials at 2K takes about a minute on a recent GPU. Lower resolutions
  bake faster.

## Roadmap

- macOS support and an installer for it.
- Light fixtures: a real light where an emitter sits behind glass.
- An Arnold pass over the benchmark, and Octane / V-Ray materials if there is interest.
- Scenes and brushes from BlenderKit.

Ideas and bug reports go to [Issues](../../issues); pull requests are welcome.

## Asset usage

Kitbridge does not grant any rights to BlenderKit assets: each asset keeps its BlenderKit license.
You may use assets in your own projects, but you may not resell or redistribute them, including
the files Kitbridge converts. See [DISCLAIMER.md](DISCLAIMER.md).

## Development

The plugin is plain Python (standard library only, Python 3.9 syntax) in `plugin/kitbridge`.

```bash
py -3 dev/fetch_client.py
```

fetches the BlenderKit client binaries that the plugin talks to. After that:

```bash
py -3 -m pytest -q
```

runs the tests, `py -3 dev/cli.py --help` drives search, login and conversion outside Cinema 4D,
and `py -3 dev/build_installer.py` builds the installer into `dist/`. To run from source, add
`plugin` as a plugin search path in Cinema 4D's preferences.

`dev/bench` renders the same assets in Blender and in Cinema 4D and scores how alike they are;
`docs/bench-findings.md` is the running log of what that found.

## Credits and license

Kitbridge is licensed under **GPL-3.0-or-later** ([LICENSE](LICENSE)); the license covers the
code only, never the assets.

Parts of the code derive from BlenderKit's GPL add-ons, and the installer ships BlenderKit's
`bk_client` (GPL, source at [github.com/BlenderKit](https://github.com/BlenderKit)), which handles
login, search and downloads. Kitbridge currently signs in through BlenderKit's shared public
client application; a dedicated one has been requested.
