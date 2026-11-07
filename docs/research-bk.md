# BlenderKit (`bk_maya` / `bk_client` / `blendkit-rhino`) Source-Code Research for Kitbridge (C4D)

Checked-out commits in `research/`:
- `bk_maya`: `https://github.com/BlenderKit/bk_maya.git` (submodules: `bk_client` @ `967c713`, `bk_maya/bk_proxor` @ `84b2ad6`)
- `bk_client`: `https://github.com/BlenderKit/bk_client.git` (`v1.13.0`)
- `blendkit-rhino`: `https://github.com/BlenderKit/blendkit-rhino.git` (`v0.1.7`)

---

## a. `bk_client`: Distribution, Startup, Ports, Registration, Endpoints & Polling

### 1. Distribution & Installation
- **Prebuilt binaries per OS/arch**: `bk_client/dev.py#L138-L155` (`BUILD_MATRIX`) cross-compiles Go (`go 1.25.8`, `bk_client/client/go.mod#L3`) into 7 standalone binaries:
  - `bk_client-windows-x86_64.exe`, `bk_client-windows-arm64.exe`, `bk_client-windows7-x86_64.exe`
  - `bk_client-macos-x86_64`, `bk_client-macos-arm64`
  - `bk_client-linux-x86_64`, `bk_client-linux-arm64`
- **Signing & packaging**: `.github/workflows/release.yml` in `bk_client` signs Windows binaries (`CN=BlenderKit s.r.o.`) and notarizes macOS binaries (`Apple Developer ID: BlenderKit s.r.o. (A839AY9877)`, hardened runtime), publishing `bk_client.zip` on GitHub Releases (`bk_client/dev.py#L438-L497`).
- **How `bk_maya` bundles it**: At release build time, `bk_maya/dev.py#L317-L474` (`download_prebuild_client_binaries`) resolves the latest release matching `CLIENT_VERSION = "v1.13"` (`bk_maya/bk_maya/core/global_vars.py#L32`), downloads `bk_client.zip` from `BlenderKit/bk_client` releases, extracts all OS binaries + `tools/` (`export_usd.py`, `export_glb.py`, `bk_mtlx/`) into `<addon>/client/vX.Y.Z/`, and writes `<addon>/client/RESOLVED_VERSION` (`bk_maya/dev.py#L204-L212`, `#L303-L315`).
- **Runtime copy before execution**: On first launch in a Maya session, `_ensure_client_binary_installed()` (`bk_maya/bk_maya/core/client_lib.py#L368-L417`) copies `<addon>/client/vX.Y.Z/<binary>` to `<global_dir>/client/bin/vX.Y.Z/<binary>` (default `~/blenderkit_data/client/bin/vX.Y.Z/`) via `shutil.copy2`, verifies file size, sets `0o755` on POSIX, and runs from `~/blenderkit_data` so plugin reinstalls never collide with a running process.

### 2. Port Selection & Startup
- **Candidate ports**: Fixed list `CLIENT_PORTS = ("62485", "65425", "55428", "49452", "35452", "25152", "5152", "1234")` (`bk_maya/bk_maya/core/client_lib.py#L63-L72`). The order is load-bearing because BlenderKit's OAuth server whitelists `http://localhost:<port>/consumer/exchange/` for these ports.
- **Discovery vs. spawn (`ensure_running`)**: `bk_maya/bk_maya/core/client_lib.py#L554-L621` first probes all `CLIENT_PORTS` via `_ping(port)` (`#L475-L497`, `GET http://127.0.0.1:{port}/v1.13/report` with JSON body and `connect_timeout=0.05s, read_timeout=0.10s`). If a `bk_client` is already running (e.g. started by Blender or another Maya instance), Maya attaches to that port and sets `_process = None`.
- **Spawning (`_start_process`)**: If no port responds, `_start_process()` (`bk_maya/bk_maya/core/client_lib.py#L510-L551`) launches the binary on `CLIENT_PORTS[_port_index % len]`:
  ```python
  cmd = [
      binary,
      "--port", port,
      "--server", global_vars.SERVER,          # "https://www.blendkit.com"
      "--proxy_which", prefs.prefs.proxy_which, # "SYSTEM" | "NONE" | "CUSTOM"
      "--proxy_address", prefs.prefs.proxy_address,
      "--trusted_ca_certs", "",
      "--ssl_context", ssl_context,            # "DEFAULT" | "DISABLED"
      "--version", f"{global_vars.ADDON_VERSION}.{global_vars.ADDON_BUILD}", # "3.20.0.260517"
      "--software", "Maya",
      "--pid", str(_app_id),                   # str(os.getpid())
  ]
  _process = subprocess.Popen(
      cmd, cwd=os.path.dirname(binary), stdout=log_fh, stderr=subprocess.STDOUT,
      creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
  )
  ```
  Logs go to `<global_dir>/client/default.log`. If the process exits early (e.g. exit code `43` = `rcServerStartSyscallEADDRINUSE` in `bk_client/client/main.go#L80`, `#L561-L571`), `ensure_running()` increments `_port_index` and retries.

### 3. Host Software Handshake & Registration
- **Startup registration**: Passing `--software Maya --pid <pid> --version <ver>` pre-populates `StartingSoftwareName` (`bk_client/client/main.go#L97-L100`), used as the `Origin-Name` HTTP header fallback (`bk_client/client/utils.go#L65-L79`).
- **Heartbeat registration (`/report` vs `/godot/report`)**:
  - `bk_client` supports two polling protocols (`bk_client/client/main.go#L3729-L3741`):
    1. `GET /{vapi}/report` (`protocolReport`, `main.go#L622-L754`): Used by Blender, `bk_maya`, and `blendkit-rhino`. Decodes `GetReportData` (`bk_client/client/structs.go#L65-L70`):
       ```go
       type GetReportData struct {
           AppID           int    `json:"app_id"`
           APIKey          string `json:"api_key"`
           AddonVersion    string `json:"addon_version"`
           BlenderVersion  string `json:"blender_version"`
           PlatformVersion string `json:"platform_version"`
           ProjectName     string `json:"project_name"`
           Software        string `json:"software"`
           SoftwareVersion string `json:"software_version"`
       }
       ```
       **Gotcha in `bk_maya`**: `_minimal_report_data()` (`bk_maya/bk_maya/core/client_lib.py#L683-L691`) sends `{"app_id": os.getpid(), "api_key": api_key, "addon_version": "3.20.0", "platform_version": "Windows-...", "project_name": ...}` and omits `"software"`. In `reportSoftwareIdentity()` (`bk_client/client/main.go#L723-L740`), if `data.Software == ""` on `/report`, `bk_client` defaults `softwareName` in `AvailableSoftwares[app_id]` to `"Blender"`! A C4D plugin should explicitly include `"software": "Cinema 4D"` and `"software_version": "<c4d_ver>"` in `_minimal_report_data()`.
    2. `GET /{vapi}/godot/report` (`protocolGodotReport`, `main.go#L4108-L4167`): Used by Godot/embedders that want `bk_client` to handle browser-initiated downloads directly.
