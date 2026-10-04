"""SeaFile HTTP enumeration through the production driver and deletion services."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from core.svr.sync_data_source import SeaFile
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def seafile_http() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            state["requests"].append(self.path)
            mode, status = state["mode"], 200
            body: Any = {}
            if path.endswith("/account/info/"):
                body = {"email": "owner@example.test"}
            elif path == "/api2/repos/":
                body = [{"id": "repo", "name": "Library"}]
            elif path.endswith("/dir/"):
                if query["p"] == ["/"]:
                    body = (
                        []
                        if mode == "empty"
                        else [
                            {"type": "file", "id": "retained", "name": "same.txt", "size": 4, "mtime": 1769904000 if mode in {"content-error", "ingestion-error"} else 1735689600},
                            {"type": "dir", "name": "nested"},
                        ]
                    )
                elif mode == "listing-error":
                    status, body = 403, {"error": "denied"}
                elif mode == "malformed":
                    body = {}
                else:
                    body = []
            elif path.endswith("/file/"):
                body = state["base"] + "/download"
            elif path == "/download":
                status, body = (403, {"error": "denied"}) if mode == "content-error" else (200, "body")
            else:
                status = 404
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "content-error", "ingestion-error"])
async def test_seafile_real_resources(connector_sync_api: dict[str, Any], seafile_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    seafile_http["mode"] = mode
    conf = {"seafile_url": seafile_http["base"], "sync_deleted_files": True, "credentials": {"seafile_token": "synthetic"}}
    await assert_deleted_sync(connector_sync_api, monkeypatch, SeaFile, "seafile", {name: f"seafile:repo:{name}" for name in ["retained", "stale"]}, conf, mode)
    assert any("/dir/" in path for path in seafile_http["requests"])
    assert ("/download" in seafile_http["requests"]) == (mode in {"content-error", "ingestion-error"})


def test_seafile_config_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(
        connector_sync_api,
        "seafile",
        {"sync_deleted_files": False, "seafile_url": "https://seafile.test", "sync_scope": "directory", "repo_id": "repo", "sync_path": "/folder", "credentials": {"repo_token": "synthetic"}},
    )
