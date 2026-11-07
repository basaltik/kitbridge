from __future__ import annotations

import http.server
import json
import logging
import os
import socket
import socketserver
import stat
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type
from unittest import mock

import pytest

from kitbridge.core import auth, blender, client, paths, search, secret_store, tasks


# ── 1. PKCE Verifier & Challenge ─────────────────────────────────────────────


def test_pkce_rfc7636_vector() -> None:
    # RFC 7636 Appendix B test vector
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    expected_challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert auth.compute_pkce_challenge(verifier) == expected_challenge


def test_pkce_pair_generation_and_bounds() -> None:
    verifier, challenge = auth.generate_pkce_pair()
    assert len(verifier) == 128
    assert verifier.isalnum()
    assert "=" not in challenge
    assert auth.compute_pkce_challenge(verifier) == challenge

    short_verifier = auth.generate_pkce_verifier(43)
    assert len(short_verifier) == 43

    with pytest.raises(ValueError):
        auth.generate_pkce_verifier(42)
    with pytest.raises(ValueError):
        auth.generate_pkce_verifier(129)
    with pytest.raises(ValueError):
        auth.compute_pkce_challenge("")


def test_build_authorize_url_and_token_refresh_window() -> None:
    url = auth.build_authorize_url(
        port="62485",
        code_challenge="abc123challenge",
        state="xyzstate",
        server="https://www.blendkit.com",
    )
    assert url.startswith("https://www.blendkit.com/o/authorize/?")
    assert f"client_id={auth.CLIENT_ID}" in url
    assert "code_challenge=abc123challenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=xyzstate" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A62485%2Fconsumer%2Fexchange%2F" in url

    now = 1_700_000_000.0
    # 4 days left -> no refresh needed
    assert not auth.needs_token_refresh(
        {"refresh_token": "r1", "expires_at": int(now + 4 * 86400)},
        now=now,
    )
    # 2 days left -> within 3-day refresh window
    assert auth.needs_token_refresh(
        {"refresh_token": "r1", "expires_at": int(now + 2 * 86400)},
        now=now,
    )
    # Manual API key without refresh_token -> never refreshes
    assert not auth.needs_token_refresh(
        {"refresh_token": "", "expires_at": 0},
        now=now,
    )


# ── 2. Search URL Builder & Result Normalisation ─────────────────────────────


def test_build_search_url_variants() -> None:
    u_model = search.build_search_url("modern chair", asset_type="model")
    assert "https://www.blendkit.com/api/v1/search/?query=" in u_model
    assert "asset_type:model+modern+chair+order:_score" in u_model
    assert "dict_parameters=1" in u_model
    assert "page_size=24" in u_model
    assert "page=" not in u_model
    assert "addon_type=" not in u_model

    u_mat = search.build_search_url(
        "oak wood",
        asset_type="material",
        category="wood",
        free_first=True,
        bookmarks=True,
        page_size=15,
        page=3,
    )
    assert (
        "asset_type:material+oak+wood+category_subtree:wood+bookmarks_rating:1+order:-is_free,_score"
        in u_mat
    )
    assert "page_size=15" in u_mat
    assert "page=3" in u_mat

    u_hdr = search.build_search_url("studio", asset_type="hdr")
    assert "asset_type:hdr+studio+order:_score" in u_hdr

    passthrough = "https://www.blendkit.com/api/v1/search/?page=2&query=asset_type:model"
    assert search.build_search_url("ignored", next_url=passthrough) == passthrough

    with pytest.raises(ValueError):
        search.build_search_url("brush", asset_type="brush")
    with pytest.raises(ValueError):
        search.build_search_url("chair", page_size=0)
    with pytest.raises(ValueError):
        search.build_search_url("chair", page=0)


