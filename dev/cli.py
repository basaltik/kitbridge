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

"""Manual test CLI for Kitbridge core & pipeline (runs under plain CPython without Cinema 4D)."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PKG_ROOT = os.path.join(REPO_ROOT, "plugin", "kitbridge")
if PLUGIN_PKG_ROOT not in sys.path:
    sys.path.insert(0, PLUGIN_PKG_ROOT)

from kitbridge.core import auth, blender, paths, pipeline, search  # noqa: E402
from kitbridge.core.client import BKClient  # noqa: E402
from kitbridge.core.tasks import Dispatcher, Poller, TaskDict  # noqa: E402

log = logging.getLogger("kitbridge")

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class _CoreSession:
    """Context manager running BKClient + Poller + Dispatcher just as the C4D plugin will."""

    def __init__(self, software_version: str = "cli") -> None:
        self.client = BKClient(software_version=software_version)
        self.dispatcher = Dispatcher()
        self.poller = Poller(
            self.client,
            interval=0.2,
            api_key_getter=lambda: auth.get_api_key(self.client),
        )
        auth.register_auth_handlers(self.dispatcher, client=self.client)

    def __enter__(self) -> _CoreSession:
        self.client.ensure_running()
        self.poller.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.poller.stop()
        try:
            self.client.unsubscribe_addon()
        except Exception as exc:
            log.debug("unsubscribe_addon on CLI exit failed: %s", exc)

    def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = 30.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = self.poller.drain()
            if tasks:
                self.dispatcher.dispatch(tasks)
            if predicate():
                return True
            time.sleep(0.05)
        tasks = self.poller.drain()
        if tasks:
            self.dispatcher.dispatch(tasks)
        return predicate()


def _format_bytes(num_bytes: int) -> str:
    val = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024.0 or unit == "GB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} B"
        val /= 1024.0
    return f"{val:.1f} GB"


def cmd_find_blender(args: argparse.Namespace) -> int:
    user_pref = getattr(args, "path", "") or ""
    with _CoreSession() as session:
        found = blender.find_blender_with_version(
            user_pref_path=user_pref,
            client=session.client,
            publish=True,
        )
    if found is None:
        print(
            f"Blender (>={blender.format_version(blender.MIN_BLENDER_VERSION)}) not found.",
            file=sys.stderr,
        )
        return 1

    exe_path, ver = found
    print(f"Blender {blender.format_version(ver)}: {exe_path}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    manual_key = getattr(args, "api_key", "") or ""
    if manual_key.strip():
        auth.set_manual_api_key(manual_key)
        print("Saved manual API key to secret store.")
        return 0

    done_event = threading.Event()
    error_holder: List[str] = []

    with _CoreSession() as session:

        def _on_login(task: TaskDict) -> None:
            status = str(task.get("status") or "")
            if status == "finished":
                done_event.set()
            elif status == "error":
                error_holder.append(str(task.get("message") or "OAuth login failed"))
                done_event.set()

        session.dispatcher.register("login", _on_login)
        auth_url = auth.start_login(session.client, open_browser=True)
        print("Opened browser for BlenderKit OAuth2 login.")
        print(f"If the browser did not open, visit:\n  {auth_url}")
        print(f"Waiting up to {int(args.timeout)}s for authorization...")

        finished = session.wait_until(done_event.is_set, timeout=float(args.timeout))
        if not finished:
            print("Timed out waiting for browser login.", file=sys.stderr)
            return 1
        if error_holder:
            print(f"Login failed: {error_holder[0]}", file=sys.stderr)
            return 1
        if not auth.is_logged_in():
            print("Login finished but no access token was saved.", file=sys.stderr)
            return 1

        print("Successfully logged in and stored token in secret store.")
        return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    if not auth.is_logged_in():
        print("Not logged in. Run 'py -3 dev/cli.py login' first.")
        return 1

    done_event = threading.Event()
    error_holder: List[str] = []

    with _CoreSession() as session:

        def _on_profile(task: TaskDict) -> None:
            status = str(task.get("status") or "")
            if status == "finished":
                done_event.set()
            elif status == "error":
                error_holder.append(str(task.get("message") or "Profile request failed"))
                done_event.set()

        session.dispatcher.register("profiles/get_user_profile", _on_profile)
        if not auth.request_user_profile(session.client):
            print("No API key available.", file=sys.stderr)
            return 1

        finished = session.wait_until(done_event.is_set, timeout=float(args.timeout))
        if not finished:
            print("Timed out waiting for user profile.", file=sys.stderr)
            return 1
        if error_holder:
            print(f"Failed to fetch user profile: {error_holder[0]}", file=sys.stderr)
            return 1

        profile = auth.get_cached_profile() or {}
        first = str(profile.get("firstName") or "")
        last = str(profile.get("lastName") or "")
        full_name = f"{first} {last}".strip() or str(profile.get("username") or "Unknown")
        email = str(profile.get("email") or "")
        user_id = profile.get("id", "")
        print(f"User: {full_name}")
        if email:
            print(f"Email: {email}")
        if user_id != "":
            print(f"ID: {user_id}")
        return 0


def _run_search(
    session: _CoreSession,
    *,
    query: str,
    asset_type: str,
    category: str = "",
    free_first: bool = False,
    bookmarks: bool = False,
    page_size: int = 15,
    page: int = 1,
    timeout: float = 30.0,
) -> List[Dict[str, Any]]:
    done_event = threading.Event()
    error_holder: List[str] = []
    results_holder: List[Dict[str, Any]] = []
    tempdir = paths.search_temp_dir(asset_type, create=True)
    api_key = auth.get_api_key(session.client)

    task_id = search.start_search(
        session.client,
        query=query,
        asset_type=asset_type,
        category=category,
        free_first=free_first,
        bookmarks=bookmarks,
        page_size=max(10, int(page_size)),
        page=int(page),
        api_key=api_key,
        tempdir=tempdir,
    )

    def _on_search_task(task: TaskDict) -> None:
        status = str(task.get("status") or "")
        if status == "finished":
            raw_res = task.get("result")
            if isinstance(raw_res, (dict, list)):
                results_holder.extend(search.normalise_results(raw_res, tempdir=tempdir))
            done_event.set()
        elif status == "error":
            error_holder.append(str(task.get("message") or "Search failed"))
            done_event.set()

    session.dispatcher.register_task(task_id, _on_search_task)
    finished = session.wait_until(done_event.is_set, timeout=float(timeout))
    if not finished:
        raise RuntimeError("Timed out waiting for search results.")
    if error_holder:
        raise RuntimeError(f"Search error: {error_holder[0]}")
    return results_holder


def cmd_search(args: argparse.Namespace) -> int:
    try:
        with _CoreSession() as session:
            results_holder = _run_search(
                session,
                query=args.query,
                asset_type=args.type,
                category=args.category,
                free_first=args.free_first,
                bookmarks=args.bookmarks,
                page_size=max(10, int(args.page_size)),
                page=int(args.page),
                timeout=float(args.timeout),
            )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    top_items = results_holder[:10]
    if not top_items:
        print("No assets found.")
        return 0

    for idx, item in enumerate(top_items, start=1):
        tier = "free" if item.get("isFree") else "paid"
        res_list = item.get("availableResolutions") or []
        res_str = ", ".join(str(r) for r in res_list) if res_list else "none"
        print(f"{idx:2d}. {item.get('name')} [{tier}] (resolutions: {res_str})")
    return 0


def _print_manifest_summary(pkg: pipeline.Package) -> None:
    manifest = pkg.manifest
    asset = manifest.get("asset") or {}
    print("\n=== Manifest Summary ===")
    print(
        f"Asset      : {asset.get('name')} "
        f"(base_id={asset.get('base_id')}, id={asset.get('id')})"
    )
    print(
        f"Type/Res   : type={asset.get('type')}, resolution={asset.get('resolution')}, "
        f"license={asset.get('license')}, author={asset.get('author')!r}"
    )

    geom_rel = manifest.get("geometry")
    if geom_rel:
        geom_abs = os.path.join(pkg.path, geom_rel)
        geom_size = os.path.getsize(geom_abs) if os.path.isfile(geom_abs) else 0
        print(f"Geometry   : {geom_rel} ({_format_bytes(geom_size)})")
    else:
        print("Geometry   : None")

    hdr_rel = manifest.get("hdr")
    if hdr_rel:
        hdr_abs = os.path.join(pkg.path, *hdr_rel.split("/"))
        hdr_size = os.path.getsize(hdr_abs) if os.path.isfile(hdr_abs) else 0
        print(f"HDR        : {hdr_rel} ({_format_bytes(hdr_size)})")

    objects = manifest.get("objects") or []
    print(f"Objects ({len(objects)}):")
    for obj in objects:
        mats_str = ", ".join(obj.get("materials") or []) or "(none)"
        print(f"  - {obj.get('name')}: materials=[{mats_str}]")

    materials = manifest.get("materials") or []
    print(f"Materials ({len(materials)}):")
    for mat in materials:
        mname = mat.get("name")
        bmethod = mat.get("blend_method")
        uv = mat.get("uv") or {}
        baked_set = set(mat.get("baked_channels") or [])
        channels = mat.get("channels") or {}

        textured_list: List[str] = []
        baked_list: List[str] = []
        value_list: List[str] = []

        for ch_name, ch_data in channels.items():
            if not isinstance(ch_data, dict):
                continue
            if "texture" in ch_data:
                tex_rel = str(ch_data["texture"])
                tex_abs = os.path.join(pkg.path, *tex_rel.split("/"))
                tex_sz = _format_bytes(os.path.getsize(tex_abs)) if os.path.isfile(tex_abs) else "MISSING"
                cs = ch_data.get("colorspace", "")
                ch_comp = ch_data.get("channel", "")
                extra = f"{tex_rel} [{cs}/{ch_comp}, {tex_sz}]"
                if ch_name in baked_set:
                    baked_list.append(f"{ch_name} -> {extra}")
                else:
                    textured_list.append(f"{ch_name} -> {extra}")
            elif "value" in ch_data:
                val = ch_data["value"]
                if isinstance(val, list):
                    val_str = "[" + ", ".join(f"{float(x):.3f}" for x in val) + "]"
                else:
                    val_str = f"{float(val):.3f}"
                value_list.append(f"{ch_name}={val_str}")

        print(
            f"  * {mname} (blend={bmethod}, uv={uv.get('map')} "
            f"scale={uv.get('scale')} offset={uv.get('offset')} rot={uv.get('rotation')})"
        )
        print(f"    - Textured channels ({len(textured_list)}): {', '.join(textured_list) or 'none'}")
        print(f"    - Baked channels    ({len(baked_list)}): {', '.join(baked_list) or 'none'}")
        print(f"    - Value channels    ({len(value_list)}): {', '.join(value_list) or 'none'}")
        warnings = mat.get("warnings") or []
        if warnings:
            print(f"    - Warnings ({len(warnings)}):")
            for w in warnings:
                print(f"        ! {w}")


def cmd_get(args: argparse.Namespace) -> int:
    query_or_id = str(args.query).strip()
    asset_type = str(args.type).strip().lower()
    index = max(0, int(args.index))
    norm_res = pipeline.normalise_resolution(args.res)

    t_start = time.perf_counter()
    search_s = 0.0
    download_s = 0.0
    convert_s = 0.0

    # Fast cache check when a direct asset_base_id UUID is provided
    if _UUID_RE.match(query_or_id):
        pkg_dir = paths.find_asset_cache_dir(query_or_id, norm_res) or paths.asset_cache_dir(
            query_or_id, norm_res, create=False
        )
        cached_pkg = pipeline._load_cached_package(pkg_dir)
        if cached_pkg is not None:
            total_s = time.perf_counter() - t_start
            print(f"Package    : {cached_pkg.path}")
            print("Cache hit  : True (no network, no Blender)")
            print(
                f"Timings    : search=0.00s, download=0.00s, convert=0.00s, total={total_s:.3f}s"
            )
            _print_manifest_summary(cached_pkg)
            return 0

    with _CoreSession() as session:
        t_search0 = time.perf_counter()
        search_query = f"+asset_base_id:{query_or_id}" if _UUID_RE.match(query_or_id) else query_or_id
        try:
            results = _run_search(
                session,
                query=search_query,
                asset_type=asset_type,
                page_size=max(15, index + 5),
                page=1,
                timeout=float(args.timeout),
            )
            if not results and _UUID_RE.match(query_or_id):
                results = _run_search(
                    session,
                    query=query_or_id,
                    asset_type=asset_type,
                    page_size=max(15, index + 5),
                    page=1,
                    timeout=float(args.timeout),
                )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        search_s = time.perf_counter() - t_search0

        if not results:
            print(f"No {asset_type} assets found for {query_or_id!r}.", file=sys.stderr)
            return 1
        if index >= len(results):
            print(
                f"Requested --index {index}, but search returned only {len(results)} result(s).",
                file=sys.stderr,
            )
            return 1

        chosen = results[index]
        tier = "free" if chosen.get("isFree") else "paid"
        print(
            f"Selected [{index}]: {chosen.get('name')} [{tier}] "
            f"(base_id={chosen.get('assetBaseId')}, type={chosen.get('assetType')})"
        )

        stage_times: Dict[str, float] = {}
        last_stage: List[str] = [""]
        last_stage_start: List[float] = [time.perf_counter()]

        def _on_progress(stage: str, frac: float, msg: str) -> None:
            now = time.perf_counter()
            prev = last_stage[0]
            if prev and prev != stage:
                stage_times[prev] = stage_times.get(prev, 0.0) + (now - last_stage_start[0])
                last_stage_start[0] = now
            elif not prev:
                last_stage_start[0] = now
            last_stage[0] = stage
            pct = int(round(frac * 100))
            print(f"  [{stage:8s} {pct:3d}%] {msg}")

        api_key = auth.get_api_key(session.client)
        t_pipe0 = time.perf_counter()
        try:
            job = pipeline.fetch_asset_async(
                session.client,
                chosen,
                resolution=norm_res,
                api_key=api_key,
                on_progress=_on_progress,
                poller=session.poller,
                dispatcher=session.dispatcher,
                convert_timeout=float(args.convert_timeout),
            )
            pkg = job.result(timeout=float(args.convert_timeout) + 120.0)
        except pipeline.PipelineError as exc:
            print(f"Pipeline error ({exc.stage}): {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Unexpected pipeline failure: {exc}", file=sys.stderr)
            return 1

        now_end = time.perf_counter()
        if last_stage[0]:
            stage_times[last_stage[0]] = stage_times.get(last_stage[0], 0.0) + (
                now_end - last_stage_start[0]
            )
        pipe_s = now_end - t_pipe0
        download_s = stage_times.get("download", 0.0)
        convert_s = stage_times.get("convert", 0.0)
        total_s = now_end - t_start

    errors = pipeline.validate_manifest(pkg.manifest, package_dir=pkg.path)
    print(f"\nPackage    : {pkg.path}")
    print(f"Cache hit  : {pkg.is_cached}")
    print(f"Validation : {'VALID (schema_version=1)' if not errors else 'INVALID: ' + '; '.join(errors)}")
    print(
        f"Timings    : search={search_s:.2f}s, download={download_s:.2f}s, "
        f"convert={convert_s:.2f}s, pipeline={pipe_s:.3f}s, total={total_s:.2f}s"
    )
    _print_manifest_summary(pkg)
    return 0 if not errors else 1


def cmd_cache(args: argparse.Namespace) -> int:
    action = args.cache_action
    if action == "list":
        packages = pipeline.list_cached()
        if not packages:
            print("Cache is empty.")
            return 0
        print(f"Cached packages ({len(packages)}):")
        for idx, pkg in enumerate(packages, start=1):
            asset = pkg.manifest.get("asset") or {}
            sz = pipeline._dir_size_bytes(pkg.path)
            print(
                f"{idx:2d}. {asset.get('name')} "
                f"[type={asset.get('type')}, res={asset.get('resolution')}, "
                f"base_id={asset.get('base_id')}, size={_format_bytes(sz)}]\n"
                f"    path: {pkg.path}"
            )
        return 0

    if action == "size":
        total = pipeline.cache_size()
        pkgs = pipeline.list_cached()
        print(f"Cache size: {_format_bytes(total)} ({total} bytes) across {len(pkgs)} package(s)")
        return 0

    if action == "clear":
        target = getattr(args, "base_id", None)
        removed = pipeline.remove(base_id=target)
        print(f"Removed {removed} cached package(s).")
        return 0

    print(f"Unknown cache action: {action}", file=sys.stderr)
    return 1


def cmd_logout(_args: argparse.Namespace) -> int:
    with _CoreSession() as session:
        auth.logout(session.client)
        # Allow a brief moment for bk_client to receive the logout call before unsubscribing.
        session.wait_until(lambda: False, timeout=0.3)
    print("Logged out and cleared stored credentials.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Kitbridge core & pipeline developer CLI.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_blender = sub.add_parser("find-blender", help="Locate and probe Blender executable.")
    p_blender.add_argument("--path", default="", help="Optional user-preferred Blender path.")
    p_blender.set_defaults(func=cmd_find_blender)

    p_login = sub.add_parser("login", help="Log in to BlenderKit via browser PKCE or API key.")
    p_login.add_argument("--api-key", default="", help="Store a manual API key instead of OAuth.")
    p_login.add_argument("--timeout", type=float, default=120.0, help="Login wait timeout (s).")
    p_login.set_defaults(func=cmd_login)

    p_whoami = sub.add_parser("whoami", help="Fetch and print the current BlenderKit user profile.")
    p_whoami.add_argument("--timeout", type=float, default=20.0, help="Profile wait timeout (s).")
    p_whoami.set_defaults(func=cmd_whoami)

    p_search = sub.add_parser("search", help="Search BlenderKit assets and print top 10 results.")
    p_search.add_argument("query", nargs="?", default="", help="Search query keywords.")
    p_search.add_argument(
        "--type",
        choices=search.VALID_ASSET_TYPES,
        default="model",
        help="Asset type (model, material, hdr).",
    )
    p_search.add_argument("--category", default="", help="Category subtree slug.")
    p_search.add_argument("--free-first", action="store_true", help="Order free assets first.")
    p_search.add_argument("--bookmarks", action="store_true", help="Filter to bookmarked assets.")
    p_search.add_argument("--page-size", type=int, default=15, help="Results per page.")
    p_search.add_argument("--page", type=int, default=1, help="Page number (1-indexed).")
    p_search.add_argument("--timeout", type=float, default=30.0, help="Search wait timeout (s).")
    p_search.set_defaults(func=cmd_search)

    p_get = sub.add_parser(
        "get",
        help="Search, download, and convert an asset into a cached Kitbridge package.",
    )
    p_get.add_argument("query", help="Search query keywords or asset_base_id UUID.")
    p_get.add_argument(
        "--type",
        choices=search.VALID_ASSET_TYPES,
        default="model",
        help="Asset type (model, material, hdr).",
    )
    p_get.add_argument(
        "--index",
        type=int,
        default=0,
        help="0-based index of search result to fetch (default: 0).",
    )
    p_get.add_argument(
        "--res",
        default="2048",
        help="Target resolution (512, 1024, 2048, 4096, 8192, ORIGINAL). Default: 2048.",
    )
    p_get.add_argument("--timeout", type=float, default=30.0, help="Search wait timeout (s).")
    p_get.add_argument(
        "--convert-timeout",
        type=float,
        default=600.0,
        help="Blender conversion timeout (s).",
    )
    p_get.set_defaults(func=cmd_get)

    p_cache = sub.add_parser("cache", help="Inspect or clear the Kitbridge asset cache.")
    p_cache.add_argument(
        "cache_action",
        choices=("list", "size", "clear"),
        help="Cache action to perform.",
    )
    p_cache.add_argument(
        "--base-id",
        default=None,
        help="Optional asset_base_id to remove when running 'cache clear'.",
    )
    p_cache.set_defaults(func=cmd_cache)

    p_logout = sub.add_parser("logout", help="Revoke tokens and clear local credentials.")
    p_logout.set_defaults(func=cmd_logout)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(errors="replace")
            except Exception:
                pass
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="[%(name)s] %(levelname)s: %(message)s",
    )
    func: Callable[[argparse.Namespace], int] = args.func
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
