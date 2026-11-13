"""Kitbridge controller shared by the window and the background timer.

Threading model: task handlers may run on any thread (the pipeline worker drains the shared
poller too), so they only push events into a queue. ``tick()`` runs on the C4D main thread,
applies those events to plain state, and performs scene imports. The dialog only reads state
and redraws when ``version`` changes.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from kitbridge.core import auth, blender, paths, pipeline, search
from kitbridge.core.client import BKClient
from kitbridge.core.tasks import Dispatcher, Poller, TaskDict
from kitbridge.ui import layout, prefs as prefs_mod

log = logging.getLogger("kitbridge")

MAX_CONCURRENT_JOBS = 2


class JobState:
    """Progress of one download/convert/import, keyed by asset base id."""

    def __init__(self, asset: Dict[str, Any], job: pipeline.PipelineJob, source: str) -> None:
        self.asset = asset
        self.job = job
        self.source = source          # "click" or "website"
        self.stage = "queued"
        self.fraction = 0.0
        self.message = "Queued"


class KitbridgeApp:
    def __init__(self) -> None:
        self.prefs: Dict[str, Any] = prefs_mod.load()
        self.client: Optional[BKClient] = None
        self.poller: Optional[Poller] = None
        self.dispatcher = Dispatcher()
        self.ready = False
        self.version = 0                         # bumped on every visible state change
        self.status = "Starting Kitbridge…"
        self.user_name = ""
        paths.set_cache_root(str(self.prefs.get("download_dir") or ""))
        self.confirm_heavy: Optional[Callable[[str, int], bool]] = None
        self.settings_open: bool = False
        self._settings_info_seq = 0
        self.settings_info: Dict[str, Any] = {"blender": "", "size": 0, "count": 0, "folders": {}, "loaded": False}
        self.current_tab: str = str(self.prefs.get("last_tab") or "model")
        self.tier_filter: str = "ALL"
        self.downloaded_type_filter: str = "ALL"
        self.renderer_name: str = "REDSHIFT"
        # Renderer for imported materials this session; reset to "auto" unless remembered.
        self.renderer_choice: str = (
            str(self.prefs.get("renderer") or "auto") if self.prefs.get("remember_renderer") else "auto"
        )
        self.results: Dict[str, List[Dict[str, Any]]] = {tab: [] for tab in prefs_mod.TABS}
        self.searching: Dict[str, bool] = {tab: False for tab in prefs_mod.TABS}
        self.jobs: Dict[str, JobState] = {}
        self.cached_ids: set = set()
        self.in_scene_ids: set = set()
        self._events: "queue.Queue[Tuple[Any, ...]]" = queue.Queue()
        self._search_seq: Dict[str, int] = {tab: 0 for tab in prefs_mod.TABS}
        self._last_search_query: Dict[str, str] = {tab: "" for tab in prefs_mod.TABS}
        # Asset the user clicked while logged out; started once a post-login search returns it.
        self._pending_after_login: Optional[Dict[str, Any]] = None
        # Downloaded packages without a thumbnail (made by the dev CLI or by older versions):
        # BlenderKit is asked for them once per session, by asset id.
        self._thumb_lookups: Dict[str, Dict[str, Any]] = {}
        self._started = False
        self._lock = threading.Lock()

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self, software_version: str) -> None:
        """Start bk_client and the poller in the background; safe to call repeatedly."""
        with self._lock:
            if self._started:
                return
            self._started = True
        _install_file_log()
        self.client = BKClient(software_version=software_version)
        self.poller = Poller(self.client, interval=0.2, api_key_getter=self._api_key)
        auth.register_auth_handlers(self.dispatcher, client=self.client)
        self.dispatcher.register("login", self._on_login_task)
        self.dispatcher.register("oauth2/logout", lambda t: self._post("login_changed"))
        self.dispatcher.register("profiles/get_user_profile", self._on_profile_task)
        self.dispatcher.register("thumbnail_download", lambda t: self._post("thumbnail"))
        self.dispatcher.register("bkclientjs/get_asset", self._on_website_asset)
        self._refresh_cached_ids()
        self._run_bg(self._boot)

    def shutdown(self) -> None:
        for state in list(self.jobs.values()):
            state.job.cancel()
        if self.poller is not None:
            self.poller.stop()
        if self.client is not None:
            try:
                self.client.unsubscribe_addon()
            except Exception as exc:
                log.debug("unsubscribe_addon failed: %s", exc)

    def _boot(self) -> None:
        assert self.client is not None and self.poller is not None
        try:
            self.client.ensure_running()
        except Exception as exc:
            log.exception("bk_client failed to start")
            self._post("status", f"Could not start the BlenderKit client: {exc}")
            return
        self.poller.start()
        if auth.is_logged_in():
            auth.request_user_profile(self.client)
        self._post("ready")

    # ── called from the UI (main thread) ──────────────────────────────────

    def set_pref(self, key: str, value: Any) -> None:
        self.prefs[key] = value
        try:
            prefs_mod.save(self.prefs)
        except OSError as exc:
            log.warning("Could not save Kitbridge prefs: %s", exc)

    def update_renderer_name(self) -> None:
        """Chip label: the forced renderer, or "Auto · <document renderer>"."""
        if self.renderer_choice != "auto":
            self.renderer_name = self.renderer_choice.upper()
            return
        try:
            self.renderer_name = "AUTO · " + self.document_renderer().upper()
        except Exception as exc:
            log.debug("Detecting the document renderer failed: %s", exc)

    def document_renderer(self) -> str:
        import c4d
        from kitbridge.c4d_import.renderer import detect_renderer

        return str(detect_renderer(c4d.documents.GetActiveDocument()))

    def import_renderer(self) -> str:
        """Renderer whose materials an import builds."""
        return self.document_renderer() if self.renderer_choice == "auto" else self.renderer_choice

    def set_renderer_choice(self, choice: str) -> None:
        if choice not in prefs_mod.RENDERER_CHOICES:
            return
        self.renderer_choice = choice
        if self.prefs.get("remember_renderer"):
            self.set_pref("renderer", choice)
        self.update_renderer_name()
        self._set_status(
            "Materials follow the document's render engine." if choice == "auto"
            else f"Imports now build {choice.capitalize()} materials."
        )

    def set_remember_renderer(self, remember: bool) -> None:
        self.set_pref("remember_renderer", bool(remember))
        self.set_pref("renderer", self.renderer_choice if remember else "auto")
        self._bump()

    def filtered_results(self, tab: Optional[str] = None) -> List[Dict[str, Any]]:
        target_tab = tab or self.current_tab
        items = self.results.get(target_tab, [])
        return layout.filter_items(
            items,
            target_tab,
            tier_filter=self.tier_filter,
            type_filter=self.downloaded_type_filter,
        )

    def is_logged_in(self) -> bool:
        return auth.is_logged_in()

    def login(self) -> None:
        if self.client is None:
            return
        self._set_status("Opening the BlenderKit login page in your browser…")
        self._run_bg(lambda: auth.start_login(self.client, open_browser=True))

    def logout(self) -> None:
        if self.client is None:
            return
        self._run_bg(lambda: auth.logout(self.client))
        self.user_name = ""
        self._set_status("Logged out.")

    def search(self, tab: str, query: str) -> None:
        self._last_search_query[tab] = query.strip()
        if tab == "downloaded":
            self._list_downloaded()
            return
        if not self.ready or self.client is None:
            self._set_status("Still connecting to BlenderKit…")
            return
        self._search_seq[tab] += 1
        seq = self._search_seq[tab]
        self.searching[tab] = True
        self._set_status("Searching…")
        free_only = bool(self.prefs.get("free_only"))
        self._run_bg(lambda: self._start_search(tab, query, free_only, seq))

    def activate(self, item: Dict[str, Any]) -> None:
        """Click on a cell: import it (cached), start a download, or cancel a running one."""
        base_id = str(item.get("assetBaseId") or "")
        if base_id in self.jobs:
            self.jobs[base_id].job.cancel()
            self._set_status(f"Cancelling {item.get('name')}…")
            return
        package_dir = str(item.get("packagePath") or "")
        if not package_dir:
            self._start_job(item, source="click")
        elif pipeline.needs_reconvert(package_dir) and len(self.jobs) < MAX_CONCURRENT_JOBS:
            self._start_reconvert(item, package_dir)
        else:
            self._import_package(package_dir, str(item.get("name") or ""), base_id)

    def remove_from_cache(self, item: Dict[str, Any]) -> None:
        base_id = str(item.get("assetBaseId") or "")
        if not base_id or base_id == "*":
            return  # pipeline.remove treats a missing id as "clear everything"
        try:
            pipeline.remove(base_id)
        except Exception as exc:
            log.exception("Removing %s from cache failed", base_id)
            self._set_status(f"Could not remove from cache: {exc}")
            return
        self._refresh_cached_ids()
        if self.results["downloaded"]:
            self._list_downloaded()
        self._set_status(f"Removed {item.get('name')} from the cache.")

    def refresh_settings_info(self) -> None:
        """Measure the cache and find Blender off the main thread: both can take a while."""
        self._settings_info_seq += 1
        seq = self._settings_info_seq

        def work() -> None:
            info = dict(pipeline.cache_summary())
            info["blender"] = self.blender_path()
            info["loaded"] = True
            self._post("settings_info", seq, info)

        self._run_bg(work)

    def set_download_dir(self, path: str) -> bool:
        """Switch the download folder ("" = default). Existing packages stay where they are."""
        cleaned = (path or "").strip()
        if cleaned:
            try:
                os.makedirs(cleaned, exist_ok=True)
                with tempfile.TemporaryFile(dir=cleaned):
                    pass
            except OSError as exc:
                self._set_status(f"Can't use this folder: {exc}")
                return False
        self.set_pref("download_dir", cleaned)
        paths.set_cache_root(cleaned)
        self._refresh_cached_ids()
        self.results["downloaded"] = []
        self.refresh_settings_info()
        self._set_status(f"Downloads now go to {paths.cache_root(create=False)}")
        return True

    def blender_path(self) -> str:
        found = blender.find_blender_with_version(
            user_pref_path=str(self.prefs.get("blender_path") or ""), client=self.client, publish=False
        )
        return found[0] if found else ""

    # ── main-thread pump ──────────────────────────────────────────────────

    def tick(self) -> bool:
        """Apply queued events and finished jobs. Returns True if the UI should redraw."""
        before = self.version
        if self.poller is not None:
            tasks = self.poller.drain(max_items=500)
            if tasks:
                self.dispatcher.dispatch(tasks)
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            try:
                self._apply(event)
            except Exception:
                log.exception("Kitbridge event %r failed", event[0])
        for base_id, state in list(self.jobs.items()):
            if state.job.done:
                del self.jobs[base_id]
                self._finish_job(state)
        return self.version != before

    def _apply(self, event: Tuple[Any, ...]) -> None:
        kind = event[0]
        if kind == "settings_info":
            if event[1] == self._settings_info_seq:  # a slower older measurement must not win
                self.settings_info = event[2]
                self._bump()
            return
        if kind == "ready":
            self.ready = True
            self._set_status("Connected. Search BlenderKit above.")
        elif kind == "status":
            self._set_status(str(event[1]))
        elif kind == "login_changed":
            if auth.is_logged_in() and self.client is not None:
                self._set_status("Logged in.")
                auth.request_user_profile(self.client)
                self._refresh_searches_after_login()
            else:
                self.user_name = ""
                self._bump()
        elif kind == "profile":
            profile = auth.get_cached_profile() or {}
            full = f"{profile.get('firstName') or ''} {profile.get('lastName') or ''}".strip()
            self.user_name = full or str(profile.get("username") or "")
            self._bump()
        elif kind == "results":
            _, tab, seq, items, error = event
            if seq != self._search_seq.get(tab):
                return  # a newer search replaced this one
            self.searching[tab] = False
            if error:
                self._set_status(f"Search failed: {error}")
                return
            self.results[tab] = items
            if self._start_pending_from(items):
                return
            last_q = getattr(self, "_last_search_query", {}).get(tab, "")
            if last_q.startswith("asset_base_id:"):
                # The code does not say its type, so every tab is searched: open the one that has it.
                if items:
                    self.current_tab = tab
                    self.set_pref("last_tab", tab)
                    first_name = items[0].get("name") or "asset"
                    self._set_status(f"From website: {first_name} — click to import")
                elif not any(self.searching.values()) and not any(
                    self.results.get(t) for t, q in self._last_search_query.items() if q == last_q
                ):
                    self._set_status("Nothing found.")
            else:
                self._set_status(f"{len(items)} results." if items else "Nothing found.")
        elif kind == "progress":
            _, base_id, stage, fraction, message = event
            state = self.jobs.get(base_id)
            if state is not None:
                state.stage, state.fraction, state.message = stage, float(fraction), str(message)
                self._bump()
        elif kind == "website_asset":
            item = event[1]
            self._set_status(f"From website: {item.get('name')}")
            if str(item.get("assetBaseId")) not in self.jobs:
                self._start_job(item, source="website")
        elif kind == "redraw":
            self._bump()
        elif kind == "thumbnail":
            self._store_found_thumbnails()
            self._bump()
        elif kind == "thumb_found":
            _, base_id, found = event
            lookup = self._thumb_lookups.get(base_id)
            if lookup is not None and found is not None:
                lookup.update(found)
                for item in self.results.get("downloaded") or []:
                    if item.get("assetBaseId") == base_id:
                        self._apply_lookup(item, lookup)
                self._store_found_thumbnails()
                self._bump()

    # ── jobs ──────────────────────────────────────────────────────────────

    def _start_job(self, item: Dict[str, Any], source: str) -> None:
        if self.client is None or self.poller is None:
            return
        if len(self.jobs) >= MAX_CONCURRENT_JOBS:
            self._set_status("Two downloads are already running; wait for one to finish.")
            return
        if not item.get("isFree") and not auth.is_logged_in():
            self._pending_after_login = item
            self._set_status(f"Log in to download {item.get('name')}; it starts by itself after login.")
            self.login()
            return
        base_id = str(item.get("assetBaseId") or "")

        def on_progress(stage: str, fraction: float, message: str) -> None:
            self._post("progress", base_id, stage, fraction, message)

        job = pipeline.fetch_asset_async(
            self.client,
            item,
            resolution=str(self.prefs.get("resolution") or "2048"),
            api_key=self._api_key(),
            on_progress=on_progress,
            poller=self.poller,
            dispatcher=self.dispatcher,
            blender_path=str(self.prefs.get("blender_path") or ""),
        )
        self.jobs[base_id] = JobState(item, job, source)
        self._set_status(f"Downloading {item.get('name')}…")

    def _refresh_searches_after_login(self) -> None:
        """Results fetched while logged out say paid assets can't be downloaded; fetch them again."""
        for tab, query in self._last_search_query.items():
            if tab != "downloaded" and (self.results.get(tab) or query):
                self.search(tab, query)

    def _start_pending_from(self, items: List[Dict[str, Any]]) -> bool:
        """Start the asset clicked before login if these fresh results contain it."""
        pending = self._pending_after_login
        if pending is None:
            return False
        wanted = str(pending.get("assetBaseId") or "")
        fresh = next((i for i in items if str(i.get("assetBaseId") or "") == wanted), None)
        if fresh is None:
            return False
        self._pending_after_login = None
        self._start_job(fresh, source="click")
        return True

    def _start_reconvert(self, item: Dict[str, Any], package_dir: str) -> None:
        base_id = str(item.get("assetBaseId") or "")

        def on_progress(stage: str, fraction: float, message: str) -> None:
            self._post("progress", base_id, stage, fraction, message)

        job = pipeline.reconvert_async(
            package_dir,
            client=self.client,
            on_progress=on_progress,
            blender_path=str(self.prefs.get("blender_path") or ""),
        )
        self.jobs[base_id] = JobState(item, job, "click")
        self._set_status(f"Updating {item.get('name')} for this Kitbridge version…")

    def _finish_job(self, state: JobState) -> None:
        name = str(state.asset.get("name") or "asset")
        try:
            package = state.job.result(timeout=0)
        except pipeline.PipelineError as exc:
            self._set_status(f"{name}: {exc}")
            return
        except Exception as exc:
            log.exception("Pipeline failed for %s", name)
            self._set_status(f"{name}: unexpected error ({exc}). See the Kitbridge log.")
            return

        try:
            dst_thumb = os.path.join(package.path, THUMBNAIL_NAME)
            src_thumb = _first_existing(state.asset.get("thumbnailPaths") or {})
            if src_thumb and not os.path.isfile(dst_thumb):
                shutil.copyfile(src_thumb, dst_thumb)
        except Exception:
            pass

        self._refresh_cached_ids()
        self._import_package(package.path, name, str(state.asset.get("assetBaseId") or ""))

    def _import_package(self, package_dir: str, name: str, base_id: str = "") -> None:
        import c4d  # only the main-thread import path touches c4d
        from kitbridge.c4d_import import import_package
        from kitbridge.c4d_import.renderer import RENDERER_ARNOLD, arnold_specular_depth

        size_mb = pipeline.geometry_size_mb(package_dir)
        if size_mb >= pipeline.HEAVY_GEOMETRY_MB and self.confirm_heavy is not None:
            if not self.confirm_heavy(name, size_mb):
                self._set_status(f"{name} was not imported. It stays in Downloaded.")
                return

        if base_id:
            self.in_scene_ids.add(base_id)
        doc = c4d.documents.GetActiveDocument()
        selection = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
        renderer = self.import_renderer()
        result = import_package(
            doc, package_dir, renderer=renderer, options={"apply_to_selection": bool(selection)}
        )
        c4d.EventAdd()
        doc_renderer = self.document_renderer()
        if result.errors:
            first = result.errors[0]
            self._set_status(f"{name}: import failed at {first.get('step')}: {first.get('error')}")
            return
        where = " to selection" if result.applied_to_selection else ""
        mats = len(result.materials)
        self._set_status(
            f"Imported {name}{where} · {mats} material{'s' if mats != 1 else ''} · {result.renderer}"
            + (f" · {len(result.warnings)} warnings" if result.warnings else "")
        )
        if result.renderer != doc_renderer:
            # Materials of another renderer show up grey/untextured in this document.
            self._set_status(
                f"Imported {name} with {result.renderer.capitalize()} materials, but this document renders "
                f"with {doc_renderer.capitalize()}. Switch the render engine to see textures."
            )
        elif result.renderer == RENDERER_ARNOLD and result.has_transmission:
            depth = arnold_specular_depth(doc)
            if depth is not None and depth < 2:
                self._set_status(f"Imported {name}. Glass may render dark: raise Arnold's GI Specular depth.")

    # ── background helpers ────────────────────────────────────────────────

    def _start_search(self, tab: str, query: str, free_only: bool, seq: int) -> None:
        assert self.client is not None
        tempdir = paths.search_temp_dir(tab, create=True)
        try:
            task_id = search.start_search(
                self.client, query=query, asset_type=tab, free_first=free_only,
                page_size=48, api_key=self._api_key(), tempdir=tempdir,
            )
        except Exception as exc:
            self._post("results", tab, seq, [], str(exc))
            return

        def on_task(task: TaskDict) -> None:
            status = str(task.get("status") or "")
            if status == "finished":
                items = search.normalise_results(task.get("result") or {}, tempdir=tempdir)
                if free_only:
                    items = [i for i in items if i.get("isFree")]
                self._post("results", tab, seq, items, "")
            elif status == "error":
                self._post("results", tab, seq, [], str(task.get("message") or "unknown error"))

        self.dispatcher.register_task(task_id, on_task)

    def _list_downloaded(self) -> None:
        items = []
        missing: List[Tuple[str, str]] = []
        for pkg in pipeline.list_cached():
            asset = pkg.manifest.get("asset") or {}
            base_id = str(asset.get("base_id") or "")
            asset_type = str(asset.get("type") or "")
            thumb_path = os.path.join(pkg.path, THUMBNAIL_NAME)
            item: Dict[str, Any] = {
                "assetBaseId": base_id,
                "name": str(asset.get("name") or os.path.basename(pkg.path)),
                "assetType": asset_type,
                "tierKnown": False,  # the package does not record Free / Full Plan
                "packagePath": pkg.path,
                "resolution": str(asset.get("resolution") or ""),
                "thumbnailPaths": {},
            }
            if os.path.isfile(thumb_path):
                item["thumbnailPaths"] = {"middle": thumb_path, "small": thumb_path, "large": thumb_path}
            if base_id:
                lookup = self._thumb_lookups.get(base_id)
                if lookup is None and not item["thumbnailPaths"]:
                    lookup = self._thumb_lookups[base_id] = {"packages": set()}
                    missing.append((base_id, asset_type))
                if lookup is not None:
                    lookup["packages"].add(pkg.path)
                    self._apply_lookup(item, lookup)
            items.append(item)
        self.results["downloaded"] = items
        self._store_found_thumbnails()
        if missing:
            self._run_bg(lambda: self._look_up_thumbnails(missing))
        self._set_status(f"{len(items)} downloaded assets (work offline).")

    @staticmethod
    def _apply_lookup(item: Dict[str, Any], lookup: Dict[str, Any]) -> None:
        if not item.get("thumbnailPaths") and lookup.get("thumbnailPaths"):
            item["thumbnailPaths"] = dict(lookup["thumbnailPaths"])
        if "isFree" in lookup:
            item.update(isFree=lookup["isFree"], isForSale=lookup.get("isForSale"), tierKnown=True)

    def _look_up_thumbnails(self, wanted: List[Tuple[str, str]]) -> None:
        """Ask BlenderKit for each asset by id; its search also downloads the thumbnails."""
        if self.client is None or not self.ready:
            for base_id, _ in wanted:  # try again on the next listing
                self._thumb_lookups.pop(base_id, None)
            return
        for base_id, asset_type in wanted:
            tab = asset_type if asset_type in ("model", "material", "hdr") else "model"
            tempdir = paths.search_temp_dir(tab, create=True)
            try:
                task_id = search.start_search(
                    self.client, query=f"asset_base_id:{base_id}", asset_type=tab,
                    page_size=1, api_key=self._api_key(), tempdir=tempdir,
                )
            except Exception as exc:
                log.debug("Thumbnail lookup for %s failed: %s", base_id, exc)
                continue

            def on_task(task: TaskDict, base_id: str = base_id, tempdir: str = tempdir) -> None:
                status = str(task.get("status") or "")
                if status == "finished":
                    found = next(
                        (i for i in search.normalise_results(task.get("result") or {}, tempdir=tempdir)
                         if str(i.get("assetBaseId") or "") == base_id),
                        None,
                    )
                    self._post("thumb_found", base_id, None if found is None else {
                        "thumbnailPaths": found.get("thumbnailPaths") or {},
                        "isFree": bool(found.get("isFree")),
                        "isForSale": found.get("isForSale"),
                    })

            self.dispatcher.register_task(task_id, on_task)

    def _store_found_thumbnails(self) -> None:
        """Copy thumbnails found by a lookup into their packages once they are on disk."""
        for lookup in self._thumb_lookups.values():
            source = _first_existing(lookup.get("thumbnailPaths") or {})
            if not source:
                continue
            for package_dir in list(lookup.get("packages") or ()):
                target = os.path.join(package_dir, THUMBNAIL_NAME)
                if os.path.isfile(target):
                    lookup["packages"].discard(package_dir)
                    continue
                try:
                    shutil.copyfile(source, target)
                    lookup["packages"].discard(package_dir)
                except OSError as exc:
                    log.debug("Could not store thumbnail in %s: %s", package_dir, exc)

    def _refresh_cached_ids(self) -> None:
        try:
            self.cached_ids = {
                str((p.manifest.get("asset") or {}).get("base_id") or "") for p in pipeline.list_cached()
            }
        except Exception as exc:
            log.warning("Listing the Kitbridge cache failed: %s", exc)

    def _on_login_task(self, task: TaskDict) -> None:
        if str(task.get("status")) in ("finished", "error"):
            self._post("login_changed")

    def _on_profile_task(self, task: TaskDict) -> None:
        if str(task.get("status")) == "finished":
            self._post("profile")

    def _on_website_asset(self, task: TaskDict) -> None:
        result = task.get("result") or {}
        raw = result.get("asset_data") if isinstance(result, dict) else None
        if isinstance(raw, dict):
            self._post("website_asset", search.normalise_asset(raw))

    def _api_key(self) -> str:
        if self.client is None:
            return ""
        try:
            return auth.get_api_key(self.client) or ""
        except Exception as exc:
            log.debug("get_api_key failed: %s", exc)
            return ""

    def _run_bg(self, fn: Callable[[], Any]) -> None:
        def runner() -> None:
            try:
                fn()
            except Exception as exc:
                log.exception("Kitbridge background task failed")
                self._post("status", f"Error: {exc}")

        threading.Thread(target=runner, name="kitbridge-bg", daemon=True).start()

    def _post(self, *event: Any) -> None:
        self._events.put(event)

    def _set_status(self, text: str) -> None:
        self.status = text
        self._bump()

    def _bump(self) -> None:
        self.version += 1


THUMBNAIL_NAME = "thumbnail.jpg"


def _first_existing(thumb_paths: Dict[str, Any]) -> str:
    for key in ("middle", "small", "large"):
        path = str(thumb_paths.get(key) or "")
        if path and os.path.isfile(path):
            return path
    return ""


def log_path() -> str:
    return os.path.join(paths.global_dir(create=True), "kitbridge", "kitbridge.log")


def _install_file_log() -> None:
    """Send the "kitbridge" logger to a rotating file once; C4D's console hides most of it."""
    from logging.handlers import RotatingFileHandler

    logger = logging.getLogger("kitbridge")
    target = log_path()
    if any(getattr(h, "baseFilename", "") == os.path.abspath(target) for h in logger.handlers):
        return
    os.makedirs(os.path.dirname(target), exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > logging.INFO:
        logger.setLevel(logging.INFO)


_APP: Optional[KitbridgeApp] = None


def get_app() -> KitbridgeApp:
    global _APP
    if _APP is None:
        _APP = KitbridgeApp()
    return _APP


def reset_app_for_dev() -> None:
    """Drop the singleton (used by the dev reload script)."""
    global _APP
    if _APP is not None:
        _APP.shutdown()
    _APP = None
