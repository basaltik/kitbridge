# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
#  Original code Copyright (C) BlenderKit developers (bk_maya / bk_client).
#  Adapted for Kitbridge.
#
# ##### END GPL LICENSE BLOCK #####

"""bk_client lifecycle management and loopback HTTP endpoint wrappers."""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import uuid
from email.message import Message
from typing import Any, Dict, IO, List, Optional, Sequence, Tuple, Union

from .. import __version__
from . import paths

log = logging.getLogger("kitbridge")

CLIENT_VERSION_SERIES = "v1.13"
DEFAULT_SERVER = os.environ.get("BLENDKIT_SERVER", "https://www.blendkit.com")
SOFTWARE_NAME = "Cinema 4D"
ADDON_VERSION = __version__
ADDON_BUILD = "261103"

# Whitelisted OAuth redirect_uri ports in exact order.
CLIENT_PORTS: Tuple[str, ...] = (
    "62485",
    "65425",
    "55428",
    "49452",
    "35452",
    "25152",
    "5152",
    "1234",
)

POLL_CONNECT_TIMEOUT = 0.20
POLL_READ_TIMEOUT = 0.50
REQUEST_CONNECT_TIMEOUT = 2.0
REQUEST_READ_TIMEOUT = 5.0
RESPAWN_AFTER_FAILURES = 10
EXIT_CODE_ADDR_IN_USE = 43


def binary_name_for_host(
    sys_name: Optional[str] = None,
    machine: Optional[str] = None,
) -> str:
    """Return the bk_client binary filename for *sys_name* and *machine*."""
    os_name = (sys_name or platform.system()).lower()
    if os_name == "darwin":
        os_name = "macos"

    arch = (machine or platform.machine()).lower()
    if arch == "amd64":
        arch = "x86_64"
    elif arch == "aarch64":
        arch = "arm64"

    name = f"bk_client-{os_name}-{arch}"
    if os_name == "windows":
        name += ".exe"
    return name


def _parse_version_folder(name: str) -> Optional[Tuple[int, ...]]:
    if not name.startswith("v"):
        return None
    try:
        return tuple(int(part) for part in name[1:].split("."))
    except ValueError:
        return None


def detect_client_version(client_root: Optional[str] = None) -> str:
    """Resolve the bundled client version (e.g. ``v1.13.9``)."""
    root = client_root or paths.bundled_client_root()
    resolved_path = os.path.join(root, "RESOLVED_VERSION")
    try:
        with open(resolved_path, encoding="utf-8") as fh:
            resolved = fh.read().strip()
        if resolved:
            return resolved if resolved.startswith("v") else f"v{resolved}"
    except OSError:
        pass

    bin_name = binary_name_for_host()
    best: Optional[Tuple[Tuple[int, ...], str]] = None
    try:
        for entry in os.listdir(root):
            parsed = _parse_version_folder(entry)
            if parsed is None:
                continue
            if not os.path.isfile(os.path.join(root, entry, bin_name)):
                continue
            if best is None or parsed > best[0]:
                best = (parsed, entry)
    except OSError:
        best = None

    if best is not None:
        return best[1]
    return f"{CLIENT_VERSION_SERIES}.0"


def api_version_prefix(version: str = CLIENT_VERSION_SERIES) -> str:
    """Return the ``vX.Y`` HTTP path prefix (e.g. ``v1.13``)."""
    ver = version if version.startswith("v") else f"v{version}"
    parts = ver.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return ver


def ensure_client_binary_installed(client_root: Optional[str] = None) -> str:
    """Copy the bundled bk_client binary to ``~/blenderkit_data/client/bin/vX.Y.Z/``."""
    root = client_root or paths.bundled_client_root()
    version = detect_client_version(root)
    bin_name = binary_name_for_host()
    src = os.path.join(root, version, bin_name)
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"Bundled bk_client binary not found at {src}. Run 'py -3 dev/fetch_client.py' first."
        )

    dst_dir = paths.client_bin_dir(version, create=True)
    dst = os.path.join(dst_dir, bin_name)

    try:
        if os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src):
            return dst
    except OSError:
        pass

    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copy2(src, dst)
        if sys.platform != "win32":
            os.chmod(dst, 0o755)
        log.info("Installed bk_client binary to %s", dst)
        return dst
    except OSError as exc:
        log.warning("Could not copy bk_client to %s (%s); running bundled binary in place.", dst, exc)
        return src