- **First-contact side effects**: When `Tasks[data.AppID] == nil`, `SubscribeNewApp()` (`bk_client/client/main.go#L744-L754`) creates the per-PID task map and fires goroutines: `CheckHealth`, `FetchDisclaimer`, `FetchCategories`, and (if `APIKey != ""`) `FetchUnreadNotifications`, `GetBookmarks`, `GetUserProfile`.
- **Lifespan & Shutdown**:
  - If no `/report` arrives from *any* app within `ReportTimeout = 3 * time.Minute` (`bk_client/client/main.go#L49`, `#L596-L606`), `bk_client` exits.
  - `monitorAvailableSoftwares()` (`bk_client/client/main.go#L4034-L4080`) drops any `protocolReport` app silent for `120s` (`60s` for `protocolGodotReport`); when `len(AvailableSoftwares) == 0`, `bk_client` exits in `0.1s`.
  - On Maya plugin unload, `client_lib.shutdown()` (`bk_maya/bk_maya/core/client_lib.py#L648-L674`) sends `GET /{vapi}/shutdown` with `_minimal_report_data()` and terminates `_process` if Maya spawned it (note: `bk_client` also exposes `GET /{vapi}/unsubscribe_addon` in `main.go#L855-L872` to detach one `app_id` without killing other connected hosts).

### 4. Every HTTP Endpoint Used by `bk_maya`

#### A. Loopback `bk_client` Endpoints (`http://127.0.0.1:{port}/v1.13/...`)
Note: `bk_client` uses Go's `http.Server`, which reads JSON request bodies even on `GET` requests. `bk_maya` sends JSON bodies on `GET` via `urllib.request.Request(..., data=raw, method="GET")` (`bk_maya/bk_maya/core/client_lib.py#L439-L472`).

| Endpoint & Method | Caller in `bk_maya` | Request JSON Shape | Response JSON Shape / Behavior |
|---|---|---|---|
| `GET /v1.13/report` | `client_lib._ping` (`#L475`), `get_reports` (`#L865`) | `{"app_id": int, "api_key": str, "addon_version": str, "platform_version": str, "project_name": str}` | `200 OK` JSON array of `Task` objects (`structs.go#L48-L58`). Always prepends `task_type="client_status"` and appends `task_type="settings"`. |
| `GET /v1.13/shutdown` | `client_lib.shutdown` (`#L648`) | `_minimal_report_data` | `200 OK` `"Shutting down..."`; exits `bk_client` in 0.1s (`main.go#L774`). |
| `POST /v1.13/oauth2/verification_data` | `client_lib.send_oauth_verification_data` (`#L717`) | `_minimal_report_data` + `{"code_verifier": str, "state": str}` | `200 OK` `"ok"`; stores PKCE verifier/state in `bk_client` memory (`login.go#L112`). |
| `GET /v1.13/refresh_token` | `client_lib.refresh_token` (`#L727`) | `_minimal_report_data` + `{"refresh_token": str}` | `200 OK` `"ok"`; spawns goroutine POSTing to `{Server}/o/token/`, emits `"login"` task on `/report` (`login.go#L61`). |
| `GET /v1.13/oauth2/logout` | `client_lib.oauth2_logout` (`#L736`) | `_minimal_report_data` + `{"refresh_token": str}` | `200 OK`; revokes access & refresh tokens at `{Server}/o/revoke_token/`, emits `"oauth2/logout"` task (`login.go#L204`). |
| `GET /v1.13/profiles/get_user_profile` | `client_lib.get_user_profile` (`#L743`) | `_minimal_report_data` | `200 OK`; fetches `GET {Server}/api/v1/me/`, emits `"profiles/get_user_profile"` task (`main.go#L1929`). |
| `POST /v1.13/ratings/send_rating` | `client_lib.send_rating` (`#L756`, used by `bookmarks.py#L99`) | `{"addon_version": str, "platform_version": str, "app_id": int, "api_key": str, "asset_id": str, "rating_type": "bookmarks"\|"quality"\|"working_hours", "rating_value": float}` | `200 OK`; sends `PUT` (or `DELETE` if value==0 and type!="bookmarks") to `{Server}/api/v1/assets/{asset_id}/rating/{rating_type}/` with `{"score": val}`, emits `"ratings/send_rating"` task (`main.go#L2160`). |
| `GET /v1.13/ratings/get_bookmarks` | `client_lib.get_bookmarks` (`#L776`) | `_minimal_report_data` | `200 OK`; fetches `GET {Server}/api/v1/search/?query=bookmarks_rating:1`, emits `"ratings/get_bookmarks"` task (`main.go#L2501`). |
| `POST /v1.13/blender/asset_search` (alias `/assets/search`) | `client_lib.asset_search` (`#L789`) | `SearchTaskData` (`structs.go#L123`): `{"app_id": int, "api_key": str, "addon_version": str, "platform_version": str, "urlquery": str, "tempdir": str, "page_size": int, "scene_uuid": str, "is_validator": bool, "blender_version": "0.0.0"}` | `200 OK` `{"task_id": "<uuid>"}`. Runs `doAssetSearch` (`main.go#L984`): retries HTTP 429 up to 4x with backoff, emits `"search"` task (`result` = `SearchResults`), then spawns `parseThumbnailsFromSearch` (`maxConcurrentThumbs = 6`, emitting `"thumbnail_download"` tasks for `small`, `full`, `photo_full`, `wire_full`). |
| `POST /v1.13/blender/asset_prxc_download` (alias `/assets/download_prxc`) | `client_lib.asset_prxc_download` (`#L831`) | `{"app_id": int, "api_key": str, "addon_version": str, "platform_version": str, "asset_base_id": str, "download_url": str, "file_path": str, "scene_uuid": str}` | `200 OK` `{"task_id": "<uuid>"}`. Resolves signed URL and downloads `.prxc` to `file_path`, emits `"prxc_download"` task (`main.go#L1506`). |
| `GET /v1.13/settings/get` | `client_lib.get_settings` (`#L926`) | None | `200 OK` `Snapshot` JSON (`{"revision": int, "shared": {"server": str, "usage_data_opt_out": bool}, "executables": {"blender": [{"path": str, "version": str, "args": []}]}}`, `settings_handlers.go#L67`). |
| `POST /v1.13/settings/set` | `client_lib.set_settings` (`#L931`) | `{"server": str?, "usage_data_opt_out": bool?}` | `200 OK` updated `Snapshot` (`settings_handlers.go#L78`). |
| `POST /v1.13/settings/set_variable` | `client_lib.set_variable` (`#L943`) | `{"plugin": str, "variable": str, "value": str}` | `200 OK` updated `Snapshot` (`settings_handlers.go#L109`). |
| `POST /v1.13/executable/set` | `client_lib.set_executable` (`#L951`) | `{"name": "blender", "path": str, "version": str, "args": list}` | `200 OK` updated `Snapshot`; persists to `~/blenderkit_data/client/settings/v1.13.json` (`settings_handlers.go#L204`). |
| `GET /v1.13/executable/get?name=blender[&version=...]` | `client_lib.get_executable` (`#L967`) | Query params `name`, `version` | `200 OK` `{"name": "blender", "executables": [{"path": str, "version": str, "args": []}]}` sorted highest-version first (`settings_handlers.go#L166`). |
| `POST /v1.13/wrappers/get_download_url` | `bg_download.py#L153-L202`, `download.py#L1197-L1206` | `DownloadData` (`structs.go#L172`): `{"app_id": int, "addon_version": str, "platform_version": str, "download_dirs": [], "asset_data": {"name": str, "id": str, "assetType": str, "resolution": str, "available_resolutions": [], "files": [{"fileType": str, "downloadUrl": str}]}, "PREFS": {"api_key": str, "scene_id": str, "resolution": str}}` | **Synchronous/blocking** `200 OK` `{"can_download": bool, "download_url": "<signed_s3_or_cdn_url>", "file_upload_size": int}` (`wrappers.go#L117-L176`). Note: `bk_client` calls `GET {file.DownloadURL}?scene_uuid={scene_id}` (`download.go#L294`) and probes candidate URLs via `HEAD`/`GET Range: bytes=0-0` (`download_sources.go#L73-L126`). |
| `POST /v1.13/wrappers/blocking_file_download` | `bg_download.py#L205-L218`, `download.py#L1215` | `{"url": "<signed_url>", "filepath": "<dest_abs_path>", "api_key": str}` | **Synchronous/blocking** `200 OK` streams file to `filepath` (`wrappers.go#L46-L113`). |

