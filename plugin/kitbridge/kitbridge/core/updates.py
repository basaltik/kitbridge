# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 3
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, see <https://www.gnu.org/licenses/>.
#
# ##### END GPL LICENSE BLOCK #####

"""Tell the user when a newer Kitbridge is released on GitHub.

Once a day at most, the latest release is read from GitHub's public API. Nothing about the user
is sent: the request carries only a generic User-Agent with the Kitbridge version. Updating
itself stays with the installer (it replaces the plugin and keeps downloads and settings).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger("kitbridge")

RELEASES_API = "https://api.github.com/repos/basaltik/kitbridge/releases/latest"
RELEASES_PAGE = "https://github.com/basaltik/kitbridge/releases/latest"
CHECK_INTERVAL_SECONDS = 24 * 3600
REQUEST_TIMEOUT_SECONDS = 5.0


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """``"v0.7.0"`` -> ``(0, 7, 0)``; ``None`` when there is no version number in it."""
    match = re.search(r"(\d+(?:\.\d+)*)", str(text or ""))
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    new, old = parse_version(latest), parse_version(current)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def _fetch_latest(current: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        RELEASES_API,
        headers={"User-Agent": f"Kitbridge/{current}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _release_url(data: Dict[str, Any]) -> str:
    url = str(data.get("html_url") or "")
    # Only ever open Kitbridge's own release page.
    return url if url.startswith("https://github.com/basaltik/kitbridge/releases/") else RELEASES_PAGE


def check_for_update(
    current: str,
    cache_path: str,
    *,
    now: Optional[float] = None,
    fetch: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Optional[Dict[str, str]]:
    """``{"version", "url"}`` of a release newer than *current*, else ``None``. Never raises."""
    now = time.time() if now is None else now
    cached: Dict[str, Any] = {}
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}
    try:
        fresh = 0.0 <= now - float(cached["checked_at"]) < CHECK_INTERVAL_SECONDS
    except (KeyError, TypeError, ValueError):
        fresh = False
    if not fresh:
        try:
            data = (fetch or _fetch_latest)(current)
            if data.get("draft") or data.get("prerelease"):
                raise ValueError("not a full release")
            cached = {
                "checked_at": now,
                "version": str(data.get("tag_name") or data.get("name") or ""),
                "url": _release_url(data),
            }
        except Exception as exc:  # offline, rate-limited, unexpected answer: try again tomorrow
            log.debug("Update check failed: %s", exc)
            cached = {"checked_at": now, "version": "", "url": ""}
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cached, fh)
        except OSError as exc:
            log.debug("Could not store the update check: %s", exc)
    version = str(cached.get("version") or "")
    if version and is_newer(version, current):
        return {"version": version.lstrip("vV"), "url": str(cached.get("url") or RELEASES_PAGE)}
    return None
