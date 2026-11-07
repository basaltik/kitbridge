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

"""Search URL builder and search result normalisation."""

from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from . import paths
from .client import DEFAULT_SERVER, BKClient

log = logging.getLogger("kitbridge")

VALID_ASSET_TYPES: Tuple[str, ...] = ("model", "material", "hdr")

# Omitted like blendkit-rhino does: we are not Maya and should not report as it.
# TODO: ask BlenderKit which addon_type value they want for Cinema 4D before public release.
DEFAULT_ADDON_TYPE = ""

_FILE_TYPE_TO_RESOLUTION_INT: Dict[str, int] = {
    "resolution_0_5K": 512,
    "resolution_1K": 1024,
    "resolution_2K": 2048,
    "resolution_4K": 4096,
    "resolution_8K": 8192,
}


def build_search_url(
    query: str = "",
    asset_type: str = "model",
    *,
    category: str = "",
    free_first: bool = False,
    bookmarks: bool = False,
    page_size: int = 24,
    page: int = 1,
    order: str = "",
    addon_type: str = DEFAULT_ADDON_TYPE,
    server: str = DEFAULT_SERVER,
    next_url: str = "",
) -> str:
    """Build a BlenderKit ``/api/v1/search/`` URL for ``model``, ``material``, or ``hdr``."""
    if next_url and next_url.strip():
        return next_url.strip()

    norm_type = (asset_type or "").strip().lower()
    if norm_type not in VALID_ASSET_TYPES:
        raise ValueError(
            f"Unsupported asset_type {asset_type!r}; expected one of {VALID_ASSET_TYPES}"
        )
    if page_size <= 0:
        raise ValueError("page_size must be a positive integer")
    if page <= 0:
        raise ValueError("page must be >= 1")

    parts: List[str] = [f"asset_type:{norm_type}"]
    clean_query = (query or "").strip()
    if clean_query:
        parts.append(clean_query)

    clean_category = (category or "").strip()
    if clean_category:
        parts.append(f"category_subtree:{clean_category}")

    if bookmarks:
        parts.append("bookmarks_rating:1")

    order_terms: List[str] = []
    if free_first:
        order_terms.append("-is_free")
    clean_order = (order or "").strip()
    order_terms.append(clean_order if clean_order else "_score")

    raw_q = "+".join(parts) + "+order:" + ",".join(order_terms)
    encoded_q = urllib.parse.quote_plus(raw_q, safe=":+-,")

    base = (server or DEFAULT_SERVER).rstrip("/")
    url_parts = [
        f"{base}/api/v1/search/?query={encoded_q}",
        "dict_parameters=1",
        f"page_size={int(page_size)}",
    ]
    if page > 1:
        url_parts.append(f"page={int(page)}")
    if addon_type:
        url_parts.append(f"addon_type={urllib.parse.quote_plus(addon_type)}")
    url_parts.append("blender_version=")

    return "&".join(url_parts)