*(Note: `bk_client` also provides async `POST /v1.13/blender/asset_download`, `POST /v1.13/blender/cancel_download`, and `POST /v1.13/run_blender_script` [alias `/v1.13/tools/run`] — used by `blendkit-rhino`, whereas `bk_maya` uses `bg_download.py` + `wrappers/*` and spawns Blender directly via `blender_runner.py`.)*

#### B. Direct HTTPS Calls Made by `bk_maya` (Bypassing `bk_client`)
1. **Search URL construction** (`bk_maya/bk_maya/api/client.py#L168-L231`, `build_search_url`): builds the `urlquery` string passed inside `POST /v1.13/blender/asset_search`:
   ```
   https://www.blendkit.com/api/v1/search/?query=<q>+asset_type:<type>+order:_score[+free_first:True][+bookmarks_rating:1][+category_subtree:<cat>]&dict_parameters=1&page_size=24&addon_version=0.1.0&addon_type=maya
   ```
2. **Quality / Working-Hours rating submission** (`bk_maya/bk_maya/ui/asset_bar.py#L361-L398` -> `bk_maya/bk_maya/api/client.py#L130-L150`, `set_asset_rating`): sends `PUT https://www.blendkit.com/api/v1/assets/{asset_id}/rating/{rating_type}/` with form body `score={value}` and `Authorization: Bearer {api_key}`.
3. **Plugin self-updater** (`bk_maya/bk_maya/core/updates.py#L44-L162`): calls `GET https://api.github.com/repos/BlenderKit/bk_maya/releases/latest` (or `/releases?per_page=30` for prereleases).
4. **Dead/legacy direct methods in `bk_maya/bk_maya/api/client.py`**: `exchange_code_for_tokens`, `refresh_tokens`, `revoke_token`, `get_profile`, `get_asset_ratings`, `search`, `download_thumbnail` exist in `api/client.py` (using `ADDON_TYPE = "maya"`, `ADDON_VERSION = "0.1.0"`) and `resolve_signed_url`/`download_file` exist in `bg_download.py#L225-L295` as a fallback when `--port` is omitted, but normal `bk_maya` execution routes all of these through `bk_client`.

### 5. Polling & Task Dispatch Architecture
- **Timer**: `_ReportPoller` (`bk_maya/bk_maya/ui/asset_bar.py#L150-L190`) runs a `QTimer` every **200 ms** on the main Qt thread, calling `client_lib.get_reports(api_key)` (`connect_timeout=0.20s, read_timeout=0.50s`) and `client_lib.dispatch_tasks(tasks)` (`bk_maya/bk_maya/core/client_lib.py#L1153-L1272`).
- **One-shot task deletion**: In `reportHandlerDo()` (`bk_client/client/main.go#L677-L704`), `bk_client` iterates `Tasks[data.AppID]`, appends all tasks to `toReport`, and **immediately deletes** any task with `status == "finished"` or `status == "error"` (`main.go#L688-L690`). Each finished/error task is delivered on `/report` **exactly once**.
- **Task JSON shape** (`bk_client/client/structs.go#L48-L58`):
  ```json
  {
    "app_id": 12345,
    "task_id": "550e8400-e29b-41d4-a716-446655440000",
    "task_type": "search | thumbnail_download | prxc_download | login | token_refresh | oauth2/logout | profiles/get_user_profile | categories_update | ratings/get_bookmarks | ratings/send_rating | disclaimer | notifications | health_check | client_status | settings",
    "message": "human status",
    "message_detailed": "optional error/stdout detail",
    "progress": 0,
    "status": "created | finished | error",
    "data": { "...original request payload..." },
    "result": { "...endpoint-specific output..." }
  }
  ```
- **Failure recovery**: After `_RESPAWN_AFTER_FAILURES = 10` consecutive failed `/report` polls (~2s), `get_reports()` calls `_respawn_client()`, killing any hung child process and re-running `ensure_running()` (`bk_maya/bk_maya/core/client_lib.py#L882-L915`).

---

## b. Authentication Flow (OAuth, Token Storage, Refresh)

1. **Browser OAuth2 + PKCE (S256)** (`bk_maya/bk_maya/core/auth.py#L132-L138`, `#L344-L388`; `bk_client/client/login.go#L45-L198`):
   - `CLIENT_ID = "IdFRwa3SGA8eMpzhRVFMg5Ts8sPK93xBjif93x0F"` (`auth.py#L28`).
   - `auth.login()` calls `client_lib.ensure_running()`, generates a 128-char random alphanumeric `code_verifier`, computes `code_challenge = base64url_nopad(sha256(verifier))`, and generates `state = secrets.token_urlsafe(16)`.
   - Registers `(verifier, state)` with `bk_client` via `POST /v1.13/oauth2/verification_data`.
   - Opens the user's browser (`webbrowser.open`) to:
     ```
     https://www.blendkit.com/o/authorize?client_id=IdFRwa3SGA8eMpzhRVFMg5Ts8sPK93xBjif93x0F&response_type=code&state=<state>&redirect_uri=http://localhost:<port>/consumer/exchange/&code_challenge=<challenge>&code_challenge_method=S256
     ```
   - After user authorization, the browser redirects to `http://localhost:<port>/consumer/exchange/?code=...&state=...` (`bk_client/client/login.go#L133-L198`). `bk_client` verifies `state`, POSTs `grant_type=authorization_code`, `code`, `code_verifier`, `client_id`, `redirect_uri` to `https://www.blendkit.com/o/token/`, redirects the browser to `https://www.blendkit.com/oauth-landing/`, and enqueues a `task_type="login"` task (`status="finished"`, `result={"access_token": ..., "refresh_token": ..., "expires_in": ...}`) for every connected `appID` in `Tasks`.
2. **Token Storage** (`bk_maya/bk_maya/core/secret_store.py#L1-L415`, `auth.py#L227-L307`):
   - Stored as JSON `{"access_token": str, "refresh_token": str, "expires_in": int, "expires_at": float}` under service `"BlendkitMaya"`, key `"tokens"` using zero-dependency OS-native credential vaults:
     - **Windows** (`secret_store.py#L114-L207`): `ctypes` calls to `advapi32.dll` (`CredWriteW`, `CredReadW`, `CredDeleteW`, `CredFree`) with `CRED_TYPE_GENERIC = 1`, `CRED_PERSIST_LOCAL_MACHINE = 2`, target `"BlendkitMaya:tokens"`, UTF-16LE blob.
     - **macOS** (`secret_store.py#L214-L255`): `/usr/bin/security` CLI (`add-generic-password -U -s BlendkitMaya -a tokens -w <val>`, `find-generic-password -w ...`).
     - **Linux** (`secret_store.py#L262-L303`): `secret-tool` (`libsecret` CLI) with attributes `service BlendkitMaya account tokens`.
     - **Fallback** (`secret_store.py#L53-L65`, `#L310-L360`): `0o600` JSON file at `<MAYA_APP_DIR or ~/Documents/maya>/blendkit_secrets.json` (plus one-time migration from legacy `blendkit_auth.json` in `auth.py#L48-L84`).
   - **Manual API Key**: In `bk_maya/bk_maya/ui/settings_dialog.py#L586-L598`, pasting a manual token saves `{"access_token": manual, "refresh_token": "", "expires_in": 2592000, "expires_at": time.time() + 2592000}`.
