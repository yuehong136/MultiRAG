"""Real DAV size filtering and failed-prune PostgreSQL commit readback."""

import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import connector_doc_id_candidates
from common.constants import ConnectorTaskType, TaskStatus
from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.webdav_connector import WebDAVConnector
from core.svr import sync_data_source as sync_module
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def size_dav(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    journal = ResourceJournal(request.node.nodeid)
    state: dict[str, Any] = {"entries": {"/docs/equal.txt": "4", "/docs/large.txt": "5"}, "requests": [], "journal": journal}

    def entry(path: str, *, directory: bool = False) -> str:
        resource = "<d:collection/>" if directory else ""
        size = state["entries"].get(path)
        size_property = "" if size is None else f"<d:getcontentlength>{escape(size)}</d:getcontentlength>"
        return (
            f"<d:response><d:href>{escape(path)}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype>{resource}</d:resourcetype>{size_property}"
            "<d:getlastmodified>Wed, 01 Jan 2025 00:00:00 GMT</d:getlastmodified>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )

    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self) -> None:
            state["requests"].append(("PROPFIND", self.path))
            directory = self.path.rstrip("/") == "/docs"
            body = entry(self.path, directory=directory)
            if directory:
                body += "".join(entry(path) for path in state["entries"])
            content = f'<d:multistatus xmlns:d="DAV:">{body}</d:multistatus>'.encode()
            self.send_response(207)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            state["requests"].append(("GET", self.path))
            self.send_response(200)
            self.send_header("Content-Length", "4")
            self.end_headers()
            self.wfile.write(b"body")

        def log_message(self, format: str, *args: Any) -> None:
            pass

    port = int(os.environ["WEBDAV_SIZE_TEST_PORT"]) if os.environ.get("WEBDAV_SIZE_TEST_PORT") else 30000 + int(uuid4().hex[:4], 16) % 20000
    address = ("127.0.0.1", port)
    journal.event("reserve-listener", address=list(address), purpose="controlled-webdav-size-protocol")
    server = ThreadingHTTPServer(address, Handler)
    name = "webdav-size-" + journal.run_id[:12]
    journal.event("reserve-thread", name=name, address=list(address))
    thread = threading.Thread(target=server.serve_forever, name=name, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{port}"
    journal.event("listener-started", address=list(address), thread_ident=thread.ident)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        with socket.socket() as reader:
            absent = reader.connect_ex(address) != 0
        journal.event("listener-independent-readback", address=list(address), absent=absent, thread_absent=not thread.is_alive())
        assert absent and not thread.is_alive()


@pytest.mark.parametrize("mode", ["load", "poll", "slim"])
def test_real_webdav_client_enforces_size_before_get(size_dav: dict[str, Any], mode: str) -> None:
    connector = WebDAVConnector(size_dav["base"], "/docs")
    connector.size_threshold = 4
    connector.load_credentials({"username": "synthetic", "password": "synthetic"})
    assert connector.client is not None
    try:
        if mode == "slim":
            docs = collect_slim_document_snapshot(connector)
        else:
            batches = connector.load_from_state() if mode == "load" else connector.poll_source(datetime(2024, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 1, 1, tzinfo=UTC).timestamp())
            docs = tuple(doc for batch in batches for doc in batch)
            assert docs[0].size_bytes == 4
        assert [doc.id for doc in docs] == [f"webdav:{size_dav['base']}:docs/equal.txt"]
        assert [path for method, path in size_dav["requests"] if method == "GET"] == ([] if mode == "slim" else ["/docs/equal.txt"])
    finally:
        connector.client.http.close()


@pytest.mark.parametrize("size", [None, "-1", "malformed", "true"])
async def test_unknown_dav_size_cannot_prune_existing_sql_documents(bootstrapped_engine: sa.Engine, size_dav: dict[str, Any], monkeypatch: pytest.MonkeyPatch, size: str | None) -> None:
    size_dav["entries"] = {"/docs/retained.txt": size}
    engine, journal = bootstrapped_engine, size_dav["journal"]
    owner, kb_id, connector_id, task_id, link_id = (uuid4().hex for _ in range(5))
    source_id = f"webdav:{size_dav['base']}:docs/retained.txt"
    document_id = connector_doc_id_candidates(kb_id, connector_id, source_id)[-1]
    original = datetime(2025, 12, 1, tzinfo=UTC)
    conf = {"base_url": size_dav["base"], "remote_path": "/docs", "sync_deleted_files": True, "credentials": {"username": "synthetic", "password": "synthetic"}}
    scoped = [
        (SyncLogs, SyncLogs.connector_id == connector_id),
        (Connector2Kb, Connector2Kb.id == link_id),
        (Document, Document.id == document_id),
        (Knowledgebase, Knowledgebase.id == kb_id),
        (Connector, Connector.id == connector_id),
    ]
    journal.event("reserve-sql-rows", database=engine.url.database, connector=connector_id, kb=kb_id, task=task_id, binding=link_id, document=document_id)

    @contextmanager
    def scratch_connection() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    monkeypatch.setattr(sync_module, "db_connection", scratch_connection)
    driver = sync_module.WebDAV(conf)
    try:
        with Session(engine) as db:
            db.add(Connector(id=connector_id, tenant_id=owner, name="WebDAV size scratch", source="webdav", input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
            db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name="WebDAV size scratch", embd_id="unused", doc_num=1, token_num=7, chunk_num=3))
            db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
            db.add(Document(id=document_id, kb_id=kb_id, source_type=f"webdav/{connector_id}", parser_id="naive", type="doc", created_by=owner, name="retained.txt", size=4, token_num=7, chunk_num=3))
            db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, task_type=ConnectorTaskType.PRUNE, status=TaskStatus.SCHEDULE, poll_range_start=original))
            db.commit()
        with engine.connect() as reader:
            before = dict(reader.execute(sa.select(Document.__table__).where(Document.id == document_id)).mappings().one())
        await driver({"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "task_type": ConnectorTaskType.PRUNE, "timeout_secs": 10, "poll_range_start": original})
        with engine.connect() as reader:
            after = dict(reader.execute(sa.select(Document.__table__).where(Document.id == document_id)).mappings().one())
            assert after == before
            log = reader.execute(sa.select(SyncLogs.__table__).where(SyncLogs.id == task_id)).mappings().one()
            assert log["status"] == TaskStatus.FAIL and log["docs_removed_from_index"] == 0
            assert "size metadata" in log["error_msg"] or "invalid literal for int()" in log["error_msg"]
            assert log["poll_range_start"] == original
            assert reader.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
            assert reader.execute(sa.select(Knowledgebase.doc_num, Knowledgebase.token_num, Knowledgebase.chunk_num).where(Knowledgebase.id == kb_id)).one() == (1, 7, 3)
        assert not any(method == "GET" for method, _ in size_dav["requests"])
        journal.event("failed-prune-independent-readback", document_unchanged=True, removed_count=0, cursor_unchanged=True, rescheduled=False)
    finally:
        if hasattr(driver, "connector") and driver.connector.client is not None:
            driver.connector.client.http.close()
        with engine.begin() as writer:
            for model, condition in scoped:
                writer.execute(sa.delete(model).where(condition))
        with engine.connect() as reader:
            absent = not any(reader.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in scoped)
        journal.event("sql-independent-cleanup-readback", database=engine.url.database, absent=absent)
        assert absent