def test_normalise_results(tmp_path: Path) -> None:
    temp_dir = str(tmp_path)
    raw_payload = {
        "count": 2,
        "results": [
            {
                "id": "ver-uuid-1",
                "assetBaseId": "base-uuid-1",
                "displayName": "Lounge Chair",
                "assetType": "model",
                "isFree": True,
                "canDownload": True,
                "thumbnailSmallUrl": "https://cdn.example.com/thumbs/small_chair.webp",
                "thumbnailMiddleUrl": "https://cdn.example.com/thumbs/mid_chair.webp",
                "files": [
                    {
                        "fileType": "blend",
                        "downloadUrl": "https://cdn.example.com/files/chair.blend",
                        "fileName": "chair.blend",
                    },
                    {
                        "fileType": "resolution_2K",
                        "downloadUrl": "https://cdn.example.com/files/chair_2k.blend",
                        "fileName": "chair_2k.blend",
                    },
                    {
                        "fileType": "resolution_1K",
                        "downloadUrl": "https://cdn.example.com/files/chair_1k.blend",
                        "fileName": "chair_1k.blend",
                    },
                ],
            },
            {
                "id": "author-1",
                "assetType": "author",
                "name": "Ignored Author",
            },
        ],
    }

    items = search.normalise_results(raw_payload, tempdir=temp_dir)
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "ver-uuid-1"
    assert item["assetBaseId"] == "base-uuid-1"
    assert item["name"] == "Lounge Chair"
    assert item["assetType"] == "model"
    assert item["isFree"] is True
    assert item["canDownload"] is True
    assert item["available_resolutions"] == [1024, 2048]
    assert item["availableResolutions"] == ["1024", "2048", "ORIGINAL"]
    assert item["thumbnailUrls"]["small"] == "https://cdn.example.com/thumbs/small_chair.webp"
    assert item["thumbnailPaths"]["small"] == os.path.join(temp_dir, "small_chair.webp")
    assert len(item["files"]) == 3


# ── 3. Steam VDF Parser ──────────────────────────────────────────────────────


def test_parse_libraryfolders_vdf_sample_file() -> None:
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "libraryfolders.vdf")
    libraries = blender.parse_libraryfolders_vdf_file(fixture_path)
    assert libraries == [
        os.path.normpath(r"C:\Program Files (x86)\Steam"),
        os.path.normpath(r"D:\Games\steam"),
        os.path.normpath(r"E:\SteamLibrary"),
    ]


def test_parse_libraryfolders_vdf_legacy_format() -> None:
    legacy_vdf = """
    "LibraryFolders"
    {
        "TimeNextStatsReport" "1590000000"
        "ContentStatsID" "-123456"
        "1" "D:\\\\Games\\\\steam"
        "2" "F:\\\\SteamGames"
    }
    """
    libraries = blender.parse_libraryfolders_vdf(legacy_vdf)
    assert libraries == [
        os.path.normpath(r"D:\Games\steam"),
        os.path.normpath(r"F:\SteamGames"),
    ]


# ── 4. Port Fallback & Loopback Client Endpoints ─────────────────────────────


class _ThreadedTCPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _start_fake_server(
    handler_cls: Type[http.server.BaseHTTPRequestHandler],
) -> Tuple[_ThreadedTCPServer, str, threading.Thread]:
    srv = _ThreadedTCPServer(("127.0.0.1", 0), handler_cls)
    port = str(srv.server_address[1])
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, port, thread