3. **Token Refresh** (`bk_maya/bk_maya/core/auth.py#L310-L342`, `#L202-L221`):
   - `_REFRESH_RESERVE = 3 * 24 * 3600` (3 days before expiry).
   - Every call to `auth.get_api_key()` checks `if refresh and time.time() + _REFRESH_RESERVE >= expires_at:`. If within 3 days of expiry, it fires a background thread calling `client_lib.refresh_token(refresh, api_key=access)` (`GET /v1.13/refresh_token`) while immediately returning the still-valid `access_token` (or `None` if already expired).
   - `bk_client` POSTs `grant_type=refresh_token` to `{Server}/o/token/` (`login.go#L61-L108`) and emits a `"login"` (or `"token_refresh"`) task on `/report`. If refresh fails permanently (`status="error"`), `_on_token_refresh_failed` (`auth.py#L212-L221`) clears stored tokens and triggers a logout UI update.

---

## c. Download Flow for Non-Blender Hosts

### 1. Resolution Selection
- **Preference & mapping**: `prefs.resolution` (`bk_maya/bk_maya/core/prefs.py#L64`, `#L124-L132`) stores one of `"512"`, `"1024"`, `"2048"`, `"4096"`, `"8192"`, `"ORIGINAL"` (default `"2048"`).
- **Candidate priority list**: In `bk_maya/bk_maya/scripts/bg_download.py#L43-L101` (`_RESOLUTION_MAP` & `pick_file_entry`), the requested resolution maps to a fallback tuple of `fileType` values searched in `asset_data["files"]`:
  - `"512"` -> `("resolution_0_5K", "blend")`
  - `"1024"` -> `("resolution_1K", "resolution_0_5K", "blend")`
  - `"2048"` -> `("resolution_2K", "resolution_1K", "resolution_0_5K", "blend")`
  - `"4096"` -> `("resolution_4K", "resolution_2K", "resolution_1K", "resolution_0_5K", "blend")`
  - `"8192"` -> `("resolution_8K", "resolution_4K", "resolution_2K", "resolution_1K", "resolution_0_5K", "blend")`
  - `"ORIGINAL"` -> `("blend", "resolution_8K", "resolution_4K", "resolution_2K", "resolution_1K", "resolution_0_5K")`
- **Server-side equivalent in `bk_client`**: `GetResolutionFile()` (`bk_client/client/download.go#L238-L290`) maps `"resolution_0_5K": 512`, `"resolution_1K": 1024`, `"resolution_2K": 2048`, `"resolution_4K": 4096`, `"resolution_8K": 8192`, picks the closest available resolution by minimum `abs(target - r)`, and falls back to `"blend"` or `"gltf"` / `"gltf_godot"`.
- **Signed URL resolution**: Each entry in `asset_data["files"]` has a `downloadUrl` like `https://www.blendkit.com/api/v1/downloads/<file_uuid>/`. `bk_client` calls `GET <downloadUrl>?scene_uuid=<uuid>` (`bk_client/client/download.go#L294-L361`), which returns JSON with `filePath` and/or `filePaths: [{"url": "https://public.blenderkit.com/..."}]` (`download_sources.go#L16-L45`).

### 2. Where Files Land on Disk
- **Global data dir**: `<global_dir>` defaults to `~/blenderkit_data` (`bk_maya/bk_maya/core/prefs.py#L42`, `#L134-L137`).
- **Per-(asset, resolution) cache directory** (`bk_maya/bk_maya/core/download.py#L121-L157`, `get_asset_cache_dir`):
  ```
  <global_dir>/blendkit_maya_cache/<asset_BaseId_or_id>_<resolution>/
  ```
  Example: `~/blenderkit_data/blendkit_maya_cache/d5368c9d-092e-4319-afe1-dd765de6da01_2048/`.
- **Files inside an asset's cache directory** (`bk_maya/bk_maya/scripts/bg_download.py#L469-L547` & `bk_client/client/tools/export_usd.py`):
  - `<clean_name>_<id8>.blend` (or `.hdr` / `.exr` for HDRIs) — raw downloaded asset (`bg_download.py#L473-L476`)
  - `textures/` — unpacked texture files extracted from the `.blend` (`export_usd.py#L125-L227`)
  - `<clean_name>_<id8>.usd` — binary USD crate containing geometry, UV sets, and `UsdPreviewSurface` fallback (`export_usd.py#L1384-L1426`)
  - `materials/<MatName>.mtlx` — standalone MaterialX 1.39 files per material (`export_usd.py#L958-L1045`)
  - `<clean_name>_<id8>.usda` — thin ASCII USD wrapper that `subLayers` `./<stem>.usd` and `prepend references` `./materials/<Mat>.mtlx` onto `/root/_materials/<Mat>` (`export_usd.py#L1086-L1138`)
  - `result.json` — manifest written by `bg_download.py#L519-L541`:
    ```json
    {"asset_id": "...", "asset_name": "...", "asset_type": "model", "resolution": "2048", "raw_file": ".../.blend", "usd_file": ".../.usda", "artifacts": [".../.usd"], "textures_dir": ".../textures", "success": true, "error": null}
    ```
- **Temporary search thumbnails & `.prxc` proxy cache**:
  - Thumbnails: `<os.TempDir()>/bktemp_<username>/<assetType>_search/<filename>` (`bk_client/client/utils.go#L402-L430`, `bk_maya/bk_maya/core/search.py#L36-L46`).
  - Proxor (`.prxc`) wireframes: `<global_dir>/tmp/<assetType>_search/<assetBaseId>.prxc` (`bk_maya/bk_maya/ui/placement.py#L964-L980`).
- **Compare with `bk_client` native download layout (used by Blender & `blendkit-rhino`)**:
  - `<global_dir>/<plural_asset_type>/<slug[:16]>_<asset_id>/<slug>_<res>_<uuid>.<ext>` (`bk_client/client/utils.go#L306-L340`, `blendkit-rhino/src/Infra/DownloadService.cs#L48-L50`).