def _filename_from_url(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.path:
        return ""
    return os.path.basename(parsed.path.rstrip("/"))


def _resolve_thumbnail_path(url: str, tempdir: str) -> str:
    if not url or not tempdir:
        return ""
    fname = _filename_from_url(url)
    if not fname:
        return ""
    return os.path.join(tempdir, fname)


def _extract_thumbnails(raw: Dict[str, Any], tempdir: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    small_url = str(
        raw.get("thumbnailSmallUrl")
        or raw.get("thumbnail_small_url")
        or raw.get("thumbnailSmallUrlWebp")
        or ""
    )
    middle_url = str(
        raw.get("thumbnailMiddleUrl")
        or raw.get("thumbnail_middle_url")
        or raw.get("thumbnailMiddleUrlWebp")
        or ""
    )
    large_url = str(
        raw.get("thumbnailLargeUrlNonsquared")
        or raw.get("thumbnail_large_url_nonsquared")
        or raw.get("thumbnailLargeUrlNonsquaredWebp")
        or raw.get("thumbnailLargeUrl")
        or ""
    )

    files = raw.get("files")
    if isinstance(files, list):
        for f in files:
            if not isinstance(f, dict) or f.get("fileType") != "thumbnail":
                continue
            if not small_url:
                small_url = str(f.get("thumbnailSmallUrl") or f.get("thumbnailSmallUrlWebp") or "")
            if not middle_url:
                middle_url = str(
                    f.get("thumbnailMiddleUrl") or f.get("thumbnailMiddleUrlWebp") or ""
                )
            if not large_url:
                large_url = str(
                    f.get("thumbnailLargeUrlNonsquared")
                    or f.get("thumbnailLargeUrlNonsquaredWebp")
                    or ""
                )

    urls = {
        "small": small_url,
        "middle": middle_url,
        "large": large_url,
    }
    paths_map = {
        "small": _resolve_thumbnail_path(small_url, tempdir),
        "middle": _resolve_thumbnail_path(middle_url, tempdir),
        "large": _resolve_thumbnail_path(large_url, tempdir),
    }
    return urls, paths_map


def _extract_resolutions_and_files(
    raw: Dict[str, Any],
) -> Tuple[List[str], List[int], List[Dict[str, Any]]]:
    res_ints: List[int] = []
    has_original = False
    normalised_files: List[Dict[str, Any]] = []

    raw_avail = raw.get("available_resolutions") or raw.get("availableResolutions")
    if isinstance(raw_avail, list):
        for item in raw_avail:
            if isinstance(item, int):
                if item not in res_ints:
                    res_ints.append(item)
            elif isinstance(item, str):
                s = item.strip()
                if s.isdigit():
                    val = int(s)
                    if val not in res_ints:
                        res_ints.append(val)
                elif s in _FILE_TYPE_TO_RESOLUTION_INT:
                    val = _FILE_TYPE_TO_RESOLUTION_INT[s]
                    if val not in res_ints:
                        res_ints.append(val)
                elif s.upper() == "ORIGINAL":
                    has_original = True

    raw_files = raw.get("files")
    if isinstance(raw_files, list):
        for entry in raw_files:
            if not isinstance(entry, dict):
                continue
            file_type = str(entry.get("fileType") or entry.get("file_type") or "")
            if file_type in _FILE_TYPE_TO_RESOLUTION_INT:
                val = _FILE_TYPE_TO_RESOLUTION_INT[file_type]
                if val not in res_ints:
                    res_ints.append(val)
            elif file_type in ("blend", "gltf", "gltf_godot"):
                has_original = True

            normalised_entry = dict(entry)
            normalised_entry["fileType"] = file_type
            normalised_entry["downloadUrl"] = str(
                entry.get("downloadUrl") or entry.get("download_url") or entry.get("url") or ""
            )
            normalised_entry["fileName"] = str(
                entry.get("fileName") or entry.get("file_name") or ""
            )
            normalised_files.append(normalised_entry)

    res_ints.sort()
    res_labels = [str(r) for r in res_ints]
    if has_original and "ORIGINAL" not in res_labels:
        res_labels.append("ORIGINAL")

    return res_labels, res_ints, normalised_files


def _extract_author(raw: Dict[str, Any]) -> str:
    author_raw = raw.get("author")
    if isinstance(author_raw, dict):
        full = str(author_raw.get("fullName") or "").strip()
        if full:
            return full
        first = str(author_raw.get("firstName") or "").strip()
        last = str(author_raw.get("lastName") or "").strip()
        combined = f"{first} {last}".strip()
        if combined:
            return combined
        if author_raw.get("id") is not None:
            return str(author_raw.get("id"))
        return ""
    if isinstance(author_raw, str):
        return author_raw.strip()
    return ""


def normalise_asset(raw: Dict[str, Any], tempdir: str = "") -> Dict[str, Any]:
    """Normalise a raw BlenderKit search item into a predictable plain dictionary."""
    if not isinstance(raw, dict):
        raise TypeError("raw asset must be a dict")

    asset_id = str(raw.get("id") or "")
    asset_base_id = str(raw.get("assetBaseId") or raw.get("asset_base_id") or asset_id)
    name = str(raw.get("displayName") or raw.get("name") or "")
    asset_type = str(raw.get("assetType") or raw.get("asset_type") or "model")
    is_free = bool(raw.get("isFree", raw.get("is_free", False)))

    if "canDownload" in raw:
        can_download = bool(raw.get("canDownload"))
    elif "can_download" in raw:
        can_download = bool(raw.get("can_download"))
    else:
        can_download = is_free

    author = _extract_author(raw)
    license_slug = str(raw.get("license") or "royalty_free").strip() or "royalty_free"
    source_url = str(
        raw.get("sourceUrl") or raw.get("source_url") or raw.get("url") or ""
    ).strip()
    if not source_url and asset_base_id:
        source_url = f"https://www.blenderkit.com/asset-gallery-detail/{asset_base_id}/"

    thumb_urls, thumb_paths = _extract_thumbnails(raw, tempdir)
    res_labels, res_ints, files = _extract_resolutions_and_files(raw)

    return {
        "id": asset_id,
        "assetBaseId": asset_base_id,
        "name": name,
        "assetType": asset_type,
        "isFree": is_free,
        "canDownload": can_download,
        "canDownloadError": raw.get("canDownloadError"),
        "author": author,
        "license": license_slug,
        "sourceUrl": source_url,
        "thumbnailUrls": thumb_urls,
        "thumbnailPaths": thumb_paths,
        "thumbnailSmallUrl": thumb_urls["small"],
        "thumbnailMiddleUrl": thumb_urls["middle"],
        "thumbnailLargeUrl": thumb_urls["large"],
        "availableResolutions": res_labels,
        "available_resolutions": res_ints,
        "files": files,
    }


def normalise_results(
    raw_results: Union[Dict[str, Any], Sequence[Dict[str, Any]]],
    tempdir: str = "",
) -> List[Dict[str, Any]]:
    """Normalise a ``SearchResults`` dict or list of raw assets into plain dicts."""
    items: Sequence[Any]
    if isinstance(raw_results, dict):
        inner = raw_results.get("results")
        items = inner if isinstance(inner, list) else []
    elif isinstance(raw_results, (list, tuple)):
        items = raw_results
    else:
        return []

    normalised: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("assetType") == "author":
            continue
        normalised.append(normalise_asset(item, tempdir=tempdir))
    return normalised


def start_search(
    client: BKClient,
    query: str = "",
    *,
    asset_type: str = "model",
    category: str = "",
    free_first: bool = False,
    bookmarks: bool = False,
    page_size: int = 24,
    page: int = 1,
    order: str = "",
    addon_type: str = DEFAULT_ADDON_TYPE,
    api_key: str = "",
    tempdir: Optional[str] = None,
    next_url: str = "",
) -> str:
    """Build a search URL and schedule it on *client*; return the ``task_id``."""
    urlquery = build_search_url(
        query=query,
        asset_type=asset_type,
        category=category,
        free_first=free_first,
        bookmarks=bookmarks,
        page_size=page_size,
        page=page,
        order=order,
        addon_type=addon_type,
        server=client.server,
        next_url=next_url,
    )
    resolved_temp = tempdir or paths.search_temp_dir(asset_type, create=True)
    return client.asset_search(
        urlquery=urlquery,
        tempdir=resolved_temp,
        asset_type=asset_type.strip().lower(),
        api_key=api_key,
        page_size=page_size,
        next_url=next_url,
        get_next=bool(next_url),
    )
