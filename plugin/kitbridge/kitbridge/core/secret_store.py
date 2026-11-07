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

"""OS credential vault storage (Windows CredMan / macOS Keychain / 0600 JSON fallback)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from typing import Dict, Optional

from . import paths

log = logging.getLogger("kitbridge")

SERVICE_NAME = "Kitbridge"
_CLI_TIMEOUT = 5.0

_lock = threading.Lock()
_backend: Optional[str] = None


def _fallback_path() -> str:
    return paths.secrets_file_path(create_parent=True)


def _detect_backend() -> str:
    override = os.environ.get("KITBRIDGE_SECRET_BACKEND", "").strip().lower()
    if override in ("wincred", "macos-keychain", "secret-tool", "file"):
        return override

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.WinDLL("advapi32")
            return "wincred"
        except OSError:
            return "file"

    if sys.platform == "darwin":
        if shutil.which("security"):
            return "macos-keychain"
        return "file"

    if shutil.which("secret-tool"):
        return "secret-tool"
    return "file"


def backend_name() -> str:
    """Return the active secret-store backend identifier."""
    global _backend
    with _lock:
        if _backend is None:
            _backend = _detect_backend()
            if _backend == "file":
                log.warning(
                    "No OS credential vault available; storing secrets in 0600 file at %s",
                    _fallback_path(),
                )
            else:
                log.debug("Secret store backend: %s", _backend)
        return _backend


def reset_backend() -> None:
    """Reset cached backend detection (used by tests)."""
    global _backend
    with _lock:
        _backend = None


# ── Windows Credential Manager (ctypes) ──────────────────────────────────────


def _win_target(name: str) -> str:
    return f"{SERVICE_NAME}:{name}"


def _win_get(name: str) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_read = advapi32.CredReadW
    cred_read.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIAL)),
    )
    cred_read.restype = wintypes.BOOL
    cred_free = advapi32.CredFree
    cred_free.argtypes = (ctypes.c_void_p,)

    pcred = ctypes.POINTER(CREDENTIAL)()
    if not cred_read(_win_target(name), 1, 0, ctypes.byref(pcred)):
        return None
    try:
        cred = pcred.contents
        size = int(cred.CredentialBlobSize)
        if size == 0:
            return ""
        blob = ctypes.string_at(cred.CredentialBlob, size)
        return blob.decode("utf-8")
    finally:
        cred_free(pcred)


def _win_set(name: str, value: str) -> bool:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        )

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_write = advapi32.CredWriteW
    cred_write.argtypes = (ctypes.POINTER(CREDENTIAL), wintypes.DWORD)
    cred_write.restype = wintypes.BOOL

    blob = value.encode("utf-8")
    blob_buf = ctypes.create_string_buffer(blob, len(blob))

    cred = CREDENTIAL()
    cred.Flags = 0
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _win_target(name)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = SERVICE_NAME
    return bool(cred_write(ctypes.byref(cred), 0))


def _win_delete(name: str) -> None:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_delete = advapi32.CredDeleteW
    cred_delete.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    cred_delete.restype = wintypes.BOOL
    cred_delete(_win_target(name), 1, 0)


# ── macOS Keychain (security CLI) ────────────────────────────────────────────


def _mac_get(name: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE_NAME, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("security find-generic-password failed: %s", exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def _mac_set(name: str, value: str) -> bool:
    try:
        out = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                SERVICE_NAME,
                "-a",
                name,
                "-w",
                value,
            ],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("security add-generic-password failed: %s", exc)
        return False
    return out.returncode == 0


def _mac_delete(name: str) -> None:
    try:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE_NAME, "-a", name],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("security delete-generic-password failed: %s", exc)


# ── Linux Secret Service (secret-tool CLI) ───────────────────────────────────


def _secret_tool_get(name: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["secret-tool", "lookup", "service", SERVICE_NAME, "account", name],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("secret-tool lookup failed: %s", exc)
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _secret_tool_set(name: str, value: str) -> bool:
    try:
        out = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label",
                f"{SERVICE_NAME} {name}",
                "service",
                SERVICE_NAME,
                "account",
                name,
            ],
            input=value,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("secret-tool store failed: %s", exc)
        return False
    return out.returncode == 0


def _secret_tool_delete(name: str) -> None:
    try:
        subprocess.run(
            ["secret-tool", "clear", "service", SERVICE_NAME, "account", name],
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("secret-tool clear failed: %s", exc)


# ── 0600 JSON File Fallback ──────────────────────────────────────────────────


def _file_read_all(file_path: Optional[str] = None) -> Dict[str, str]:
    target = file_path or _fallback_path()
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _file_write_all(data: Dict[str, str], file_path: Optional[str] = None) -> bool:
    target = file_path or _fallback_path()
    try:
        parent = os.path.dirname(os.path.abspath(target))
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        finally:
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    except OSError as exc:
        log.error("Could not write fallback secret store file %s: %s", target, exc)
        return False
    return True


def _file_get(name: str, file_path: Optional[str] = None) -> Optional[str]:
    with _lock:
        return _file_read_all(file_path).get(name)


def _file_set(name: str, value: str, file_path: Optional[str] = None) -> bool:
    with _lock:
        data = _file_read_all(file_path)
        data[name] = value
        return _file_write_all(data, file_path)


def _file_delete(name: str, file_path: Optional[str] = None) -> None:
    target = file_path or _fallback_path()
    with _lock:
        data = _file_read_all(target)
        if name not in data:
            return
        del data[name]
        if data:
            _file_write_all(data, target)
        else:
            try:
                os.remove(target)
            except OSError:
                pass


# ── Public API ───────────────────────────────────────────────────────────────


def get_secret(
    name: str,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> Optional[str]:
    """Retrieve the stored secret *name*, or ``None`` if not present."""
    active = backend or backend_name()
    try:
        if active == "wincred":
            return _win_get(name)
        if active == "macos-keychain":
            return _mac_get(name)
        if active == "secret-tool":
            return _secret_tool_get(name)
    except Exception as exc:
        log.warning("Native secret store read failed (%s); falling back to file: %s", active, exc)
        return _file_get(name, file_path=file_path)
    return _file_get(name, file_path=file_path)


def set_secret(
    name: str,
    value: str,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> bool:
    """Persist secret *name* to the active vault (or 0600 fallback file)."""
    active = backend or backend_name()
    try:
        if active == "wincred" and _win_set(name, value):
            return True
        if active == "macos-keychain" and _mac_set(name, value):
            return True
        if active == "secret-tool" and _secret_tool_set(name, value):
            return True
    except Exception as exc:
        log.warning("Native secret store write failed (%s); falling back to file: %s", active, exc)
    return _file_set(name, value, file_path=file_path)


def delete_secret(
    name: str,
    *,
    backend: Optional[str] = None,
    file_path: Optional[str] = None,
) -> None:
    """Delete secret *name* from the active vault and any fallback file."""
    active = backend or backend_name()
    try:
        if active == "wincred":
            _win_delete(name)
        elif active == "macos-keychain":
            _mac_delete(name)
        elif active == "secret-tool":
            _secret_tool_delete(name)
    except Exception as exc:
        log.debug("Native secret store delete failed (%s): %s", active, exc)
    _file_delete(name, file_path=file_path)
