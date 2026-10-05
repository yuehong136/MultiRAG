"""RSS feed membership pruning and failure readback in isolated real storage."""

import hashlib
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import feedparser
import pytest
import requests

from common.data_source.rss_connector import RSSConnector
from core.svr.sync_data_source import RSS
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def rss_http() -> Iterator[dict[str, Any]]:
    item = '<item><guid isPermaLink="false">retained</guid><title>Retained</title><pubDate>Wed, 01 Jan 2025 00:00:00 GMT</pubDate></item>'
    head = '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel><title>Rolling feed</title><link>https://feed.test</link><description>Recent entries</description>'
    state: dict[str, Any] = {"mode": "window", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state["requests"].append(self.path)
            mode = state["mode"]
            if self.path == "/posts/stale":
                content, status = b"Historical article still exists", 200
            elif mode == "http-error":
                content, status = b"Unavailable", 503
            else:
                body = "" if mode == "empty" else item
                if mode == "paged":
                    body += '<atom:link rel="next" href="/page-2.xml"/>'
                content = (head + body + ("<broken" if mode == "partial" else "</channel></rss>")).encode()
                status = 200
                state["feed"] = content
            self.send_response(status)
            self.send_header("Content-Type", "application/rss+xml")
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


@pytest.mark.parametrize("mode", ["window", "paged", "partial", "empty", "http-error", "disabled"])
async def test_current_feed_membership_controls_deletion(connector_sync_api: dict[str, Any], rss_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    rss_http["mode"] = mode
    # Only permit this loopback fixture; the production SSRF guard stays unchanged.
    monkeypatch.setattr(RSSConnector, "_validate_feed_url", lambda self: ("127.0.0.1", "127.0.0.1"))
    source_ids = {name: f"rss:{hashlib.md5(name.encode()).hexdigest()}" for name in ["retained", "stale"]}
    conf = {"feed_url": rss_http["base"] + "/feed.xml", "sync_deleted_files": mode != "disabled"}
    # Rolling-window eviction intentionally prunes when enabled, even if the article still exists.
    await assert_deleted_sync(
        connector_sync_api, monkeypatch, RSS, "rss", source_ids, conf, "listing-error" if mode in {"empty", "http-error", "partial"} else "disabled" if mode == "disabled" else "complete"
    )
    assert rss_http["requests"] == ["/feed.xml"]
    if mode == "partial":
        parsed = feedparser.parse(rss_http["feed"])
        assert parsed.bozo and len(parsed.entries) == 1
    response = requests.get(rss_http["base"] + "/posts/stale", timeout=5)
    assert response.status_code == 200 and response.content == b"Historical article still exists"


def test_rss_configuration_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(connector_sync_api, "rss", {"feed_url": "https://feed.test/rss", "batch_size": 2})
