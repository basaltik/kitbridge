"""Custom GeUserArea views for Kitbridge v2: Header, Filter Row, and Search Button.

Docs reference: docs/spec-ui-v2.md
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import c4d

from kitbridge.core import paths
from kitbridge.ui import layout, theme
from kitbridge.ui.theme import AssetCache

log = logging.getLogger("kitbridge")


# Tab keys are singular, the pre-rendered PNGs are named after the plural labels.
_TAB_ASSET = {"model": "models", "material": "materials", "hdr": "hdris", "downloaded": "downloaded"}


def _local_xy(area: c4d.gui.GeUserArea, msg: c4d.BaseContainer) -> tuple:
    """BFM_INPUT_X/Y are global: convert them to the user area's own coordinates."""
    local = area.Global2Local()
    return float(msg[c4d.BFM_INPUT_X] + local["x"]), float(msg[c4d.BFM_INPUT_Y] + local["y"])


class TopHeaderArea(c4d.gui.GeUserArea):
    """Draws the header band, meta chips and tabs (geometry in layout.py)."""

    def __init__(
        self,
        app: Any,
        on_tab_changed: Callable[[str], None],
        on_user_click: Callable[[], None],
        assets: AssetCache,
        on_renderer_click: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.app = app
        self.on_tab_changed = on_tab_changed
        self.on_user_click = on_user_click
        self.on_renderer_click = on_renderer_click
        self.assets = assets
        self._renderer_chip = layout.Rect(0, 0, 0, 0)  # set while drawing; the chip width follows its label

    def GetMinSize(self) -> tuple:
        return 360, layout.HEADER_H

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)

        w = self.GetWidth()
        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1

        # 1. Base window background #262626
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)

        # 2. Header band
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawRectangle(0, 0, w, layout.HEADER_BAND_H)

        # Name, left edge at the margin, centred in the band
        bmp_title = self.assets.get("header_title", scale=scale)
        if bmp_title is not None:
            bw = bmp_title.GetBw() // scale
            bh = bmp_title.GetBh() // scale
            ty = (layout.HEADER_BAND_H - bh) // 2
            self.DrawBitmap(
                bmp_title, layout.MARGIN, ty, bw, bh,
                0, 0, bmp_title.GetBw(), bmp_title.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )

        # 3. Meta chips: blendkit, renderer, resolution
        self._draw_chips(w, scale)

        # 4. Tabs, on a rule across the window
        self._draw_tabs(scale)
        self.DrawSetPen(theme.color_vector("line"))
        rule_y = layout.TABS_Y + layout.TAB_H - 1
        self.DrawLine(0, rule_y, w, rule_y)

    def _draw_chips(self, w: int, scale: int) -> None:
        cy, ch = layout.CHIPS_Y, 24
        x = layout.MARGIN

        # Panel background pen
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)

        # Chip 1: BLENDKIT
        bmp_bk = self.assets.get("chip_blendkit", scale=scale)
        if bmp_bk is not None:
            bw = bmp_bk.GetBw() // scale
            bh = bmp_bk.GetBh() // scale
            chip_w = bw + 20
            self.DrawSetPen(theme.color_vector("panel"))
            self.DrawRectangle(x, cy, x + chip_w - 1, cy + ch - 1)
            self.DrawBitmap(
                bmp_bk, x + 10, cy + (ch - bh) // 2, bw, bh,
                0, 0, bmp_bk.GetBw(), bmp_bk.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )
            x += chip_w + 6

        # Chip 2: RENDERER (clickable: picks the material system for imports)
        renderer_text = getattr(self.app, "renderer_name", "REDSHIFT").title()
        rw = self.DrawGetTextWidth(renderer_text) + 20
        self._renderer_chip = layout.Rect(x, cy, rw, ch)
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawRectangle(x, cy, x + rw - 1, cy + ch - 1)
        self.DrawSetTextCol(theme.color_vector("text"), c4d.COLOR_TRANS)
        self.DrawText(renderer_text, x + 10, cy + 5)
        x += rw + 6

        # Chip 3: RESOLUTION
        res_key = str(self.app.prefs.get("resolution") or "2048")
        res_text = layout.RES_TO_LABEL.get(res_key, res_key)
        res_w = self.DrawGetTextWidth(res_text) + 20
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawRectangle(x, cy, x + res_w - 1, cy + ch - 1)
        self.DrawSetTextCol(theme.color_vector("muted"), c4d.COLOR_TRANS)
        self.DrawText(res_text, x + 10, cy + 5)

        # User chip (right-aligned)
        logged_in = self.app.is_logged_in()
        uname = self.app.user_name if logged_in else "Log in"
        urect = layout.user_chip_rect(w, uname, margin=layout.MARGIN, y=cy, h=ch)
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawRectangle(urect.x, urect.y, urect.x + urect.w - 1, urect.y + urect.h - 1)

        # Dot: green if logged in, muted if not
        dot_col = theme.color_vector("online") if logged_in else theme.color_vector("muted")
        self.DrawSetPen(dot_col)
        self.DrawRectangle(urect.x + 10, cy + 9, urect.x + 16, cy + 15)

        # User name text
        self.DrawSetTextCol(theme.color_vector("text"), c4d.COLOR_TRANS)
        self.DrawText(uname, urect.x + 22, cy + 5)

    def _draw_tabs(self, scale: int) -> None:
        y = layout.TABS_Y
        cur_tab = getattr(self.app, "current_tab", "model")
        self.DrawSetPen(theme.color_vector("bg"))
        for key, rect in layout.tab_rects(margin=layout.MARGIN, y=y):
            is_active = key == cur_tab and not getattr(self.app, "settings_open", False)
            state = "active" if is_active else "inactive"
            asset_name = f"tab_btn_{_TAB_ASSET[key]}_{state}"
            bmp = self.assets.get(asset_name, scale=scale)
            if bmp is not None:
                self.DrawBitmap(
                    bmp, rect.x, rect.y, rect.w, rect.h,
                    0, 0, bmp.GetBw(), bmp.GetBh(),
                    c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
                )

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        if (
            msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSELEFT
        ):
            x, y = _local_xy(self, msg)
            # Check tab click
            hit_tab = layout.tab_hit_test(x, y, margin=layout.MARGIN)
            if hit_tab is not None:
                self.on_tab_changed(hit_tab)
                return True

            if self.on_renderer_click is not None and self._renderer_chip.contains(x, y):
                self.on_renderer_click()
                return True

            # Check user chip click
            w = self.GetWidth()
            uname = self.app.user_name if self.app.is_logged_in() else "Log in"
            urect = layout.user_chip_rect(w, uname, margin=layout.MARGIN, h=24)
            if urect.contains(x, y):
                self.on_user_click()
                return True
        return False

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        mid = msg.GetId()
        if mid == c4d.BFM_GETCURSORINFO:
            local = self.Screen2Local()
            x = float(msg[c4d.BFM_DRAG_SCREENX] + local["x"])
            y = float(msg[c4d.BFM_DRAG_SCREENY] + local["y"])
            hit_tab = layout.tab_hit_test(x, y, margin=layout.MARGIN)
            w = self.GetWidth()
            uname = self.app.user_name if self.app.is_logged_in() else "Log in"
            urect = layout.user_chip_rect(w, uname, margin=layout.MARGIN, h=24)
            if hit_tab is not None or urect.contains(x, y) or self._renderer_chip.contains(x, y):
                result.SetId(c4d.BFM_GETCURSORINFO)
                result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
                if self._renderer_chip.contains(x, y):
                    result.SetString(c4d.RESULT_BUBBLEHELP, "Renderer for imported materials (click to change)")
                return True
        return super().Message(msg, result)


