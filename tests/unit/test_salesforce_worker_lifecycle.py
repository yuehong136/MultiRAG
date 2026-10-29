"""Salesforce workers drain cancellation and interrupt owned slow HTTP sockets."""

from __future__ import annotations

import asyncio
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests
from requests.models import PreparedRequest, Response
from sqlalchemy.orm import Session

from api.db.services.connector_service import SyncTaskCancelled
from common.data_source import salesforce_connector as salesforce
from common.data_source.models import Document
from core.svr import sync_data_source as sync
from tests.support.resource_journal import ResourceJournal
from tests.support.salesforce_transport import SalesforceTransport, configuration, install_transport


@pytest.mark.parametrize(
    "reindex,poll_start,expected_start",
    [("0", None, 0.0), ("1", datetime(2026, 1, 1, tzinfo=UTC), 0.0), ("0", datetime(1970, 1, 1, tzinfo=UTC), 0.0), ("0", datetime(2026, 1, 1, tzinfo=UTC), 1767225600.0)],
)
async def test_every_worker_window_has_a_fixed_upper_bound(monkeypatch: pytest.MonkeyPatch, reindex: str, poll_start: datetime | None, expected_start: float) -> None:
    fixed = datetime(2026, 2, 1, 12, 0, 0, 123456, tzinfo=UTC)
    calls: list[tuple[float, float]] = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return fixed

    class Connector:
        def poll_source(self, start: float, end: float) -> Iterator[list[Document]]:
            calls.append((start, end))
            return iter(())

    async def initialize(self: sync.Salesforce, task: dict[str, Any]) -> None:
        self.connector = Connector()

    monkeypatch.setattr(sync, "datetime", Clock)
    monkeypatch.setattr(sync.Salesforce, "_initialize_connector", initialize)
    generator = await sync.Salesforce({})._generate({"reindex": reindex, "poll_range_start": poll_start})
    assert list(generator) == []
    assert calls == [(expected_start, fixed.timestamp())]


async def test_repeated_cancel_drains_controlled_request_before_closing_without_writes(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    journal = ResourceJournal(request.node.nodeid)
    entered, release = threading.Event(), threading.Event()
    writes: list[str] = []
    initial_watchers = set(threading.enumerate())

    class BlockingTransport(SalesforceTransport):
        def send(self, prepared: PreparedRequest, **kwargs: Any) -> Response:
            if urlsplit(prepared.url or "").path.endswith("/query"):
                self.closed = False
                entered.set()
                assert release.wait(timeout=5), "test did not release its controlled query"
            return super().send(prepared, **kwargs)

    @contextmanager
    def database() -> Iterator[Session]:
        with Session() as session:
            yield session

    def no_cancel(*args: Any) -> None:
        return None

    def headers(*args: Any) -> list[Any]:
        return []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        writes.append("duplicate_and_parse")
        raise AssertionError("a cancelled Salesforce request must not persist a document")

    boundary = BlockingTransport()
    journal.event("reserve-controlled-salesforce-transport", objects=["Account"], network="none", thread="salesforce-request-guard")
    install_transport(monkeypatch, boundary)
    monkeypatch.setattr(sync, "db_connection", database)
    monkeypatch.setattr(sync.SyncLogsService, "raise_if_cancelled", no_cancel)
    monkeypatch.setattr(sync.DocumentService, "list_doc_headers_by_kb_and_source_type", headers)
    monkeypatch.setattr(sync.SyncLogsService, "duplicate_and_parse", forbidden)
    worker = sync.Salesforce(configuration())
    task = asyncio.create_task(worker._run_sync_task_logic({"id": "owned-task", "kb_id": "owned-kb", "connector_id": "owned-connector", "poll_range_start": None, "reindex": "0", "timeout_secs": 30}))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 3), timeout=4)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not boundary.closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    assert writes == [] and boundary.closed
    assert worker.connector._session is None and worker.connector._access_token is None
    assert not [thread for thread in threading.enumerate() if thread not in initial_watchers and thread.name == "salesforce-request-guard"]
    journal.event("cancel-drain-readback", document_writes=len(writes), transport_closed=boundary.closed, pending_request_guards=0)


@dataclass
class SlowHTTP:
    origin: str
    entered: threading.Event
    peer_closed: threading.Event
    listener: socket.socket
    accepted: list[socket.socket]
    thread: threading.Thread


