"""The Kitbridge window (v2 restyle with Barlow typography, filters, and dockable GeUserAreas).

Docs reference: docs/spec-ui-v2.md
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import c4d

from kitbridge.core import paths, pipeline
from kitbridge.ui import disclaimer, layout, prefs, theme
from kitbridge.ui.app import get_app
from kitbridge.ui.grid import AssetGrid
from kitbridge.ui.theme import AssetCache
from kitbridge.ui.views import (
    BottomFooterArea,
    FilterRowArea,
    SearchButtonArea,
    SettingsArea,
    SettingsButtonArea,
    TopHeaderArea,
)

log = logging.getLogger("kitbridge")

ID_ROOT = 1005
ID_HEADER = 1010
ID_SEARCH_GROUP = 1020
ID_QUERY = 1021
ID_SEARCH_BTN = 1022
ID_SETTINGS_TOGGLE = 1023
ID_FILTER_ROW = 1025

ID_FILTER_GROUP = 1026
ID_SETTINGS_GROUP = 1030
ID_SETTINGS_AREA = 1031

ID_NOTICE_GROUP = 1040
ID_NOTICE_TEXT = 1041
ID_NOTICE_OK = 1042

ID_GRID_GROUP = 1050
ID_GRID = 1051
ID_BOTTOM = 1060


class KitbridgeDialog(c4d.gui.GeDialog):
    def __init__(self) -> None:
        super().__init__()
        self.assets = AssetCache()
        self.app = get_app()
        self.app.confirm_heavy = self._confirm_heavy
        self.header_area = TopHeaderArea(
            self.app, self._on_tab_changed, self._on_user_click, self.assets,
            on_renderer_click=self._on_renderer_click,
        )
        self.search_btn = SearchButtonArea(self._search, self.assets)
        self.settings_btn = SettingsButtonArea(self._toggle_settings, self.app)
        self.settings_area = SettingsArea(self.app, self._on_settings_action, self.assets)
        self.filter_row_area = FilterRowArea(
            self.app, self._on_filter_changed, self.assets
        )
        self.grid = AssetGrid(self.app, self._on_activate, self.assets)
        self.footer_area = BottomFooterArea(self.app, self.assets)

        self._seen_version = -1
        self._lang = disclaimer.current_language()
        self._notice_visible = False
        self._last_clipboard_check = 0.0
        self._last_clipboard_text: Optional[str] = None

    # ── layout ────────────────────────────────────────────────────────────

    def CreateLayout(self) -> bool:
        self.SetTitle("Kitbridge")

        # Everything lives in one named group: LayoutChanged(0) would only relayout the first
        # unnamed group and leave hidden panels taking up the grid's height.
        self.GroupBegin(ID_ROOT, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=1)
        self.GroupSpace(0, 0)

        # 1. Header band + Meta chips + Section title + Tabs
        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=1, rows=1):
            self.GroupBorderSpace(0, 0, 0, 0)
            self.AddUserArea(ID_HEADER, c4d.BFH_SCALEFIT, 360, layout.HEADER_H)
            self.AttachUserArea(self.header_area, ID_HEADER)
            self.GroupEnd()

        # 2. Search row (edit text + search button + settings gear)
        if self.GroupBegin(ID_SEARCH_GROUP, c4d.BFH_SCALEFIT, cols=3, rows=1):
            self.GroupBorderSpace(layout.MARGIN, 4, layout.MARGIN, 4)
            self.GroupSpace(8, 0)
            self.AddEditText(ID_QUERY, c4d.BFH_SCALEFIT | c4d.BFV_CENTER, initw=0, inith=0)
            self.AddUserArea(ID_SEARCH_BTN, c4d.BFH_RIGHT, *layout.SEARCH_BUTTON_SIZE)
            self.AttachUserArea(self.search_btn, ID_SEARCH_BTN)
            self.AddUserArea(ID_SETTINGS_TOGGLE, c4d.BFH_RIGHT, *layout.SETTINGS_BUTTON_SIZE)
            self.AttachUserArea(self.settings_btn, ID_SETTINGS_TOGGLE)
            self.GroupEnd()

        # 3. Filter row (ALL/FREE/FULL PLAN or Downloaded type + 1K 2K 4K 8K)
        if self.GroupBegin(ID_FILTER_GROUP, c4d.BFH_SCALEFIT, cols=1, rows=1):
            self.GroupBorderSpace(0, 2, 0, 4)
            self.AddUserArea(ID_FILTER_ROW, c4d.BFH_SCALEFIT, 360, 26)
            self.AttachUserArea(self.filter_row_area, ID_FILTER_ROW)
            self.GroupEnd()

        # 4. Settings screen (takes the grid's place while open)
        if self.ScrollGroupBegin(
            ID_SETTINGS_GROUP, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
            c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, 0, 0,
        ):
            self.AddUserArea(ID_SETTINGS_AREA, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT)
            self.AttachUserArea(self.settings_area, ID_SETTINGS_AREA)
            self.GroupEnd()

        # 5. First-run disclaimer notice
        if self.GroupBegin(ID_NOTICE_GROUP, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=1, rows=2):
            self.GroupBorderSpace(10, 10, 10, 10)
            self.AddMultiLineEditText(
                ID_NOTICE_TEXT, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 0, 200,
                c4d.DR_MULTILINE_READONLY | c4d.DR_MULTILINE_WORDWRAP,
            )
            self.AddButton(ID_NOTICE_OK, c4d.BFH_CENTER, name=disclaimer.BUTTON[self._lang])
            self.GroupEnd()

        # 6. Grid (fills available vertical space)
        if self.ScrollGroupBegin(
            ID_GRID_GROUP, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
            c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, 0, 0,
        ):
            self.AddUserArea(ID_GRID, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT)
            self.AttachUserArea(self.grid, ID_GRID)
            self.GroupEnd()

        # 7. Status box + Footer
        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=1, rows=1):
            self.GroupBorderSpace(0, 0, 0, 0)
            self.AddUserArea(ID_BOTTOM, c4d.BFH_SCALEFIT, 360, 180)
            self.AttachUserArea(self.footer_area, ID_BOTTOM)
            self.GroupEnd()

        self.GroupEnd()
        return True

    def InitValues(self) -> bool:
        tab = str(self.app.prefs.get("last_tab") or "model")
        self.app.current_tab = tab
        self.grid.tab = tab

        self.SetString(ID_NOTICE_TEXT, disclaimer.TEXT[self._lang])
        self._show_notice(not self.app.prefs.get("accepted_disclaimer"))

        self.app.update_renderer_name()
        self.app.start(str(c4d.GetC4DVersion()))
        self.SetTimer(100)
        self._sync()
        return True

    # ── events ────────────────────────────────────────────────────────────

    def Timer(self, msg: c4d.BaseContainer) -> None:
        try:
            self.app.tick()
            now = time.monotonic()
            if now - self._last_clipboard_check >= 1.0:
                self._last_clipboard_check = now
                self._check_clipboard()
                # The user may switch documents or render engines at any time.
                before = self.app.renderer_name
                self.app.update_renderer_name()
                if self.app.renderer_name != before:
                    self.header_area.Redraw()
            self._sync()
        except Exception:
            log.exception("Kitbridge timer failed")

    def Command(self, cid: int, msg: c4d.BaseContainer) -> bool:
        try:
            self._command(cid)
        except Exception as exc:
            log.exception("Kitbridge command %s failed", cid)
            self.app._set_status(f"Error: {exc}")
        return True

    def Message(self, msg: c4d.BaseContainer, result: c4d.BaseContainer) -> Any:
        if (
            msg.GetId() == c4d.BFM_INPUT
            and msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_KEYBOARD
            and msg[c4d.BFM_INPUT_CHANNEL] == c4d.KEY_ENTER
            and self.IsActive(ID_QUERY)
        ):
            self._search()
            return True
        return super().Message(msg, result)

    def _command(self, cid: int) -> None:
        if cid == ID_NOTICE_OK:
            self.app.set_pref("accepted_disclaimer", True)
            self._show_notice(False)

    def _toggle_settings(self) -> None:
        self._set_settings_open(not self.app.settings_open)

    def _set_settings_open(self, is_open: bool) -> None:
        self.app.settings_open = is_open
        if is_open:
            self.app.refresh_settings_info()
        self._apply_view()
        self.header_area.Redraw()
        self.settings_btn.Redraw()

    def _on_settings_action(self, action: str) -> None:
        try:
            self._settings_action(action)
        except Exception as exc:
            log.exception("Settings action %s failed", action)
            self.app._set_status(f"Error: {exc}")
        self.settings_area.Redraw()
        self.header_area.Redraw()
        self.filter_row_area.Redraw()
        self.footer_area.Redraw()

    def _settings_action(self, action: str) -> None:
        if action == "browse_folder":
            chosen = c4d.storage.LoadDialog(
                title="Choose the Kitbridge download folder",
                flags=c4d.FILESELECT_DIRECTORY,
                def_path=paths.cache_root(create=False),
            )
            if chosen:
                self.app.set_download_dir(str(chosen))
        elif action == "default_folder":
            self.app.set_download_dir("")
        elif action.startswith("open:"):
            folder = os.path.join(paths.cache_root(create=False), action.split(":", 1)[1])
            os.makedirs(folder, exist_ok=True)
            c4d.storage.ShowInFinder(folder, True)
        elif action.startswith("res:"):
            self.app.set_pref("resolution", action.split(":", 1)[1])
        elif action == "browse_blender":
            chosen = c4d.storage.LoadDialog(title="Choose blender.exe", flags=c4d.FILESELECT_LOAD)
            if chosen:
                self.app.set_pref("blender_path", str(chosen))
                self.app.refresh_settings_info()
        elif action == "auto_blender":
            self.app.set_pref("blender_path", "")
            self.app.refresh_settings_info()
        elif action == "clear_cache":
            self._clear_cache()
        elif action == "toggle_menu":
            self.app.set_pref("show_main_menu", not self.app.prefs.get("show_main_menu", True))

    def _clear_cache(self) -> None:
        question = (
            "Delete all downloaded assets from this folder?\n"
            "Scenes that use them may lose their textures."
        )
        if not c4d.gui.QuestionDialog(question):
            return
        try:
            pipeline.clear_cache()
        except Exception as exc:
            self.app._set_status(f"Could not clear cache: {exc}")
            return
        self.app._refresh_cached_ids()
        self.app.results["downloaded"] = []
        self.app.refresh_settings_info()
        self.app._set_status("Kitbridge cache cleared.")

    def _search(self) -> None:
        if self.app.settings_open:
            self._set_settings_open(False)
        tab = self.app.current_tab
        self.app.search(tab, self.GetString(ID_QUERY).strip())

    def _on_tab_changed(self, tab: str) -> None:
        if self.app.settings_open:
            self._set_settings_open(False)
        self.app.current_tab = tab
        self.app.set_pref("last_tab", tab)
        self.grid.tab = tab
        self.grid.highlighted_base_id = None
        if tab == "downloaded" or not self.app.results.get(tab):
            self.app.search(tab, self.GetString(ID_QUERY).strip())
        self._seen_version = -1
        self._sync(force=True)

    def _on_filter_changed(self, kind: str, val: str) -> None:
        if kind == "tier":
            self.app.tier_filter = val
        elif kind == "type":
            self.app.downloaded_type_filter = val
        elif kind == "resolution":
            self.app.set_pref("resolution", val)
            self.header_area.Redraw()
        self._sync(force=True)

    def _confirm_heavy(self, name: str, size_mb: int) -> bool:
        return bool(
            c4d.gui.QuestionDialog(
                f"{name} is a very large model ({size_mb} MB).\n\n"
                "Cinema 4D may stop responding for several minutes while it loads. Import it now?"
            )
        )

    def _on_user_click(self) -> None:
        if self.app.is_logged_in():
            self.app.logout()
        else:
            self.app.login()

    def _on_renderer_click(self) -> None:
        """Popup: Auto / Redshift / Arnold / Standard, plus "remember as default"."""
        from kitbridge.c4d_import.renderer import renderer_installed

        first = c4d.FIRST_POPUP_ID
        labels = {
            "auto": "Auto (document's render engine: " + self.app.document_renderer().capitalize() + ")",
            "redshift": "Redshift", "arnold": "Arnold", "standard": "Standard / Physical",
        }
        bc = c4d.BaseContainer()
        for index, choice in enumerate(prefs.RENDERER_CHOICES):
            flags = "&c&" if choice == self.app.renderer_choice else ""
            if choice in ("redshift", "arnold") and not renderer_installed(choice):
                flags += "&d&"  # engine not installed: materials could not be built
            bc.InsData(first + index, labels[choice] + flags)
        bc.InsData(0, "")  # separator
        remember_id = first + len(prefs.RENDERER_CHOICES)
        bc.InsData(remember_id, "Remember as default" + ("&c&" if self.app.prefs.get("remember_renderer") else ""))
        picked = c4d.gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if picked == remember_id:
            self.app.set_remember_renderer(not self.app.prefs.get("remember_renderer"))
        elif first <= picked < remember_id:
            self.app.set_renderer_choice(prefs.RENDERER_CHOICES[picked - first])
        self._sync(force=True)

    def _check_clipboard(self) -> None:
        """Watch clipboard for website 'Get this' code asset_base_id:<uuid>."""
        try:
            raw = c4d.GetStringFromClipboard()
        except Exception:
            return
        first_read = self._last_clipboard_text is None
        if raw == self._last_clipboard_text:
            return
        self._last_clipboard_text = raw
        if first_read or not raw:  # what was copied before the window opened is not a new code
            return
        uuid = layout.parse_clipboard_code(raw)
        if not uuid:
            return
        code_query = f"asset_base_id:{uuid}"
        self.SetString(ID_QUERY, code_query)
        self.grid.highlighted_base_id = uuid
        for tab in ("model", "material", "hdr"):
            self.app.search(tab, code_query)

    def _on_activate(self, item: Dict[str, Any]) -> None:
        if not self.app.prefs.get("accepted_disclaimer"):
            return
        self.app.activate(item)
        self._sync(force=True)

    def _show_notice(self, visible: bool) -> None:
        self._notice_visible = visible
        self._apply_view()

    def _apply_view(self) -> None:
        """Exactly one of notice, settings and grid fills the middle of the window."""
        settings = self.app.settings_open and not self._notice_visible
        self.HideElement(ID_NOTICE_GROUP, not self._notice_visible)
        self.HideElement(ID_SETTINGS_GROUP, not settings)
        self.HideElement(ID_GRID_GROUP, self._notice_visible or settings)
        self.HideElement(ID_FILTER_GROUP, settings)
        self.LayoutChanged(ID_ROOT)

    def _sync(self, force: bool = False) -> None:
        if not force and self._seen_version == self.app.version:
            return
        self._seen_version = self.app.version

        tab = self.app.current_tab
        self.grid.tab = tab
        filtered = self.app.filtered_results(tab)
        self.grid.set_items(filtered)

        self.header_area.Redraw()
        self.filter_row_area.Redraw()
        self.footer_area.Redraw()
        self.settings_area.Redraw()
        self.settings_btn.Redraw()


_DIALOG: Any = None


def open_dialog(plugin_id: int = 0) -> bool:
    """Open (or bring forward) the dockable Kitbridge window."""
    global _DIALOG
    if _DIALOG is None:
        _DIALOG = KitbridgeDialog()
    return _DIALOG.Open(c4d.DLG_TYPE_ASYNC, pluginid=plugin_id, defaultw=512, defaulth=900)


def restore_dialog(plugin_id: int, sec_ref: Any) -> bool:
    global _DIALOG
    if _DIALOG is None:
        _DIALOG = KitbridgeDialog()
    return _DIALOG.Restore(pluginid=plugin_id, secret=sec_ref)


def close_for_dev() -> None:
    global _DIALOG
    if _DIALOG is not None:
        _DIALOG.Close()
    _DIALOG = None
