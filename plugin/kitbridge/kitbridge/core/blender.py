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

"""Blender executable discovery (including all Steam libraries) and version probing."""

from __future__ import annotations

import glob
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import paths
from .client import BKClient

log = logging.getLogger("kitbridge")

VersionTuple = Tuple[int, int, int]

# Minimum supported Blender version: 4.2.0.
# Verified against bk_client/client/tools/ (export_usd.py, export_glb.py, and bk_mtlx):
# those headless conversion scripts rely on Blender 4.1+/4.2+ USD/glTF export operators
# (export_textures_mode="KEEP", export_image_format="AUTO") and MaterialX 1.38 node trees.
MIN_BLENDER_VERSION: VersionTuple = (4, 2, 0)

PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_MARKER = "KITBRIDGE_BLENDER_VERSION:"

# Matches `"path"   "D:\\Games\\steam"` inside nested libraryfolders.vdf blocks
_VDF_PATH_KEY_RE = re.compile(r'"path"\s+"([^"]+)"', re.IGNORECASE)
# Matches legacy flat format `"1"   "D:\\SteamLibrary"`
_VDF_LEGACY_INDEX_RE = re.compile(r'^\s*"\d+"\s+"([^"]+)"\s*$', re.MULTILINE)

_PYTHON_ENV_VARS_TO_STRIP = (
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "VIRTUAL_ENV",
)


def format_version(version: VersionTuple) -> str:
    """Format a ``(major, minor, patch)`` tuple as ``'X.Y.Z'``."""
    return f"{version[0]}.{version[1]}.{version[2]}"


def parse_version_string(raw: str) -> Optional[VersionTuple]:
    """Parse a ``'major.minor[.patch]'`` string into a 3-int tuple."""
    text = (raw or "").strip()
    if not text:
        return None
    parts = text.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(re.match(r"^\d+", parts[2]).group(0)) if len(parts) >= 3 and re.match(r"^\d+", parts[2]) else 0
        return (major, minor, patch)
    except ValueError:
        return None


# ── Steam Library VDF Parsing ────────────────────────────────────────────────


def parse_libraryfolders_vdf(vdf_content: str) -> List[str]:
    """Extract Steam library root directories from Valve KeyValues ``libraryfolders.vdf`` text.

    Supports both the current nested block format (``"0" { "path" "..." }``) and
    the older flat ``"1" "D:\\\\SteamLibrary"`` format.
    """
    if not vdf_content:
        return []

    libraries: List[str] = []
    seen = set()

    def _add(raw_path: str) -> None:
        unescaped = raw_path.replace("\\\\", "\\").strip()
        if not unescaped:
            return
        norm = os.path.normpath(unescaped)
        key = norm.lower() if sys.platform == "win32" else norm
        if key not in seen:
            seen.add(key)
            libraries.append(norm)

    for match in _VDF_PATH_KEY_RE.finditer(vdf_content):
        _add(match.group(1))

    for match in _VDF_LEGACY_INDEX_RE.finditer(vdf_content):
        candidate = match.group(1)
        if "/" in candidate or "\\" in candidate:
            _add(candidate)

    return libraries


