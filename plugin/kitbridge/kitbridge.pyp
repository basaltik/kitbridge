"""Kitbridge — BlenderKit assets in Cinema 4D. Entry point loaded by Cinema 4D."""

import logging
import os
import sys

import c4d

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from kitbridge import __version__  # noqa: E402

# Registered at developers.maxon.net/forum/pid ("Kitbridge", "Kitbridgebackground").
PLUGIN_ID_COMMAND = 1073125
PLUGIN_ID_PUMP = 1073126

log = logging.getLogger("kitbridge")


class KitbridgeCommand(c4d.plugins.CommandData):
    def Execute(self, doc):
        from kitbridge.ui.dialog import open_dialog

        return open_dialog(PLUGIN_ID_COMMAND)

    def RestoreLayout(self, sec_ref):
        from kitbridge.ui.dialog import restore_dialog

        return restore_dialog(PLUGIN_ID_COMMAND, sec_ref)


class KitbridgePump(c4d.plugins.MessageData):
    """Keeps downloads and website "Get this" imports going while the window is closed."""

    def GetTimer(self):
        return 250

    def CoreMessage(self, mid, bc):
        if mid == c4d.MSG_TIMER:
            from kitbridge.ui import app

            if app._APP is not None:  # only once the window has started the app
                try:
                    app._APP.tick()
                except Exception:
                    log.exception("Kitbridge background tick failed")
        return True


def _icon():
    bmp = c4d.bitmaps.BaseBitmap()
    path = os.path.join(PLUGIN_DIR, "res", "icon.png")
    if os.path.isfile(path) and bmp.InitWith(path)[0] == c4d.IMAGERESULT_OK:
        return bmp
    return None


def EnhanceMainMenu():
    from kitbridge.ui import prefs as prefs_mod

    p = prefs_mod.load()
    if not p.get("show_main_menu", True):
        return
    mainMenu = c4d.gui.GetMenuResource("M_EDITOR")
    if not mainMenu:
        return
    pluginsMenu = c4d.gui.SearchPluginMenuResource()
    menu = c4d.BaseContainer()
    menu.InsData(c4d.MENURESOURCE_SUBTITLE, "Kitbridge")
    menu.InsData(c4d.MENURESOURCE_COMMAND, f"PLUGIN_CMD_{PLUGIN_ID_COMMAND}")
    if pluginsMenu:
        mainMenu.InsDataAfter(c4d.MENURESOURCE_STRING, menu, pluginsMenu)
    else:
        mainMenu.InsData(c4d.MENURESOURCE_STRING, menu)


def PluginMessage(mid, data):
    if mid == c4d.C4DPL_BUILDMENU:
        try:
            EnhanceMainMenu()
        except Exception:
            log.exception("Kitbridge main menu setup failed")
        return True
    if mid == c4d.C4DPL_ENDACTIVITY:
        from kitbridge.ui import app

        if app._APP is not None:
            app._APP.shutdown()
    return False


if __name__ == "__main__":
    c4d.plugins.RegisterCommandPlugin(
        PLUGIN_ID_COMMAND, "Kitbridge", 0, _icon(),
        "Browse and import BlenderKit assets (v%s)" % __version__, KitbridgeCommand(),
    )
    c4d.plugins.RegisterMessagePlugin(PLUGIN_ID_PUMP, "Kitbridge background", 0, KitbridgePump())
