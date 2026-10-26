"""Actual socket body slow-drip checks with a durably reserved local listener."""

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from uuid import uuid4

import pytest

from common.data_source import microsoft_graph as module
from tests.support.resource_journal import ResourceJournal
from tests.support.teams_onedrive_fakes import CREDS


@pytest.mark.parametrize("event", ["deadline", "cancel"])
@pytest.mark.parametrize("phase", ["body", "headers", "token-discovery", "token-exchange"])
def test_body_slow_drip_observes_absolute_guard(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, event: str, phase: str) -> None:
    journal = ResourceJournal(request.node.nodeid)
    port = 20000 + int(uuid4().hex[:4], 16) % 30000
    thread_name = "graph-drip-" + journal.run_id
    journal.event("reserve-listener-thread", host="127.0.0.1", port=port, thread=thread_name, watchdog="microsoft-graph-request-guard")
    cancelled = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                if phase == "body":
                    self.send_response(200)
                    self.send_header("Content-Length", "80")
                    self.end_headers()
                else:
                    self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                for index in range(80):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    if event == "cancel" and index == 3:
                        cancelled.set()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self) -> None:
            self.do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.handle_request, name=thread_name)
    original = module._send_request
    monkeypatch.setattr(module, "_send_request", lambda session, method, url, **kwargs: original(session, method, f"http://127.0.0.1:{port}/drip", **kwargs))
    deadline = time.monotonic() + 0.2

    class Guard:
        def __call__(self) -> None:
            if event == "deadline" and time.monotonic() >= deadline:
                raise TimeoutError("controlled absolute deadline")
            if cancelled.is_set():
                raise InterruptedError("controlled cancellation")

        def remaining_seconds(self) -> float:
            self()
            return max(0.001, deadline - time.monotonic()) if event == "deadline" else 1.0

    thread.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError if event == "deadline" else InterruptedError):
            connection = module.GraphConnection(CREDS, Guard())
            if phase == "token-discovery":
                # Actual MSAL constructor uses the bounded HTTP client for its
                # first OpenID metadata request; no fake token callback here.
                connection.token()
            elif phase == "token-exchange":

                class App:
                    def __init__(self, **kwargs: Any) -> None:
                        self.http = kwargs["http_client"]

                    def acquire_token_for_client(self, **kwargs: Any) -> dict[str, Any]:
                        return self.http.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token", data={"client_secret": "synthetic"}).json()

                monkeypatch.setattr(module.msal, "ConfidentialClientApplication", App)
                connection.token()
            else:
                connection.read("https://graph.microsoft.com/v1.0/drives/A/items/x/content", headers={"Authorization": "Bearer synthetic"}, content=True)
        elapsed = time.monotonic() - started
        assert elapsed < 0.6
        journal.event("body-drip-stopped", stop_event=event, phase=phase, elapsed=elapsed)
    finally:
        server.server_close()
        thread.join(timeout=2)
        with socket.socket() as probe:
            absent = probe.connect_ex(("127.0.0.1", port)) != 0
        journal.event("listener-thread-cleanup", port=port, absent=absent, thread_stopped=not thread.is_alive())
        assert absent and not thread.is_alive()
        assert not any(item.name == "microsoft-graph-request-guard" for item in threading.enumerate())
