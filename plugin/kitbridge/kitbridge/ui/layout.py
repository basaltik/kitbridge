"""Thumbnail grid and window layout geometry, hit testing, and tier logic.

Pure Python so it can be unit-tested without Cinema 4D.
Docs reference: docs/spec-ui-v2.md
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

MARGIN = 18

CLIPBOARD_PATTERN = re.compile(r"^asset_base_id:([0-9a-fA-F-]{36})$")

TAB_SPECS = (
    ("model", "Models", 56),
    ("material", "Materials", 68),
    ("hdr", "HDRIs", 50),
    ("downloaded", "Downloaded", 84),
)

# Header geometry (band, chips, tabs), in px from the top of the header area.
HEADER_BAND_H = 48
CHIPS_Y = 58
TABS_Y = 90
TAB_H = 28
HEADER_H = 122
SEARCH_BUTTON_SIZE = (34, 34)
SETTINGS_BUTTON_SIZE = (34, 34)

TIER_SEGMENTS = ("ALL", "FREE", "FULL PLAN")  # PAID omitted because BlenderKit API has no individual sales
DOWNLOADED_SEGMENTS = ("ALL", "MODELS", "MATERIALS", "HDRIS")
RES_SEGMENTS = ("1K", "2K", "4K", "8K")
RES_VALUES = {"1K": "1024", "2K": "2048", "4K": "4096", "8K": "8192"}
RES_TO_LABEL = {"1024": "1K", "2048": "2K", "4096": "4K", "8192": "8K"}


class Rect(NamedTuple):
    x: int
    y: int
    w: int
    h: int

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px < self.x + self.w and self.y <= py < self.y + self.h

    def intersects_rows(self, top: int, bottom: int) -> bool:
        return self.y < bottom and self.y + self.h > top


class GridMetrics:
    """Card dimensions and layout spacing."""

    def __init__(
        self,
        card_w: int = 140,
        card_h: int = 176,
        thumb_h: int = 112,
        gap: int = 12,
        margin: int = 18,
        cell: Optional[int] = None,
        label: Optional[int] = None,
    ) -> None:
        if cell is not None:
            card_w = cell
        if label is not None:
            card_h = card_w + label
            thumb_h = card_w
        self.card_w = int(card_w)
        self.card_h = int(card_h)
        self.thumb_h = int(thumb_h)
        self.gap = int(gap)
        self.margin = int(margin)

    @property
    def cell(self) -> int:
        return self.card_w

    @property
    def label(self) -> int:
        return self.card_h - self.card_w

    @property
    def pitch_x(self) -> int:
        return self.card_w + self.gap

    @property
    def pitch_y(self) -> int:
        return self.card_h + self.gap

    def _replace(self, **kwargs: Any) -> "GridMetrics":
        new_card_w = kwargs.get("card_w", kwargs.get("cell", self.card_w))
        new_card_h = kwargs.get("card_h", self.card_h)
        new_thumb_h = kwargs.get("thumb_h", self.thumb_h)
        new_gap = kwargs.get("gap", self.gap)
        new_margin = kwargs.get("margin", self.margin)
        return GridMetrics(
            card_w=new_card_w,
            card_h=new_card_h,
            thumb_h=new_thumb_h,
            gap=new_gap,
            margin=new_margin,
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, GridMetrics):
            return False
        return (
            self.card_w == other.card_w
            and self.card_h == other.card_h
            and self.thumb_h == other.thumb_h
            and self.gap == other.gap
            and self.margin == other.margin
        )


def columns(width: int, m: GridMetrics) -> int:
    usable = max(0, width - 2 * m.margin + m.gap)
    return max(1, usable // m.pitch_x)


def fit(width: int, base: GridMetrics, max_cell: int = 240) -> GridMetrics:
    """Stretch cells so the columns fill width exactly with equal side margins."""
    cols = columns(width, base)
    usable = width - 2 * base.margin - (cols - 1) * base.gap
    card_w = max(base.card_w // 2, min(max_cell, usable // cols))
    scale = card_w / float(base.card_w) if base.card_w > 0 else 1.0
    card_h = int(round(base.card_h * scale))
    thumb_h = int(round(base.thumb_h * scale))
    return base._replace(card_w=int(card_w), card_h=card_h, thumb_h=thumb_h)


def cell_rect(index: int, cols: int, m: GridMetrics) -> Rect:
    row, col = divmod(index, max(1, cols))
    return Rect(
        m.margin + col * m.pitch_x,
        m.margin + row * m.pitch_y,
        m.card_w,
        m.card_h,
    )


def content_height(count: int, cols: int, m: GridMetrics) -> int:
    if count <= 0:
        return 2 * m.margin
    rows = (count + cols - 1) // max(1, cols)
    return 2 * m.margin + rows * m.pitch_y - m.gap


def hit_test(px: float, py: float, count: int, cols: int, m: GridMetrics) -> Optional[int]:
    """Index of the cell under (px, py) or None (gaps and margins miss)."""
    px, py = int(px), int(py)
    if px < m.margin or py < m.margin:
        return None
    col, rx = divmod(px - m.margin, m.pitch_x)
    row, ry = divmod(py - m.margin, m.pitch_y)
    if col >= cols or rx >= m.card_w or ry >= m.card_h:
        return None
    index = row * cols + col
    return index if index < count else None


def ellipsize(text: str, max_chars: int) -> str:
    if max_chars <= 1:
        return text[:max_chars]
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


# ── Header & Tabs Geometry ──────────────────────────────────────────────────

def tab_rects(margin: int = 18, y: int = TABS_Y, gap: int = 4) -> List[Tuple[str, Rect]]:
    """Return [(tab_key, Rect), ...] for the 4 tabs."""
    res = []
    x = margin
    for key, _, width in TAB_SPECS:
        res.append((key, Rect(x, y, width, TAB_H)))
        x += width + gap
    return res


def tab_hit_test(px: float, py: float, margin: int = 18, y: int = TABS_Y) -> Optional[str]:
    """Check if (px, py) hits any of the 4 tab buttons."""
    if not (y <= py < y + TAB_H):
        return None
    for key, rect in tab_rects(margin=margin, y=y):
        if rect.contains(px, py):
            return key
    return None


def user_chip_rect(width: int, name: str, margin: int = 18, y: int = CHIPS_Y, h: int = 24) -> Rect:
    """Calculate the right-aligned user chip Rect."""
    # Approximate width based on name length: 10px padding left + dot (14px) + text + 10px padding right
    text_len = max(len(name), 6)
    chip_w = max(70, text_len * 8 + 32)
    return Rect(width - margin - chip_w, y, chip_w, h)


# ── Filter Row Geometry ─────────────────────────────────────────────────────

def filter_row_rects(
    width: int,
    tab: str,
    margin: int = 18,
    y: int = 2,
    h: int = 22,
    gap: int = 2,
) -> Tuple[List[Tuple[str, Rect]], List[Tuple[str, Rect]]]:
    """Return (left_segments, resolution_segments) Rects.

    Left segments: tier (ALL, FREE, FULL PLAN) or downloaded type (ALL, MODELS, MATERIALS, HDRIS).
    Right segments: resolution (1K, 2K, 4K, 8K) 30 px cells.
    """
    left_specs = DOWNLOADED_SEGMENTS if tab == "downloaded" else TIER_SEGMENTS
    left_rects: List[Tuple[str, Rect]] = []
    x = margin
    for label in left_specs:
        # Segment width based on label
        w = max(30, len(label) * 8 + 14)
        left_rects.append((label, Rect(x, y, w, h)))
        x += w + gap

    # Right side: 1K 2K 4K 8K (30 px each, 2 px gap)
    res_rects: List[Tuple[str, Rect]] = []
    rx = width - margin - (4 * 30 + 3 * gap)
    for res_lbl in RES_SEGMENTS:
        res_rects.append((res_lbl, Rect(rx, y, 30, h)))
        rx += 30 + gap

    return left_rects, res_rects


def filter_row_hit_test(
    px: float,
    py: float,
    width: int,
    tab: str,
    margin: int = 18,
    y: int = 2,
    h: int = 22,
) -> Optional[Tuple[str, str]]:
    """Return ('tier', val) or ('type', val) or ('resolution', val) or None."""
    if not (y <= py < y + h):
        return None
    left_rects, res_rects = filter_row_rects(width, tab, margin=margin, y=y, h=h)
    kind = "type" if tab == "downloaded" else "tier"
    for label, rect in left_rects:
        if rect.contains(px, py):
            return (kind, label)
    for res_lbl, rect in res_rects:
        if rect.contains(px, py):
            return ("resolution", RES_VALUES.get(res_lbl, res_lbl))
    return None


# ── Settings Screen Geometry ────────────────────────────────────────────────

SETTINGS_HEIGHT = 516
SETTINGS_FOLDERS = ("models", "materials", "hdris")
SETTINGS_RESOLUTIONS = (
    ("512", "512", 40),
    ("1K", "1024", 40),
    ("2K", "2048", 40),
    ("4K", "4096", 40),
    ("8K", "8192", 40),
    ("ORIGINAL", "ORIGINAL", 66),
)


def settings_rects(width: int, margin: int = MARGIN) -> Dict[str, Rect]:
    """Clickable and drawn parts of the settings screen, by name."""
    inner = max(280, width - 2 * margin)
    right = margin + inner
    browse_w = 92
    rects: Dict[str, Rect] = {
        "folder_field": Rect(margin, 40, inner - browse_w - 8, 32),
        "browse_folder": Rect(right - browse_w, 40, browse_w, 32),
        "default_folder": Rect(right - 110, 170, 110, 26),
        "blender_field": Rect(margin, 300, inner - browse_w - 8, 32),
        "browse_blender": Rect(right - browse_w, 300, browse_w, 32),
        "auto_blender": Rect(right - 64, 340, 64, 26),
        "clear_cache": Rect(right - 110, 402, 110, 26),
        "toggle_menu": Rect(margin, 474, min(inner, 300), 18),
    }
    gap = 8
    tile_w = (inner - gap * (len(SETTINGS_FOLDERS) - 1)) // len(SETTINGS_FOLDERS)
    for i, folder in enumerate(SETTINGS_FOLDERS):
        rects[f"open:{folder}"] = Rect(margin + i * (tile_w + gap), 106, tile_w, 54)
    x = margin
    for label, value, seg_w in SETTINGS_RESOLUTIONS:
        rects[f"res:{value}"] = Rect(x, 238, seg_w, 22)
        x += seg_w + 2
    return rects


def settings_hit_test(px: float, py: float, width: int, margin: int = MARGIN) -> Optional[str]:
    """Name of the clickable part under the point (the path fields are display only)."""
    for name, rect in settings_rects(width, margin).items():
        if not name.endswith("_field") and rect.contains(px, py):
            return name
    return None


# ── Tier Classification & Filtering ─────────────────────────────────────────

def classify_tier(item: Dict[str, Any]) -> str:
    """Classify an asset into 'FREE', 'FULL PLAN', or 'PAID'."""
    if item.get("isFree") or str(item.get("access") or "").lower() == "free":
        return "FREE"
    if item.get("isForSale") or (bool(item.get("basePrice")) and not item.get("isFree")):
        return "PAID"
    return "FULL PLAN"


def filter_items(
    items: List[Dict[str, Any]],
    tab: str,
    tier_filter: str = "ALL",
    type_filter: str = "ALL",
) -> List[Dict[str, Any]]:
    """Filter search results or cached assets."""
    if tab == "downloaded":
        if type_filter == "ALL":
            return list(items)
        type_map = {"MODELS": "model", "MATERIALS": "material", "HDRIS": "hdr"}
        target = type_map.get(type_filter, type_filter.lower())
        return [i for i in items if str(i.get("assetType") or "").lower() == target]
    else:
        if tier_filter == "ALL":
            return list(items)
        return [i for i in items if classify_tier(i) == tier_filter]


# ── Clipboard Code Handling ─────────────────────────────────────────────────

def parse_clipboard_code(text: str) -> Optional[str]:
    """Extract UUID if string matches 'asset_base_id:<uuid>', else None."""
    if not text:
        return None
    m = CLIPBOARD_PATTERN.match(text.strip())
    if m:
        return m.group(1).lower()
    return None
