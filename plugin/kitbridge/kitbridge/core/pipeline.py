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

"""Download & convert pipeline (Milestone 2a).

Given a normalized search result (or raw BlenderKit asset dict), downloads the
asset via ``bk_client`` and converts it via headless Blender (``kb_export.py``)
into a renderer-agnostic cached package on disk with ``kitbridge.json``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from . import blender, paths, search
from .blender import KBExportResult, KBLine, parse_kb_line
from .client import BKClient
from .shader_graph import (
    GraphLink,
    GraphNode,
    GraphSocket,
    MaterialClassification,
    SocketClassification,
    UVTransform,
    classify_material_output,
    classify_principled_bsdf,
    classify_socket,
)
from .tasks import Dispatcher, Poller

log = logging.getLogger("kitbridge")

SCHEMA_VERSION = 1
# Keep equal to kb_export.EXPORTER_VERSION. Packages without the field count as version 1.
EXPORTER_VERSION = 8
# Mazda MX-5 (207 MB FBX, 4.45M polygons) froze Cinema 4D for over 200 s; an 80 MB one was fine.
HEAVY_GEOMETRY_MB = 100
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 600.0  # with no progress at all: a slow download keeps going
DEFAULT_CONVERT_TIMEOUT_SECONDS = 600.0

VALID_ASSET_TYPES = ("model", "material", "hdr")
VALID_BLEND_METHODS = ("opaque", "clip", "blend")
VALID_COLORSPACES = ("srgb", "raw")
VALID_CHANNELS = ("rgb", "r", "g", "b", "a")
VALID_CHANNEL_NAMES = (
    "base_color",
    "metallic",
    "roughness",
    "specular",
    "ior",
    "normal",
    "bump",
    "displacement",
    "alpha",
    "emission",
    "transmission",
    "coat",
    "coat_roughness",
    "sheen",
    "subsurface",
    "translucency",
    "ambient_occlusion",
)

ProgressCallback = Callable[[str, float, str], None]

_RESOLUTION_TO_NORM: Dict[str, str] = {
    "512": "512",
    "0.5k": "512",
    "0_5k": "512",
    "resolution_0_5k": "512",
    "1024": "1024",
    "1k": "1024",
    "resolution_1k": "1024",
    "2048": "2048",
    "2k": "2048",
    "resolution_2k": "2048",
    "4096": "4096",
    "4k": "4096",
    "resolution_4k": "4096",
    "8192": "8192",
    "8k": "8192",
    "resolution_8k": "8192",
    "original": "ORIGINAL",
}

_NORM_TO_BK_RESOLUTION: Dict[str, str] = {
    "512": "resolution_0_5K",
    "1024": "resolution_1K",
    "2048": "resolution_2K",
    "4096": "resolution_4K",
    "8192": "resolution_8K",
    "ORIGINAL": "ORIGINAL",
}

_HDR_UNPACK_SCRIPT = """\
import os
import sys
import shutil
import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
out_dir = argv[0]
os.makedirs(out_dir, exist_ok=True)

try:
    bpy.ops.file.unpack_all(method="WRITE_LOCAL")
except Exception:
    pass

target_img = None
world = bpy.context.scene.world
if world and world.use_nodes and world.node_tree:
    for node in world.node_tree.nodes:
        if node.type == "TEX_ENVIRONMENT" and getattr(node, "image", None):
            target_img = node.image
            break

if target_img is None:
    for img in bpy.data.images:
        if img.name not in ("Render Result", "Viewer Node"):
            target_img = img
            break

if target_img is None:
    print("KB_ERROR No environment image found in HDR .blend file", flush=True)
    sys.exit(1)

src_path = bpy.path.abspath(target_img.filepath_raw or target_img.filepath)
if src_path and os.path.isfile(src_path):
    fname = os.path.basename(src_path)
    dst_path = os.path.join(out_dir, fname)
    if os.path.abspath(src_path) != os.path.abspath(dst_path):
        shutil.copy2(src_path, dst_path)
    print("KB_DONE " + fname, flush=True)
    sys.exit(0)

ext = ".exr" if getattr(target_img, "file_format", "").upper() in ("OPEN_EXR", "HDR") else ".hdr"
fname = (target_img.name or "environment") + ext
dst_path = os.path.join(out_dir, fname)
target_img.filepath_raw = dst_path
target_img.save()
print("KB_DONE " + fname, flush=True)
"""


class PipelineError(RuntimeError):
    """User-facing error raised by download or conversion stages."""

    def __init__(
        self,
        message: str,
        *,
        stage: str = "",
        log_path: str = "",
        log_tail: Optional[List[str]] = None,
    ) -> None:
        super().__init__(message)
        self.stage: str = stage
        self.log_path: str = log_path
        self.log_tail: List[str] = list(log_tail or [])


@dataclass(frozen=True)
class Package:
    """Resolved Kitbridge asset package on disk."""

    path: str
    manifest: Dict[str, Any]
    is_cached: bool = False


class PipelineJob:
    """Handle for a ``fetch_asset`` call running on a background worker thread."""

    def __init__(
        self,
        target: Callable[[threading.Event], Package],
        cancel_event: Optional[threading.Event] = None,
    ) -> None:
        self._cancel_event = cancel_event or threading.Event()
        self._done_event = threading.Event()
        self._package: Optional[Package] = None
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(
            target=self._run,
            args=(target,),
            name="kitbridge-pipeline-worker",
            daemon=True,
        )
        self._thread.start()

    def _run(self, target: Callable[[threading.Event], Package]) -> None:
        try:
            self._package = target(self._cancel_event)
        except BaseException as exc:
            self._error = exc
        finally:
            self._done_event.set()

    @property
    def done(self) -> bool:
        return self._done_event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def cancel(self) -> None:
        """Signal the in-flight download or Blender conversion to abort."""
        self._cancel_event.set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Wait for completion; return ``True`` if finished."""
        return self._done_event.wait(timeout=timeout)

    def result(self, timeout: Optional[float] = None) -> Package:
        """Wait for completion and return the :class:`Package` or re-raise any error."""
        if not self._done_event.wait(timeout=timeout):
            raise TimeoutError("PipelineJob did not finish within timeout")
        if self._error is not None:
            raise self._error
        assert self._package is not None
        return self._package


