"""Real Chromium redirect/navigation/resource blocking with a controlled origin."""

import socket
import threading
from collections import Counter
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import pytest
import requests

from tests.integration.test_runtime_document_upload import read_object, runtime_upload_api  # noqa: F401


@pytest.fixture
def crawl_origins(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    hits: Counter[str] = Counter()

    class Trap(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits["private"] += 1
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"private data")

        def log_message(self, *_args: object) -> None:
            pass

    trap = ThreadingHTTPServer(("127.0.0.1", 0), Trap)
    private = f"http://127.0.0.1:{trap.server_port}"

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits[self.path] += 1
            if self.path == "/race" and hits[self.path] > 1:
                self.send_response(302)
                self.send_header("Location", f"{private}/redirect")
                self.end_headers()
                return
            html = "<html><body><p>Public crawl content from the controlled test origin. " + "This paragraph is safe and readable. " * 20 + "</p>"
            if self.path == "/navigation":
                html += f'<script>location.href = "{private}/javascript";</script>'
            if self.path == "/resources":
                html += f'<img src="{private}/image"><iframe src="{private}/frame"></iframe><script>fetch("{private}/fetch").catch(()=>{{}}); new WebSocket("ws://127.0.0.1:{trap.server_port}/socket");</script>'
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write((html + "</body></html>").encode())

        def log_message(self, *_args: object) -> None:
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in [trap, origin]]
    for thread in threads:
        thread.start()
    getaddrinfo = socket.getaddrinfo
    send = requests.Session.request

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        if host == "upload-public.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", int(port or 0)))]
        return getaddrinfo(host, port, *args, **kwargs)

    def controlled_transport(session: requests.Session, method: str, url: str, *args: Any, **kwargs: Any) -> requests.Response:
        # Only the test origin's already-validated HTTP transport is relocated.
        # Private targets still hit the real guard and would reach Trap on bypass.
        if urlparse(url).hostname == "upload-public.test":
            url = f"http://127.0.0.1:{origin.server_port}{urlparse(url).path}"
        return send(session, method, url, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(requests.Session, "request", controlled_transport)
    try:
        yield {"hits": hits, "public": "http://upload-public.test"}
    finally:
        for server, thread in zip([trap, origin], threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_browser_rechecks_changed_redirect_and_all_network_targets(runtime_upload_api: dict[str, Any], crawl_origins: dict[str, Any]) -> None:
    env = runtime_upload_api
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    endpoint = f"{env['base']}/api/v1/documents/upload"
    for path in ["/race", "/navigation", "/resources"]:
        response = requests.post(endpoint, headers=headers, params={"url": crawl_origins["public"] + path}, timeout=90)
        assert response.status_code == 200 and response.json()["code"] == 101, response.json()
        assert "data" not in response.json()
        assert crawl_origins["hits"][path] >= 2  # preflight 200, then the real browser relay
        assert crawl_origins["hits"]["private"] == 0
    assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
    body = requests.post(endpoint, headers=headers, params={"url": crawl_origins["public"] + "/public"}, timeout=90).json()
    assert body["code"] == 0 and isinstance(body["data"], dict), body
    descriptor = body["data"]
    binary = read_object(env["storage"], env["bucket"], f"{descriptor['created_by']}-downloads/{descriptor['id']}")
    assert binary.startswith(b"%PDF") and descriptor["mime_type"] == "application/pdf" and descriptor["size"] == len(binary)
    assert crawl_origins["hits"]["private"] == 0
    print("real Chromium: preflight-200/browser-private-302, JS navigation, iframe/image/fetch/WebSocket blocked before private request; controlled public PDF succeeded")
