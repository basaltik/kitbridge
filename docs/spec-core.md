# Kitbridge — core spec (milestone 1)

Kitbridge is a Cinema 4D plugin that brings BlenderKit assets into C4D, modeled on
BlenderKit's official Maya/Rhino plugins (see `docs/research-bk.md`, sources in `research/`).
License: GPL-3.0-or-later. Code adapted from `bk_maya` / `bk_client` keeps its copyright header
plus a note "Adapted for Kitbridge".

## Hard constraints

- **Python 3.9 syntax floor** (C4D 2023.0 ships 3.9.1; 2024+ ship 3.11). No `match`, no
  `X | Y` unions evaluated at runtime, no `dict | dict`, no `str.removeprefix`-era-only tricks
  beyond 3.9. `from __future__ import annotations` is fine. Check with `vermin -t=3.9-`.
- **Stdlib only.** C4D has no pip and no Qt. No `requests`, no PySide.
- **`kitbridge/core/` never imports `c4d` or `maxon`.** Everything in core must run under plain
  CPython so it is unit-testable and usable from the dev CLI.
- **Never block the C4D main thread for network I/O.** Core exposes a background poller thread
  that pushes finished tasks into a `queue.Queue`; the C4D side drains it from a timer.
- **Never spawn `sys.executable`** (inside C4D it is `Cinema 4D.exe`). Only spawn `bk_client`
  and `blender`.
- Windows first, but keep macOS paths working (C4D users are often on Mac). Linux: best effort.

## Layout

```
kitbridge/                      repo root
  plugin/kitbridge/             what users drop into C4D's plugins folder
    kitbridge.pyp               C4D entry point (milestone 2, not now)
    kitbridge/                  python package
      __init__.py               __version__
      core/
        paths.py                data dirs, cache dirs
        client.py               bk_client lifecycle + HTTP endpoint wrappers
        tasks.py                background /report poller + dispatcher
        auth.py                 OAuth2 PKCE login, token storage, refresh
        secret_store.py         OS credential vault (WinCred / macOS security / 0600 file)
        search.py               search URL builder + result normalisation
        blender.py              find Blender (incl. every Steam library), version probe
      client/                   bundled bk_client binaries (fetched, git-ignored)
  dev/
    fetch_client.py             downloads bk_client release zip (v1.13.x) into plugin/kitbridge/client/
    cli.py                      manual test CLI (no C4D): find-blender | login | whoami | search <q> | logout
  tests/                        pytest, pure python, no network
```

## Module requirements

### paths.py
- Global dir: `~/blenderkit_data` (shared with the Blender add-on, like bk_maya does — the
  client binary copy and its settings live there).
- Kitbridge cache: `~/blenderkit_data/kitbridge_cache/<assetBaseId>_<resolution>/`.
- Log dir for client logs.

### client.py  (adapt `bk_maya/core/client_lib.py`)
- Locate bundled binary for OS/arch under `plugin/kitbridge/client/vX.Y.Z/`, copy it to
  `~/blenderkit_data/client/bin/vX.Y.Z/` before running (same reason as bk_maya).
- Port list exactly `("62485","65425","55428","49452","35452","25152","5152","1234")` (OAuth
  whitelist). Probe first, attach to a running client if one answers; else spawn with
  `--port --server https://www.blendkit.com --software "Cinema 4D" --pid <pid> --version <ver>`
  and the proxy/ssl flags, `CREATE_NO_WINDOW` on Windows. On exit code 43 try next port.
- **Every report body includes `"software": "Cinema 4D"` and `"software_version"`** (bk_maya's
  bug: it omits this and registers as Blender).
- Wrappers for: report, unsubscribe_addon (on plugin unload — do NOT call /shutdown, other apps
  may share the client), oauth2/verification_data, refresh_token, oauth2/logout,
  profiles/get_user_profile, blender/asset_search, blender/asset_download, blender/cancel_download,
  run_blender_script, executable/get, executable/set, settings/get.
  Read `bk_client/client/*.go` for the exact request structs of asset_download and
  run_blender_script (Rhino uses them: `blendkit-rhino/src/Infra/*.cs`).
- `software_version` is injected by the host (C4D passes `c4d.GetC4DVersion()`); core takes it
  as a constructor argument.
- Short timeouts (connect 0.2s / read 0.5s for report). Respawn after 10 consecutive failures.

### tasks.py
- `Poller(client, interval=0.2)`: daemon thread calling report; puts tasks into a queue.
  `drain(max_items)` returns pending tasks for the main thread.
- `Dispatcher`: register handlers per `task_type`; `dispatch(tasks)`. Must include a slot for
  `bkclientjs/get_asset` (website "Get this" → C4D), plus login, token_refresh, oauth2/logout,
  search, thumbnail_download, asset_download progress, run_blender_script progress, client_status,
  settings. Unknown types are logged at debug level, not errors.
- Clean `stop()`.

### secret_store.py / auth.py  (adapt bk_maya)
- Service name `Kitbridge`. Windows CredMan via ctypes, macOS `security` CLI, fallback JSON 0600
  in `~/blenderkit_data/kitbridge/secrets.json`.
- PKCE S256 login exactly like bk_maya (same CLIENT_ID — it is BlenderKit's public addon client;
  note in a comment that we should ask BlenderKit for our own client id before public release).
- Token refresh 3 days before expiry, non-blocking.
- Manual API-key paste supported.
- Never log tokens.

### search.py
- Build search URL for asset types `model`, `material`, `hdr` (+ category, free_first,
  bookmarks, page_size, page). `addon_type` param: check what bk_client/server accept; if unsure,
  mirror bk_maya and leave a TODO.
- Normalise results into plain dicts: id, assetBaseId, name, assetType, isFree, canDownload,
  thumbnail urls/paths, available resolutions, files.

### blender.py
- Search order: user pref path → bk_client executable registry → `$BLENDER_PATH` → PATH →
  **every Steam library** (read `SteamPath` from `HKCU\Software\Valve\Steam`, parse
  `steamapps/libraryfolders.vdf`, check `steamapps/common/Blender/blender(.exe)`; macOS:
  `~/Library/Application Support/Steam/steamapps/...Blender.app`) → standard install globs
  (Program Files, LocalAppData, scoop, macOS /Applications) → pick highest version.
- `probe(path)` runs `blender -b --factory-startup --python-expr` printing version, timeout 30s,
  returns version tuple or None. Minimum supported Blender: 4.2 (verify against bk_client tools'
  requirements and note it).
- Publish the found path to bk_client via executable/set.

### dev/cli.py
- `python dev/cli.py find-blender` / `login` (opens browser, waits for login task, stores token)
  / `whoami` / `search chair --type model` (prints top 10: name, free/paid, resolutions) /
  `logout`. Uses core exactly as the plugin will.

### tests
- PKCE verifier/challenge, search URL builder, vdf parser (with a sample file), port fallback
  (fake HTTP server on localhost), task dispatcher routing, secret_store fallback backend.
