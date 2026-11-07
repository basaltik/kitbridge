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
#  Original code Copyright (C) BlenderKit developers (bk_maya / bk_client).
#  Adapted for Kitbridge.
#
# ##### END GPL LICENSE BLOCK #####

from __future__ import annotations

import os
import re

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_PACKAGE_NAME_RE = re.compile(r"^.+_(?:\d+|ORIGINAL)$")

# Downloads are sorted into one subfolder per asset type inside the chosen folder.
ASSET_FOLDERS = {"model": "models", "material": "materials", "hdr": "hdris"}
OTHER_FOLDER = "other"

_cache_root_override = ""


def _sanitize_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_TOKEN_RE.sub("_", (value or "").strip()).strip("._")
    return cleaned or fallback


def global_dir(create: bool = True) -> str:
    """Return ``~/blenderkit_data`` (or ``$KITBRIDGE_GLOBAL_DIR`` override)."""
    override = os.environ.get("KITBRIDGE_GLOBAL_DIR", "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    else:
        path = os.path.join(os.path.expanduser("~"), "blenderkit_data")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def set_cache_root(path: str) -> None:
    """Use *path* as the download folder; an empty value restores the default."""
    global _cache_root_override
    cleaned = (path or "").strip()
    _cache_root_override = os.path.abspath(os.path.expanduser(cleaned)) if cleaned else ""


def default_cache_root() -> str:
    return os.path.join(global_dir(create=False), "kitbridge_cache")


def cache_root(create: bool = True) -> str:
    path = _cache_root_override or default_cache_root()
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def asset_folder_name(asset_type: str) -> str:
    return ASSET_FOLDERS.get((asset_type or "").strip().lower(), OTHER_FOLDER)


def _package_name(asset_base_id: str, resolution: str) -> str:
    safe_id = _sanitize_component(asset_base_id, "unknown")
    safe_res = _sanitize_component(resolution, "ORIGINAL")
    return f"{safe_id}_{safe_res}"


def asset_cache_dir(
    asset_base_id: str,
    resolution: str = "ORIGINAL",
    *,
    asset_type: str = "",
    create: bool = True,
) -> str:
    """Return ``<root>/<type folder>/<assetBaseId>_<resolution>``.

    Without *asset_type* it is the older flat ``<root>/<assetBaseId>_<resolution>``.
    """
    root = cache_root(create=create)
    parent = os.path.join(root, asset_folder_name(asset_type)) if asset_type else root
    path = os.path.join(parent, _package_name(asset_base_id, resolution))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _package_parents() -> list:
    root = cache_root(create=False)
    folders = list(ASSET_FOLDERS.values()) + [OTHER_FOLDER]
    return [os.path.join(root, name) for name in folders] + [root]


def find_asset_cache_dir(asset_base_id: str, resolution: str = "ORIGINAL") -> str:
    """Existing package dir of this asset in any subfolder (or the flat layout), else ``""``."""
    name = _package_name(asset_base_id, resolution)
    for parent in _package_parents():
        candidate = os.path.join(parent, name)
        if os.path.isdir(candidate):
            return candidate
    return ""


def _is_package_dir(path: str) -> bool:
    """Only folders Kitbridge made: the user's folder may hold anything else."""
    if not _PACKAGE_NAME_RE.match(os.path.basename(path)):
        return False
    return os.path.isfile(os.path.join(path, "kitbridge.json")) or os.path.isdir(os.path.join(path, "source"))


def package_dirs() -> list:
    """All Kitbridge package folders under the current download folder."""
    found = []
    for parent in _package_parents():
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        found.extend(p for p in (os.path.join(parent, n) for n in names) if _is_package_dir(p))
    return found


def client_dir(create: bool = True) -> str:
    path = os.path.join(global_dir(create=create), "client")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def client_bin_dir(version: str, create: bool = True) -> str:
    ver = version if version.startswith("v") else f"v{version}"
    path = os.path.join(client_dir(create=create), "bin", ver)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def client_log_dir(create: bool = True) -> str:
    return client_dir(create=create)


def client_log_path(create: bool = True) -> str:
    return os.path.join(client_log_dir(create=create), "default.log")


def kitbridge_data_dir(create: bool = True) -> str:
    path = os.path.join(global_dir(create=create), "kitbridge")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def secrets_file_path(create_parent: bool = True) -> str:
    """Return ``~/blenderkit_data/kitbridge/secrets.json``."""
    return os.path.join(kitbridge_data_dir(create=create_parent), "secrets.json")


def search_temp_dir(asset_type: str = "model", create: bool = True) -> str:
    safe_type = _sanitize_component(asset_type.lower(), "model")
    path = os.path.join(global_dir(create=create), f"{safe_type}_search")
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def plugin_root() -> str:
    """Return ``<repo>/plugin/kitbridge``."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def bundled_client_root() -> str:
    """Return ``plugin/kitbridge/client`` (with fallback to ``kitbridge/client``)."""
    primary = os.path.join(plugin_root(), "client")
    if os.path.isdir(primary):
        return primary
    secondary = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client")
    if os.path.isdir(secondary):
        return secondary
    return primary