def test_port_fallback_and_report_payload() -> None:
    recorded_reports: List[Dict[str, Any]] = []
    recorded_downloads: List[Dict[str, Any]] = []
    recorded_scripts: List[Dict[str, Any]] = []

    class OccupiedPortHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(404)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    class ValidBKClientHandler(http.server.BaseHTTPRequestHandler):
        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b"{}"
            return json.loads(raw.decode("utf-8"))

        def _send_json(self, payload: Any, status: int = 200) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1.13/report":
                recorded_reports.append(self._read_json())
                self._send_json(
                    [
                        {
                            "app_id": os.getpid(),
                            "task_id": "t-1",
                            "task_type": "client_status",
                            "status": "finished",
                            "progress": 100,
                            "message": "ok",
                        }
                    ]
                )
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/v1.13/blender/asset_download":
                recorded_downloads.append(self._read_json())
                self._send_json({"task_id": "dl-task-123"})
            elif self.path == "/v1.13/run_blender_script":
                recorded_scripts.append(self._read_json())
                self._send_json({"task_id": "script-task-456"})
            elif self.path == "/v1.13/addons/unsubscribe":
                self._send_json({"status": "ok"})
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

    bad_srv, bad_port, _ = _start_fake_server(OccupiedPortHandler)
    good_srv, good_port, _ = _start_fake_server(ValidBKClientHandler)

    try:
        bk = client.BKClient(
            software_version="2025.1.0",
            ports=(bad_port, good_port),
        )
        chosen_port = bk.ensure_running(timeout=2.0)
        assert chosen_port == good_port
        assert bk.active_port == good_port

        tasks_list = bk.report(api_key="secret_tok", project_name="test.c4d")
        assert len(tasks_list) == 1
        assert tasks_list[0]["task_id"] == "t-1"

        assert len(recorded_reports) >= 2
        last_report = recorded_reports[-1]
        assert last_report["software"] == "Cinema 4D"
        assert last_report["software_version"] == "2025.1.0"
        assert last_report["project_name"] == "test.c4d"
        assert last_report["app_id"] == bk.app_id

        dl_id = bk.asset_download(
            asset_data={
                "id": "asset-ver-1",
                "assetBaseId": "asset-base-1",
                "name": "Wooden Table",
                "assetType": "model",
                "available_resolutions": [1024, 2048],
                "files": [{"fileType": "resolution_2K", "downloadUrl": "https://example.com/f"}],
            },
            resolution="resolution_2K",
            download_dirs=["C:/tmp/cache"],
        )
        assert dl_id == "dl-task-123"
        assert recorded_downloads[0]["asset_data"]["available_resolutions"] == [1024, 2048]
        assert recorded_downloads[0]["PREFS"]["scene_id"] != ""

        script_id = bk.run_blender_script(
            blender_exe_path="C:/Blender/blender.exe",
            script_id="export_glb",
            blend_path="C:/tmp/cache/table.blend",
            output_path="C:/tmp/cache/table.glb",
            params={"subdivision": 0},
        )
        assert script_id == "script-task-456"
        assert recorded_scripts[0]["software"] == "Cinema 4D"
        assert recorded_scripts[0]["script_id"] == "export_glb"

        bk.unsubscribe_addon()
    finally:
        bad_srv.shutdown()
        good_srv.shutdown()
        bad_srv.server_close()
        good_srv.server_close()


def test_port_fallback_on_exit_code_43() -> None:
    """When spawning bk_client exits with code 43 (EADDRINUSE), BKClient tries the next port."""
    good_srv: Optional[_ThreadedTCPServer] = None

    class ReadyHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/v1.13/report":
                body = b"[]"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    good_srv, good_port, _ = _start_fake_server(ReadyHandler)
    # A port nothing listens on: a real bk_client may be running on 62485 during development.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        busy_port = str(probe.getsockname()[1])
    try:
        bk = client.BKClient(ports=(busy_port, good_port))
        spawned_ports: List[str] = []

        class FakeProc:
            def __init__(self, exit_code: Optional[int]) -> None:
                self._rc = exit_code

            def poll(self) -> Optional[int]:
                return self._rc

        def fake_spawn(port: str) -> Any:
            spawned_ports.append(port)
            if port == busy_port:
                return FakeProc(client.EXIT_CODE_ADDR_IN_USE)
            return FakeProc(None)

        with mock.patch.object(bk, "find_running_client", return_value=None), mock.patch.object(
            bk, "_spawn", side_effect=fake_spawn
        ):
            active = bk.ensure_running(timeout=1.5)
            assert active == good_port
            assert spawned_ports == [busy_port, good_port]
    finally:
        if good_srv is not None:
            good_srv.shutdown()
            good_srv.server_close()


# ── 5. Task Dispatcher Routing & Poller ──────────────────────────────────────


