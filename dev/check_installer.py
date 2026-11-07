#!/usr/bin/env python3
"""Run the built setup .exe against throwaway folders: nothing real is touched."""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    exe = sorted(glob.glob(os.path.join(REPO, "dist", "KitbridgeSetup-*.exe")), key=os.path.getmtime)[-1]
    tmp = tempfile.mkdtemp(prefix="kb setup ")
    plugins, state, data = (os.path.join(tmp, n) for n in ("c4d plugins", "state", "blenderkit_data"))
    dest = os.path.join(plugins, "kitbridge")
    pyp = os.path.join(dest, "kitbridge.pyp")
    failed = []

    def run(*args):
        cmd = [exe, "/silent", "/noregister", f"/plugins={plugins}", f"/state={state}", f"/data={data}", *args]
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace").returncode

    def check(name, ok):
        print(("ok    " if ok else "FAIL  ") + name)
        if not ok:
            failed.append(name)

    def touch(path, content=""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    try:
        os.makedirs(plugins)
        check("install exit 0", run() == 0)
        check("plugin copied", os.path.isfile(pyp))
        check("bundled client", any("bk_client-windows" in f for _r, _d, fs in os.walk(dest) for f in fs))
        check("other platforms left out", not any("bk_client-linux" in f or "bk_client-macos" in f for _r, _d, fs in os.walk(dest) for f in fs))
        check("uninstaller and record", os.path.isfile(os.path.join(state, "uninstall.exe")) and os.path.isfile(os.path.join(state, "targets.txt")))

        old = os.path.join(dest, "stale.txt")
        touch(old)
        check("update exit 0", run() == 0)
        check("update replaces old files", not os.path.exists(old) and os.path.isfile(pyp))

        # Plain uninstall keeps downloads and settings.
        cache = os.path.join(data, "kitbridge_cache", "models", "a_1024", "kitbridge.json")
        prefs = os.path.join(data, "kitbridge", "prefs.json")
        addon = os.path.join(data, "models", "blender_addon_asset.blend")
        touch(cache)
        touch(addon)
        custom = os.path.join(tmp, "my downloads")
        touch(prefs, json.dumps({"download_dir": custom}))
        package = os.path.join(custom, "models", "b_2048", "kitbridge.json")
        lookalike = os.path.join(custom, "models", "photos_2024", "holiday.jpg")
        mine = os.path.join(custom, "my_notes.txt")
        touch(package)
        touch(lookalike)
        touch(mine)

        check("uninstall exit 0", run("/uninstall") == 0)
        check("plugin removed", not os.path.exists(dest))
        check("state removed", not os.path.exists(state))
        check("no leftover temp folder", not os.path.exists(dest + ".kbnew"))
        check("plain uninstall keeps data", os.path.isfile(cache) and os.path.isfile(prefs) and os.path.isfile(package))

        # Full removal takes only what is Kitbridge's.
        check("reinstall exit 0", run() == 0)
        check("purge exit 0", run("/uninstall", "/purge") == 0)
        check("purge removes cache and settings", not os.path.exists(os.path.dirname(prefs)) and not os.path.exists(os.path.join(data, "kitbridge_cache")))
        check("purge removes custom packages", not os.path.exists(os.path.dirname(package)))
        check("purge keeps BlenderKit add-on files", os.path.isfile(addon))
        check("purge keeps a look-alike folder", os.path.isfile(lookalike))
        check("purge keeps the user's own files", os.path.isfile(mine))

        # Someone else's folder named kitbridge.
        foreign = os.path.join(dest, "someone_elses.txt")
        touch(foreign)
        check("foreign folder reported", run() == 1)
        check("foreign folder kept", os.path.isfile(foreign) and not os.path.isfile(pyp))
        check("uninstall keeps foreign folder", run("/uninstall") == 0 and os.path.isfile(foreign))
    finally:
        time.sleep(0.5)
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"{'FAILED ' + str(len(failed)) if failed else 'all passed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
