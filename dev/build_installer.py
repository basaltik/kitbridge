#!/usr/bin/env python3
"""Build dist/KitbridgeSetup-<version>.exe: one file with the plugin inside, Windows only.

Needs nothing beyond the C# compiler that ships with Windows. Pillow adds the icon if present.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(REPO, "plugin", "kitbridge")
HERE = os.path.join(REPO, "dev", "installer")
DIST = os.path.join(REPO, "dist")
CSC = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe")
OTHER_PLATFORMS = re.compile(r"bk_client-(linux|macos)")


def version() -> str:
    with open(os.path.join(PLUGIN, "kitbridge", "__init__.py"), encoding="utf-8") as fh:
        return re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)


def write_payload(path: str) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(PLUGIN):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if not name.endswith(".pyc") and not OTHER_PLATFORMS.match(name):
                    full = os.path.join(root, name)
                    z.write(full, "kitbridge/" + os.path.relpath(full, PLUGIN).replace("\\", "/"))


def write_icon(path: str) -> bool:
    try:
        from PIL import Image
    except ImportError:
        return False
    Image.open(os.path.join(PLUGIN, "res", "icon.png")).convert("RGBA").save(
        path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )
    return True


def main() -> int:
    ver = version()
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, f"KitbridgeSetup-{ver}.exe")
    with tempfile.TemporaryDirectory() as tmp:
        payload = os.path.join(tmp, "payload.zip")
        write_payload(payload)
        info = os.path.join(tmp, "BuildInfo.cs")
        with open(info, "w", encoding="utf-8") as fh:
            fh.write(f'static class BuildInfo {{ public const string Version = "{ver}"; }}\n')
        icon = os.path.join(tmp, "icon.ico")
        cmd = [
            CSC, "/nologo", "/target:winexe", "/optimize+", "/codepage:65001", f"/out:{out}",
            f"/win32manifest:{os.path.join(HERE, 'setup.manifest')}", f"/resource:{payload},payload.zip",
            "/r:System.IO.Compression.dll", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll", "/r:System.Core.dll",
        ]
        if write_icon(icon):
            cmd.append(f"/win32icon:{icon}")
        cmd += [os.path.join(HERE, "setup.cs"), info]
        done = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if done.returncode != 0:
            print(done.stdout)
            return done.returncode
    print(out, round(os.path.getsize(out) / 1048576, 1), "MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
