"""Colour tokens and asset cache of the v2 window (docs/spec-ui-v2.md).

Pure Python, safe to import without Cinema 4D.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

TOKENS: Dict[str, str] = {
    "bg": "#262626",            # window background
    "panel": "#2E2E2E",         # chips, inactive tabs, status box
    "card": "#363636",          # asset cards
    "field": "#1D1D1D",         # search field
    "line": "#424242",          # 1 px outlines
    "text": "#E2E2E2",          # primary text
    "muted": "#8E8E8E",         # secondary text
    "accent": "#4D7FD6",        # header band, active tab/segment, FULL PLAN tag, progress
    "accent_light": "#7FA8F0",  # "IN SCENE", status label
    "dark": "#161616",          # index diamond, busy overlay (78 % opacity), PAID tag bg
    "online": "#5BC46A",        # user dot
    "paid_label": "#9DBDF5",    # PAID tag label
    "track": "#555555",         # progress bar track
    "white": "#FFFFFF",
}

MARGIN = 18
BUSY_OPACITY = 0.78


def rgb255(name_or_hex: str) -> Tuple[int, int, int]:
    """``"accent"`` or ``"#4D7FD6"`` → ``(77, 127, 214)``."""
    value = TOKENS.get(name_or_hex, name_or_hex).lstrip("#")
    if len(value) != 6:
        raise ValueError(f"not a colour: {name_or_hex!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb(name_or_hex: str) -> Tuple[float, float, float]:
    """Token or hex → floats in 0..1 (what ``c4d.Vector`` wants)."""
    r, g, b = rgb255(name_or_hex)
    return r / 255.0, g / 255.0, b / 255.0


def color_vector(name_or_hex: str) -> Any:
    """Return a ``c4d.Vector(r, g, b)`` if c4d is available, else ``(r, g, b)`` tuple."""
    r, g, b = rgb(name_or_hex)
    try:
        import c4d
        return c4d.Vector(r, g, b)
    except ImportError:
        return (r, g, b)


class AssetCache:
    """Loads and caches pre-rendered PNG bitmaps for C4D GeUserAreas."""

    def __init__(self, res_dir: Optional[str] = None) -> None:
        if res_dir is None:
            self.res_dir = os.path.normpath(
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "res")
            )
        else:
            self.res_dir = res_dir
        self.ui_dir = os.path.join(self.res_dir, "ui")
        self._cache: Dict[str, Any] = {}

    def get_path(self, name: str, scale: int = 1) -> str:
        if scale >= 2:
            p2 = os.path.join(self.ui_dir, f"{name}@2x.png")
            if os.path.isfile(p2):
                return p2
        p1 = os.path.join(self.ui_dir, f"{name}.png")
        if os.path.isfile(p1):
            return p1
        p_res = os.path.join(self.res_dir, f"{name}.png")
        if os.path.isfile(p_res):
            return p_res
        return ""

    def get(self, name: str, scale: int = 1) -> Any:
        key = f"{name}@{scale}"
        if key in self._cache:
            return self._cache[key]
        path = self.get_path(name, scale=scale)
        if not path:
            self._cache[key] = None
            return None
        try:
            import c4d
            bmp = c4d.bitmaps.BaseBitmap()
            if bmp.InitWith(path)[0] == c4d.IMAGERESULT_OK:
                self._cache[key] = bmp
                return bmp
        except ImportError:
            pass
        self._cache[key] = path
        return path