# ── Resolution Helpers ───────────────────────────────────────────────────────


def normalise_resolution(resolution: Union[str, int, None]) -> str:
    """Normalise a resolution specifier to ``'512'|'1024'|'2048'|'4096'|'8192'|'ORIGINAL'``."""
    if resolution is None:
        return "2048"
    raw = str(resolution).strip()
    if not raw:
        return "2048"
    mapped = _RESOLUTION_TO_NORM.get(raw.lower())
    if mapped is not None:
        return mapped
    if raw.isdigit():
        val = int(raw)
        if val <= 512:
            return "512"
        if val <= 1024:
            return "1024"
        if val <= 2048:
            return "2048"
        if val <= 4096:
            return "4096"
        return "8192"
    return "2048"


def to_bk_resolution(resolution: Union[str, int, None]) -> str:
    """Convert a resolution specifier to the ``bk_client`` resolution key (e.g. ``'resolution_2K'``)."""
    norm = normalise_resolution(resolution)
    return _NORM_TO_BK_RESOLUTION.get(norm, "resolution_2K")


def resolution_to_int(resolution: Union[str, int, None], default: int = 2048) -> int:
    """Convert a resolution specifier to a pixel dimension integer."""
    norm = normalise_resolution(resolution)
    if norm.isdigit():
        return int(norm)
    return default


# ── Manifest Schema Validation ───────────────────────────────────────────────


def _is_number(val: Any) -> bool:
    return isinstance(val, (int, float)) and not isinstance(val, bool)


def _is_number_list(val: Any, length: int) -> bool:
    return (
        isinstance(val, list)
        and len(val) == length
        and all(_is_number(item) for item in val)
    )


def _resolve_pkg_file(package_dir: str, rel_path: str) -> str:
    norm_rel = rel_path.replace("\\", "/").lstrip("/")
    return os.path.join(package_dir, *norm_rel.split("/"))


UDIM_TOKEN = "<UDIM>"


def udim_tiles(path_with_token: str) -> List[str]:
    """Existing tile files for a path like ``.../body.<UDIM>.png`` (tiles 1001..1999), sorted."""
    folder, name = os.path.split(path_with_token)
    head, _, tail = name.partition(UDIM_TOKEN)
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    tiles = []
    for candidate in names:
        middle = candidate[len(head):len(candidate) - len(tail)] if tail else candidate[len(head):]
        if (candidate.startswith(head) and candidate.endswith(tail) and len(middle) == 4
                and middle.isdigit() and 1001 <= int(middle) <= 1999):
            tiles.append(os.path.join(folder, candidate))
    return sorted(tiles)


def _validate_channel_entry(
    mat_name: str,
    ch_name: str,
    entry: Any,
    package_dir: Optional[str],
    errors: List[str],
) -> None:
    prefix = f"material[{mat_name!r}].channels[{ch_name!r}]"
    if ch_name not in VALID_CHANNEL_NAMES:
        errors.append(f"{prefix}: unknown channel name {ch_name!r}")
    if not isinstance(entry, dict):
        errors.append(f"{prefix}: expected dict, got {type(entry).__name__}")
        return

    has_val = "value" in entry
    has_tex = "texture" in entry
    if not has_val and not has_tex:
        errors.append(f"{prefix}: must specify either 'value' or 'texture'")
        return

    if has_val:
        val = entry["value"]
        if not (_is_number(val) or _is_number_list(val, 3)):
            errors.append(
                f"{prefix}.value: expected float/int or [r, g, b] list, got {val!r}"
            )

    if has_tex:
        tex = entry["texture"]
        if not isinstance(tex, str) or not tex.strip():
            errors.append(f"{prefix}.texture: expected non-empty relative path string")
        elif os.path.isabs(tex):
            errors.append(f"{prefix}.texture: path must be relative, got {tex!r}")
        elif package_dir is not None and UDIM_TOKEN in tex:
            if not udim_tiles(_resolve_pkg_file(package_dir, tex)):
                errors.append(f"{prefix}.texture: no UDIM tiles found on disk for {tex!r}")
        elif package_dir is not None:
            abs_tex = _resolve_pkg_file(package_dir, tex)
            if not os.path.isfile(abs_tex):
                errors.append(f"{prefix}.texture: file not found on disk: {tex!r}")
            elif os.path.getsize(abs_tex) <= 0:
                errors.append(f"{prefix}.texture: file is empty (0 bytes): {tex!r}")

    if "colorspace" in entry and entry["colorspace"] not in VALID_COLORSPACES:
        errors.append(
            f"{prefix}.colorspace: expected one of {VALID_COLORSPACES}, got {entry['colorspace']!r}"
        )
    if "channel" in entry and entry["channel"] not in VALID_CHANNELS:
        errors.append(
            f"{prefix}.channel: expected one of {VALID_CHANNELS}, got {entry['channel']!r}"
        )
    for num_key in ("strength", "distance", "midlevel", "scale"):
        if num_key in entry and not _is_number(entry[num_key]):
            errors.append(f"{prefix}.{num_key}: expected number, got {entry[num_key]!r}")
    for bool_key in ("invert", "flip_green"):
        if bool_key in entry and not isinstance(entry[bool_key], bool):
            errors.append(f"{prefix}.{bool_key}: expected bool, got {entry[bool_key]!r}")