### 3. File Types Arriving per Asset Type
From `bk_maya/bk_maya/scripts/bg_download.py#L43-L101`, `bk_client/client/main.go#L1267-L1318`, `bk_client/client/download.go#L220-L290`, and `bk_maya/bk_maya/core/search.py#L22`:
- **`model` / `printable`**: `files[]` contains `"blend"`, `"resolution_0_5K"`, `"resolution_1K"`, `"resolution_2K"`, `"resolution_4K"`, `"resolution_8K"` (all `.blend` files on the CDN; `bk_client/client/main.go#L3486` also supports `"zip_file"`), plus `"prxc"` (`.prxc` quantized proxy mesh/wireframe for viewport drag preview), and thumbnails (`"thumbnail"`, `"photo_thumbnail"`, `"wire_thumbnail"`). Some assets also have `"gltf"` or `"gltf_godot"` entries (`bk_client/client/download.go#L220-L235`), though `bk_maya` ignores `"gltf"` and always downloads the `.blend` to convert via Blender (`bg_download.py#L75-L101`).
- **`material`**: `files[]` contains `"blend"` and `"resolution_*K"` (all `.blend` files containing the `bpy.data.materials` datablock, packed textures, and sometimes no mesh — see `export_usd.py#L1146-L1216`), plus thumbnails.
- **`hdr`**: `files[]` contains `"resolution_*K"` and `"blend"` entries whose signed URLs point directly to **`.hdr` or `.exr` image files** (not `.blend`). However, if an HDR arrives packed inside a `.blend` (header `BLENDER`), `bk_maya/bk_maya/core/download.py#L384-L539` (`_ensure_hdr_image_file`) runs an inline headless Blender script (`_HDR_UNPACK_BLENDER_SCRIPT`, `#L391-L445`) to unpack the first `IMAGE` with `.hdr`/`.exr` extension or `float_buffer`.
- **`scene` / `brush` / `nodegroup`**: Present in the BlenderKit API (`bk_client/client/utils.go#L330-L340`, `search.py#L140`) and delivered as `.blend` (`"blend"` / `"resolution_*K"`), **but `bk_maya` explicitly filters UI tabs to only `ASSET_TYPES = ["MODEL", "MATERIAL", "HDR"]`** (`bk_maya/bk_maya/core/search.py#L22`). `scene`, `brush`, `nodegroup`, and `printable` are not exposed or imported in `bk_maya`.

---

## d. Conversion Pipeline (Blender Discovery, CLI, Scripts, USD/MaterialX, Textures, Caching)

### 1. How `bk_maya` Finds Blender
`find_blender()` in `bk_maya/bk_maya/core/blender_runner.py#L254-L287` checks in order:
1. **Explicit caller override** (`preferred_path` / `--blender-path` passed from `prefs.blender_path`).
2. **Client-shared executable registry**: `_client_shared_blender()` (`blender_runner.py#L218-L237`) queries `client_lib.get_Cached_blender_path()` (populated from every `/report` `"settings"` task Snapshot) and `GET /v1.13/executable/get?name=blender`. If the Blender add-on or another host registered its Blender binary with `bk_client`, Maya reuses it automatically.
3. **`$BLENDER_PATH` environment variable** (`blender_runner.py#L272-L275`).
4. **`shutil.which("blender")`** on `PATH` (`#L277-L279`).
5. **OS-specific standard install globbing** (`_iter_candidate_paths()`, `blender_runner.py#L119-L215`), picking the highest version via `_version_key` (`#L94-L116`):
   - **Windows**: `%ProgramFiles%\Blender Foundation\Blender *\blender.exe`, `%ProgramFiles(x86)%\Blender Foundation\Blender *\blender.exe`, `%LOCALAPPDATA%\Programs\Blender Foundation\Blender *\blender.exe`, `%USERPROFILE%\scoop\apps\blender\current\blender.exe`, `C:\ProgramData\chocolatey\bin\blender.exe`, `%ProgramFiles(x86)%\Steam\steamapps\common\Blender\blender.exe`.
   - **macOS**: `/Applications/Blender*.app/Contents/MacOS/Blender`, `~/Applications/Blender*.app/Contents/MacOS/Blender`, `/opt/homebrew/bin/blender`, `/usr/local/bin/blender`.
   - **Linux**: `/usr/bin/blender`, `/usr/local/bin/blender`, `/snap/bin/blender`, `/var/lib/flatpak/exports/bin/org.blender.Blender`, `~/blender*/blender`, `/opt/blender*/blender`.
6. Once found, `_publish_blender_to_client()` (`blender_runner.py#L240-L251`) registers the path and parsed version back to `bk_client` via `POST /v1.13/executable/set`.

### 2. Two-Stage Subprocess Architecture & Exact Command Lines
`bk_maya` never runs network downloads or conversions inside Maya's Python process. Instead:
- **Stage 1 — `mayapy` worker (`download.py#L841-L970`)**:
  `DownloadThread` (`QThread` in `bk_maya/bk_maya/core/download.py#L651-L971`) locates `mayapy` next to `sys.executable` (`_find_mayapy()`, `#L642-L648`) and spawns `bk_maya/bk_maya/scripts/bg_download.py`:
  ```python
  cmd = [
      mayapy, "-u", "<addon>/bk_maya/scripts/bg_download.py",
      "--asset-json", "<cache_dir>/asset.json",
      "--output-dir", "<cache_dir>",
      "--resolution", "2048",
      "--api-key", api_key,
      "--blender-path", blender_path,
      "--port", str(client_port),
      "--app-id", str(os.getpid()),
  ]
  ```
- **Stage 2 — Headless Blender (`blender_runner.py#L305-L380`)**:
  For `.blend` files (`model` and `material`), `bg_download.py#L317-L445` imports `bk_maya.core.blender_runner.run_export_usd()`, which locates `export_usd.py` (`_find_export_usd_script()`, `blender_runner.py#L51-L91` -> `<addon>/client/vX.Y.Z/tools/export_usd.py`), writes a temporary `bk_export_usd_<rand>.json` params file (`{"blend_path": ..., "out_usd": ..., "max_resolution": ..., "asset_type": ..., "asset_name": ..., "asset_id": ...}`), and runs:
  ```
  "<blender_exe>" -b --factory-startup --python "<addon>/client/v1.13.0/tools/export_usd.py" -- "<temp_dir>/bk_export_usd_xxxx.json"
  ```
  - Note: `-b` (`--background`) + `--factory-startup` disables user add-ons/preferences so a user's installed Blender add-ons cannot crash or pollute headless conversion.
  - Note: `bk_client` also has a built-in endpoint `POST /v1.13/run_blender_script` (`bk_client/client/run_blender_script.go#L356-L365`) that extracts embedded `tools/export_usd.py` + `tools/bk_mtlx/` to `<os.TempDir()>/bk_client/v<ClientVersion>/tools/` (`#L200-L231`) and runs:
    ```
    "<blender_exe>" --background --factory-startup ["<blend_path>"] --python "<script_path>" -- "<params.json>"
    ```
- **Stdout IPC protocol**: Both `bg_download.py` and `export_usd.py` communicate with the parent thread via line-buffered stdout tags (`blender_runner.py#L22-L27`, `export_usd.py#L48-L100`):
  - `BK_STATUS <stage>`
  - `BK_PROGRESS <0.000..1.000> <msg>`
  - `BK_ARTIFACT <side_output_path>`
  - `BK_DONE <primary_output_path>`
  - `BK_ERROR <msg>`

### 3. Conversion Scripts & Export Format (`export_usd.py` + `bk_mtlx`)
- **Script paths**:
  - `bk_client/client/tools/export_usd.py` (1500 lines, embedded in `bk_client` via `//go:embed` and shipped in `<addon>/client/vX.Y.Z/tools/export_usd.py`).
  - `bk_client/client/tools/bk_mtlx/` (`blender_materialx_exporter.py`, `materialx_library_core.py`, `mtlxutils/`), a vendored pure-Python MaterialX node-graph exporter using Blender 4.1+'s bundled `MaterialX` (`PyMaterialX`) module (`export_usd.py#L889-L906`).