@contextmanager
def slow_http(journal: ResourceJournal, mode: str) -> Iterator[SlowHTTP]:
    """Reserve the precise loopback port before binding; never use a cloud URL."""
    listener: socket.socket | None = None
    port = 0
    for _attempt in range(20):
        port = 20000 + secrets.randbelow(40000)
        journal.event("reserve-listener-before-bind", host="127.0.0.1", port=port, mode=mode)
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError as error:
            candidate.close()
            journal.event("listener-reservation-unavailable", host="127.0.0.1", port=port, errno=error.errno, socket_closed=candidate.fileno() == -1)
            continue
        listener = candidate
        break
    assert listener is not None, "could not reserve an owned loopback listener"
    listener.listen(1)
    listener.settimeout(0.1)
    entered, peer_closed, release = threading.Event(), threading.Event(), threading.Event()
    accepted: list[socket.socket] = []
    errors: list[str] = []

    def serve() -> None:
        try:
            while not release.is_set():
                try:
                    connection, peer = listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            accepted.append(connection)
            journal.event("accepted-owned-socket-before-read", host="127.0.0.1", port=port, peer_host=peer[0], peer_port=peer[1])
            connection.settimeout(0.1)
            request_bytes = bytearray()
            while b"\r\n\r\n" not in request_bytes and not release.is_set():
                try:
                    block = connection.recv(4096)
                except TimeoutError:
                    continue
                if not block:
                    peer_closed.set()
                    return
                request_bytes.extend(block)
                assert len(request_bytes) <= 65536, "controlled HTTP request was unexpectedly large"
            if mode == "body":
                connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 99999\r\nConnection: close\r\n\r\n{")
            entered.set()
            # Leave headers, or a declared body, incomplete until the client's
            # production watchdog closes its own socket. The server never
            # releases the client first on the successful cancellation path.
            while not release.is_set():
                try:
                    if not connection.recv(4096):
                        peer_closed.set()
                        return
                except TimeoutError:
                    continue
                except (ConnectionError, OSError):
                    peer_closed.set()
                    return
        except OSError as error:
            if not release.is_set():
                errors.append(type(error).__name__)
        except Exception as error:
            errors.append(type(error).__name__)

    name = "salesforce-owned-slow-" + mode
    journal.event("reserve-server-thread-before-start", name=name, listener_host="127.0.0.1", listener_port=port)
    thread = threading.Thread(target=serve, name=name)
    thread.start()
    try:
        yield SlowHTTP(f"http://127.0.0.1:{port}", entered, peer_closed, listener, accepted, thread)
    finally:
        release.set()
        listener.close()
        for connection in accepted:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        thread.join(timeout=3)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            listener_absent = probe.connect_ex(("127.0.0.1", port)) != 0
        sockets_closed = listener.fileno() == -1 and all(connection.fileno() == -1 for connection in accepted)
        journal.event(
            "slow-http-cleanup-readback", host="127.0.0.1", port=port, listener_absent=listener_absent, sockets_closed=sockets_closed, thread_joined=not thread.is_alive(), server_errors=errors
        )
        assert listener_absent and sockets_closed and not thread.is_alive() and errors == []


@pytest.mark.parametrize("mode", ["headers", "body"])
async def test_watchdog_interrupts_real_slow_http_and_joins_before_return(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, mode: str) -> None:
    journal = ResourceJournal(request.node.nodeid)
    cancelled = threading.Event()
    initial_watchers = set(threading.enumerate())
    trackers: list[Any] = []
    tracker_type = salesforce._SocketTracker

    class Tracker(tracker_type):
        def __init__(self) -> None:
            super().__init__()
            trackers.append(self)

    def check_active() -> None:
        if cancelled.is_set():
            raise SyncTaskCancelled("owned Salesforce HTTP request cancelled")

    monkeypatch.setattr(salesforce, "_SocketTracker", Tracker)
    connector = salesforce.SalesforceConnector.build_connector(configuration(), check_active=check_active)
    with slow_http(journal, mode) as server:

        def send(session: requests.Session, method: str, url: str, **kwargs: Any) -> requests.Response:
            # Production has already validated the Salesforce URL and installed
            # its tracked adapter. Only this network seam changes the endpoint.
            target = urlsplit(url)
            mapped = server.origin + target.path + ("?" + target.query if target.query else "")
            adapter = session.get_adapter(mapped)
            assert isinstance(adapter, salesforce._TrackedAdapter)
            return session.request(method, mapped, **kwargs)

        def run() -> list[Document]:
            return [document for batch in connector.load_from_state() for document in batch]

        monkeypatch.setattr(salesforce, "_send_request", send)
        journal.event(
            "reserve-controlled-request-before-use", origin=server.origin, configured_origin=configuration()["credentials"]["instance_url"], request_guard_name="salesforce-request-guard", mode=mode
        )
        operation = asyncio.create_task(asyncio.to_thread(run))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(server.entered.wait, 3), timeout=4)
            started = time.monotonic()
            cancelled.set()
            with pytest.raises(SyncTaskCancelled):
                await asyncio.wait_for(asyncio.shield(operation), timeout=3)
            elapsed = time.monotonic() - started
            assert elapsed < 3
            assert await asyncio.wait_for(asyncio.to_thread(server.peer_closed.wait, 1), timeout=2)
            assert len(trackers) == 1 and trackers[0].closed
            assert trackers[0].sockets and all(sock.fileno() == -1 for sock in trackers[0].sockets)
            assert connector._session is None and connector._access_token is None
            assert not [thread for thread in threading.enumerate() if thread not in initial_watchers and thread.name == "salesforce-request-guard"]
            journal.event("slow-http-cancel-readback", mode=mode, elapsed_seconds=elapsed, peer_closed=True, tracked_sockets_closed=True, request_guard_joined=True)
        finally:
            cancelled.set()
            # Failure teardown must also drain the to_thread operation before
            # the surrounding listener context performs its independent readback.
            for tracker in trackers:
                tracker.close()
            if not operation.done():
                try:
                    await asyncio.wait_for(asyncio.shield(operation), timeout=3)
                except (SyncTaskCancelled, requests.RequestException):
                    pass