def parse_libraryfolders_vdf_file(vdf_path: str) -> List[str]:
    """Read and parse a ``libraryfolders.vdf`` file on disk."""
    try:
        with open(vdf_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        log.debug("Could not read Steam VDF file %s: %s", vdf_path, exc)
        return []
    return parse_libraryfolders_vdf(content)


def _windows_steam_root_from_registry() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            val, _ = winreg.QueryValueEx(key, "SteamPath")
            if isinstance(val, str) and val.strip():
                return os.path.normpath(val.strip())
    except OSError:
        pass
    return None


def _steam_roots() -> List[str]:
    roots: List[str] = []
    if sys.platform == "win32":
        reg_root = _windows_steam_root_from_registry()
        if reg_root:
            roots.append(reg_root)
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for default_root in (os.path.join(pf86, "Steam"), os.path.join(pf, "Steam")):
            if os.path.isdir(default_root) and default_root not in roots:
                roots.append(os.path.normpath(default_root))
    elif sys.platform == "darwin":
        mac_steam = os.path.expanduser("~/Library/Application Support/Steam")
        if os.path.isdir(mac_steam):
            roots.append(mac_steam)
    else:
        for linux_steam in (
            os.path.expanduser("~/.steam/steam"),
            os.path.expanduser("~/.local/share/Steam"),
        ):
            if os.path.isdir(linux_steam):
                roots.append(linux_steam)
    return roots


def steam_blender_candidates() -> List[str]:
    """Return candidate Blender executable paths across all discovered Steam libraries."""
    library_dirs: List[str] = []
    seen_libs = set()

    def _add_lib(path: str) -> None:
        norm = os.path.normpath(path)
        key = norm.lower() if sys.platform == "win32" else norm
        if key not in seen_libs:
            seen_libs.add(key)
            library_dirs.append(norm)

    for root in _steam_roots():
        vdf_candidates = (
            os.path.join(root, "steamapps", "libraryfolders.vdf"),
            os.path.join(root, "config", "libraryfolders.vdf"),
        )
        for vdf_path in vdf_candidates:
            if os.path.isfile(vdf_path):
                for lib in parse_libraryfolders_vdf_file(vdf_path):
                    _add_lib(lib)
        _add_lib(root)

    candidates: List[str] = []
    for lib_dir in library_dirs:
        common_blender = os.path.join(lib_dir, "steamapps", "common", "Blender")
        if sys.platform == "win32":
            candidates.append(os.path.join(common_blender, "blender.exe"))
        elif sys.platform == "darwin":
            candidates.append(
                os.path.join(common_blender, "Blender.app", "Contents", "MacOS", "Blender")
            )
            candidates.append(os.path.join(common_blender, "blender"))
        else:
            candidates.append(os.path.join(common_blender, "blender"))
    return candidates


def _standard_install_candidates() -> List[str]:
    patterns: List[str] = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_app_data = os.environ.get(
            "LOCALAPPDATA", os.path.expanduser(r"~\AppData\Local")
        )
        user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        patterns.extend(
            [
                os.path.join(pf, "Blender Foundation", "Blender*", "blender.exe"),
                os.path.join(pf86, "Blender Foundation", "Blender*", "blender.exe"),
                os.path.join(
                    local_app_data, "Programs", "Blender Foundation", "Blender*", "blender.exe"
                ),
                os.path.join(
                    local_app_data,
                    "Microsoft",
                    "WindowsApps",
                    "BlenderFoundation.Blender*",
                    "Blender",
                    "blender.exe",
                ),
                os.path.join(
                    user_profile, "scoop", "apps", "blender", "current", "blender.exe"
                ),
                os.path.join(user_profile, "scoop", "apps", "blender", "*", "blender.exe"),
            ]
        )
    elif sys.platform == "darwin":
        patterns.extend(
            [
                "/Applications/Blender*.app/Contents/MacOS/Blender",
                os.path.expanduser("~/Applications/Blender*.app/Contents/MacOS/Blender"),
            ]
        )
    else:
        patterns.extend(
            [
                "/usr/bin/blender",
                "/usr/local/bin/blender",
                "/snap/bin/blender",
                os.path.expanduser("~/blender*/blender"),
                "/opt/blender*/blender",
            ]
        )

    matched: List[str] = []
    for pattern in patterns:
        if "*" in pattern or "?" in pattern:
            hits = sorted(glob.glob(pattern), reverse=True)
            matched.extend(hits)
        elif os.path.isfile(pattern):
            matched.append(pattern)
    return matched


def _normalise_executable_path(path: str) -> str:
    cleaned = os.path.expanduser((path or "").strip().strip('"'))
    if not cleaned:
        return ""
    if sys.platform == "darwin" and cleaned.endswith(".app"):
        macos_bin = os.path.join(cleaned, "Contents", "MacOS", "Blender")
        if os.path.isfile(macos_bin):
            return os.path.abspath(macos_bin)
    if os.path.isdir(cleaned):
        exe_name = "blender.exe" if sys.platform == "win32" else "blender"
        candidate = os.path.join(cleaned, exe_name)
        if os.path.isfile(candidate):
            cleaned = candidate
    resolved = os.path.abspath(cleaned)
    if os.path.exists(resolved):
        try:
            resolved = os.path.realpath(resolved)
        except OSError:
            pass
    return resolved


# ── Headless Blender Version Probe ───────────────────────────────────────────


def _clean_blender_env() -> Dict[str, str]:
    env = dict(os.environ)
    for key in _PYTHON_ENV_VARS_TO_STRIP:
        env.pop(key, None)
    return env


def probe(
    path: str,
    *,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> Optional[VersionTuple]:
    """Run ``blender -b --factory-startup --python-expr`` to determine its version.

    Returns ``(major, minor, patch)`` or ``None`` if *path* is missing, fails, or
    times out after *timeout* seconds.
    """
    exe_path = _normalise_executable_path(path)
    if not exe_path or not os.path.isfile(exe_path):
        return None

    expr = f"import bpy; v = bpy.app.version; print('{_VERSION_MARKER}%d.%d.%d' % (v[0], v[1], v[2]))"
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

    try:
        proc = subprocess.run(
            [exe_path, "-b", "--factory-startup", "--python-expr", expr],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_blender_env(),
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("Blender version probe timed out after %.1fs: %s", timeout, exe_path)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Blender version probe failed for %s: %s", exe_path, exc)
        return None

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    for line in combined.splitlines():
        if _VERSION_MARKER in line:
            ver_part = line.split(_VERSION_MARKER, 1)[1].strip()
            parsed = parse_version_string(ver_part)
            if parsed is not None:
                return parsed

    log.debug(
        "Blender probe returned exit code %s without version marker for %s",
        proc.returncode,
        exe_path,
    )
    return None


# ── Discovery & Publication ──────────────────────────────────────────────────


def _publish_blender_to_client(
    client: Optional[BKClient],
    exe_path: str,
    version: VersionTuple,
) -> None:
    if client is None:
        return
    try:
        client.set_executable("blender", exe_path, version=format_version(version))
        log.info(
            "Published Blender %s (%s) to bk_client executable registry.",
            format_version(version),
            exe_path,
        )
    except Exception as exc:
        log.debug("Could not publish Blender path to bk_client: %s", exc)


def _check_candidate(
    raw_path: str,
    *,
    min_version: VersionTuple,
    timeout: float,
    probed_cache: Dict[str, Optional[VersionTuple]],
) -> Optional[Tuple[str, VersionTuple]]:
    exe_path = _normalise_executable_path(raw_path)
    if not exe_path or not os.path.isfile(exe_path):
        return None

    cache_key = exe_path.lower() if sys.platform == "win32" else exe_path
    if cache_key in probed_cache:
        version = probed_cache[cache_key]
    else:
        version = probe(exe_path, timeout=timeout)
        probed_cache[cache_key] = version

    if version is None:
        return None
    if version < min_version:
        log.info(
            "Skipping Blender at %s (version %s < minimum %s)",
            exe_path,
            format_version(version),
            format_version(min_version),
        )
        return None
    return exe_path, version


def find_blender_with_version(
    user_pref_path: str = "",
    client: Optional[BKClient] = None,
    *,
    min_version: VersionTuple = MIN_BLENDER_VERSION,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    publish: bool = True,
) -> Optional[Tuple[str, VersionTuple]]:
    """Locate a supported Blender executable and return ``(path, version_tuple)``.

    Search order:
    1. *user_pref_path*
    2. ``bk_client`` executable registry (``executable/get?name=blender``)
    3. ``$BLENDER_PATH`` environment variable
    4. System ``PATH`` (``shutil.which("blender")``)
    5. Every Steam library (via ``HKCU\\Software\\Valve\\Steam`` + ``libraryfolders.vdf``)
    6. Standard OS installation globs (Program Files, LocalAppData, scoop, ``/Applications``)
    7. Pick highest version across discovered installations.
    """
    probed_cache: Dict[str, Optional[VersionTuple]] = {}

    # 1. Explicit user preference path
    if user_pref_path and user_pref_path.strip():
        hit = _check_candidate(
            user_pref_path,
            min_version=min_version,
            timeout=timeout,
            probed_cache=probed_cache,
        )
        if hit is not None:
            if publish:
                _publish_blender_to_client(client, hit[0], hit[1])
            return hit

    # 2. bk_client executable registry
    if client is not None:
        for entry in client.get_executable("blender"):
            reg_path = str(entry.get("path") or "")
            hit = _check_candidate(
                reg_path,
                min_version=min_version,
                timeout=timeout,
                probed_cache=probed_cache,
            )
            if hit is not None:
                if publish:
                    _publish_blender_to_client(client, hit[0], hit[1])
                return hit

    # 3. $BLENDER_PATH environment variable
    env_blender = os.environ.get("BLENDER_PATH", "").strip()
    if env_blender:
        hit = _check_candidate(
            env_blender,
            min_version=min_version,
            timeout=timeout,
            probed_cache=probed_cache,
        )
        if hit is not None:
            if publish:
                _publish_blender_to_client(client, hit[0], hit[1])
            return hit

    # 4. System PATH
    which_blender = shutil.which("blender")
    if which_blender:
        hit = _check_candidate(
            which_blender,
            min_version=min_version,
            timeout=timeout,
            probed_cache=probed_cache,
        )
        if hit is not None:
            if publish:
                _publish_blender_to_client(client, hit[0], hit[1])
            return hit

    # 5 & 6. Every Steam library + standard install globs -> pick highest version
    discovered_candidates: List[str] = []
    discovered_candidates.extend(steam_blender_candidates())
    discovered_candidates.extend(_standard_install_candidates())

    best: Optional[Tuple[str, VersionTuple]] = None
    for candidate in discovered_candidates:
        hit = _check_candidate(
            candidate,
            min_version=min_version,
            timeout=timeout,
            probed_cache=probed_cache,
        )
        if hit is None:
            continue
        if best is None or hit[1] > best[1]:
            best = hit

    if best is not None:
        if publish:
            _publish_blender_to_client(client, best[0], best[1])
        return best

    return None


def find_blender(
    user_pref_path: str = "",
    client: Optional[BKClient] = None,
    *,
    min_version: VersionTuple = MIN_BLENDER_VERSION,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    publish: bool = True,
) -> Optional[str]:
    """Locate a supported Blender executable and return its path (or ``None``)."""
    found = find_blender_with_version(
        user_pref_path=user_pref_path,
        client=client,
        min_version=min_version,
        timeout=timeout,
        publish=publish,
    )
    return found[0] if found is not None else None


# ── KB_* Stdout Protocol & Headless Export Runner ────────────────────────────

DEFAULT_EXPORT_TIMEOUT_SECONDS = 600.0
LOG_TAIL_LINE_COUNT = 30


@dataclass(frozen=True)
class KBLine:
    """Parsed ``KB_*`` stdout protocol line emitted by ``kb_export.py``."""

    kind: str  # "progress" | "warning" | "done" | "error"
    tag: str  # "KB_PROGRESS" | "KB_WARNING" | "KB_DONE" | "KB_ERROR"
    fraction: Optional[float] = None
    message: str = ""
    path: str = ""

    @property
    def type(self) -> str:
        return self.kind

    @property
    def progress(self) -> Optional[float]:
        return self.fraction

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "type": self.kind,
            "tag": self.tag,
            "fraction": self.fraction,
            "progress": self.fraction,
            "message": self.message,
            "path": self.path,
        }

    def __getitem__(self, key: str) -> Any:
        data = self.to_dict()
        if key in data:
            return data[key]
        raise KeyError(key)


@dataclass
class KBExportResult:
    """Result of running ``kb_export.py`` inside headless Blender."""

    returncode: int
    manifest_path: str = ""
    warnings: List[str] = field(default_factory=list)
    error_message: str = ""
    log_tail: List[str] = field(default_factory=list)
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.cancelled
            and self.returncode == 0
            and not self.error_message
            and bool(self.manifest_path)
        )


def parse_kb_line(line: str) -> Optional[KBLine]:
    """Parse a single stdout line for ``KB_PROGRESS``, ``KB_WARNING``, ``KB_DONE``, or ``KB_ERROR``.

    Returns a :class:`KBLine` if the line matches the protocol, or ``None`` for
    ordinary Blender log lines.
    """
    if not isinstance(line, str):
        return None
    stripped = line.strip()
    if not stripped.startswith("KB_"):
        return None

    if stripped == "KB_PROGRESS" or stripped.startswith("KB_PROGRESS "):
        rest = stripped[len("KB_PROGRESS") :].strip()
        if not rest:
            return None
        parts = rest.split(None, 1)
        try:
            raw_frac = float(parts[0])
        except ValueError:
            return None
        if raw_frac != raw_frac:  # NaN check
            return None
        frac = max(0.0, min(1.0, raw_frac))
        msg = parts[1].strip() if len(parts) > 1 else ""
        return KBLine(kind="progress", tag="KB_PROGRESS", fraction=frac, message=msg)

    if stripped == "KB_WARNING" or stripped.startswith("KB_WARNING "):
        msg = stripped[len("KB_WARNING") :].strip()
        return KBLine(kind="warning", tag="KB_WARNING", message=msg)

    if stripped == "KB_DONE" or stripped.startswith("KB_DONE "):
        manifest_path = stripped[len("KB_DONE") :].strip()
        if not manifest_path:
            return None
        return KBLine(
            kind="done",
            tag="KB_DONE",
            message=manifest_path,
            path=manifest_path,
        )

    if stripped == "KB_ERROR" or stripped.startswith("KB_ERROR "):
        msg = stripped[len("KB_ERROR") :].strip()
        return KBLine(kind="error", tag="KB_ERROR", message=msg or "Unknown Blender export error")

    return None


def kb_export_script_path() -> str:
    """Return the path to ``plugin/kitbridge/kitbridge/blender_scripts/kb_export.py``."""
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(pkg_dir, "blender_scripts", "kb_export.py")


def _terminate_process(proc: subprocess.Popen[str]) -> None:
    try:
        proc.kill()
    except OSError as exc:
        log.debug("Failed to kill Blender process: %s", exc)
    try:
        proc.wait(timeout=3.0)
    except (OSError, subprocess.SubprocessError):
        pass


def run_kb_export(
    blender_exe: str,
    blend_path: str,
    params: Dict[str, Any],
    *,
    script_path: Optional[str] = None,
    on_progress: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = DEFAULT_EXPORT_TIMEOUT_SECONDS,
    tail_lines: int = LOG_TAIL_LINE_COUNT,
) -> KBExportResult:
    """Run ``blender -b --factory-startup <blend_path> --python kb_export.py -- <params.json>``.

    Streams stdout lines, parses ``KB_*`` messages, kills the subprocess on
    cancellation or timeout, and captures the last *tail_lines* of output.
    """
    exe_path = _normalise_executable_path(blender_exe)
    if not exe_path or not os.path.isfile(exe_path):
        return KBExportResult(
            returncode=127,
            error_message=f"Blender executable not found: {blender_exe}",
        )

    resolved_script = script_path or kb_export_script_path()
    if not os.path.isfile(resolved_script):
        return KBExportResult(
            returncode=127,
            error_message=f"Export script not found: {resolved_script}",
        )

    output_dir = str(params.get("output_dir") or os.path.dirname(os.path.abspath(blend_path)) or ".")
    os.makedirs(output_dir, exist_ok=True)

    fd, params_json_path = tempfile.mkstemp(
        prefix="kb_export_params_",
        suffix=".json",
        dir=output_dir,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(params, fh, indent=2)

        cmd = [
            exe_path,
            "-b",
            "--factory-startup",
            os.path.abspath(blend_path),
            "--python",
            os.path.abspath(resolved_script),
            "--",
            os.path.abspath(params_json_path),
        ]
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        log.info("Running headless Blender export: %s", " ".join(cmd))

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_clean_blender_env(),
                creationflags=creation_flags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return KBExportResult(
                returncode=1,
                error_message=f"Failed to launch Blender: {exc}",
            )

        line_queue: queue.Queue[Optional[str]] = queue.Queue()

        def _reader() -> None:
            try:
                if proc.stdout is not None:
                    for raw_line in proc.stdout:
                        line_queue.put(raw_line.rstrip("\r\n"))
            except Exception as exc:
                log.debug("Blender stdout reader error: %s", exc)
            finally:
                line_queue.put(None)

        reader_thread = threading.Thread(
            target=_reader,
            name="kitbridge-blender-stdout",
            daemon=True,
        )
        reader_thread.start()

        all_lines: List[str] = []
        warnings: List[str] = []
        manifest_path = ""
        error_message = ""
        timed_out = False
        cancelled = False
        eof_reached = False
        deadline = time.monotonic() + max(1.0, float(timeout))

        while not eof_reached:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _terminate_process(proc)
                break

            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process(proc)
                break

            try:
                item = line_queue.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None and not reader_thread.is_alive():
                    break
                continue

            if item is None:
                eof_reached = True
                break

            all_lines.append(item)
            if len(all_lines) > max(tail_lines * 4, 200):
                all_lines = all_lines[-max(tail_lines * 2, 100) :]

            parsed = parse_kb_line(item)
            if parsed is None:
                log.debug("[blender] %s", item)
                continue

            if parsed.kind == "progress":
                if on_progress is not None and parsed.fraction is not None:
                    try:
                        on_progress(parsed.fraction, parsed.message)
                    except Exception as exc:
                        log.debug("on_progress callback raised: %s", exc)
            elif parsed.kind == "warning":
                warnings.append(parsed.message)
                log.warning("[kb_export] %s", parsed.message)
            elif parsed.kind == "done":
                manifest_path = parsed.path
            elif parsed.kind == "error":
                error_message = parsed.message
                log.error("[kb_export] %s", parsed.message)

        try:
            rc = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            rc = proc.returncode if proc.returncode is not None else -1

        reader_thread.join(timeout=1.0)
        while True:
            try:
                rem = line_queue.get_nowait()
            except queue.Empty:
                break
            if rem is not None:
                all_lines.append(rem)

        tail = all_lines[-tail_lines:] if tail_lines > 0 else []
        if cancelled and not error_message:
            error_message = "Conversion cancelled."
        elif timed_out and not error_message:
            error_message = f"Blender conversion timed out after {timeout:.0f}s."

        return KBExportResult(
            returncode=int(rc if rc is not None else -1),
            manifest_path=manifest_path,
            warnings=warnings,
            error_message=error_message,
            log_tail=tail,
            timed_out=timed_out,
            cancelled=cancelled,
        )
    finally:
        try:
            if os.path.exists(params_json_path):
                os.remove(params_json_path)
        except OSError:
            pass
