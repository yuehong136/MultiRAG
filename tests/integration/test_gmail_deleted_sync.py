"""Real Gmail client against loopback HTTP, with owned SQL/object/index readback."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httplib2
import pytest
import sqlalchemy as sa
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build_from_document
from googleapiclient.discovery_cache import get_static_doc
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import FileSource, TaskStatus
from common.data_source import gmail_connector as gmail
from core.svr import sync_data_source
from tests.integration.test_document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def gmail_http() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlsplit(self.path).query)
            state["requests"].append({"path": urlsplit(self.path).path, "query": query})
            mode = state["mode"]
            code = 200
            if self.path.split("?")[0].endswith("/threads/new"):
                if mode == "content-error":
                    code, body = 503, {"error": {"message": "controlled read failure"}}
                else:
                    body = {
                        "id": "new",
                        "messages": [
                            {
                                "id": "new-message",
                                "payload": {
                                    "headers": [{"name": "subject", "value": "Controlled Gmail"}, {"name": "date", "value": "Thu, 01 Jan 2026 12:00:00 +0000"}],
                                    "body": {"data": "Y29udHJvbGxlZCBib2R5"},
                                },
                            }
                        ],
                    }
            elif "after:" in query.get("q", [""])[0]:
                body = {"resultSizeEstimate": 0}
                if mode in {"content-error", "ingestion-error"}:
                    body = {"resultSizeEstimate": 1, "threads": [{"id": "new"}]}
            elif mode == "empty":
                body = {"resultSizeEstimate": 0}
            elif "pageToken" not in query:
                body = {"threads": [{"id": "retained"}], "resultSizeEstimate": 1, "nextPageToken": "next"}
            elif mode == "listing-error":
                code, body = 403, {"error": {"message": "controlled later page denial"}}
            elif mode == "malformed":
                body = {}
            else:
                body = {"resultSizeEstimate": 0}
            content = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    discovery = json.loads(get_static_doc("gmail", "v1"))
    discovery["rootUrl"] = f"http://127.0.0.1:{server.server_port}/"
    discovery["servicePath"] = ""
    api = build_from_document(discovery, http=httplib2.Http(timeout=10, proxy_info=None))
    state["api"] = api
    try:
        yield state
    finally:
        api.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "content-error", "ingestion-error"])
async def test_real_gmail_driver_reconciles_only_after_complete_success(parse_api: dict[str, Any], gmail_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = parse_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    source = f"{FileSource.GMAIL}/{connector_id}"
    original = datetime(2025, 12, 1, tzinfo=UTC)
    ids = {name: connector_doc_id_candidates(kb_id, connector_id, name)[-1] for name in ["retained", "stale"]}
    conf = {"sync_deleted_files": True, "credentials": {"google_primary_admin": "user@example.test", "google_tokens": "synthetic"}}
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="Gmail scratch", source=FileSource.GMAIL, input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=original))
        db.commit()
        errors, created = SyncLogsService.duplicate_and_parse(
            db,
            db.get(Knowledgebase, kb_id),
            [{"id": identifier, "semantic_identifier": name, "extension": ".txt", "blob": name.encode()} for name, identifier in ids.items()],
            owner,
            source,
            auto_parse=False,
        )
        assert not errors and set(created) == set(ids.values())
        locations = {doc.id: doc.location for doc in db.scalars(sa.select(Document).where(Document.id.in_(ids.values())))}
    settings.docStoreConn.create_idx(env["collection"], kb_id, 768)
    assert (
        settings.docStoreConn.insert(
            [{"id": uuid4().hex, "doc_id": identifier, "kb_id": kb_id, "content_with_weight": identifier, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768} for identifier in ids.values()],
            env["collection"],
            kb_id,
        )
        == []
    )
    before_objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
    assert len(before_objects) == 2
    gmail_http["mode"] = mode
    monkeypatch.setattr(gmail, "get_google_creds", lambda **kwargs: (Credentials("synthetic"), None))
    monkeypatch.setattr(gmail, "get_gmail_service", lambda *args: gmail_http["api"])
    if mode == "ingestion-error":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion error"], []))
    driver = sync_data_source.Gmail(conf)
    current = {"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
    try:
        await driver(current)
        success = mode in {"complete", "empty"}
        expected = {ids["retained"]} if mode == "complete" else set() if mode == "empty" else set(ids.values())
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == (TaskStatus.DONE if success else TaskStatus.FAIL)
            assert log.docs_removed_from_index == (2 - len(expected) if success else 0)
            if not success:
                assert log.poll_range_start == original
                assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
            assert set(db.scalars(sa.select(Document.id).where(Document.source_type == source))) == expected
            assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == len(expected)
            assert set(db.scalars(sa.select(File2Document.document_id).where(File2Document.document_id.in_(ids.values())))) == expected
        indexed = settings.docStoreConn.query(env["collection"], filter=f'kb_id == "{kb_id}"', output_fields=["doc_id"], consistency_level="Strong")
        assert {row["doc_id"] for row in indexed} == expected
        objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
        assert objects == {name for name in before_objects if any(locations[identifier] in name for identifier in expected)}
        requests = gmail_http["requests"]
        assert requests and "q" not in requests[0]["query"]
        if mode in {"listing-error", "malformed"}:
            assert len(requests) == 2
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()
