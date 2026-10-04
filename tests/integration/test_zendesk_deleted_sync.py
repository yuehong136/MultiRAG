"""Zendesk article cleanup and safe ticket rejection over HTTP and real storage."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from common.data_source import zendesk_connector as module
from common.data_source.zendesk_connector import ZendeskClient
from core.svr.sync_data_source import Zendesk
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def zendesk_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}
    # Keep real HTTP retries without spending minutes on intentional 403 responses.
    retry_builder = module.retry_builder
    monkeypatch.setattr(module, "retry_builder", lambda: retry_builder(tries=2, delay=0, jitter=0))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            state["requests"].append((path, query))
            mode, status = state["mode"], 200
            body: Any = {}
            if path.endswith("/guide/content_tags"):
                body = {"records": [], "meta": {"has_more": False}}
            elif path.endswith("/help_center/articles"):
                if query.get("page[after]"):
                    if mode == "listing-error":
                        status = 403
                    elif mode == "malformed":
                        body = {"articles": []}
                    else:
                        body = {"articles": [], "meta": {"has_more": mode == "loop", "after_cursor": "next"}}
                else:
                    records = (
                        []
                        if mode == "empty"
                        else [
                            {
                                "id": 1,
                                "title": "Live",
                                "body": "<p>body</p>",
                                "draft": False,
                                "label_names": [],
                                "updated_at": "2026-02-01T00:00:00Z" if mode == "ingestion-error" else "2025-01-01T00:00:00Z",
                            },
                            {"id": 2, "title": "Draft", "body": "body", "draft": True, "label_names": []},
                        ]
                    )
                    if mode == "content-error":
                        records[0].pop("title")
                    body = {"articles": records, "meta": {"has_more": mode != "empty", "after_cursor": "next"}}
            elif path.endswith("/incremental/tickets.json"):
                body = {
                    "tickets": [{"id": 1, "status": "open", "subject": "Live", "updated_at": "2025-01-01T00:00:00Z"}, {"id": 2, "status": "deleted"}],
                    "end_of_stream": True,
                    "end_time": 1770000000,
                }
            elif path.endswith("/tickets/1/comments"):
                body = {"comments": [], "meta": {"has_more": False}}
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
    original = ZendeskClient.__init__

    def initialize(self: ZendeskClient, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self.base_url = state["base"] + "/api/v2"

    monkeypatch.setattr(ZendeskClient, "__init__", initialize)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "loop", "content-error", "ingestion-error", "ticket-gap", "disabled"])
async def test_zendesk_real_resources(connector_sync_api: dict[str, Any], zendesk_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    zendesk_http["mode"] = mode
    tickets = mode in {"ticket-gap", "disabled"}
    conf = {
        "zendesk_content_type": "tickets" if tickets else "articles",
        "sync_deleted_files": mode != "disabled",
        "credentials": {"zendesk_subdomain": "synthetic", "zendesk_email": "owner@test", "zendesk_token": "synthetic"},
    }
    await assert_deleted_sync(
        connector_sync_api, monkeypatch, Zendesk, "zendesk", {"retained": "zendesk_ticket_1" if tickets else "article:1", "stale": "zendesk_ticket_2" if tickets else "article:2"}, conf, mode
    )
    paths = [path for path, _ in zendesk_http["requests"]]
    if mode == "ticket-gap":
        assert paths == []
    elif tickets:
        assert "/api/v2/incremental/tickets.json" in paths
        assert "/api/v2/guide/content_tags" not in paths
        assert "/api/v2/tickets/2/comments" not in paths
    else:
        assert "/api/v2/help_center/articles" in paths


def test_zendesk_configuration_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(
        connector_sync_api,
        "zendesk",
        {"sync_deleted_files": False, "zendesk_content_type": "articles", "credentials": {"zendesk_subdomain": "synthetic", "zendesk_email": "owner@test", "zendesk_token": "synthetic"}},
    )
