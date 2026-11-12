"""Thumbnail grid drawn in a GeUserArea for Kitbridge v2.

Docs reference: docs/spec-ui-v2.md
"""

from __future__ import annotations

import logging
import os
import webbrowser
from typing import Any, Callable, Dict, List, Optional

import c4d

from kitbridge.ui import layout, theme
from kitbridge.ui.theme import AssetCache

log = logging.getLogger("kitbridge")

MENU_IMPORT = c4d.FIRST_POPUP_ID + 1
MENU_OPEN_WEB = c4d.FIRST_POPUP_ID + 2
MENU_REMOVE = c4d.FIRST_POPUP_ID + 3


def _cover_crop(src_w: int, src_h: int, dst_w: int, dst_h: int) -> tuple:
    """Centred source rect (x, y, w, h) that fills dst without distorting the image."""
    if src_w * dst_h > src_h * dst_w:
        crop_w = src_h * dst_w // max(1, dst_h)
        return (src_w - crop_w) // 2, 0, crop_w, src_h
    crop_h = src_w * dst_h // max(1, dst_w)
    return 0, (src_h - crop_h) // 2, src_w, crop_h


class AssetGrid(c4d.gui.GeUserArea):
    def __init__(
        self,
        app: Any,
        on_activate: Callable[[Dict[str, Any]], None],
        assets: Optional[AssetCache] = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.on_activate = on_activate
        self.assets = assets or AssetCache()
        self.items: List[Dict[str, Any]] = []
        self.tab = "model"
        self._base_metrics = layout.GridMetrics(card_w=140, card_h=176, thumb_h=112, gap=12, margin=18)
        self.metrics = self._base_metrics
        self._bitmaps: Dict[str, Optional[c4d.bitmaps.BaseBitmap]] = {}
        self._last_height = 0
        self._hover: Optional[int] = None
        self.highlighted_base_id: Optional[str] = None

    # ── data ──────────────────────────────────────────────────────────────

    def set_items(self, items: List[Dict[str, Any]]) -> None:
        self.items = items
        # Forget thumbnails that were not on disk (or half-written) yet, so they are tried again:
        # a failed load used to stay blank until the window was reopened.
        self._bitmaps = {p: b for p, b in self._bitmaps.items() if b is not None}
        self._relayout()
        self.Redraw()

    def refresh(self) -> None:
        self._bitmaps = {p: b for p, b in self._bitmaps.items() if b is not None}
        self._relayout()
        self.Redraw()

    def _relayout(self) -> None:
        self.metrics = layout.fit(self.GetWidth(), self._base_metrics)
        cols = layout.columns(self.GetWidth(), self.metrics)
        height = layout.content_height(len(self.items), cols, self.metrics)
        if height != self._last_height:
            self._last_height = height
            self.LayoutChanged()

    # ── GeUserArea overrides ──────────────────────────────────────────────

    def GetMinSize(self) -> tuple:
        return 160, max(self._last_height, 120)

    def Sized(self, w: int, h: int) -> None:
        self._relayout()
        self.Redraw()

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)
        if not self.items:
            self._draw_empty()
            return
        cols = layout.columns(self.GetWidth(), self.metrics)
        for index, item in enumerate(self.items):
            rect = layout.cell_rect(index, cols, self.metrics)
            if rect.intersects_rows(y1, y2 + 1):
                try:
                    self._draw_cell(rect, item, index)
                except Exception:
                    log.exception("Drawing grid cell %s failed", item.get("name"))

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        mid = msg.GetId()
        if mid == c4d.BFM_GETCURSORINFO:
            try:
                index = self._index_at(msg[c4d.BFM_DRAG_SCREENX], msg[c4d.BFM_DRAG_SCREENY], screen=True)
            except Exception:
                log.exception("Grid hover failed")
                index = None
            self._set_hover(index)
            if index is not None and index < len(self.items):
                result.SetId(c4d.BFM_GETCURSORINFO)
                result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
                result.SetString(c4d.RESULT_BUBBLEHELP, str(self.items[index].get("name") or ""))
            return True
        if mid == c4d.BFM_CURSORINFO_REMOVE:
            self._set_hover(None)
        return super().Message(msg, result)

    def _set_hover(self, index: Optional[int]) -> None:
        if index != self._hover:
            self._hover = index
            self.Redraw()

    def _index_at(self, gx: float, gy: float, screen: bool = False) -> Optional[int]:
        if screen:
            local = self.Screen2Local()
        else:
            local = self.Global2Local()
        x = float(gx + local["x"])
        y = float(gy + local["y"])
        cols = layout.columns(self.GetWidth(), self.metrics)
        return layout.hit_test(x, y, len(self.items), cols, self.metrics)

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        channel = msg[c4d.BFM_INPUT_CHANNEL]
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE or channel not in (
            c4d.BFM_INPUT_MOUSELEFT, c4d.BFM_INPUT_MOUSERIGHT
        ):
            return False
        index = self._index_at(msg[c4d.BFM_INPUT_X], msg[c4d.BFM_INPUT_Y])
        if index is None or index >= len(self.items):
            return True
        item = self.items[index]
        try:
            if channel == c4d.BFM_INPUT_MOUSELEFT:
                self.on_activate(item)
            elif channel == c4d.BFM_INPUT_MOUSERIGHT:
                self._context_menu(item)
        except Exception:
            log.exception("Grid click failed")
        return True

    # ── drawing helpers ───────────────────────────────────────────────────

    def _draw_empty(self) -> None:
        self.DrawSetTextCol(theme.color_vector("text"), c4d.COLOR_TRANS)
        text = "Searching…" if self.app.searching.get(self.tab, False) else "No assets to show yet."
        self.DrawText(text, self.metrics.margin, self.metrics.margin)

    def _draw_cell(self, rect: layout.Rect, item: Dict[str, Any], index: int) -> None:
        m = self.metrics
        x, y = rect.x, rect.y
        w = m.card_w
        h = m.card_h
        th = m.thumb_h
        self.DrawSetFont(c4d.FONT_BOLD)

        # 1. Card fill #363636
        self.DrawSetPen(theme.color_vector("card"))
        self.DrawRectangle(x, y, x + w - 1, y + h - 1)

        # 2. Thumbnail
        bmp = self._thumbnail(item)
        if bmp is not None:
            sx, sy, sw, sh = _cover_crop(bmp.GetBw(), bmp.GetBh(), w, th)
            self.DrawBitmap(bmp, x, y, w, th, sx, sy, sw, sh, c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA)
        else:
            self.DrawSetPen(theme.color_vector("dark"))
            self.DrawRectangle(x, y, x + w - 1, y + th - 1)
            self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)
            self.DrawText(str(item.get("assetType") or "").upper(), x + 10, y + th // 2 - 6)

        # 4. Tier tag top-right
        tier = layout.classify_tier(item)
        self._draw_tier_tag(x, y, w, tier)

        # 5. Busy overlay if job active
        base_id = str(item.get("assetBaseId") or "")
        state = self.app.jobs.get(base_id)
        if state is not None:
            self._draw_busy_overlay(x, y, w, th, state)

        # 6. Hover highlight: 2 px accent outline
        is_hovered = (self._hover is not None and self._hover < len(self.items) and self.items[self._hover] is item)
        is_highlighted = (self.highlighted_base_id and self.highlighted_base_id == base_id)
        if is_hovered or is_highlighted:
            self.DrawSetPen(theme.color_vector("accent"))
            for inset in (0, 1):
                left, top, right, bottom = x + inset, y + inset, x + w - 1 - inset, y + h - 1 - inset
                self.DrawLine(left, top, right, top)
                self.DrawLine(right, top, right, bottom)
                self.DrawLine(right, bottom, left, bottom)
                self.DrawLine(left, bottom, left, top)

        # 7. Name bold 17, text colour, 1 line, ellipsis, at (10, 122)
        name_str = str(item.get("name") or "")
        self.DrawSetTextCol(theme.color_vector("text"), c4d.COLOR_TRANS)
        self.DrawText(self._fit(name_str, w - 20), x + 10, y + th + 10)

        # 8. State line at (10, 148) 12 px
        self._draw_state_line(x + 10, y + th + 36, base_id, tier, item, state)

    def _draw_tier_tag(self, x: int, y: int, w: int, tier: str) -> None:
        tw = self.DrawGetTextWidth(tier) + 10
        th = self.DrawGetFontHeight() + 4
        tx = x + w - tw - 7
        ty = y + 7

        if tier == "FREE":
            self.DrawSetPen(theme.color_vector("text"))
            self.DrawRectangle(tx, ty, tx + tw - 1, ty + th - 1)
            self.DrawSetTextCol(theme.color_vector("dark"), c4d.COLOR_TRANS)
            self.DrawText(tier, tx + 5, ty + 2)
        elif tier == "FULL PLAN":
            self.DrawSetPen(theme.color_vector("accent"))
            self.DrawRectangle(tx, ty, tx + tw - 1, ty + th - 1)
            self.DrawSetTextCol(theme.color_vector("white"), c4d.COLOR_TRANS)
            self.DrawText(tier, tx + 5, ty + 2)
        elif tier == "PAID":
            self.DrawSetPen(theme.color_vector("dark"))
            self.DrawRectangle(tx, ty, tx + tw - 1, ty + th - 1)
            self.DrawSetPen(theme.color_vector("accent"))
            self.DrawLine(tx, ty, tx + tw - 1, ty)
            self.DrawLine(tx + tw - 1, ty, tx + tw - 1, ty + th - 1)
            self.DrawLine(tx + tw - 1, ty + th - 1, tx, ty + th - 1)
            self.DrawLine(tx, ty + th - 1, tx, ty)
            self.DrawSetTextCol(theme.color_vector("paid_label"), c4d.COLOR_TRANS)
            self.DrawText(tier, tx + 5, ty + 2)

    def _draw_busy_overlay(self, x: int, y: int, w: int, th: int, state: Any) -> None:
        self.DrawSetOpacity(theme.BUSY_OPACITY)
        self.DrawSetPen(theme.color_vector("dark"))
        self.DrawRectangle(x, y, x + w - 1, y + th - 1)
        self.DrawSetOpacity(1.0)

        pct = f"{int(max(0.0, min(1.0, state.fraction)) * 100)}%"
        pw = self.DrawGetTextWidth(pct)
        ph = self.DrawGetFontHeight()
        self.DrawSetTextCol(theme.color_vector("white"), c4d.COLOR_TRANS)
        self.DrawText(pct, x + (w - pw) // 2, y + th // 2 - ph)

        stage_text = {"download": "DOWNLOADING", "convert": "CONVERTING", "cache": "CACHED"}.get(
            state.stage, state.stage.upper()
        )
        sw = self.DrawGetTextWidth(stage_text)
        self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)
        self.DrawText(stage_text, x + (w - sw) // 2, y + th // 2 + 4)

        self.DrawSetPen(theme.color_vector("track"))
        self.DrawRectangle(x, y + th - 4, x + w - 1, y + th - 1)
        bar_w = int(w * max(0.0, min(1.0, state.fraction)))
        if bar_w > 0:
            self.DrawSetPen(theme.color_vector("accent"))
            self.DrawRectangle(x, y + th - 4, x + bar_w - 1, y + th - 1)

    def _draw_state_line(
        self, x: int, y: int, base_id: str, tier: str, item: Dict[str, Any], state: Any
    ) -> None:
        if state is not None:
            self.DrawSetTextCol(theme.color_vector("accent_light"), c4d.COLOR_TRANS)
            self.DrawText("CLICK TO CANCEL", x, y)
        elif base_id in getattr(self.app, "in_scene_ids", set()):
            self.DrawSetTextCol(theme.color_vector("accent_light"), c4d.COLOR_TRANS)
            self.DrawText("IN SCENE", x, y)
        elif base_id in self.app.cached_ids or item.get("packagePath"):
            self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)
            self.DrawText("IN CACHE ✓", x, y)
        elif tier == "PAID" and not item.get("canDownload"):
            self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)
            self.DrawText("BUY ON BLENDKIT", x, y)

    def _fit(self, text: str, max_width: int) -> str:
        if self.DrawGetTextWidth(text) <= max_width:
            return text
        chars = len(text)
        while chars > 1 and self.DrawGetTextWidth(layout.ellipsize(text, chars)) > max_width:
            chars -= 1
        return layout.ellipsize(text, chars)

    def _thumbnail(self, item: Dict[str, Any]) -> Optional[c4d.bitmaps.BaseBitmap]:
        paths = item.get("thumbnailPaths") or {}
        for key in ("middle", "small", "large"):
            path = str(paths.get(key) or "")
            if not path:
                continue
            if path in self._bitmaps:
                if self._bitmaps[path] is not None:
                    return self._bitmaps[path]
                continue
            bmp = None
            if os.path.isfile(path):
                candidate = c4d.bitmaps.BaseBitmap()
                result = candidate.InitWith(path)
                if result[0] == c4d.IMAGERESULT_OK:
                    bmp = candidate
            self._bitmaps[path] = bmp
            if bmp is not None:
                return bmp
        return None

    def _context_menu(self, item: Dict[str, Any]) -> None:
        bc = c4d.BaseContainer()
        bc.InsData(MENU_IMPORT, "Import")
        if item.get("sourceUrl"):
            bc.InsData(MENU_OPEN_WEB, "Open on blenderkit.com")
        if str(item.get("assetBaseId") or "") in self.app.cached_ids:
            bc.InsData(MENU_REMOVE, "Remove from cache")
        choice = c4d.gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if choice == MENU_IMPORT:
            self.on_activate(item)
        elif choice == MENU_OPEN_WEB:
            webbrowser.open(str(item["sourceUrl"]))
        elif choice == MENU_REMOVE:
            self.app.remove_from_cache(item)
