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

"""Background /report poller and thread-safe task dispatcher."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .client import BKClient

log = logging.getLogger("kitbridge")

TaskDict = Dict[str, Any]
TaskHandler = Callable[[TaskDict], None]

KNOWN_TASK_TYPES: Tuple[str, ...] = (
    "bkclientjs/get_asset",
    "login",
    "token_refresh",
    "oauth2/logout",
    "profiles/get_user_profile",
    "search",
    "thumbnail_download",
    "asset_download",
    "run_blender_script",
    "client_status",
    "settings",
)

_TERMINAL_STATUSES = ("finished", "error", "cancelled")


class Poller:
    """Daemon thread that polls ``client.report()`` and buffers tasks in a queue."""

    def __init__(
        self,
        client: BKClient,
        interval: float = 0.2,
        *,
        api_key_getter: Optional[Callable[[], str]] = None,
        project_name_getter: Optional[Callable[[], str]] = None,
    ) -> None:
        self.client: BKClient = client
        self.interval: float = max(0.01, float(interval))
        self._api_key_getter = api_key_getter
        self._project_name_getter = project_name_getter

        self._queue: queue.Queue[TaskDict] = queue.Queue()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the background polling daemon thread if not already running."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._run_loop,
                name="kitbridge-report-poller",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the poller thread to exit and wait for it to finish."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def poll_once(self) -> int:
        """Execute a single ``/report`` poll and enqueue returned tasks."""
        api_key = ""
        project_name = ""
        if self._api_key_getter is not None:
            try:
                api_key = self._api_key_getter() or ""
            except Exception as exc:
                log.warning("api_key_getter raised in Poller: %s", exc)
        if self._project_name_getter is not None:
            try:
                project_name = self._project_name_getter() or ""
            except Exception as exc:
                log.warning("project_name_getter raised in Poller: %s", exc)

        tasks = self.client.report(api_key=api_key, project_name=project_name)
        count = 0
        for task in tasks:
            if isinstance(task, dict):
                self._queue.put(task)
                count += 1
        return count

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                log.warning("Unexpected error in Poller loop: %s", exc)
            if self._stop_event.wait(self.interval):
                break

    def drain(self, max_items: Optional[int] = None) -> List[TaskDict]:
        """Drain up to *max_items* tasks from the queue without blocking."""
        items: List[TaskDict] = []
        limit = max_items if (max_items is not None and max_items > 0) else None
        while limit is None or len(items) < limit:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items


class Dispatcher:
    """Thread-safe task router for bk_client `/report` tasks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handlers: Dict[str, List[TaskHandler]] = {
            task_type: [] for task_type in KNOWN_TASK_TYPES
        }
        self._task_id_handlers: Dict[str, Tuple[TaskHandler, bool]] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        """Register *handler* for *task_type*."""
        if not task_type:
            raise ValueError("task_type must be non-empty")
        with self._lock:
            bucket = self._handlers.setdefault(task_type, [])
            if handler not in bucket:
                bucket.append(handler)

    on = register

    def unregister(self, task_type: str, handler: TaskHandler) -> None:
        """Remove *handler* from *task_type* if registered."""
        with self._lock:
            bucket = self._handlers.get(task_type)
            if bucket and handler in bucket:
                bucket.remove(handler)

    def register_task(
        self,
        task_id: str,
        handler: TaskHandler,
        *,
        auto_remove_on_finish: bool = True,
    ) -> None:
        """Register a callback for a specific *task_id*."""
        if not task_id:
            raise ValueError("task_id must be non-empty")
        with self._lock:
            self._task_id_handlers[task_id] = (handler, auto_remove_on_finish)

    def unregister_task(self, task_id: str) -> None:
        """Remove any callback registered for *task_id*."""
        with self._lock:
            self._task_id_handlers.pop(task_id, None)

    def clear(self) -> None:
        """Remove all registered callbacks while preserving standard task slots."""
        with self._lock:
            self._handlers = {task_type: [] for task_type in KNOWN_TASK_TYPES}
            self._task_id_handlers.clear()

    def known_slots(self) -> Tuple[str, ...]:
        """Return the tuple of registered task-type slots."""
        with self._lock:
            return tuple(self._handlers.keys())

    def dispatch(self, tasks: Iterable[TaskDict]) -> None:
        """Route each task in *tasks* to matching registered handlers."""
        for task in tasks:
            if not isinstance(task, dict):
                continue

            task_type = str(task.get("task_type") or "")
            task_id = str(task.get("task_id") or "")
            status = str(task.get("status") or "")

            with self._lock:
                is_known_type = task_type in self._handlers
                type_callbacks = list(self._handlers.get(task_type, ()))
                id_entry = self._task_id_handlers.get(task_id) if task_id else None
                if (
                    id_entry is not None
                    and id_entry[1]
                    and status in _TERMINAL_STATUSES
                ):
                    self._task_id_handlers.pop(task_id, None)

            if not is_known_type and id_entry is None:
                log.debug(
                    "Ignoring unknown task_type=%r (task_id=%s, status=%s)",
                    task_type,
                    task_id,
                    status,
                )
                continue

            for cb in type_callbacks:
                try:
                    cb(task)
                except Exception:
                    log.exception("Task handler raised for task_type=%r", task_type)

            if id_entry is not None:
                try:
                    id_entry[0](task)
                except Exception:
                    log.exception("Task-ID handler raised for task_id=%r", task_id)
