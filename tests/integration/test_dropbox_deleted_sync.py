"""Real Dropbox SDK over loopback HTTP and scratch SQL/object/index readback."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from dropbox import Dropbox
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import FileSource, TaskStatus
from core.svr import sync_data_source
from tests.integration.test_document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def dropbox_http(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    def metadata(identity: str = "id:retained", path: str = "/same.txt") -> dict[str, Any]:
        return {
            ".tag": "file",
            "id": identity,
            "name": "same.txt",
            "path_lower": path,
            "path_display": path,
            "client_modified": "2026-02-01T00:00:00Z" if state["mode"] in {"content-error", "ingestion-error"} else "2025-01-01T00:00:00Z",
            "server_modified": "2026-02-01T00:00:00Z",
            "rev": "0123456789",
            "size": 4,
        }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            arg = json.loads(self.headers.get("Dropbox-API-Arg") or self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state["requests"].append({"path": path, "arg": arg})
            mode, status, download = state["mode"], 200, path.endswith("/download")
            body: Any
            if path.endswith("/list_folder"):
                if mode == "empty":
                    body = {"entries": [], "cursor": "done", "has_more": False}
                elif arg["path"] == "":
                    body = {"entries": [metadata(), {".tag": "folder", "id": "id:folder", "name": "folder", "path_lower": "/folder", "path_display": "/folder"}], "cursor": "next", "has_more": True}
                else:
                    body = {"entries": [metadata("id:nested", "/folder/same.txt")], "cursor": "nested-done", "has_more": False}
            elif path.endswith("/list_folder/continue"):
                if mode == "listing-error":
                    status, body = 401, {"error_summary": "invalid_access_token/", "error": {".tag": "invalid_access_token"}}
                elif mode == "malformed":
                    body = {}
                else:
                    body = {"entries": [], "cursor": "done", "has_more": False}
            elif download:
                assert arg["path"] in {"id:retained", "id:nested"}
                if mode == "content-error":
                    status, body = 401, {"error_summary": "invalid_access_token/", "error": {".tag": "invalid_access_token"}}
                else:
                    body = metadata(arg["path"])
            else:
                status, body = 400, {"message": "unexpected route"}
            content = b"body" if download and status == 200 else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("x-dropbox-request-id", "controlled-request")
            if download and status == 200:
                self.send_header("Dropbox-API-Result", json.dumps(body))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["base"] = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(Dropbox, "_get_route_url", lambda self, host, route: f"{state['base']}/2/{route}")
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
async def test_real_dropbox_driver_reconciles_only_after_complete_success(parse_api: dict[str, Any], dropbox_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = parse_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    source = f"{FileSource.DROPBOX}/{connector_id}"
    original = datetime(2025, 12, 1, tzinfo=UTC)
    ids = {name: connector_doc_id_candidates(kb_id, connector_id, f"dropbox:id:{name}")[-1] for name in ["retained", "stale"]}
    conf = {"sync_deleted_files": True, "batch_size": 2, "credentials": {"dropbox_access_token": "synthetic"}}
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="Dropbox scratch", source=FileSource.DROPBOX, input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
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
    dropbox_http["mode"] = mode
    if mode == "ingestion-error":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion error"], []))
    driver = sync_data_source.Dropbox(conf)
    current = {"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
    try:
        await driver(current)
        success = mode in {"complete", "empty"}
        expected = {ids["retained"]} if mode == "complete" else set() if mode == "empty" else set(ids.values())
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == (TaskStatus.DONE if success else TaskStatus.FAIL), (log.full_exception_trace, dropbox_http["requests"]) if log else "missing log"
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
        requests = dropbox_http["requests"]
        assert requests
        assert requests[0]["arg"] == {
            "path": "",
            "recursive": False,
            "include_media_info": False,
            "include_deleted": False,
            "include_has_explicit_shared_members": False,
            "include_mounted_folders": True,
            "include_non_downloadable_files": False,
        }
        downloads = [item for item in requests if item["path"].endswith("/download")]
        assert bool(downloads) == (mode in {"content-error", "ingestion-error"})
        if mode in {"listing-error", "malformed"}:
            assert len(requests) == 3
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()


def test_dropbox_configuration_http_save_and_independent_readback(parse_api: dict[str, Any]) -> None:
    env = parse_api
    base = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    config = {"sync_deleted_files": False, "batch_size": 2, "custom_option": {"keep": True}, "credentials": {"dropbox_access_token": "synthetic"}}
    response = requests.post(base, headers=headers, json={"name": "Dropbox configuration", "source": "dropbox", "config": config}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0
    connector_id = response.json()["data"]["id"]
    try:
        for enabled in [True, False]:
            config = {**config, "sync_deleted_files": enabled}
            response = requests.patch(f"{base}/{connector_id}", headers=headers, json={"config": config}, timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] == 0
            readback = requests.get(f"{base}/{connector_id}", headers=headers, timeout=30)
            assert readback.status_code == 200 and readback.json()["retcode"] == 0
            assert readback.json()["data"]["config"] == config
            with Session(env["engine"]) as db:
                stored = db.get(Connector, connector_id)
                assert stored is not None and stored.config == config
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()
