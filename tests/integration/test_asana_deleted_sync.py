"""Real Asana SDK pagination over loopback, then isolated deletion readbacks."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from common.data_source.asana_connector import AsanaAPI
from core.svr.sync_data_source import Asana
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def asana_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            state["requests"].append((path, query))
            mode, status = state["mode"], 200
            data: Any = []
            next_page: Any = None
            if path == "/users":
                data = [{"gid": "user", "email": "owner@test"}]
            elif path == "/projects/project/project_memberships":
                data = [{"gid": "member", "user": {"gid": "user", "email": "owner@test"}}]
            elif path == "/projects":
                data = [{"gid": "project"}]
            elif path == "/projects/project":
                data = {"gid": "project", "name": "Project", "archived": False, "team": {"gid": "team"}, "privacy_setting": "private"}
            elif path == "/projects/project/tasks":
                assert "modified_since" not in query
                data = [
                    {
                        "gid": "task",
                        "name": "Task",
                        "notes": "body",
                        "created_by": None,
                        "due_on": None,
                        "completed_at": None,
                        "modified_at": "2026-02-01T00:00:00Z" if mode in {"content-error", "ingestion-error"} else "2025-01-01T00:00:00Z",
                        "permalink_url": "https://asana.test/task",
                    }
                ]
            elif path == "/attachments":
                assert query["parent"] == ["task"]
                if mode == "empty":
                    data = []
                elif not query.get("offset"):
                    data = [{"gid": "retained"}]
                    next_page = {"offset": "next", "path": "/attachments?offset=next", "uri": state["base"] + "/attachments?offset=next"}
                elif mode == "listing-error":
                    status = 403
                elif mode == "malformed":
                    next_page = {}
                elif mode == "loop":
                    next_page = {"offset": "next"}
            elif path == "/attachments/retained":
                data = {"gid": "retained", "name": "file.txt", "size": 4, "download_url": state["base"] + "/download"}
            elif path == "/tasks/task/stories":
                data = []
            elif path == "/download":
                status = 403 if mode == "content-error" else 200
                data = "body"
            else:
                status = 404
            body = {"data": data, "next_page": next_page} if status == 200 else {"errors": [{"message": "denied"}]}
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
    original = AsanaAPI.__init__

    def initialize(self: AsanaAPI, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.configuration.host = state["base"]

    monkeypatch.setattr(AsanaAPI, "__init__", initialize)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "loop", "content-error", "ingestion-error"])
async def test_asana_real_resources(connector_sync_api: dict[str, Any], asana_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    asana_http["mode"] = mode
    conf = {"asana_workspace_id": "workspace", "asana_project_ids": "project", "asana_team_id": "team", "sync_deleted_files": True, "credentials": {"asana_api_token_secret": "synthetic"}}
    await assert_deleted_sync(connector_sync_api, monkeypatch, Asana, "asana", {name: f"asana:task:{name}" for name in ["retained", "stale"]}, conf, mode)
    paths = [path for path, _ in asana_http["requests"]]
    assert "/attachments" in paths
    assert ("/download" in paths) == (mode in {"content-error", "ingestion-error"})
    assert ("/tasks/task/stories" in paths) == (mode in {"content-error", "ingestion-error"})


def test_asana_config_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(
        connector_sync_api, "asana", {"sync_deleted_files": False, "asana_workspace_id": "workspace", "asana_project_ids": "project", "credentials": {"asana_api_token_secret": "synthetic"}}
    )
