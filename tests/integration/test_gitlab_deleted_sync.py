"""Real GitLab SDK over loopback HTTP and scratch SQL/object/index readback."""

import base64
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import FileSource, TaskStatus
from core.svr import sync_data_source
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def gitlab_http() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"mode": "complete", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            query = parse_qs(urlsplit(self.path).query)
            state["requests"].append({"path": path, "query": query})
            mode, status, link = state["mode"], 200, None
            body: Any
            if path.endswith("/repository/tree"):
                if mode == "empty":
                    body = []
                elif "page" not in query:
                    body = [{"path": "retained.txt", "name": "retained.txt", "type": "blob"}]
                    link = f'<{state["base"]}{path}?page=2&ref=main>; rel="next"'
                elif mode == "listing-error":
                    status, body = 403, {"message": "controlled later page denial"}
                elif mode == "malformed":
                    body = {"unexpected": "not a tree"}
                else:
                    body = []
            elif "/repository/files/" in path:
                if mode == "content-error":
                    status, body = 403, {"message": "controlled content denial"}
                else:
                    body = {"file_path": "retained.txt", "encoding": "base64", "content": base64.b64encode(b"controlled content").decode()}
            elif path.endswith("/repository/commits"):
                if mode == "timestamp-error":
                    status, body = 403, {"message": "controlled timestamp denial"}
                else:
                    body = [{"id": "commit", "committed_date": "2026-02-01T00:00:00Z" if mode == "ingestion-error" else "2025-01-01T00:00:00Z"}]
            else:
                body = {"id": 1, "default_branch": "main", "empty_repo": False}
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            if link:
                self.send_header("Link", link)
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


@pytest.mark.parametrize("mode", ["complete", "empty", "listing-error", "malformed", "content-error", "timestamp-error", "ingestion-error"])
async def test_real_gitlab_driver_reconciles_only_after_complete_success(parse_api: dict[str, Any], gitlab_http: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = parse_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    source = f"{FileSource.GITLAB}/{connector_id}"
    original = datetime(2025, 12, 1, tzinfo=UTC)
    ids = {name: connector_doc_id_candidates(kb_id, connector_id, f"{gitlab_http['base']}/owner/repo/-/blob/main/{name}.txt")[-1] for name in ["retained", "stale"]}
    conf = {
        "sync_deleted_files": True,
        "project_owner": "owner",
        "project_name": "repo",
        "gitlab_url": gitlab_http["base"],
        "include_code_files": True,
        "credentials": {"gitlab_access_token": "synthetic"},
    }
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="GitLab scratch", source=FileSource.GITLAB, input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
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
    gitlab_http["mode"] = mode
    if mode == "ingestion-error":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion error"], []))
    driver = sync_data_source.Gitlab(conf)
    current = {"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
    try:
        await driver(current)
        success = mode in {"complete", "empty"}
        expected = {ids["retained"]} if mode == "complete" else set() if mode == "empty" else set(ids.values())
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == (TaskStatus.DONE if success else TaskStatus.FAIL), (log.full_exception_trace, gitlab_http["requests"]) if log else "missing log"
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
        requests = gitlab_http["requests"]
        assert requests
        trees = [item for item in requests if item["path"].endswith("/repository/tree")]
        assert trees and trees[0]["query"]["ref"] == ["main"]
        if mode in {"listing-error", "malformed"}:
            assert len(trees) == 2
            assert not any("/repository/files/" in item["path"] for item in requests)
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()


def test_gitlab_configuration_http_save_and_independent_readback(parse_api: dict[str, Any]) -> None:
    env = parse_api
    base = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    config = {
        "sync_deleted_files": False,
        "project_owner": "owner",
        "project_name": "repo",
        "state_filter": "opened",
        "include_mrs": True,
        "include_issues": False,
        "include_code_files": True,
        "custom_option": {"keep": True},
        "credentials": {"gitlab_access_token": "synthetic"},
    }
    response = requests.post(base, headers=headers, json={"name": "GitLab configuration", "source": "gitlab", "config": config}, timeout=30)
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