- **Step-by-step pipeline inside `export_usd.py` (`export_to_usd`, `#L1271-L1437`)**:
  1. `bpy.ops.wm.open_mainfile(filepath=blend_path)` (`#L1299`).
  2. **Material asset preview mesh** (`_prepare_material_asset`, `#L1146-L1216`): If `asset_type == "material"` and no visible mesh uses the material datablock, creates a 2x2m unit plane with a `0..1` `UVMap` and assigns the material so `wm.usd_export` will emit it.
  3. **Unhide all scene objects** (`#L1310-L1314`).
  4. **Rig consolidation** (`_flatten_rig_to_single_root`, `#L1219-L1268`): If the asset has `ARMATURE` objects (many BlenderKit vehicles/props use rigid bone-parenting), detaches child objects preserving `matrix_world`, deletes the armatures, and parents all root objects under a single Empty named after the asset.
  5. **Texture unpacking** (`unpack_textures`, `#L184-L227`): Disables `use_autopack`, rewrites `image.packed_files[*].filepath` and `image.filepath` to `//textures/<basename>` (collision-resolving with `000`, `001` suffixes), and calls `image.unpack(method="WRITE_ORIGINAL")`. Does **not** re-save the `.blend` (`#L1343-L1347`).
  6. **Flatten shader node groups** (`_flatten_material_node_groups`, `#L239-L249`, `#L341-L435`): Recursively expands all `ShaderNodeGroup` nodes via `bpy.ops.node.group_ungroup()` (with a pure-Python `_manual_inline` fallback if the operator fails) because Blender's USD exporter ignores textures inside node groups.
  7. **Rename shader nodes** (`_rename_shader_nodes_to_material`, `#L251-L338`): Prefixes `Material Output`, surface BSDFs, and `ShaderNodeTexImage` node names with `<MaterialName>_` so `UsdPreviewSurface` prim names don't collide across materials.
  8. **MaterialX sidecar export** (`export_materialx_sidecar`, `#L958-L1045`): Before destructive mesh splitting, runs `bk_mtlx.blender_materialx_exporter.export_material_to_materialx()` on every used material to write `materials/<SafeMatName>.mtlx` (MaterialX `1.39`, `copy_textures=False`, `texture_folder_name="../textures"`), then runs `_fix_mtlx_normal_maps()` (`#L908-L955`) to rewrite `color3` normal images feeding `<bump>` into `vector3` raw images feeding `<normalmap>`.
  9. **Single-material-per-mesh enforcement** (`_split_meshes_by_material` `#L473-L588` & `_force_single_material_per_mesh` `#L590-L644`): Splits non-rigged multi-material meshes via `bpy.ops.mesh.separate(type="MATERIAL")` and collapses remaining multi-slot meshes to their dominant material slot.
  10. **UV cleanup & sanitization** (`_remove_empty_uv_layers` `#L647-L723`, `_fix_primary_uv_set` `#L726-L808`, `_sanitize_meshes_for_usd_export` `#L811-L866`): Drops all-zero/zero-area UV layers, marks the UV layer wired to `ShaderNodeUVMap` as `active_render`, and runs `bpy.ops.object.convert(target="MESH")` + `mesh.validate()` so `wm.usd_export` writes valid `faceVarying` UV indices.
  11. **USD crate export** (`bpy.ops.wm.usd_export`, `#L1384-L1426`):
      ```python
      desired = {
          "filepath": out_usd, "selected_objects_only": False, "visible_objects_only": True,
          "export_animation": True, "export_hair": True, "export_uvmaps": True,
          "export_normals": True, "export_materials": True, "export_textures": False,
          "overwrite_textures": False, "relative_paths": True,
          "generate_preview_surface": True, "generate_materialx_network": not use_refs,
          "root_prim_path": "/root", "default_prim_path": "/root",
          "export_global_forward_selection": "Y", "export_global_up_selection": "Z",
      }
      ```
  12. **USDA MaterialX reference layer** (`write_mtlx_reference_layer`, `#L1086-L1138`): When `export_materialx_sidecar` succeeded for at least one material (`use_refs=True`), writes `<stem>.usda` (`defaultPrim = "root"`, `metersPerUnit = 1`, `upAxis = "Z"`, `subLayers = [@./<stem>.usd@]`) that overlays `over "root" { over "_materials" { over "<MatPrim>" ( prepend references = @./materials/<Mat>.mtlx@</MaterialX/Materials/<SurfName>> ) {} } }`, emits `BK_ARTIFACT <out_usd>`, and returns `<stem>.usda` via `BK_DONE`.

### 4. Host-Side Material & Texture Translation in `bk_maya`
- **Model import** (`_import_model`, `bk_maya/bk_maya/core/download.py#L164-L280`):
  - Two modes via `prefs.model_import_method`:
    1. `"maya"` (default): Calls `cmds.file(usd_path, i=True, type="USD Import", returnNewNodes=True, options="shadingMode=[[useRegistry,MaterialX],[useRegistry,UsdPreviewSurface],[displayColor,default]];preferredMaterial=none;readAnimData=1;primPath=/")`, converts imported `standardSurface` / `usdPreviewSurface` shaders into native `openPBRSurface` (Maya 2025+) via `_convert_imported_materials_to_openpbr()` (`#L1014-L1135`), and re-parents meshes under a single group with Z-up to Y-up (`rotateX = -90`) and meter-to-cm scale correction if `mayaUsd` did not already convert units (`_detect_usd_compensation`, `#L77-L114`).
    2. `"stage"`: Creates a `mayaUsdProxyShape` node pointing `.filePath` at the `.usda`/`.usd` file (`_create_usd_stage`, `#L283-L321`).
- **Material import** (`_import_material`, `bk_maya/bk_maya/core/download.py#L324-L355`): Imports the material `.usda`/`.usd` via `USD Import`, extracts the imported `shadingEngine`, deletes the temporary preview plane meshes, converts the surface shader to `openPBRSurface`, and assigns the `shadingEngine` to the target mesh (`cmds.sets(target, e=True, forceElement=sg)`).
- **Caching** (`is_asset_cached`, `bk_maya/bk_maya/core/download.py#L121-L157`, `bg_download.py#L483-L513`): Keyed by `<global_dir>/blendkit_maya_cache/<assetBaseId>_<resolution>/`. If `result.json` exists with `"success": true` and `usd_file` (or `raw_file` for HDR) exists on disk, `bg_download.py` (and `is_asset_cached`) skips both download and Blender conversion and immediately returns the cached path.

---

## e. How Website "Get This / Send to App" (`bkclientjs`) Works

1. **Browser -> `bk_client` (`bk_client/client/main.go#L3674-L3894`)**:
   - The BlenderKit website runs `bkclient-js` in the user's browser, which probes localhost ports with CORS + Chromium Private Network Access (`Access-Control-Allow-Private-Network: true`, `main.go#L3809-L3839`; allowed origins: `blendkit.com`, `*.blendkit.com`, `localhost`):
     - `GET http://127.0.0.1:<port>/bkclientjs/status` (`bkclientjsStatusHandler`, `main.go#L3674-L3699`) -> returns `{"clientVersion": "1.13.0", "softwares": [{"name": ..., "version": ..., "appID": <pid>, "addonVersion": ..., "projectName": ...}]}` from `AvailableSoftwares`.
     - When the user clicks "Get This Model", the browser sends `POST http://127.0.0.1:<port>/bkclientjs/get_asset` (`bkclientjsGetAssetHandler`, `main.go#L3769-L3805`) with JSON:
       ```json
       {"asset_id": "...", "asset_base_id": "...", "resolution": "resolution_2K", "api_key": "...", "app_id": 12345}
       ```
   - `bkclientjsGetAsset()` (`main.go#L3849-L3894`) fetches full asset metadata via `GET {Server}/api/v1/search/?query=asset_base_id:{assetBaseID}` (`GetAssetInstance`, `#L3974-L4016`), schedules thumbnail downloads, and (for `/report` plugins where `protocol == protocolReport`) enqueues a finished task on `Tasks[appID]`:
     ```json
     {
       "app_id": 12345,
       "task_id": "<uuid>",
       "task_type": "bkclientjs/get_asset",
       "status": "finished",
       "message": "Download requested from browser.",
       "result": {
         "api_key": "...",
         "asset_id": "...",
         "asset_base_id": "...",
         "resolution": "resolution_2K",
         "asset_data": { "...full Asset struct..." }
       }
     }
     ```
