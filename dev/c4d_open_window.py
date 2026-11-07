"""Open the Kitbridge window inside a running Cinema 4D without installing the plugin.

Run inside C4D (Script Manager or an exec): reloads every kitbridge module from the repo and
opens a fresh window, so code changes show up without restarting Cinema 4D.
"""

import os
import sys
import traceback

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    globals().get("__file__") or r"E:\claude softiki\kitbridge\dev\c4d_open_window.py"))), "plugin", "kitbridge")


def main() -> None:
    try:
        old_ui = sys.modules.get("kitbridge.ui.dialog")
        if old_ui is not None:
            old_ui.close_for_dev()
        old_app = sys.modules.get("kitbridge.ui.app")
        if old_app is not None:
            old_app.reset_app_for_dev()
        for name in [n for n in sys.modules if n == "kitbridge" or n.startswith("kitbridge.")]:
            del sys.modules[name]
        if PLUGIN_DIR not in sys.path:
            sys.path.insert(0, PLUGIN_DIR)

        from kitbridge.ui.dialog import open_dialog

        print("KB_WINDOW opened" if open_dialog() else "KB_WINDOW Open() returned False")
    except Exception:
        print("KB_WINDOW error\n" + traceback.format_exc())


main()