def get_bundled_tool_path(tool_filename: str, client_root: Optional[str] = None) -> Optional[str]:
    """Return the absolute path to a bundled tool script under ``client/vX.Y.Z/tools/``."""
    root = client_root or paths.bundled_client_root()
    version = detect_client_version(root)
    candidate = os.path.join(root, version, "tools", tool_filename)
    if os.path.isfile(candidate):
        return candidate
    return None


def _http_request(
    method: str,
    url: str,
    body: Optional[Dict[str, Any]] = None,
    *,
    connect_timeout: float = REQUEST_CONNECT_TIMEOUT,
    read_timeout: float = REQUEST_READ_TIMEOUT,
) -> Any:
    """Execute an HTTP request over loopback with separate connect and read timeouts."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    payload: Optional[bytes] = None
    headers: Dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))

    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(read_timeout)
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        status = resp.status
        reason = resp.reason
        resp_headers = resp.headers
    finally:
        conn.close()

    if status >= 400:
        hdrs = resp_headers if isinstance(resp_headers, Message) else Message()
        raise urllib.error.HTTPError(url, status, reason, hdrs, io.BytesIO(raw))

    if not raw:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# Cinema 4D sets these in its native environment (not visible in os.environ) pointing at its own
# bundled cacert.pem; bk_client honours them and then rejects blendkit.com's certificate.
_STRIPPED_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")


def _child_env() -> Dict[str, str]:
    """Environment for bk_client: os.environ (never the host's hidden native vars) minus TLS overrides."""
    env = dict(os.environ)
    for name in _STRIPPED_ENV_VARS:
        env.pop(name, None)
    return env


class BKClient:
    """Manages the local bk_client process and wraps its HTTP API."""

    def __init__(
        self,
        software_version: Union[str, int] = "",
        *,
        server: str = DEFAULT_SERVER,
        addon_version: str = ADDON_VERSION,
        app_id: Optional[int] = None,
        ports: Sequence[str] = CLIENT_PORTS,
        client_root: Optional[str] = None,
        proxy_which: str = "SYSTEM",
        proxy_address: str = "",
        trusted_ca_certs: str = "",
        ssl_context: str = "",
    ) -> None:
        self.software_version: str = str(software_version) if software_version is not None else ""
        self.server: str = server
        self.addon_version: str = addon_version
        self.app_id: int = int(app_id) if app_id is not None else os.getpid()
        self.platform_version: str = platform.platform()
        self.proxy_which: str = proxy_which
        self.proxy_address: str = proxy_address
        self.trusted_ca_certs: str = trusted_ca_certs
        self.ssl_context: str = ssl_context

        self._client_root: Optional[str] = client_root
        self._ports: Tuple[str, ...] = tuple(str(p) for p in ports)
        if not self._ports:
            raise ValueError("ports must not be empty")

        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._log_fh: Optional[IO[bytes]] = None
        self._active_port: str = self._ports[0]
        self._port_index: int = 0
        self._failed_reports: int = 0
        self._have_client: bool = False
        self._scene_id: str = str(uuid.uuid4())

    @property
    def api_version(self) -> str:
        return api_version_prefix(CLIENT_VERSION_SERIES)

    @property
    def active_port(self) -> str:
        with self._lock:
            return self._active_port

    def base_url(self, port: Optional[str] = None) -> str:
        target_port = port or self.active_port
        return f"http://127.0.0.1:{target_port}/{self.api_version}"

    def minimal_report_data(
        self,
        api_key: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        """Build the GetReportData payload required by bk_client.

        Explicitly includes ``software="Cinema 4D"`` and ``software_version`` on
        every call so bk_client registers the host as Cinema 4D rather than
        defaulting to Blender.
        """
        return {
            "app_id": self.app_id,
            "api_key": api_key,
            "addon_version": self.addon_version,
            "blender_version": "",
            "platform_version": self.platform_version,
            "project_name": project_name,
            "software": SOFTWARE_NAME,
            "software_version": self.software_version,
        }

    def _prefs_block(
        self,
        api_key: str = "",
        scene_id: str = "",
        resolution: str = "ORIGINAL",
    ) -> Dict[str, Any]:
        return {
            "api_key": api_key,
            "api_key_refresh": "",
            "api_key_timeout": 0,
            "scene_id": scene_id or self._scene_id,
            "app_id": self.app_id,
            "unpack_files": False,
            "create_asset_library": False,
            "resolution": resolution,
            "project_subdir": "",
            "global_dir": paths.global_dir(create=True),
            "binary_path": "",
            "addon_dir": "",
            "addon_module_name": "kitbridge",
        }

    # ── Process Discovery & Lifecycle ────────────────────────────────────────

    def _ping(self, port: str) -> bool:
        try:
            resp = _http_request(
                "GET",
                f"{self.base_url(port)}/report",
                body=self.minimal_report_data(),
                connect_timeout=POLL_CONNECT_TIMEOUT,
                read_timeout=POLL_READ_TIMEOUT,
            )
            return isinstance(resp, list)
        except Exception:
            return False

    def find_running_client(self) -> Optional[str]:
        """Probe candidate ports in order and return the first responsive port."""
        for port in self._ports:
            if self._ping(port):
                return port
        return None

    def _client_process_alive(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def _close_log_handle(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    def _spawn(self, port: str) -> subprocess.Popen[bytes]:
        binary = ensure_client_binary_installed(self._client_root)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        log_path = paths.client_log_path(create=True)
        self._close_log_handle()
        self._log_fh = open(log_path, "ab")

        args = [
            binary,
            "--port",
            port,
            "--server",
            self.server,
            "--proxy_which",
            self.proxy_which,
            "--proxy_address",
            self.proxy_address,
            "--trusted_ca_certs",
            self.trusted_ca_certs,
            "--ssl_context",
            self.ssl_context,
            "--version",
            f"{self.addon_version}.{ADDON_BUILD}",
            "--software",
            SOFTWARE_NAME,
            "--pid",
            str(self.app_id),
        ]
        log.info("Spawning bk_client on port %s: %s", port, binary)
        proc = subprocess.Popen(
            args,
            cwd=os.path.dirname(binary),
            env=_child_env(),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
            close_fds=(sys.platform != "win32"),
        )
        return proc

    def ensure_running(self, timeout: float = 8.0) -> str:
        """Attach to an existing bk_client or spawn a new one; return the active port."""
        with self._lock:
            existing = self.find_running_client()
            if existing is not None:
                self._active_port = existing
                self._have_client = True
                self._failed_reports = 0
                log.debug("Attached to running bk_client on port %s", existing)
                return existing

            if self._client_process_alive() and self._ping(self._active_port):
                self._have_client = True
                self._failed_reports = 0
                return self._active_port

        num_ports = len(self._ports)
        per_port_timeout = max(0.5, timeout)

        for _attempt in range(num_ports):
            with self._lock:
                port = self._ports[self._port_index % num_ports]
                self._process = self._spawn(port)
                self._active_port = port
                proc = self._process

            deadline = time.monotonic() + per_port_timeout
            while time.monotonic() < deadline:
                if self._ping(port):
                    with self._lock:
                        self._have_client = True
                        self._failed_reports = 0
                    log.info("bk_client ready on port %s", port)
                    return port

                rc = proc.poll()
                if rc is not None:
                    with self._lock:
                        self._port_index = (self._port_index + 1) % num_ports
                        self._process = None
                    if rc == EXIT_CODE_ADDR_IN_USE:
                        log.warning(
                            "bk_client exited with code 43 (EADDRINUSE) on port %s; trying next port.",
                            port,
                        )
                    else:
                        log.warning(
                            "bk_client exited early with code %s on port %s; trying next port.",
                            rc,
                            port,
                        )
                    break
                time.sleep(0.1)
            else:
                # Timed out waiting for this process without an exit code.
                break

        raise RuntimeError(
            f"bk_client did not respond on port {self.active_port} within {timeout}s "
            f"(log: {paths.client_log_path(create=False)})"
        )

    def _try_respawn(self) -> None:
        log.warning(
            "bk_client unresponsive after %d consecutive /report failures; respawning.",
            RESPAWN_AFTER_FAILURES,
        )
        with self._lock:
            if self._process is not None:
                try:
                    self._process.terminate()
                except OSError as exc:
                    log.debug("Failed to terminate unresponsive bk_client: %s", exc)
                self._process = None
            self._close_log_handle()
            self._have_client = False
        try:
            self.ensure_running()
        except Exception as exc:
            log.error("bk_client respawn failed: %s", exc)

    # ── HTTP Endpoint Wrappers ───────────────────────────────────────────────

    def report(
        self,
        api_key: str = "",
        project_name: str = "",
    ) -> List[Dict[str, Any]]:
        """Poll ``GET /v1.13/report`` with short timeouts; respawn after 10 failures."""
        url = f"{self.base_url()}/report"
        try:
            resp = _http_request(
                "GET",
                url,
                body=self.minimal_report_data(api_key=api_key, project_name=project_name),
                connect_timeout=POLL_CONNECT_TIMEOUT,
                read_timeout=POLL_READ_TIMEOUT,
            )
        except Exception as exc:
            with self._lock:
                self._failed_reports += 1
                failures = self._failed_reports
            log.debug("Report poll failed (%d/%d): %s", failures, RESPAWN_AFTER_FAILURES, exc)
            if failures >= RESPAWN_AFTER_FAILURES:
                with self._lock:
                    self._failed_reports = 0
                self._try_respawn()
            return []

        with self._lock:
            self._failed_reports = 0
            self._have_client = True

        if not isinstance(resp, list):
            return []
        return [item for item in resp if isinstance(item, dict)]

    get_reports = report

    def unsubscribe_addon(self) -> None:
        """Detach this Cinema 4D instance from bk_client without killing shared clients."""
        body = {"app_id": self.app_id}
        try:
            _http_request("POST", f"{self.base_url()}/addons/unsubscribe", body=body)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                _http_request("GET", f"{self.base_url()}/blender/unsubscribe_addon", body=body)
            else:
                raise
        finally:
            self._close_log_handle()

    def oauth2_verification_data(self, code_verifier: str, state: str) -> None:
        """Register PKCE ``code_verifier`` and ``state`` with bk_client."""
        body = self.minimal_report_data()
        body["code_verifier"] = code_verifier
        body["state"] = state
        _http_request("POST", f"{self.base_url()}/oauth2/verification_data", body=body)

    send_oauth_verification_data = oauth2_verification_data

    def refresh_token(self, refresh_token_str: str, api_key: str = "") -> None:
        """Request an asynchronous OAuth token refresh via bk_client."""
        body = self.minimal_report_data(api_key=api_key)
        body["refresh_token"] = refresh_token_str
        _http_request("GET", f"{self.base_url()}/refresh_token", body=body)

    def oauth2_logout(self, refresh_token_str: str, api_key: str = "") -> None:
        """Revoke active tokens on the server via bk_client."""
        body = self.minimal_report_data(api_key=api_key)
        body["refresh_token"] = refresh_token_str
        _http_request("GET", f"{self.base_url()}/oauth2/logout", body=body)

    def get_user_profile(self, api_key: str = "") -> None:
        """Trigger an asynchronous fetch of the current user's profile."""
        body = self.minimal_report_data(api_key=api_key)
        _http_request("GET", f"{self.base_url()}/profiles/get_user_profile", body=body)

    def asset_search(
        self,
        *,
        urlquery: str,
        tempdir: str,
        asset_type: str = "model",
        api_key: str = "",
        page_size: int = 24,
        next_url: str = "",
        get_next: bool = False,
        scene_uuid: str = "",
        # Empty keeps bk_client on JPG/PNG thumbnails; it only picks WebP when told
        # the host is Blender >= 3.4, and C4D's BaseBitmap WebP support is unverified.
        blender_version: str = "",
        is_validator: bool = False,
    ) -> str:
        """Schedule an asset search via ``POST /v1.13/blender/asset_search``; return ``task_id``."""
        effective_scene = scene_uuid or self._scene_id
        body: Dict[str, Any] = {
            "PREFS": self._prefs_block(api_key=api_key, scene_id=effective_scene),
            "addon_version": self.addon_version,
            "platform_version": self.platform_version,
            "api_key": api_key,
            "app_id": self.app_id,
            "asset_type": asset_type,
            "blender_version": blender_version,
            "get_next": get_next or bool(next_url),
            "next": next_url,
            "page_size": page_size,
            "scene_uuid": effective_scene,
            "tempdir": tempdir,
            "urlquery": urlquery,
            "is_validator": is_validator,
            "history_id": "",
        }
        resp = _http_request("POST", f"{self.base_url()}/blender/asset_search", body=body)
        if not isinstance(resp, dict) or not resp.get("task_id"):
            raise RuntimeError(f"Unexpected /blender/asset_search response: {resp!r}")
        return str(resp["task_id"])

    def asset_download(
        self,
        *,
        asset_data: Dict[str, Any],
        resolution: str = "resolution_2K",
        download_dirs: Optional[List[str]] = None,
        api_key: str = "",
        scene_id: str = "",
        unpack_files: bool = False,
    ) -> str:
        """Schedule an asset download via ``POST /v1.13/blender/asset_download``.

        Matches ``DownloadData`` / ``DownloadAssetData`` in ``bk_client/client/structs.go``.
        """
        asset_type = str(asset_data.get("assetType") or asset_data.get("asset_type") or "model")
        asset_base_id = str(asset_data.get("assetBaseId") or asset_data.get("id") or "unknown")
        if download_dirs is None:
            download_dirs = [paths.asset_cache_dir(asset_base_id, resolution, create=True)]

        avail_res = asset_data.get("available_resolutions")
        if not isinstance(avail_res, list):
            raw_avail = asset_data.get("availableResolutions") or []
            avail_res = [int(r) for r in raw_avail if isinstance(r, int) or str(r).isdigit()]

        download_asset_data: Dict[str, Any] = {
            "name": str(asset_data.get("name") or asset_data.get("displayName") or ""),
            "id": str(asset_data.get("id") or asset_base_id),
            "available_resolutions": avail_res,
            "files": list(asset_data.get("files") or []),
            "assetType": asset_type,
            "resolution": resolution,
        }

        prefs_payload = self._prefs_block(
            api_key=api_key,
            scene_id=scene_id or self._scene_id,
            resolution=resolution,
        )
        prefs_payload["unpack_files"] = unpack_files

        body: Dict[str, Any] = {
            "addon_version": self.addon_version,
            "platform_version": self.platform_version,
            "app_id": self.app_id,
            "download_dirs": list(download_dirs),
            "resolution": resolution,
            "asset_data": download_asset_data,
            "PREFS": prefs_payload,
        }
        resp = _http_request("POST", f"{self.base_url()}/blender/asset_download", body=body)
        if not isinstance(resp, dict) or not resp.get("task_id"):
            raise RuntimeError(f"Unexpected /blender/asset_download response: {resp!r}")
        return str(resp["task_id"])

    def cancel_download(self, task_id: str) -> None:
        """Cancel an in-flight download via ``POST /v1.13/blender/cancel_download``."""
        body = {"task_id": task_id, "app_id": self.app_id}
        _http_request("POST", f"{self.base_url()}/blender/cancel_download", body=body)

    def run_blender_script(
        self,
        *,
        blender_exe_path: str = "",
        script_id: str = "",
        script_path: str = "",
        blend_path: str = "",
        output_path: str = "",
        params: Optional[Dict[str, Any]] = None,
        status_message: str = "",
        blender_version: str = "",
    ) -> str:
        """Run a headless Blender recipe via ``POST /v1.13/run_blender_script``.

        Matches ``RunBlenderScriptData`` in ``bk_client/client/run_blender_script.go``.
        """
        if bool(script_id) == bool(script_path):
            raise ValueError("Specify exactly one of script_id or script_path")

        body: Dict[str, Any] = {
            "blender_exe_path": blender_exe_path,
            "app_id": self.app_id,
            "addon_version": self.addon_version,
            "platform_version": self.platform_version,
            "software": SOFTWARE_NAME,
        }
        if script_id:
            body["script_id"] = script_id
        if script_path:
            body["script_path"] = script_path
        if blend_path:
            body["blend_path"] = blend_path
        if output_path:
            body["output_path"] = output_path
        if params is not None:
            body["params"] = params
        if status_message:
            body["status_message"] = status_message
        if blender_version:
            body["blender_version"] = blender_version

        resp = _http_request("POST", f"{self.base_url()}/run_blender_script", body=body)
        if not isinstance(resp, dict) or not resp.get("task_id"):
            raise RuntimeError(f"Unexpected /run_blender_script response: {resp!r}")
        return str(resp["task_id"])

    def get_executable(self, name: str = "blender", version: str = "") -> List[Dict[str, Any]]:
        """Query stored executables via ``GET /v1.13/executable/get``."""
        query: Dict[str, str] = {"name": name}
        if version:
            query["version"] = version
        url = f"{self.base_url()}/executable/get?{urllib.parse.urlencode(query)}"
        try:
            resp = _http_request("GET", url)
        except Exception as exc:
            log.debug("executable/get failed for %s: %s", name, exc)
            return []
        if isinstance(resp, dict) and isinstance(resp.get("executables"), list):
            return [e for e in resp["executables"] if isinstance(e, dict)]
        return []

    get_executables = get_executable

    def set_executable(
        self,
        name: str,
        path: str,
        version: str = "",
        args: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Register an executable path with bk_client via ``POST /v1.13/executable/set``."""
        body: Dict[str, Any] = {"name": name, "path": path}
        if version:
            body["version"] = version
        if args:
            body["args"] = list(args)
        resp = _http_request("POST", f"{self.base_url()}/executable/set", body=body)
        return resp if isinstance(resp, dict) else None

    def get_settings(self) -> Optional[Dict[str, Any]]:
        """Return the current settings Snapshot via ``GET /v1.13/settings/get``."""
        try:
            resp = _http_request("GET", f"{self.base_url()}/settings/get")
        except Exception as exc:
            log.debug("settings/get failed: %s", exc)
            return None
        return resp if isinstance(resp, dict) else None


Client = BKClient
