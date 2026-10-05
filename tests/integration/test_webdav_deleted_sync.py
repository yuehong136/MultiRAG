"""Real WebDAV HTTP protocol, scratch SQL/MinIO/Milvus and API config readback."""

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from core.svr.sync_data_source import WebDAV
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def webdav_http() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    def entry(path: str, directory: bool = False, status: str = "200 OK") -> str:
        resource = "<d:collection/>" if directory else ""
        modified = "Sun, 01 Feb 2026 00:00:00 GMT" if state["mode"] in {"content-error", "ingestion-error"} else "Wed, 01 Jan 2025 00:00:00 GMT"
        size = "" if state["mode"] == "missing-size" and not directory else "<d:getcontentlength>4</d:getcontentlength>"
        return f"<d:response><d:href>{path}</d:href><d:propstat><d:prop><d:resourcetype>{resource}</d:resourcetype>{size}<d:getlastmodified>{modified}</d:getlastmodified></d:prop><d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>"

    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self) -> None:
            state["requests"].append(("PROPFIND", self.path))
            is_file = self.path.endswith(".txt")
            assert self.headers["Depth"] == (None if is_file else "1")
            mode = state["mode"]
            if (mode == "listing-error" and self.path.rstrip("/") == "/docs/sub") or mode == "root-error":
                self.send_response(403)
                self.end_headers()
                return
            body = entry(self.path, directory=not is_file)
            if self.path.rstrip("/") == "/docs" and mode != "empty":
                body += entry("/docs/retained.txt") + entry("/docs/sub/", directory=True)
            if mode == "missing-root":
                body = ""
            elif mode == "propstat-error":
                body += entry("/docs/denied.txt", status="403 Forbidden")
            elif mode == "member-error":
                body += "<d:response><d:href>/docs/denied</d:href><d:status>HTTP/1.1 403 Forbidden</d:status></d:response>"
            elif mode == "outside":
                body += entry("/other/file.txt")
            content = f'<d:multistatus xmlns:d="DAV:">{body}</d:multistatus>'.encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            state["requests"].append(("GET", self.path))
            self.send_response(403 if state["mode"] == "content-error" else 200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"body")

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


@pytest.mark.parametrize(
    "mode", ["complete", "empty", "listing-error", "root-error", "missing-root", "member-error", "propstat-error", "missing-size", "outside", "content-error", "ingestion-error", "disabled"]
)
async def test_webdav_real_resources(connector_sync_api: dict[str, Any], webdav_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    webdav_http["mode"] = mode
    conf = {"base_url": webdav_http["base"], "remote_path": "/docs", "batch_size": "1", "sync_deleted_files": mode != "disabled", "credentials": {"username": "synthetic", "password": "synthetic"}}
    source_ids = {name: f"webdav:{webdav_http['base']}:docs/{name}.txt" for name in ["retained", "stale"]}
    await assert_deleted_sync(connector_sync_api, monkeypatch, WebDAV, "webdav", source_ids, conf, mode)
    assert any(method == "GET" for method, _ in webdav_http["requests"]) == (mode in {"content-error", "ingestion-error"})


def test_webdav_config_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(
        connector_sync_api, "webdav", {"base_url": "https://dav.test", "remote_path": "/docs", "allow_images": True, "batch_size": 2, "credentials": {"username": "synthetic", "password": "synthetic"}}
    )