2. **How `bk_maya` handles it (Two Bugs / Missing Pieces in `bk_maya`)**:
   - **Bug 1**: As noted in Section (a.3), `bk_maya`'s `_minimal_report_data()` (`bk_maya/bk_maya/core/client_lib.py#L683-L691`) omits `"software"` in `/report` payloads, so `bk_client/client/main.go#L728-L730` registers Maya in `AvailableSoftwares` as `"name": "Blender"` instead of `"Maya"`.
   - **Bug 2**: In `bk_maya/bk_maya/core/client_lib.py#L1153-L1272` (`dispatch_tasks`), `bk_maya` handles `search`, `thumbnail_download`, `prxc_download`, `login`, `token_refresh`, `oauth2/logout`, `profiles/get_user_profile`, `categories_update`, `ratings/get_bookmarks`, `ratings/send_rating`, `disclaimer`, `notifications`, `client_status`, and `settings`, **but has no branch for `task_type == "bkclientjs/get_asset"`**! The task is silently ignored when polled.
   - **Lesson for Kitbridge (C4D)**: Include `"software": "Cinema 4D"` and `"software_version": ...` in every `/report` body, and handle `task_type == "bkclientjs/get_asset"` in `dispatch_tasks()` by extracting `task["result"]["asset_data"]` and `task["result"]["resolution"]` and triggering `start_download_and_import()`.

---

## f. How `blendkit-rhino` Differs in Conversion & Architecture

1. **Architecture**: Written in C# (`.NET 7`, Eto.Forms UI, `BlendkitRhino.rhp`) for Rhino 8 (`blendkit-rhino/src/BlendkitRhino.csproj#L11`). Uses `bk_client`'s built-in async endpoints instead of a separate Python background wrapper:
   - Downloads via `POST /blender/asset_download` (`blendkit-rhino/src/Infra/DownloadService.cs#L98`) rather than `bg_download.py` + `wrappers/blocking_file_download`.
   - Runs headless Blender via `POST /run_blender_script` (`BlenderConvertService.cs#L81`, `MaterialConvertService.cs#L56`), letting `bk_client` manage the `blender --background --factory-startup <blend> --python <script> -- <params.json>` subprocess and stream progress via `/report` (`run_blender_script.go#L268-L444`).
