# Kitbridge

Kitbridge is a Cinema 4D plugin that brings [BlenderKit](https://www.blenderkit.com/) 3D models, materials, and HDRIs into Cinema 4D via the official `bk_client` background daemon and headless Blender conversion recipes.

**Status:** Work in progress (Milestone 1 — core library & dev CLI).

## Requirements

- **Cinema 4D:** 2023.0 or newer (Python 3.9+ stdlib only)
- **Blender:** 4.2 or newer (installed locally via Blender Foundation installer, Steam, or package manager; used headlessly for `.blend` conversion)
- **OS:** Windows (primary), macOS, Linux (best effort)

## Dev Setup

1. Fetch the prebuilt `bk_client` v1.13.x release binaries and conversion tools:
   ```bash
   py -3 dev/fetch_client.py
   ```
2. Run the manual test CLI (outside Cinema 4D):
   ```bash
   py -3 dev/cli.py find-blender
   py -3 dev/cli.py login
   py -3 dev/cli.py whoami
   py -3 dev/cli.py search chair --type model
   py -3 dev/cli.py logout
   ```
3. Run tests and Python 3.9 syntax checks:
   ```bash
   py -3 -m pip install pytest vermin
   py -3 -m pytest -q
   py -3 -m vermin -t=3.9- --no-tips --violations plugin dev
   ```

## Asset usage

Kitbridge is unofficial and does not grant any rights to BlenderKit assets: each asset keeps its
BlenderKit license. You may use assets in your own projects, but you may not resell or
redistribute them, including the files Kitbridge converts. Paid assets need your own
subscription. See [DISCLAIMER.md](DISCLAIMER.md).

## License

GPL-3.0-or-later for the Kitbridge code. See [LICENSE](LICENSE). This license covers the code
only, never the assets.
