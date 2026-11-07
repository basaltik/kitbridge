#!/usr/bin/env python3
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

"""Download and extract the latest v1.13.x bk_client release into plugin/kitbridge/client/."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("kitbridge")

GITHUB_RELEASES_API = "https://api.github.com/repos/BlenderKit/bk_client/releases?per_page=30"
TARGET_SERIES = "v1.13"
ASSET_NAME = "bk_client.zip"
NETWORK_TIMEOUT = 30.0
DOWNLOAD_TIMEOUT = 120.0


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _default_dest_dir() -> str:
    return os.path.join(_repo_root(), "plugin", "kitbridge", "client")


def _parse_version_tag(tag: str) -> Optional[Tuple[int, ...]]:
    cleaned = tag.strip()
    if cleaned.startswith("v"):
        cleaned = cleaned[1:]
    parts = cleaned.split(".")
    if len(parts) < 2:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _matches_series(tag: str, series: str = TARGET_SERIES) -> bool:
    series_tuple = _parse_version_tag(series)
    tag_tuple = _parse_version_tag(tag)
    if series_tuple is None or tag_tuple is None:
        return False
    return tag_tuple[: len(series_tuple)] == series_tuple


def _fetch_releases(api_url: str = GITHUB_RELEASES_API, timeout: float = NETWORK_TIMEOUT) -> List[Dict[str, Any]]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Kitbridge-FetchClient/0.1",
    }
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(api_url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Unexpected GitHub releases response format.")
    return data


def resolve_latest_release(
    releases: List[Dict[str, Any]],
    series: str = TARGET_SERIES,
) -> Tuple[str, str]:
    """Return ``(tag_name, download_url)`` for the newest matching release."""
    best_ver: Optional[Tuple[int, ...]] = None
    best_tag = ""
    best_url = ""

    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "").strip()
        if not _matches_series(tag, series):
            continue
        ver = _parse_version_tag(tag)
        if ver is None:
            continue

        download_url = ""
        for asset in rel.get("assets") or []:
            if isinstance(asset, dict) and asset.get("name") == ASSET_NAME:
                download_url = str(asset.get("browser_download_url") or "")
                break
        if not download_url:
            continue

        if best_ver is None or ver > best_ver:
            best_ver = ver
            best_tag = tag if tag.startswith("v") else f"v{tag}"
            best_url = download_url

    if not best_tag or not best_url:
        raise RuntimeError(f"No {series}.x release with {ASSET_NAME} found on GitHub.")
    return best_tag, best_url


def _download_file(url: str, dest_path: str, timeout: float = DOWNLOAD_TIMEOUT) -> None:
    headers = {"User-Agent": "Kitbridge-FetchClient/0.1"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as out_fh:
        shutil.copyfileobj(resp, out_fh)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _should_extract_member(member_name: str) -> bool:
    norm = member_name.replace("\\", "/").lstrip("/")
    if not norm or norm.endswith("/") or ".." in norm.split("/"):
        return False
    base = os.path.basename(norm)
    if norm == base and base.startswith("bk_client-"):
        return True
    if norm.startswith("tools/"):
        return True
    if norm in ("VERSION", "manifest.json"):
        return True
    return False


def _extract_archive(zip_path: str, target_version_dir: str) -> List[str]:
    if os.path.isdir(target_version_dir):
        shutil.rmtree(target_version_dir)
    os.makedirs(target_version_dir, exist_ok=True)

    extracted: List[str] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            norm = info.filename.replace("\\", "/").lstrip("/")
            if not _should_extract_member(norm):
                continue
            dest = os.path.abspath(os.path.join(target_version_dir, *norm.split("/")))
            if not dest.startswith(os.path.abspath(target_version_dir) + os.sep):
                raise RuntimeError(f"Refusing to extract unsafe zip path: {info.filename}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(info, "r") as src_fh, open(dest, "wb") as dst_fh:
                shutil.copyfileobj(src_fh, dst_fh)
            if os.path.basename(dest).startswith("bk_client-") and not dest.endswith(".exe"):
                try:
                    os.chmod(dest, 0o755)
                except OSError as exc:
                    log.debug("Could not chmod %s: %s", dest, exc)
            extracted.append(dest)

    manifest_path = os.path.join(target_version_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for binary in manifest.get("binaries") or []:
            fname = binary.get("filename")
            expected_sha = binary.get("sha256")
            if not fname or not expected_sha:
                continue
            bin_path = os.path.join(target_version_dir, fname)
            if os.path.isfile(bin_path):
                actual_sha = _sha256_file(bin_path)
                if actual_sha.lower() != str(expected_sha).lower():
                    raise RuntimeError(
                        f"SHA-256 mismatch for {fname}: expected {expected_sha}, got {actual_sha}"
                    )

    return extracted


def fetch_client(dest_root: Optional[str] = None, series: str = TARGET_SERIES) -> Tuple[str, str]:
    """Download and extract the latest bk_client release for *series*.

    Returns ``(resolved_tag, version_dir)``.
    """
    client_root = os.path.abspath(dest_root or _default_dest_dir())
    os.makedirs(client_root, exist_ok=True)

    log.info("Querying GitHub releases for BlenderKit/bk_client (%s.x)...", series)
    releases = _fetch_releases()
    tag, download_url = resolve_latest_release(releases, series=series)
    log.info("Resolved bk_client %s -> %s", tag, download_url)

    version_dir = os.path.join(client_root, tag)
    with tempfile.TemporaryDirectory(prefix="kitbridge_fetch_") as tmpdir:
        zip_path = os.path.join(tmpdir, ASSET_NAME)
        _download_file(download_url, zip_path)
        extracted = _extract_archive(zip_path, version_dir)
        log.info("Extracted %d files into %s", len(extracted), version_dir)

    resolved_file = os.path.join(client_root, "RESOLVED_VERSION")
    with open(resolved_file, "w", encoding="utf-8") as fh:
        fh.write(f"{tag}\n")
    log.info("Wrote %s (%s)", resolved_file, tag)
    return tag, version_dir


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Fetch prebuilt bk_client release binaries.")
    parser.add_argument(
        "--dest",
        default=_default_dest_dir(),
        help="Destination client root directory (default: plugin/kitbridge/client)",
    )
    parser.add_argument(
        "--series",
        default=TARGET_SERIES,
        help="Target minor series tag prefix (default: v1.13)",
    )
    args = parser.parse_args(argv)

    try:
        tag, version_dir = fetch_client(dest_root=args.dest, series=args.series)
    except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
        log.error("Failed to fetch bk_client: %s", exc)
        return 1

    print(f"Fetched bk_client {tag} -> {version_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