2. **Model Conversion — GLB instead of USD + Texture Downscaling**:
   - Rhino converts model `.blend` files to **`.glb`** using `bk_client/client/tools/export_glb.py` (`BlenderConvertService.cs#L48-L80`).
   - Because some BlenderKit assets lack server-side resolution variants and ship 8K PNGs inside the original `.blend` (producing 100–270 MB GLBs that freeze Rhino's synchronous UI-thread importer), `export_glb.py#L155-L190` (`downscale_images`) scales `bpy.data.images` in-memory to `texture_max_px` before calling `bpy.ops.export_scene.gltf(filepath=out_glb, export_format="GLB", export_yup=True, export_apply=True)`.
   - Output files are named `<stem>_t<textureMaxPx>.glb` (`BlenderConvertService.cs#L43-L45`) because `bk_client`'s `/run_blender_script` cache (`run_blender_script.go#L290-L299`) keys solely on `os.Stat(data.OutputPath)`.
3. **Material Conversion — Flat Principled BSDF JSON Manifest**:
   - Instead of USD/MaterialX, Rhino runs `blendkit-rhino/python/blendkit_rhino_material_extract.py` (`#L1-L263`) inside headless Blender (`MaterialConvertService.cs#L26-L60`).
   - The script unpacks images to `//textures_<res>/` (`#L101-L140`), finds the first `BSDF_PRINCIPLED` node (`#L145-L151`), walks upstream links from each socket (`Base Color`, `Metallic`, `Roughness`, `Normal`, `Emission Color`/`Emission`, `Emission Strength`, `Alpha`) to the nearest `TEX_IMAGE` node (`upstream_image`, `#L154-L170`), and writes `<blend>.material.json` (`#L196-L225`):
     ```json
     {
       "name": "MatName",
       "base_color_texture": "C:/.../tex.jpg", "base_color_rgba": [0.8, 0.8, 0.8, 1.0],
       "metallic_texture": null,               "metallic": 0.0,
       "roughness_texture": "C:/.../rough.jpg","roughness": 0.5,
       "normal_texture": "C:/.../norm.png",
       "emission_texture": null,               "emission_rgba": [0.0, 0.0, 0.0, 1.0],
       "emission_strength": 0.0,
       "alpha_texture": null,                  "alpha": 1.0
     }
     ```
   - `MaterialJsonImporter.cs#L78-L237` reads `<blend>.material.json` and constructs a native Rhino `Material` (`mat.ToPhysicallyBased()`) with bitmap `Texture` slots. (Notably, this approach loses procedural nodes/mixes/ramps, whereas `bk_maya`'s `export_usd.py` + `bk_mtlx` preserves full node graphs as MaterialX.)
4. **Viewport Decimated Proxy Conduit**: `ProxyMeshService.cs#L31-L130` auto-generates 5,000-face decimated proxy meshes (or attaches `.prxc` meshes via `PrxToMesh.cs`) for imported objects >20,000 faces in Wireframe/Shaded modes.

---

## g. Licenses & Implications for a GPL Cinema 4D Plugin ("Kitbridge")

| Repository / Component | License | Evidence in Code |
|---|---|---|
| `bk_maya` | **GPL-3.0-or-later** | `bk_maya/LICENSE` (full GPLv3 text); `bk_maya/pyproject.toml#L9` (`license = "GPL-3.0-or-later"`). |
| `bk_client` | **GPL-2.0-or-later** | `bk_client/LICENSE` (full GPLv2 text); Go source headers (`bk_client/client/main.go#L1-L17`, `run_blender_script.go#L1-L8`) state *"either version 2 of the License, or (at your option) any later version"*. |
| `bk_maya/bk_maya/bk_proxor` | **No explicit LICENSE file** | Submodule (`https://github.com/BlenderKit/bk_proxor.git`) has no `LICENSE` file or license field in `pyproject.toml`, and no license headers in `src/bk_proxor/prx_format.py`. Distributed inside `bk_maya` (GPL-3.0-or-later). |
| `bk_client/client/tools/bk_mtlx` | **GPL-2.0-or-later (via `bk_client`)** | Vendored from `bl_mtlx` inside `bk_client/client/tools/bk_mtlx/`; no separate LICENSE file in subdir, covered by `bk_client` repo license. |
| `blendkit-rhino` | **No explicit LICENSE file** | Shallow clone of `https://github.com/BlenderKit/blendkit-rhino.git` contains no `LICENSE` file and no `<PackageLicenseExpression>` in `src/BlendkitRhino.csproj`. |

**Implications for a GPLv3+ Cinema 4D Plugin ("Kitbridge")**:
1. **License compatibility**: Because `bk_client` is **GPL-2.0-or-later** ("any later version") and `bk_maya` is **GPL-3.0-or-later**, both are 100% compatible with releasing Kitbridge under **GPL-3.0-or-later**.
2. **Bundling prebuilt `bk_client` binaries**: Kitbridge can redistribute the official signed/notarized `bk_client` binaries (which talk to the plugin over localhost HTTP IPC) under GPLv3, provided Kitbridge includes the GPLv3 license text, copyright notices, and a link/offer for the corresponding `bk_client` source tag (`v1.13.x`).
3. **Adapting Python code**: Core modules from `bk_maya/bk_maya/core/` (`client_lib.py`, `auth.py`, `secret_store.py`, `blender_runner.py`, `prefs.py`, `search.py`, `bookmarks.py`) and `bk_client/client/tools/` (`export_usd.py`, `export_glb.py`, `bk_mtlx/`) can be directly adapted into Kitbridge under GPL-3.0-or-later while preserving copyright headers. For `bk_proxor` (or any code from `blendkit-rhino`), verify with BlenderKit upstream or rely on the `bk_maya` GPLv3 distribution before copying verbatim.
4. **C4D host compatibility**: Running a GPLv3 Python plugin inside proprietary Cinema 4D via C4D's public `c4d` Python API and communicating with `bk_client` / `blender` over subprocess + HTTP IPC is standard practice across BlenderKit's Maya (`bk_maya`) and Rhino (`blendkit-rhino`) plugins.

---

## h. Blockers & Gotchas for a Cinema 4D (2023–2027) Port

1. **No Qt (`PySide2`/`PySide6`) Bundled in Cinema 4D**:
   - `bk_maya` is heavily coupled to Qt (`bk_maya/bk_maya/ui/qt_compat.py`, `asset_bar.py`, `placement.py`, `settings_dialog.py`) and even uses `QThread`/`Signal` inside `bk_maya/bk_maya/core/download.py#L45-L60`, `#L651-L665` (`DownloadThread(QThread)`) and `QTimer` in `_ReportPoller` (`ui/asset_bar.py#L150-L190`).
   - **C4D impact**: Cinema 4D does **not** ship PySide/Qt; it uses `c4d.gui.GeDialog`, `c4d.gui.GeUserArea` (custom 2D grid/card drawing), and `c4d.plugins.ToolData` / `DescriptionToolData` or `SceneHookData`.
   - **Fix**: Replace `QThread` in `download.py` with Python `threading.Thread` + `queue.Queue` (or `c4d.threading.C4DThread`), and drive `/report` polling + main-thread task dispatch from `GeDialog.Timer(msg)` (`SetTimer(200)`) or `c4d.SpecialEventAdd()` + ` CoreMessage()`.
2. **Subprocess Interpreter (`sys.executable` is `Cinema 4D.exe`, NOT `mayapy`)**:
   - `bk_maya/bk_maya/core/download.py#L642-L648` (`_find_mayapy`) spawns `mayapy` to run `bg_download.py`.
   - **C4D impact**: In Cinema 4D, `sys.executable` points to the main `Cinema 4D.exe` / `Cinema 4D.app/Contents/MacOS/Cinema 4D` binary (or `c4dpy` if installed separately, which end users rarely have). Spawning `sys.executable` will launch a second full Cinema 4D GUI instance!
   - **Fix**: Do **not** spawn an intermediate `bg_download.py` Python subprocess at all. Either:
     - Run the HTTP download directly in a Python `threading.Thread` inside C4D (via `POST /v1.13/wrappers/get_download_url` + `POST /v1.13/wrappers/blocking_file_download`, or `POST /v1.13/blender/asset_download` polled via `/report` like `blendkit-rhino`), and then spawn `blender_exe -b --factory-startup --python export_usd.py -- params.json` directly from a `threading.Thread` (or via `POST /v1.13/run_blender_script`).
3. **C4D Main-Thread Requirement for Scene Modifications**:
   - Just like Maya's `maya.utils.executeDeferred` (`bk_maya/bk_maya/core/download.py#L619-L639`), Cinema 4D strictly forbids modifying the active `BaseDocument`, inserting objects/materials, or calling `c4d.EventAdd()` from background threads.
   - **Fix**: Keep all scene/material creation on the main thread inside `GeDialog.Timer()` or `GeDialog.CoreMessage()` draining a thread-safe `queue.Queue`.
4. **Python Version Matrix (C4D 2023–2027)**:
   - C4D 2023 uses Python **3.10.8**; C4D 2024–2025 use Python **3.11.x**; C4D 2026–2027 use Python **3.11+ / 3.12+**.
   - `bk_maya`'s core modules use `from __future__ import annotations` and stdlib-only calls (`urllib.request`, `json`, `ctypes`, `subprocess`, `hashlib`, `secrets`), which work out-of-the-box on Python **3.10+** with zero external `pip` dependencies.
5. **Coordinate System, Handedness & Units (Blender vs. Maya vs. C4D)**:
   - **Blender**: Right-handed, **+Z up**, **-Y forward**, **1 unit = 1 meter**.
   - **Maya**: Right-handed, **+Y up**, **+Z forward**, **1 unit = 1 centimeter**.
   - **Cinema 4D**: **Left-handed**, **+Y up**, **+Z forward**, default **1 unit = 1 centimeter**.
   - Both `placement.py#L1467-L1473` and `bk_proxor` (`prx_format.py#L467-L561`) have explicit `axis_swap_yz` and `world_scale` parameters; for C4D viewport drawing (`BaseDraw.DrawLine`), transform Blender `(x, y, z)` meters to C4D `(x * 100, z * 100, y * 100)` cm.
6. **Material & Geometry Import Strategy in C4D (USD vs. GLB vs. Node Material JSON)**:
   - `bk_maya`'s conversion pipeline (`export_usd.py`) outputs `.usda` referencing MaterialX 1.39 `.mtlx` + `UsdPreviewSurface` `.usd` crate. While Maya (`mayaUsd` + `LookdevX` / `openPBRSurface`) natively consumes USD + MaterialX, **Cinema 4D's built-in USD importer has limited MaterialX node-graph support** and users frequently target **Redshift** (C4D's default renderer since 2023), **Standard/Physical**, **Octane**, or **Corona**.
   - **Practical approach for Kitbridge**:
     - Reuse `export_usd.py`'s battle-tested mesh/UV/rig/unpack pre-processing (or `export_glb.py` / FBX / USD for geometry import via `c4d.documents.MergeDocument`), **plus** emit a rich shader-graph JSON manifest alongside the unpacked `textures/` folder (expanding on `blendkit-rhino`'s `blendkit_rhino_material_extract.py` or `bk_mtlx`'s traversal) so Kitbridge can build native C4D **Redshift Node Materials** (`c4d.nodes` / `maxon.NodesGraphModelRef`) and **C4D Standard/PBR Materials** directly via Python.
7. **Windows Low-Level Mouse Hook (`WH_MOUSE_LL`) Warning**:
   - `bk_maya/bk_maya/ui/placement.py#L220-L435` installs a Win32 `SetWindowsHookExW(WH_MOUSE_LL)` hook via `ctypes` to intercept mouse wheel / LMB-up over Maya's viewport. In Cinema 4D, **do not use OS hooks**: implement placement as a native `c4d.plugins.ToolData` (`MouseInput`, `GetCursorInfo`, `Draw`) or `GeUserArea.HandleMouseDrag()`, where C4D natively provides viewport rays (`BaseView.SW`) and mouse/keyboard state (`c4d.gui.GetInputState`).
