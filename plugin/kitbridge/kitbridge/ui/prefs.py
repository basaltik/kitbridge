"""Small JSON preferences file for the Kitbridge window. Pure Python, no c4d."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any, Dict, Optional

from kitbridge.core import paths

log = logging.getLogger("kitbridge")

RESOLUTIONS = ("512", "1024", "2048", "4096", "8192", "ORIGINAL")
TABS = ("model", "material", "hdr", "downloaded")

DEFAULTS: Dict[str, Any] = {
    "resolution": "2048",
    "blender_path": "",
    "free_only": False,
    "accepted_disclaimer": False,
    "last_tab": "model",
    "show_main_menu": True,
    "download_dir": "",
    "renderer": "auto",            # material system for imports; "auto" = the document's render engine
    "remember_renderer": False,    # keep "renderer" across sessions instead of resetting to auto
}

RENDERER_CHOICES = ("auto", "redshift", "arnold", "standard")


def prefs_path() -> str:
    return os.path.join(paths.global_dir(create=True), "kitbridge", "prefs.json")


def _sanitise(data: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        if key in data and isinstance(data[key], type(default)):
            out[key] = data[key]
    if out["resolution"] not in RESOLUTIONS:
        out["resolution"] = DEFAULTS["resolution"]
    if out["last_tab"] not in TABS:
        out["last_tab"] = DEFAULTS["last_tab"]
    if out["renderer"] not in RENDERER_CHOICES:
        out["renderer"] = DEFAULTS["renderer"]
    return out


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """Return prefs merged over defaults; a missing or broken file gives the defaults."""
    target = path or prefs_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return dict(DEFAULTS)
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable Kitbridge prefs %s: %s", target, exc)
        return dict(DEFAULTS)
    return _sanitise(data if isinstance(data, dict) else {})


def save(values: Dict[str, Any], path: Optional[str] = None) -> None:
    """Atomically write *values* (unknown keys are dropped)."""
    target = path or prefs_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    clean = _sanitise(values)
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=1)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
