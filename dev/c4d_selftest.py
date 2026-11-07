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

"""In-Cinema 4D self-test harness for Milestone 2b (``c4d_import``).

Usage:
- Open or paste into Cinema 4D's **Script Manager** and click **Execute** (or run via ``c4dpy``).
- Optional environment variables:
  - ``KB_RENDERER``: ``"redshift"`` (default), ``"arnold"``, or ``"standard"``
  - ``KB_PACKAGES``: semicolon- or comma-separated list of package directories
    (defaults to all subdirectories in ``~/blenderkit_data/kitbridge_cache/``
    containing ``kitbridge.json``).
  - ``KB_PLUGIN_DIR``: override path to ``plugin/kitbridge``.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from typing import Any, Dict, List

DEFAULT_REPO_PLUGIN_DIR = r"E:\claude softiki\kitbridge\plugin\kitbridge"


def _ensure_sys_path() -> str:
    candidates: List[str] = []
    env_dir = os.environ.get("KB_PLUGIN_DIR", "").strip()
    if env_dir:
        candidates.append(env_dir)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates.append(os.path.normpath(os.path.join(here, "..", "plugin", "kitbridge")))
    except NameError:
        pass
    candidates.append(DEFAULT_REPO_PLUGIN_DIR)

    chosen = DEFAULT_REPO_PLUGIN_DIR
    for path in candidates:
        if path and os.path.isdir(path):
            chosen = path
            break

    if chosen not in sys.path:
        sys.path.insert(0, chosen)
    return chosen


def _reload_kitbridge_modules() -> None:
    """Reload ``kitbridge.*`` in dependency order so edits take effect without restarting C4D."""
    module_order = (
        "kitbridge",
        "kitbridge.c4d_import.compat",
        "kitbridge.c4d_import.renderer",
        "kitbridge.c4d_import.materials.base",
        "kitbridge.c4d_import.materials.redshift",
        "kitbridge.c4d_import.materials.arnold",
        "kitbridge.c4d_import.materials.standard",
        "kitbridge.c4d_import.materials",
        "kitbridge.c4d_import.geometry",
        "kitbridge.c4d_import.hdr",
        "kitbridge.c4d_import",
    )
    for mod_name in module_order:
        if mod_name in sys.modules:
            importlib.reload(sys.modules[mod_name])
        else:
            importlib.import_module(mod_name)


def _discover_packages() -> List[str]:
    env_pkgs = os.environ.get("KB_PACKAGES", "").strip()
    if env_pkgs:
        sep = ";" if ";" in env_pkgs else ","
        paths = [os.path.expanduser(p.strip()) for p in env_pkgs.split(sep) if p.strip()]
        return [p for p in paths if os.path.isfile(os.path.join(p, "kitbridge.json"))]

    cache_root = os.path.join(os.path.expanduser("~"), "blenderkit_data", "kitbridge_cache")
    if not os.path.isdir(cache_root):
        return []

    found: List[str] = []
    for parent in (cache_root, *(os.path.join(cache_root, n) for n in ("models", "materials", "hdris", "other"))):
        if not os.path.isdir(parent):
            continue
        for entry in sorted(os.listdir(parent)):
            pkg_dir = os.path.join(parent, entry)
            if os.path.isdir(pkg_dir) and os.path.isfile(os.path.join(pkg_dir, "kitbridge.json")):
                found.append(pkg_dir)
    return found


def _describe_object(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {"name": None, "type_id": None, "type_name": None, "tags": []}
    name = str(obj.GetName()) if hasattr(obj, "GetName") else repr(obj)
    type_id = int(obj.GetType()) if hasattr(obj, "GetType") else None
    type_name = str(obj.GetTypeName()) if hasattr(obj, "GetTypeName") else str(type_id)
    tags_info: List[Dict[str, Any]] = []
    if hasattr(obj, "GetTags"):
        for t in obj.GetTags():
            t_entry: Dict[str, Any] = {
                "type_id": int(t.GetType()) if hasattr(t, "GetType") else None,
                "name": str(t.GetName()) if hasattr(t, "GetName") else "",
            }
            if hasattr(t, "GetMaterial"):
                try:
                    m = t.GetMaterial()
                    if m is not None and hasattr(m, "GetName"):
                        t_entry["material"] = str(m.GetName())
                except Exception:
                    pass
            tags_info.append(t_entry)
    return {
        "name": name,
        "type_id": type_id,
        "type_name": type_name,
        "tags": tags_info,
    }


def main() -> List[Dict[str, Any]]:
    plugin_dir = _ensure_sys_path()
    _reload_kitbridge_modules()

    import c4d  # type: ignore[import-not-found]
    from kitbridge.c4d_import import compat, import_package
    from kitbridge.c4d_import.renderer import detect_renderer, set_document_renderer

    compat.clear_resolution_log()

    target_renderer = os.environ.get("KB_RENDERER", "redshift").strip().lower() or "redshift"

    # Create a fresh document and make it active
    doc = c4d.documents.BaseDocument()
    doc.SetDocumentName(f"Kitbridge_Selftest_{target_renderer}")
    c4d.documents.InsertBaseDocument(doc)
    c4d.documents.SetActiveDocument(doc)

    renderer_set_ok = set_document_renderer(doc, target_renderer)
    detected_after_set = detect_renderer(doc)

    packages = _discover_packages()
    reports: List[Dict[str, Any]] = []

    if not packages:
        empty_report = {
            "step": "discover_packages",
            "error": "No cached packages found in ~/blenderkit_data/kitbridge_cache/ or KB_PACKAGES.",
            "plugin_dir": plugin_dir,
            "c4d_version": compat.get_c4d_version(),
        }
        print("KB_SELFTEST " + json.dumps(empty_report, ensure_ascii=False))
        return [empty_report]

    prev_res_idx = 0
    for pkg_dir in packages:
        pkg_name = os.path.basename(os.path.normpath(pkg_dir))
        try:
            # Clear active object selection before each package so material assets
            # import their preview sphere instead of applying to the previous asset's Null.
            try:
                doc.SetActiveObject(None)
            except Exception:
                pass

            res = import_package(
                doc,
                pkg_dir,
                renderer=target_renderer,
                options={"apply_to_selection": False},
            )

            all_resolutions = compat.get_resolution_log()
            new_resolutions = all_resolutions[prev_res_idx:]
            prev_res_idx = len(all_resolutions)

            objs_summary = [_describe_object(o) for o in res.objects]
            mats_summary = [
                {
                    "name": info.name,
                    "manifest_name": info.manifest_name,
                    "renderer": info.renderer,
                    "node_count": info.node_count,
                    "connected_channels": info.connected_channels,
                    "value_channels": info.value_channels,
                    "reused": info.reused,
                }
                for info in res.material_details
            ]

            pkg_report: Dict[str, Any] = {
                "package_dir": pkg_dir,
                "asset_name": res.asset_name,
                "asset_type": res.asset_type,
                "renderer": res.renderer,
                "renderer_set_ok": renderer_set_ok,
                "detected_renderer": detected_after_set,
                "c4d_version": compat.get_c4d_version(),
                "root_object": res.root_object.GetName() if res.root_object is not None else None,
                "objects_created_count": len(objs_summary),
                "objects_created": objs_summary,
                "materials_created_count": len(mats_summary),
                "materials_created": mats_summary,
                "warnings": res.warnings,
                "errors": res.errors,
                "runtime_resolutions": new_resolutions,
            }
        except Exception as exc:
            pkg_report = {
                "package_dir": pkg_dir,
                "package_name": pkg_name,
                "renderer": target_renderer,
                "c4d_version": compat.get_c4d_version(),
                "step": f"selftest_loop:{pkg_name}",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }

        reports.append(pkg_report)
        print("KB_SELFTEST " + json.dumps(pkg_report, ensure_ascii=False))

    c4d.EventAdd()
    return reports


if __name__ == "__main__":
    main()