def _validate_material_entry(
    idx: int,
    mat: Any,
    package_dir: Optional[str],
    errors: List[str],
) -> None:
    if not isinstance(mat, dict):
        errors.append(f"materials[{idx}]: expected dict, got {type(mat).__name__}")
        return
    name = mat.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"materials[{idx}].name: expected non-empty string")
        name = f"#{idx}"

    blend = mat.get("blend_method")
    if blend not in VALID_BLEND_METHODS:
        errors.append(
            f"material[{name!r}].blend_method: expected one of {VALID_BLEND_METHODS}, got {blend!r}"
        )

    channels = mat.get("channels")
    if not isinstance(channels, dict):
        errors.append(f"material[{name!r}].channels: expected dict")
    else:
        for ch_name, ch_entry in channels.items():
            _validate_channel_entry(name, str(ch_name), ch_entry, package_dir, errors)

    uv = mat.get("uv")
    if not isinstance(uv, dict):
        errors.append(f"material[{name!r}].uv: expected dict")
    else:
        if not isinstance(uv.get("map"), str):
            errors.append(f"material[{name!r}].uv.map: expected str")
        if not _is_number_list(uv.get("scale"), 2):
            errors.append(f"material[{name!r}].uv.scale: expected [u, v] numbers")
        if not _is_number_list(uv.get("offset"), 2):
            errors.append(f"material[{name!r}].uv.offset: expected [u, v] numbers")
        if not _is_number(uv.get("rotation")):
            errors.append(f"material[{name!r}].uv.rotation: expected number")

    baked = mat.get("baked_channels")
    if not isinstance(baked, list) or not all(isinstance(x, str) for x in baked):
        errors.append(f"material[{name!r}].baked_channels: expected list of strings")

    warnings = mat.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(x, str) for x in warnings):
        errors.append(f"material[{name!r}].warnings: expected list of strings")


def validate_manifest(
    manifest: Any,
    package_dir: Optional[str] = None,
) -> List[str]:
    """Validate a ``kitbridge.json`` manifest dictionary.

    Returns a list of human-readable error strings (empty list when valid).
    When *package_dir* is provided, also verifies that ``geometry``, ``hdr``,
    and all referenced texture files exist on disk and are non-empty.
    """
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["Manifest root must be a JSON object (dict)."]

    schema_ver = manifest.get("schema_version")
    if isinstance(schema_ver, bool) or schema_ver != SCHEMA_VERSION:
        errors.append(
            f"schema_version: expected {SCHEMA_VERSION}, got {schema_ver!r}"
        )

    asset = manifest.get("asset")
    asset_type = ""
    if not isinstance(asset, dict):
        errors.append("asset: expected dict")
    else:
        for req_str in ("id", "base_id", "name", "resolution", "author", "license", "source_url"):
            val = asset.get(req_str)
            if not isinstance(val, str):
                errors.append(f"asset.{req_str}: expected str, got {type(val).__name__}")
        for non_empty in ("id", "base_id", "name"):
            val = asset.get(non_empty)
            if isinstance(val, str) and not val.strip():
                errors.append(f"asset.{non_empty}: must not be empty")
        asset_type = str(asset.get("type") or "")
        if asset_type not in VALID_ASSET_TYPES:
            errors.append(
                f"asset.type: expected one of {VALID_ASSET_TYPES}, got {asset_type!r}"
            )

    units = manifest.get("units")
    if not isinstance(units, dict):
        errors.append("units: expected dict")
    else:
        mpu = units.get("meters_per_unit")
        if not _is_number(mpu) or float(mpu) <= 0:
            errors.append(f"units.meters_per_unit: expected positive number, got {mpu!r}")
        if units.get("up_axis") != "Y":
            errors.append(f"units.up_axis: expected 'Y', got {units.get('up_axis')!r}")

    geom = manifest.get("geometry")
    if asset_type == "hdr":
        if geom is not None:
            errors.append(f"geometry: expected None for hdr asset, got {geom!r}")
        hdr_rel = manifest.get("hdr")
        if not isinstance(hdr_rel, str) or not hdr_rel.strip():
            errors.append("hdr: expected non-empty relative path string for hdr asset")
        elif os.path.isabs(hdr_rel):
            errors.append(f"hdr: path must be relative, got {hdr_rel!r}")
        elif package_dir is not None:
            abs_hdr = _resolve_pkg_file(package_dir, hdr_rel)
            if not os.path.isfile(abs_hdr):
                errors.append(f"hdr: file not found on disk: {hdr_rel!r}")
            elif os.path.getsize(abs_hdr) <= 0:
                errors.append(f"hdr: file is empty: {hdr_rel!r}")
    elif asset_type in ("model", "material"):
        if not isinstance(geom, str) or not geom.strip():
            errors.append(f"geometry: expected non-empty relative path for {asset_type}")
        elif os.path.isabs(geom):
            errors.append(f"geometry: path must be relative, got {geom!r}")
        elif package_dir is not None:
            abs_geom = _resolve_pkg_file(package_dir, geom)
            if not os.path.isfile(abs_geom):
                errors.append(f"geometry: file not found on disk: {geom!r}")
            elif os.path.getsize(abs_geom) <= 0:
                errors.append(f"geometry: file is empty: {geom!r}")

    objects = manifest.get("objects")
    if not isinstance(objects, list):
        errors.append("objects: expected list")
    else:
        for idx, obj in enumerate(objects):
            if not isinstance(obj, dict):
                errors.append(f"objects[{idx}]: expected dict")
                continue
            if not isinstance(obj.get("name"), str) or not obj["name"].strip():
                errors.append(f"objects[{idx}].name: expected non-empty string")
            mats = obj.get("materials")
            if not isinstance(mats, list) or not all(isinstance(m, str) for m in mats):
                errors.append(f"objects[{idx}].materials: expected list of strings")

    materials = manifest.get("materials")
    if not isinstance(materials, list):
        errors.append("materials: expected list")
    else:
        for idx, mat in enumerate(materials):
            _validate_material_entry(idx, mat, package_dir, errors)

    return errors