def test_dispatcher_routing_and_unknown_debug_log(caplog: pytest.LogCaptureFixture) -> None:
    disp = tasks.Dispatcher()
    slots = disp.known_slots()
    for required in (
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
    ):
        assert required in slots

    received_get_asset: List[Dict[str, Any]] = []
    received_by_id: List[Dict[str, Any]] = []

    disp.register("bkclientjs/get_asset", received_get_asset.append)
    disp.register_task("dl-99", received_by_id.append)

    disp.dispatch(
        [
            {
                "task_id": "web-1",
                "task_type": "bkclientjs/get_asset",
                "status": "finished",
                "result": {"asset_base_id": "abc"},
            },
            {
                "task_id": "dl-99",
                "task_type": "asset_download",
                "status": "progress",
                "progress": 45,
            },
            {
                "task_id": "dl-99",
                "task_type": "asset_download",
                "status": "finished",
                "progress": 100,
            },
            {
                "task_id": "dl-99",
                "task_type": "asset_download",
                "status": "finished",
                "progress": 100,
            },
        ]
    )

    assert len(received_get_asset) == 1
    assert received_get_asset[0]["task_id"] == "web-1"
    # Progress + first finished delivered; second finished ignored because auto_remove_on_finish=True
    assert [t["status"] for t in received_by_id] == ["progress", "finished"]

    with caplog.at_level(logging.DEBUG, logger="kitbridge"):
        disp.dispatch(
            [
                {
                    "task_id": "u-1",
                    "task_type": "future/unknown_event",
                    "status": "finished",
                }
            ]
        )
    assert any(
        rec.levelname == "DEBUG" and "future/unknown_event" in rec.message
        for rec in caplog.records
    )
    assert not any(rec.levelname in ("ERROR", "WARNING") for rec in caplog.records)


def test_poller_background_drain_and_stop() -> None:
    fake_client = mock.MagicMock()
    fake_client.report.return_value = [
        {"task_id": "1", "task_type": "search", "status": "finished"},
        {"task_id": "2", "task_type": "thumbnail_download", "status": "finished"},
    ]

    poller = tasks.Poller(fake_client, interval=0.02, api_key_getter=lambda: "k")
    poller.start()
    try:
        deadline = time.monotonic() + 1.0
        drained: List[Dict[str, Any]] = []
        while time.monotonic() < deadline and len(drained) < 2:
            drained.extend(poller.drain(max_items=1))
            time.sleep(0.01)
        assert len(drained) >= 2
        assert drained[0]["task_id"] == "1"
    finally:
        poller.stop(timeout=1.0)
    assert not poller.is_running


# ── 6. Secret Store Fallback Backend ─────────────────────────────────────────


def test_secret_store_file_fallback_and_auth_persistence(tmp_path: Path) -> None:
    secrets_file = str(tmp_path / "kitbridge" / "secrets.json")

    assert secret_store.get_secret("tokens", backend="file", file_path=secrets_file) is None

    ok = auth.save_tokens(
        access_token="access_abc",
        refresh_token="refresh_xyz",
        expires_in=3600,
        backend="file",
        file_path=secrets_file,
    )
    assert ok is True
    assert os.path.isfile(secrets_file)

    mode = stat.S_IMODE(os.stat(secrets_file).st_mode)
    if os.name != "nt":
        assert mode == 0o600
    else:
        assert mode & stat.S_IREAD

    loaded = auth.load_tokens(backend="file", file_path=secrets_file)
    assert loaded["access_token"] == "access_abc"
    assert loaded["refresh_token"] == "refresh_xyz"
    assert loaded["expires_in"] == 3600
    assert loaded["expires_at"] > int(time.time())

    assert auth.get_api_key(backend="file", file_path=secrets_file) == "access_abc"

    auth.set_manual_api_key("manual_key_999", backend="file", file_path=secrets_file)
    assert auth.get_api_key(backend="file", file_path=secrets_file) == "manual_key_999"

    auth.clear_tokens(backend="file", file_path=secrets_file)
    assert not auth.is_logged_in(backend="file", file_path=secrets_file)
    assert not os.path.exists(secrets_file)