class SearchButtonArea(c4d.gui.GeUserArea):
    """Draws the search button, a magnifier: the one primary action in the window."""

    def __init__(self, on_click: Callable[[], None], assets: AssetCache) -> None:
        super().__init__()
        self.on_click = on_click
        self.assets = assets

    def GetMinSize(self) -> tuple:
        return layout.SEARCH_BUTTON_SIZE

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)

        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1
        bmp = self.assets.get("btn_search", scale=scale)
        if bmp is not None:
            self.DrawBitmap(
                bmp, 0, 0, layout.SEARCH_BUTTON_SIZE[0], layout.SEARCH_BUTTON_SIZE[1],
                0, 0, bmp.GetBw(), bmp.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        if (
            msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSELEFT
        ):
            self.on_click()
            return True
        return False

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        if msg.GetId() == c4d.BFM_GETCURSORINFO:
            result.SetId(c4d.BFM_GETCURSORINFO)
            result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
            result.SetString(c4d.RESULT_BUBBLEHELP, "Search")
            return True
        return super().Message(msg, result)


class SettingsButtonArea(c4d.gui.GeUserArea):
    """Small settings toggle: a native button would stretch the whole search row."""

    def __init__(self, on_click: Callable[[], None], app: Any = None) -> None:
        super().__init__()
        self.on_click = on_click
        self.app = app

    def GetMinSize(self) -> tuple:
        return layout.SETTINGS_BUTTON_SIZE

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)
        fill = theme.color_vector("accent" if getattr(self.app, "settings_open", False) else "panel")
        self.DrawSetPen(fill)
        self.DrawRectangle(0, 0, self.GetWidth() - 1, self.GetHeight() - 1)
        self.DrawSetTextCol(theme.color_vector("text"), fill)
        tw = self.DrawGetTextWidth("⚙")
        self.DrawText("⚙", (self.GetWidth() - tw) // 2, (self.GetHeight() - self.DrawGetFontHeight()) // 2)

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        if (
            msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSELEFT
        ):
            self.on_click()
            return True
        return False

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        if msg.GetId() == c4d.BFM_GETCURSORINFO:
            result.SetId(c4d.BFM_GETCURSORINFO)
            result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
            result.SetString(c4d.RESULT_BUBBLEHELP, "Settings")
            return True
        return super().Message(msg, result)


class SettingsArea(c4d.gui.GeUserArea):
    """The settings screen, drawn with the same tokens and labels as the rest of the window."""

    def __init__(self, app: Any, on_action: Callable[[str], None], assets: AssetCache) -> None:
        super().__init__()
        self.app = app
        self.on_action = on_action
        self.assets = assets

    def GetMinSize(self) -> tuple:
        return 360, layout.SETTINGS_HEIGHT

    # ── drawing ───────────────────────────────────────────────────────────

    def _png(self, name: str, x: int, y: int, under: str = "bg") -> None:
        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1
        bmp = self.assets.get(name, scale=scale)
        if bmp is None:
            return
        self.DrawSetPen(theme.color_vector(under))  # shows through transparent pixels
        self.DrawBitmap(
            bmp, x, y, bmp.GetBw() // scale, bmp.GetBh() // scale,
            0, 0, bmp.GetBw(), bmp.GetBh(),
            c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
        )

    def _text(self, text: str, x: int, y: int, color: str = "muted") -> None:
        self.DrawSetTextCol(theme.color_vector(color), c4d.COLOR_TRANS)
        self.DrawText(text, x, y)

    def _fit(self, text: str, max_width: int) -> str:
        """Keep the end of a path, the part that tells folders apart."""
        if self.DrawGetTextWidth(text) <= max_width:
            return text
        keep = len(text)
        while keep > 4 and self.DrawGetTextWidth("…" + text[-keep:]) > max_width:
            keep -= 1
        return "…" + text[-keep:]

    def _field(self, rect: layout.Rect, text: str, color: str = "text") -> None:
        self.DrawSetPen(theme.color_vector("line"))
        self.DrawRectangle(rect.x, rect.y, rect.x + rect.w - 1, rect.y + rect.h - 1)
        self.DrawSetPen(theme.color_vector("field"))
        self.DrawRectangle(rect.x + 1, rect.y + 1, rect.x + rect.w - 2, rect.y + rect.h - 2)
        y = rect.y + (rect.h - self.DrawGetFontHeight()) // 2
        self._text(self._fit(text, rect.w - 20), rect.x + 10, y, color)

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)
        self.DrawSetFont(c4d.FONT_BOLD)

        rects = layout.settings_rects(self.GetWidth())
        info = getattr(self.app, "settings_info", {})
        margin = layout.MARGIN

        # Download folder: one choice, sorted into subfolders
        self._png("sec_download_folder", margin, 14)
        self._field(rects["folder_field"], paths.cache_root(create=False))
        r = rects["browse_folder"]
        self._png("btn_browse", r.x, r.y)
        self._text("Kitbridge sorts downloads into a subfolder for each type.", margin, 82)
        for folder in layout.SETTINGS_FOLDERS:
            r = rects[f"open:{folder}"]
            self.DrawSetPen(theme.color_vector("card"))
            self.DrawRectangle(r.x, r.y, r.x + r.w - 1, r.y + r.h - 1)
            self._png(f"tab_label_{folder}_active", r.x + 10, r.y + 10, under="card")
            count = str(info.get("folders", {}).get(folder, 0)) if info.get("loaded") else "…"
            self._text(count, r.x + r.w - 10 - self.DrawGetTextWidth(count), r.y + 8, "text")
            self._text(f"{folder}/", r.x + 10, r.y + 30)
        self._text("Downloaded assets stay in the old folder.", margin, 175)
        if str(self.app.prefs.get("download_dir") or ""):
            r = rects["default_folder"]
            self._png("btn_use_default", r.x, r.y)

        # Quality
        self._png("sec_quality", margin, 212)
        current = str(self.app.prefs.get("resolution") or "2048")
        for label, value, _width in layout.SETTINGS_RESOLUTIONS:
            r = rects[f"res:{value}"]
            active = value == current
            fill = "accent" if active else "panel"
            self.DrawSetPen(theme.color_vector(fill))
            self.DrawRectangle(r.x, r.y, r.x + r.w - 1, r.y + r.h - 1)
            state = "active" if active else "inactive"
            self._png_centred(f"seg_{label.lower()}_{state}", r, fill)

        # Blender
        self._png("sec_blender", margin, 274)
        chosen = str(self.app.prefs.get("blender_path") or "")
        found = chosen or str(info.get("blender") or "")
        self._field(rects["blender_field"], found or ("Looking for Blender…" if not info.get("loaded") else "Not found"))
        r = rects["browse_blender"]
        self._png("btn_browse", r.x, r.y)
        self._text("Converts .blend files in the background.", margin, 345)
        if chosen:
            r = rects["auto_blender"]
            self._png("btn_auto", r.x, r.y)

        # Cache
        self._png("sec_cache", margin, 376)
        if info.get("loaded"):
            size_mb = float(info.get("size") or 0) / (1024 * 1024)
            summary = f"{size_mb:.1f} MB · {int(info.get('count') or 0)} assets in this folder"
        else:
            summary = "Measuring…"
        self._text(summary, margin, 407, "text")
        r = rects["clear_cache"]
        self._png("btn_clear_cache", r.x, r.y)

        # Interface
        self._png("sec_interface", margin, 446)
        r = rects["toggle_menu"]
        on = bool(self.app.prefs.get("show_main_menu", True))
        self.DrawSetPen(theme.color_vector("accent" if on else "line"))
        self.DrawRectangle(r.x, r.y + 1, r.x + 15, r.y + 16)
        if not on:
            self.DrawSetPen(theme.color_vector("field"))
            self.DrawRectangle(r.x + 1, r.y + 2, r.x + 14, r.y + 15)
        else:
            self._text("✓", r.x + 3, r.y + 1, "white")
        self._text("Show Kitbridge menu (after restart)", r.x + 24, r.y + 1, "text")

    def _png_centred(self, name: str, r: layout.Rect, under: str) -> None:
        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1
        bmp = self.assets.get(name, scale=scale)
        if bmp is not None:
            self._png(name, r.x + (r.w - bmp.GetBw() // scale) // 2, r.y + (r.h - bmp.GetBh() // scale) // 2, under)

    # ── input ─────────────────────────────────────────────────────────────

    def _visible(self, name: Optional[str]) -> bool:
        """Conditional buttons only react while they are drawn."""
        if name == "default_folder":
            return bool(self.app.prefs.get("download_dir"))
        if name == "auto_blender":
            return bool(self.app.prefs.get("blender_path"))
        return name is not None

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        if (
            msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSELEFT
        ):
            x, y = _local_xy(self, msg)
            name = layout.settings_hit_test(x, y, self.GetWidth())
            if name is not None and self._visible(name):
                self.on_action(name)
            return True
        return False

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        if msg.GetId() == c4d.BFM_GETCURSORINFO:
            local = self.Screen2Local()
            x = float(msg[c4d.BFM_DRAG_SCREENX] + local["x"])
            y = float(msg[c4d.BFM_DRAG_SCREENY] + local["y"])
            name = layout.settings_hit_test(x, y, self.GetWidth())
            if name is not None and self._visible(name):
                result.SetId(c4d.BFM_GETCURSORINFO)
                result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
                if name.startswith("open:"):
                    result.SetString(c4d.RESULT_BUBBLEHELP, "Open this folder")
                return True
        return super().Message(msg, result)


class FilterRowArea(c4d.gui.GeUserArea):
    """Segmented ALL · FREE · FULL PLAN (left) and 1K 2K 4K 8K (right)."""

    def __init__(
        self,
        app: Any,
        on_filter_changed: Callable[[str, str], None],
        assets: AssetCache,
    ) -> None:
        super().__init__()
        self.app = app
        self.on_filter_changed = on_filter_changed
        self.assets = assets

    def GetMinSize(self) -> tuple:
        return 360, 26

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)

        w = self.GetWidth()
        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1
        tab = getattr(self.app, "current_tab", "model")
        left_rects, res_rects = layout.filter_row_rects(w, tab, margin=layout.MARGIN, y=2, h=22)

        # Active filters
        active_left = (
            getattr(self.app, "downloaded_type_filter", "ALL")
            if tab == "downloaded"
            else getattr(self.app, "tier_filter", "ALL")
        )
        res_key = str(self.app.prefs.get("resolution") or "2048")
        active_res = layout.RES_TO_LABEL.get(res_key, "2K")

        # 1. Left segments (tier or downloaded type)
        for label, rect in left_rects:
            is_active = (label == active_left)
            fill_pen = theme.color_vector("accent") if is_active else theme.color_vector("panel")
            self.DrawSetPen(fill_pen)
            self.DrawRectangle(rect.x, rect.y, rect.x + rect.w - 1, rect.y + rect.h - 1)

            clean = label.lower().replace(" ", "_")
            state_sfx = "active" if is_active else "inactive"
            bmp = self.assets.get(f"seg_{clean}_{state_sfx}", scale=scale)
            if bmp is not None:
                bw = bmp.GetBw() // scale
                bh = bmp.GetBh() // scale
                bx = rect.x + (rect.w - bw) // 2
                by = rect.y + (rect.h - bh) // 2
                self.DrawBitmap(
                    bmp, bx, by, bw, bh,
                    0, 0, bmp.GetBw(), bmp.GetBh(),
                    c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
                )

        # 2. Right segments (resolution 1K, 2K, 4K, 8K)
        for res_lbl, rect in res_rects:
            is_active = (res_lbl == active_res)
            fill_pen = theme.color_vector("accent") if is_active else theme.color_vector("panel")
            self.DrawSetPen(fill_pen)
            self.DrawRectangle(rect.x, rect.y, rect.x + rect.w - 1, rect.y + rect.h - 1)

            clean = res_lbl.lower()
            state_sfx = "active" if is_active else "inactive"
            bmp = self.assets.get(f"seg_{clean}_{state_sfx}", scale=scale)
            if bmp is not None:
                bw = bmp.GetBw() // scale
                bh = bmp.GetBh() // scale
                bx = rect.x + (rect.w - bw) // 2
                by = rect.y + (rect.h - bh) // 2
                self.DrawBitmap(
                    bmp, bx, by, bw, bh,
                    0, 0, bmp.GetBw(), bmp.GetBh(),
                    c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
                )

    def InputEvent(self, msg: c4d.BaseContainer) -> bool:
        if (
            msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_MOUSE
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.BFM_INPUT_MOUSELEFT
        ):
            x, y = _local_xy(self, msg)
            w = self.GetWidth()
            tab = getattr(self.app, "current_tab", "model")
            hit = layout.filter_row_hit_test(x, y, w, tab, margin=layout.MARGIN, y=2, h=22)
            if hit is not None:
                kind, val = hit
                self.on_filter_changed(kind, val)
                return True
        return False

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        if msg.GetId() == c4d.BFM_GETCURSORINFO:
            local = self.Screen2Local()
            x = float(msg[c4d.BFM_DRAG_SCREENX] + local["x"])
            y = float(msg[c4d.BFM_DRAG_SCREENY] + local["y"])
            w = self.GetWidth()
            tab = getattr(self.app, "current_tab", "model")
            hit = layout.filter_row_hit_test(x, y, w, tab, margin=layout.MARGIN, y=2, h=22)
            if hit is not None:
                result.SetId(c4d.BFM_GETCURSORINFO)
                result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
                return True
        return super().Message(msg, result)


class BottomFooterArea(c4d.gui.GeUserArea):
    """Draws the status box and the footer with mark and license notices."""

    def __init__(self, app: Any, assets: AssetCache) -> None:
        super().__init__()
        self.app = app
        self.assets = assets

    def GetMinSize(self) -> tuple:
        return 360, 180

    def DrawMsg(self, x1: int, y1: int, x2: int, y2: int, msg: c4d.BaseContainer) -> None:
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        self.DrawSetPen(theme.color_vector("bg"))
        self.DrawRectangle(x1, y1, x2, y2)

        w = self.GetWidth()
        scale = 2 if getattr(self, "GetPixelRatio", lambda: 1.0)() >= 1.5 else 1

        # 1. Status box (y: 6..54, h: 48)
        # Panel fill, radius 4
        sb_x1, sb_y1 = layout.MARGIN, 6
        sb_x2, sb_y2 = w - layout.MARGIN, 54
        self.DrawSetPen(theme.color_vector("panel"))
        self.DrawRectangle(sb_x1, sb_y1, sb_x2, sb_y2)

        # 4 px accent bar on the left
        self.DrawSetPen(theme.color_vector("accent"))
        self.DrawRectangle(sb_x1, sb_y1, sb_x1 + 4, sb_y2)

        # Top label "KITBRIDGE" in accent-light
        self.DrawSetPen(theme.color_vector("panel"))
        bmp_lbl = self.assets.get("status_kitbridge", scale=scale)
        if bmp_lbl is not None:
            bw = bmp_lbl.GetBw() // scale
            bh = bmp_lbl.GetBh() // scale
            self.DrawBitmap(
                bmp_lbl, sb_x1 + 12, sb_y1 + 8, bw, bh,
                0, 0, bmp_lbl.GetBw(), bmp_lbl.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )
        else:
            self.DrawSetTextCol(theme.color_vector("accent_light"), c4d.COLOR_TRANS)
            self.DrawText("KITBRIDGE", sb_x1 + 12, sb_y1 + 8)

        # One line of status text in text colour
        status_text = getattr(self.app, "status", "")
        self.DrawSetTextCol(theme.color_vector("text"), c4d.COLOR_TRANS)
        max_status_w = sb_x2 - sb_x1 - 24
        fitted_status = layout.ellipsize(status_text, 80)
        self.DrawText(fitted_status, sb_x1 + 12, sb_y1 + 26)

        # 2. Footer: logo centred 20 px below status box
        logo_y = sb_y2 + 20
        self.DrawSetPen(theme.color_vector("bg"))
        bmp_logo = self.assets.get("footer_logo", scale=scale)
        if bmp_logo is not None:
            lw = bmp_logo.GetBw() // scale
            lh = bmp_logo.GetBh() // scale
            lx = (w - lw) // 2
            self.DrawBitmap(
                bmp_logo, lx, logo_y, lw, lh,
                0, 0, bmp_logo.GetBw(), bmp_logo.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )
            line1_y = logo_y + lh + 20
        else:
            line1_y = logo_y + 49

        # Two centred lines 12 px muted 20 px below the logo
        bmp_line1 = self.assets.get("footer_line1", scale=scale)
        if bmp_line1 is not None:
            l1w = bmp_line1.GetBw() // scale
            l1h = bmp_line1.GetBh() // scale
            l1x = (w - l1w) // 2
            self.DrawBitmap(
                bmp_line1, l1x, line1_y, l1w, l1h,
                0, 0, bmp_line1.GetBw(), bmp_line1.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )
            line2_y = line1_y + l1h + 4
        else:
            line2_y = line1_y + 16

        bmp_line2 = self.assets.get("footer_line2", scale=scale)
        if bmp_line2 is not None:
            l2w = bmp_line2.GetBw() // scale
            l2h = bmp_line2.GetBh() // scale
            l2x = (w - l2w) // 2
            self.DrawBitmap(
                bmp_line2, l2x, line2_y, l2w, l2h,
                0, 0, bmp_line2.GetBw(), bmp_line2.GetBh(),
                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA,
            )