def is_valid_manifest(
    manifest: Any,
    package_dir: Optional[str] = None,
) -> bool:
    """Return ``True`` when *manifest* conforms to schema_version 1."""
    return len(validate_manifest(manifest, package_dir=package_dir)) == 0


# ── Internal Helpers ─────────────────────────────────────────────────────────


def _emit_progress(
    on_progress: Optional[ProgressCallback],
    stage: str,
    fraction: float,
    message: str,
) -> None:
    if on_progress is None:
        return
    clamped = max(0.0, min(1.0, float(fraction)))
    try:
        on_progress(stage, clamped, message)
    except Exception as exc:
        log.debug("Progress callback raised during stage %s: %s", stage, exc)


def _extract_asset_info(
    asset: Dict[str, Any],
    norm_res: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Return ``(asset_meta_for_manifest, download_asset_payload)``."""
    raw = asset.get("raw") if isinstance(asset.get("raw"), dict) else {}
    merged: Dict[str, Any] = dict(raw)
    for k, v in asset.items():
        if k != "raw" and v is not None:
            merged[k] = v

    base_id = str(
        merged.get("assetBaseId")
        or merged.get("base_id")
        or merged.get("id")
        or ""
    ).strip()
    asset_id = str(merged.get("id") or base_id).strip()
    if not base_id:
        raise PipelineError("Asset dictionary is missing 'assetBaseId' / 'id'.", stage="cache")

    name = str(
        merged.get("displayName")
        or merged.get("name")
        or base_id
    ).strip()
    asset_type = str(
        merged.get("assetType")
        or merged.get("asset_type")
        or merged.get("type")
        or "model"
    ).strip().lower()
    if asset_type not in VALID_ASSET_TYPES:
        asset_type = "model"

    author = str(merged.get("author") or "")
    if not author and isinstance(raw, dict):
        author_dict = raw.get("author")
        if isinstance(author_dict, dict):
            author = str(
                author_dict.get("fullName")
                or f"{author_dict.get('firstName', '')} {author_dict.get('lastName', '')}".strip()
            )

    raw_lic = str(merged.get("license") or "royalty_free").lower()
    license_str = "cc_zero" if ("cc0" in raw_lic or "cc_zero" in raw_lic or "public" in raw_lic) else "royalty_free"
    source_url = str(
        merged.get("sourceUrl")
        or merged.get("source_url")
        or (f"https://www.blenderkit.com/asset-gallery-detail/{base_id}/" if base_id else "")
    )

    asset_meta = {
        "id": asset_id,
        "base_id": base_id,
        "name": name,
        "type": asset_type,
        "resolution": norm_res,
        "author": author,
        "license": license_str,
        "source_url": source_url,
    }
    return asset_meta, merged


def _load_cached_package(pkg_dir: str) -> Optional[Package]:
    manifest_path = os.path.join(pkg_dir, "kitbridge.json")
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        log.debug("Failed to read cached manifest %s: %s", manifest_path, exc)
        return None

    errors = validate_manifest(manifest, package_dir=pkg_dir)
    if errors:
        log.debug("Cached manifest %s failed validation: %s", manifest_path, "; ".join(errors))
        return None

    return Package(path=pkg_dir, manifest=manifest, is_cached=True)


def _check_download_permissions(merged_asset: Dict[str, Any], api_key: str) -> None:
    is_free = bool(merged_asset.get("isFree", True))
    can_download = merged_asset.get("canDownload", True)
    can_download_err = merged_asset.get("canDownloadError")

    if not is_free and not (api_key or "").strip():
        raise PipelineError(
            f"Asset {merged_asset.get('name') or merged_asset.get('assetBaseId')!r} is a paid asset. "
            "Please log in to a BlenderKit account with an active Full Plan subscription.",
            stage="download",
        )
    if can_download is False:
        detail = f" ({can_download_err})" if can_download_err is not None else ""
        if not (api_key or "").strip():
            raise PipelineError(
                f"Cannot download paid asset{detail}: not logged in.",
                stage="download",
            )
        raise PipelineError(
            f"Cannot download asset{detail}: active BlenderKit subscription required.",
            stage="download",
        )


def _format_download_error(raw_message: str) -> str:
    lower = raw_message.lower()
    if "401" in lower or "unauthorized" in lower or "invalid token" in lower:
        return f"Authentication failed (not logged in or token expired): {raw_message}"
    if "402" in lower or "403" in lower or "forbidden" in lower or "subscription" in lower:
        return f"BlenderKit subscription required to download this asset: {raw_message}"
    return f"Asset download failed: {raw_message}"


def _collect_downloaded_files(
    source_dir: str,
    reported_paths: List[str],
) -> List[str]:
    """Flatten files downloaded by ``bk_client`` into ``source_dir`` root."""
    os.makedirs(source_dir, exist_ok=True)
    candidates: List[str] = []
    for rp in reported_paths:
        if isinstance(rp, str) and os.path.isfile(rp):
            candidates.append(os.path.abspath(rp))

    if not candidates:
        for root, _dirs, files in os.walk(source_dir):
            for fname in files:
                if fname.endswith(".part") or fname.endswith(".tmp"):
                    continue
                candidates.append(os.path.abspath(os.path.join(root, fname)))

    final_files: List[str] = []
    subdirs_to_clean: List[str] = []
    norm_source = os.path.abspath(source_dir)

    for src_file in candidates:
        parent = os.path.dirname(src_file)
        dst_file = os.path.join(norm_source, os.path.basename(src_file))
        if os.path.normcase(parent) != os.path.normcase(norm_source):
            try:
                if os.path.exists(dst_file):
                    os.remove(dst_file)
                shutil.move(src_file, dst_file)
                if parent not in subdirs_to_clean:
                    subdirs_to_clean.append(parent)
                final_files.append(dst_file)
            except OSError:
                final_files.append(src_file)
        else:
            final_files.append(src_file)

    for subdir in subdirs_to_clean:
        try:
            if (
                os.path.isdir(subdir)
                and os.path.normcase(subdir) != os.path.normcase(norm_source)
                and not os.listdir(subdir)
            ):
                os.rmdir(subdir)
        except OSError:
            pass

    return final_files


def _download_via_client(
    client: BKClient,
    merged_asset: Dict[str, Any],
    bk_res: str,
    source_dir: str,
    api_key: str,
    *,
    on_progress: Optional[ProgressCallback] = None,
    poller: Optional[Poller] = None,
    dispatcher: Optional[Dispatcher] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
) -> List[str]:
    """Schedule ``blender/asset_download`` and wait for completion via ``/report``."""
    _check_download_permissions(merged_asset, api_key)
    os.makedirs(source_dir, exist_ok=True)
    client.ensure_running()

    _emit_progress(on_progress, "download", 0.0, "Starting download...")
    try:
        task_id = client.asset_download(
            asset_data=merged_asset,
            resolution=bk_res,
            download_dirs=[source_dir],
            api_key=api_key,
            unpack_files=False,
        )
    except Exception as exc:
        raise PipelineError(f"Failed to start asset download: {exc}", stage="download") from exc

    done_event = threading.Event()
    state: Dict[str, Any] = {
        "status": "",
        "message": "",
        "file_paths": [],
        "best_progress": -1.0,
        "last_progress_at": time.monotonic(),
    }

    def _handle_task(task: Dict[str, Any]) -> None:
        if str(task.get("task_id") or "") != task_id:
            return
        status = str(task.get("status") or "")
        msg = str(task.get("message") or "")
        raw_prog = task.get("progress")
        if _is_number(raw_prog):
            prog_f = float(raw_prog)
            frac = prog_f / 100.0 if prog_f > 1.0 else prog_f
            if prog_f > state["best_progress"]:  # raw value: 1 means 1 %, not the 1.0 it maps to below
                state["best_progress"] = prog_f
                state["last_progress_at"] = time.monotonic()
            _emit_progress(on_progress, "download", frac, msg or "Downloading...")

        if status == "finished":
            result = task.get("result")
            fps: List[str] = []
            if isinstance(result, dict):
                raw_fps = result.get("file_paths") or []
                if isinstance(raw_fps, list):
                    fps = [str(p) for p in raw_fps if p]
                elif isinstance(result.get("file_path"), str):
                    fps = [result["file_path"]]
            state["status"] = "finished"
            state["file_paths"] = fps
            state["message"] = msg
            done_event.set()
        elif status in ("error", "cancelled"):
            state["status"] = status
            state["message"] = msg or f"Download {status}"
            done_event.set()

    if dispatcher is not None:
        dispatcher.register_task(task_id, _handle_task)

    stall_limit = max(1.0, float(timeout))
    try:
        while not done_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                try:
                    client.cancel_download(task_id)
                except Exception as exc:
                    log.debug("cancel_download(%s) raised: %s", task_id, exc)
                raise PipelineError("Download cancelled.", stage="download")

            if time.monotonic() - state["last_progress_at"] >= stall_limit:
                try:
                    client.cancel_download(task_id)
                except Exception as exc:
                    log.debug("cancel_download(%s) raised: %s", task_id, exc)
                raise PipelineError(
                    f"Asset download stalled: no progress for {timeout:.0f}s.",
                    stage="download",
                )

            if dispatcher is not None:
                if poller is not None:
                    if not poller.is_running:
                        poller.poll_once()
                    drained = poller.drain()
                    if drained:
                        dispatcher.dispatch(drained)
                else:
                    tasks = client.report(api_key=api_key)
                    if tasks:
                        dispatcher.dispatch(tasks)
            else:
                if poller is not None:
                    if not poller.is_running:
                        poller.poll_once()
                    tasks = poller.drain()
                else:
                    tasks = client.report(api_key=api_key)
                for t in tasks:
                    _handle_task(t)

            if done_event.wait(timeout=0.15):
                break
    finally:
        if dispatcher is not None:
            dispatcher.unregister_task(task_id)

    if state["status"] == "cancelled":
        raise PipelineError("Download cancelled.", stage="download")
    if state["status"] == "error":
        raise PipelineError(_format_download_error(state["message"]), stage="download")

    files = _collect_downloaded_files(source_dir, state["file_paths"])
    if not files:
        raise PipelineError(
            f"Download finished for task {task_id}, but no files were found in {source_dir}.",
            stage="download",
        )
    _emit_progress(on_progress, "download", 1.0, "Download complete")
    return files


# ── HDR Unpack & Packaging ───────────────────────────────────────────────────


def _is_blend_file(filepath: str) -> bool:
    if filepath.lower().endswith(".blend"):
        return True
    try:
        with open(filepath, "rb") as fh:
            header = fh.read(7)
        return header == b"BLENDER"
    except OSError:
        return False


def _unpack_hdr_from_blend(
    blend_file: str,
    textures_dir: str,
    blender_path: str,
    client: Optional[BKClient],
    timeout: float,
) -> str:
    found = blender.find_blender_with_version(user_pref_path=blender_path, client=client)
    if found is None:
        raise PipelineError(
            "Blender >= 4.2 is required to unpack .blend HDR assets, but was not found.",
            stage="convert",
        )
    exe_path = found[0]
    os.makedirs(textures_dir, exist_ok=True)
    fd, script_path = tempfile.mkstemp(prefix="kb_hdr_unpack_", suffix=".py", dir=textures_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_HDR_UNPACK_SCRIPT)
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        proc = subprocess.run(
            [
                exe_path,
                "-b",
                "--factory-startup",
                os.path.abspath(blend_file),
                "--python",
                os.path.abspath(script_path),
                "--",
                os.path.abspath(textures_dir),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creation_flags,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(
            f"HDR unpack timed out after {timeout:.0f}s.",
            stage="convert",
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineError(f"Failed to run Blender for HDR unpack: {exc}", stage="convert") from exc
    finally:
        try:
            if os.path.exists(script_path):
                os.remove(script_path)
        except OSError:
            pass

    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    for line in combined.splitlines():
        parsed = parse_kb_line(line)
        if parsed is not None and parsed.kind == "done" and parsed.path:
            candidate = os.path.join(textures_dir, parsed.path)
            if os.path.isfile(candidate):
                return parsed.path
        if parsed is not None and parsed.kind == "error":
            raise PipelineError(f"HDR unpack failed: {parsed.message}", stage="convert")

    raise PipelineError("HDR unpack failed to produce an environment image.", stage="convert")


def _build_hdr_package(
    pkg_dir: str,
    downloaded_files: List[str],
    asset_meta: Dict[str, Any],
    *,
    blender_path: str = "",
    client: Optional[BKClient] = None,
    on_progress: Optional[ProgressCallback] = None,
    timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> Package:
    _emit_progress(on_progress, "convert", 0.2, "Preparing HDR environment texture...")
    textures_dir = os.path.join(pkg_dir, "textures")
    os.makedirs(textures_dir, exist_ok=True)

    primary = downloaded_files[0]
    for f in downloaded_files:
        if f.lower().endswith((".exr", ".hdr", ".blend")):
            primary = f
            break

    if _is_blend_file(primary):
        hdr_filename = _unpack_hdr_from_blend(
            primary,
            textures_dir,
            blender_path=blender_path,
            client=client,
            timeout=timeout,
        )
    else:
        hdr_filename = os.path.basename(primary)
        dst_path = os.path.join(textures_dir, hdr_filename)
        if os.path.abspath(primary) != os.path.abspath(dst_path):
            shutil.copy2(primary, dst_path)

    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "asset": asset_meta,
        "units": {"meters_per_unit": 1.0, "up_axis": "Y"},
        "geometry": None,
        "hdr": f"textures/{hdr_filename}",
        "objects": [],
        "materials": [],
    }

    errors = validate_manifest(manifest, package_dir=pkg_dir)
    if errors:
        raise PipelineError(
            f"Generated HDR manifest failed validation: {'; '.join(errors)}",
            stage="convert",
        )

    manifest_path = os.path.join(pkg_dir, "kitbridge.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    _emit_progress(on_progress, "done", 1.0, "HDR package ready")
    return Package(path=pkg_dir, manifest=manifest, is_cached=False)


# ── Model & Material Conversion ──────────────────────────────────────────────


def _write_convert_log(pkg_dir: str, log_tail: List[str]) -> str:
    os.makedirs(pkg_dir, exist_ok=True)
    log_path = os.path.join(pkg_dir, "convert.log")
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(log_tail) + ("\n" if log_tail else ""))
    except OSError as exc:
        log.warning("Could not write convert.log at %s: %s", log_path, exc)
    return log_path


def _find_source_blend(downloaded_files: List[str], source_dir: str) -> str:
    for path in downloaded_files:
        if path.lower().endswith(".blend") and os.path.isfile(path):
            return path
    for root, _dirs, files in os.walk(source_dir):
        for fname in files:
            if fname.lower().endswith(".blend"):
                return os.path.join(root, fname)
    for path in downloaded_files:
        if os.path.isfile(path) and _is_blend_file(path):
            return path
    raise PipelineError(
        f"No .blend file found in downloaded source directory ({source_dir}).",
        stage="convert",
    )


def _convert_model_or_material(
    pkg_dir: str,
    downloaded_files: List[str],
    asset_meta: Dict[str, Any],
    norm_res: str,
    *,
    blender_path: str = "",
    client: Optional[BKClient] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> Package:
    source_dir = os.path.join(pkg_dir, "source")
    blend_file = _find_source_blend(downloaded_files, source_dir)

    found = blender.find_blender_with_version(user_pref_path=blender_path, client=client)
    if found is None:
        raise PipelineError(
            "Blender >= 4.2 not found. Please install Blender 4.2+ (or Steam Blender) "
            "or set BLENDER_PATH.",
            stage="convert",
        )
    exe_path, ver_tuple = found
    log.info(
        "Converting %s (%s) with Blender %s at %s",
        asset_meta["name"],
        asset_meta["type"],
        blender.format_version(ver_tuple),
        exe_path,
    )

    res_px = resolution_to_int(norm_res, default=2048)
    params: Dict[str, Any] = {
        "output_dir": pkg_dir,
        "asset_type": asset_meta["type"],
        "bake_resolution": res_px,
        "max_texture_size": res_px,
        "asset": asset_meta,
        "asset_meta": asset_meta,
    }

    export_res: KBExportResult = blender.run_kb_export(
        blender_exe=exe_path,
        blend_path=blend_file,
        params=params,
        on_progress=lambda frac, msg: _emit_progress(on_progress, "convert", frac, msg),
        cancel_event=cancel_event,
        timeout=timeout,
    )

    manifest_path = os.path.join(pkg_dir, "kitbridge.json")
    if not export_res.ok or not os.path.isfile(manifest_path):
        convert_log = _write_convert_log(pkg_dir, export_res.log_tail)
        tail_preview = "\n".join(export_res.log_tail[-10:])
        reason = export_res.error_message or f"Blender exited with code {export_res.returncode}"
        msg = f"Blender conversion failed: {reason} (log saved to {convert_log})"
        if tail_preview:
            msg = f"{msg}\n--- Last log lines ---\n{tail_preview}"
        raise PipelineError(
            msg,
            stage="convert",
            log_path=convert_log,
            log_tail=export_res.log_tail,
        )

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        convert_log = _write_convert_log(pkg_dir, export_res.log_tail)
        raise PipelineError(
            f"Failed to read generated kitbridge.json: {exc}",
            stage="convert",
            log_path=convert_log,
            log_tail=export_res.log_tail,
        ) from exc

    errors = validate_manifest(manifest, package_dir=pkg_dir)
    if errors:
        convert_log = _write_convert_log(pkg_dir, export_res.log_tail)
        raise PipelineError(
            f"Generated kitbridge.json failed schema validation: {'; '.join(errors)}",
            stage="convert",
            log_path=convert_log,
            log_tail=export_res.log_tail,
        )

    _emit_progress(on_progress, "done", 1.0, "Package ready")
    return Package(path=pkg_dir, manifest=manifest, is_cached=False)


# ── Public Pipeline Entrypoints ──────────────────────────────────────────────


def fetch_asset(
    client: BKClient,
    asset: Dict[str, Any],
    resolution: Union[str, int] = "2048",
    api_key: str = "",
    on_progress: Optional[ProgressCallback] = None,
    *,
    poller: Optional[Poller] = None,
    dispatcher: Optional[Dispatcher] = None,
    cancel_event: Optional[threading.Event] = None,
    blender_path: str = "",
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    convert_timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> Package:
    """Download and convert *asset* at *resolution*, or return a valid cached :class:`Package`.

    Progress callbacks receive ``(stage, fraction, message)`` where ``stage`` is
    ``'cache'``, ``'download'``, ``'convert'``, or ``'done'``.
    """
    norm_res = normalise_resolution(resolution)
    bk_res = to_bk_resolution(norm_res)
    asset_meta, merged_asset = _extract_asset_info(asset, norm_res)
    base_id = asset_meta["base_id"]

    pkg_dir = paths.find_asset_cache_dir(base_id, norm_res) or paths.asset_cache_dir(
        base_id, norm_res, asset_type=str(asset_meta.get("type") or ""), create=False
    )
    cached = _load_cached_package(pkg_dir)
    if cached is not None:
        log.info("Cache hit for %s (%s) at %s", asset_meta["name"], norm_res, pkg_dir)
        if needs_reconvert(pkg_dir):
            return reconvert(
                pkg_dir,
                client=client,
                on_progress=on_progress,
                cancel_event=cancel_event,
                blender_path=blender_path,
                timeout=convert_timeout,
            )
        _emit_progress(on_progress, "cache", 1.0, "Loaded from cache")
        return cached

    if cancel_event is not None and cancel_event.is_set():
        raise PipelineError("Operation cancelled.", stage="cache")

    os.makedirs(pkg_dir, exist_ok=True)
    source_dir = os.path.join(pkg_dir, "source")
    textures_dir = os.path.join(pkg_dir, "textures")
    os.makedirs(source_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)

    try:
        downloaded_files = _download_via_client(
            client=client,
            merged_asset=merged_asset,
            bk_res=bk_res,
            source_dir=source_dir,
            api_key=api_key,
            on_progress=on_progress,
            poller=poller,
            dispatcher=dispatcher,
            cancel_event=cancel_event,
            timeout=download_timeout,
        )

        if asset_meta["type"] == "hdr":
            return _build_hdr_package(
                pkg_dir=pkg_dir,
                downloaded_files=downloaded_files,
                asset_meta=asset_meta,
                blender_path=blender_path,
                client=client,
                on_progress=on_progress,
                timeout=convert_timeout,
            )

        return _convert_model_or_material(
            pkg_dir=pkg_dir,
            downloaded_files=downloaded_files,
            asset_meta=asset_meta,
            norm_res=norm_res,
            blender_path=blender_path,
            client=client,
            on_progress=on_progress,
            cancel_event=cancel_event,
            timeout=convert_timeout,
        )
    except PipelineError as exc:
        if cancel_event is None or not cancel_event.is_set():
            raise
        # Nothing here is worth keeping, and the client may still hold a partial file: best effort.
        shutil.rmtree(pkg_dir, ignore_errors=True)
        raise PipelineError("Cancelled.", stage=exc.stage) from exc


def fetch_asset_async(
    client: BKClient,
    asset: Dict[str, Any],
    resolution: Union[str, int] = "2048",
    api_key: str = "",
    on_progress: Optional[ProgressCallback] = None,
    *,
    poller: Optional[Poller] = None,
    dispatcher: Optional[Dispatcher] = None,
    cancel_event: Optional[threading.Event] = None,
    blender_path: str = "",
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    convert_timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> PipelineJob:
    """Run :func:`fetch_asset` on a daemon worker thread and return a :class:`PipelineJob`."""
    return PipelineJob(
        target=lambda ev: fetch_asset(
            client=client,
            asset=asset,
            resolution=resolution,
            api_key=api_key,
            on_progress=on_progress,
            poller=poller,
            dispatcher=dispatcher,
            cancel_event=ev,
            blender_path=blender_path,
            download_timeout=download_timeout,
            convert_timeout=convert_timeout,
        ),
        cancel_event=cancel_event,
    )


def needs_reconvert(package_dir: str) -> bool:
    """True for a package from an older exporter whose ``.blend`` is still on disk."""
    pkg = _load_cached_package(package_dir)
    if pkg is None or pkg.manifest.get("exporter_version", 1) >= EXPORTER_VERSION:
        return False
    if (pkg.manifest.get("asset") or {}).get("type") not in ("model", "material"):
        return False
    source_dir = os.path.join(package_dir, "source")
    return any(name.lower().endswith(".blend") for _r, _d, names in os.walk(source_dir) for name in names)


def reconvert(
    package_dir: str,
    *,
    client: Optional[BKClient] = None,
    on_progress: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    blender_path: str = "",
    timeout: float = DEFAULT_CONVERT_TIMEOUT_SECONDS,
) -> Package:
    """Convert a package's ``.blend`` again. On failure the old package stays and is returned."""
    old = _load_cached_package(package_dir)
    if old is None:
        raise PipelineError(f"No valid package at {package_dir}.", stage="convert")
    asset_meta = dict(old.manifest["asset"])
    work_dir = os.path.join(package_dir, ".reconvert")
    shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(os.path.join(work_dir, "textures"))
    try:
        source_dir = os.path.join(package_dir, "source")
        blend = _find_source_blend([], source_dir)
        new = _convert_model_or_material(
            pkg_dir=work_dir,
            downloaded_files=[blend],
            asset_meta=asset_meta,
            norm_res=str(asset_meta.get("resolution") or ""),
            blender_path=blender_path,
            client=client,
            on_progress=on_progress,
            cancel_event=cancel_event,
            timeout=timeout,
        )
        # The manifest goes last and its paths are relative, so a failed swap keeps a usable package.
        for name in sorted(os.listdir(work_dir), key=lambda n: n == "kitbridge.json"):
            target = os.path.join(package_dir, name)
            if os.path.isdir(target):
                shutil.rmtree(target)
            os.replace(os.path.join(work_dir, name), target)
        return Package(path=package_dir, manifest=new.manifest, is_cached=False)
    except (PipelineError, OSError) as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise PipelineError("Cancelled.", stage="convert") from exc
        log.warning("Reconverting %s failed, using the old package: %s", package_dir, exc)
        _emit_progress(on_progress, "cache", 1.0, "Loaded from cache")
        return old
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def reconvert_async(
    package_dir: str,
    *,
    client: Optional[BKClient] = None,
    on_progress: Optional[ProgressCallback] = None,
    blender_path: str = "",
) -> PipelineJob:
    """Run :func:`reconvert` on a daemon worker thread and return a :class:`PipelineJob`."""
    return PipelineJob(
        target=lambda ev: reconvert(
            package_dir, client=client, on_progress=on_progress, cancel_event=ev, blender_path=blender_path
        )
    )


def geometry_size_mb(package_dir: str) -> int:
    """Size of the package's FBX, the best hint of how long Cinema 4D will take to load it."""
    pkg = _load_cached_package(package_dir)
    rel = str(pkg.manifest.get("geometry") or "") if pkg is not None else ""
    if not rel:
        return 0
    try:
        return os.path.getsize(_resolve_pkg_file(package_dir, rel)) // (1024 * 1024)
    except OSError:
        return 0


# ── Cache Maintenance Helpers ────────────────────────────────────────────────


def list_cached() -> List[Package]:
    """Return all valid cached packages under the current download folder."""
    packages: List[Package] = []
    for pkg_dir in paths.package_dirs():
        pkg = _load_cached_package(pkg_dir)
        if pkg is not None:
            packages.append(pkg)
    return packages


def _dir_size_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for fname in files:
            fp = os.path.join(root, fname)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def cache_size() -> int:
    """Return the disk usage in bytes of Kitbridge's packages in the download folder."""
    return sum(_dir_size_bytes(path) for path in paths.package_dirs())


def cache_summary() -> Dict[str, Any]:
    """Size, package count and per-subfolder counts for the settings screen."""
    dirs = paths.package_dirs()
    folders: Dict[str, int] = {}
    for path in dirs:
        name = os.path.basename(os.path.dirname(path))
        folders[name] = folders.get(name, 0) + 1
    return {"size": sum(_dir_size_bytes(p) for p in dirs), "count": len(dirs), "folders": folders}


def remove(
    base_id: Optional[str] = None,
    resolution: Optional[str] = None,
) -> int:
    """Remove cached package(s) matching *base_id* (and optional *resolution*).

    If *base_id* is ``None`` or ``'*'``, clears all cached packages.
    Returns the number of package directories removed.
    """
    removed = 0
    target_id = (base_id or "").strip()
    norm_res = normalise_resolution(resolution) if resolution is not None else None

    # Only package folders: the download folder is the user's and may hold other files.
    for full_path in paths.package_dirs():
        entry = os.path.basename(full_path)
        match = False
        if not target_id or target_id == "*":
            match = True
        elif norm_res is not None:
            expected = f"{target_id}_{norm_res}"
            if entry == expected:
                match = True
        else:
            if entry == target_id or entry.startswith(f"{target_id}_"):
                match = True

        if match:
            try:
                shutil.rmtree(full_path)
                removed += 1
            except OSError as exc:
                log.warning("Failed to remove cached package %s: %s", full_path, exc)

    return removed


def clear_cache() -> int:
    """Remove all Kitbridge packages in the current download folder."""
    return remove(base_id=None)
