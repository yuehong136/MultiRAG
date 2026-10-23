"""Controlled HTTP through the real REST API, sync worker, SQL and MinIO chain."""

import json
import socket
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

from api.db.db_models import Connector, Connector2Kb, Document, DocumentMetadata, SyncLogs
from common.constants import TaskStatus
from common.data_source import rest_api_connector as rest_module
from core.svr.sync_data_source import REST_API
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def vendor(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = connector_sync_api
    state: dict[str, Any] = {"mode": "ok", "calls": [], "method": "GET"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.handle_api()

        def do_POST(self) -> None:
            self.handle_api()

        def handle_api(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["calls"].append({"method": self.command, "path": self.path, "body": raw.decode(), "auth": self.headers.get("X-Controlled-Key")})
            page = int(parse_qs(urlsplit(self.path).query).get("page", [1])[0])
            status = 401 if state["mode"] == "auth" else 200
            if state["mode"] == "failed-page" and page == 2:
                data: Any = {"error": "controlled malformed response"}
            else:
                data = {
                    "data": [
                        {"id": str(page), "title": "Title " + str(page), "body": "body " + str(page), "metadata": {"category": state.get("category", "news")}, "updated": "2026-01-04T08:00:00+08:00"}
                    ],
                    "more": page == 1,
                }
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    state["port"] = port
    env["record"]["rest_vendor"] = {"port": port, "hostname": "rest-api.test", "process": "pytest ThreadingHTTPServer", "connector_ids": [], "task_ids": []}
    env["record_path"].write_text(json.dumps(env["record"]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_guard = rest_module.assert_url_is_safe

    def safe(url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        if parsed.hostname == "rest-api.test" and parsed.port == port:
            return "rest-api.test", "127.0.0.1"
        return original_guard(url)

    monkeypatch.setattr(rest_module, "assert_url_is_safe", safe)
    state["url"] = f"http://rest-api.test:{port}/items"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", port)) != 0
        assert not thread.is_alive()
        env["record"]["rest_vendor"]["listener_closed"] = True


def snapshot(env: dict[str, Any], connector_id: str) -> dict[str, Any]:
    with env["engine"].connect() as db:
        return {
            "connector": dict(db.execute(sa.select(Connector.__table__).where(Connector.id == connector_id)).mappings().one()),
            "logs": [dict(r) for r in db.execute(sa.select(SyncLogs.__table__).where(SyncLogs.connector_id == connector_id)).mappings()],
            "documents": [dict(r) for r in db.execute(sa.select(Document.__table__).where(Document.kb_id == env["kb"])).mappings()],
        }


async def test_saved_test_sync_duplicates_failure_and_denial(connector_sync_api: dict[str, Any], vendor: dict[str, Any]) -> None:
    env = connector_sync_api
    url = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    config = {
        "url": vendor["url"],
        "content_fields": ["title", "body"],
        "metadata_fields": ["metadata.category"],
        "id_field": "id",
        "poll_timestamp_field": "updated",
        "items_path": "$.data[*]",
        "pagination_type": "page",
        "pagination_config": {"page_param": "page", "page_size": 1, "has_next_page_path": "$.more"},
        "batch_size": 1,
        "request_delay": 0,
        "auth_type": "api_key_header",
        "auth_config": {"header_name": "X-Controlled-Key", "api_key_value": "controlled"},
    }
    response = requests.post(url, headers=headers, json={"name": "REST scratch", "source": "rest_api", "config": config}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    connector_id = response.json()["data"]["id"]
    manifest = env["record"]["rest_vendor"]
    manifest["connector_ids"].append(connector_id)
    env["record_path"].write_text(json.dumps(env["record"]))
    start = datetime(2025, 1, 1, tzinfo=UTC)
    try:
        assert vendor["calls"] == []  # Saving is strictly offline.
        assert snapshot(env, connector_id)["connector"]["config"] == config
        before = snapshot(env, connector_id)
        result = requests.post(f"{url}/{connector_id}/test", headers=headers, timeout=30)
        assert result.status_code == 200 and result.json()["retcode"] == 0, result.text
        assert result.json()["data"] == {"message": "Connection successful."}
        assert snapshot(env, connector_id) == before
        assert len(vendor["calls"]) == 1 and vendor["calls"][0]["auth"] == "controlled"
        vendor["mode"] = "auth"
        result = requests.post(f"{url}/{connector_id}/test", headers=headers, timeout=30)
        assert result.json()["retcode"] == 102 and "401" in result.json()["retmsg"]
        assert snapshot(env, connector_id) == before
        vendor["mode"] = "ok"
        from api.apps import manager

        foreign = manager.create_access_token(data={"sub": env["owners"][1] + "@upload.test"})
        vendor["calls"].clear()
        result = requests.post(f"{url}/{connector_id}/test", headers={"Authorization": "Bearer " + foreign}, timeout=30)
        assert result.json()["retcode"] == 109
        assert vendor["calls"] == [] and snapshot(env, connector_id) == before
        with Session(env["engine"]) as db:
            db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=env["kb"], auto_parse="0"))
            db.commit()

        async def run(conf: dict[str, Any], success: bool, full: bool = False) -> dict[str, Any]:
            task_id = uuid4().hex
            manifest["task_ids"].append(task_id)
            env["record_path"].write_text(json.dumps(env["record"]))
            with Session(env["engine"]) as db:
                db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=env["kb"], status=TaskStatus.SCHEDULE, poll_range_start=start))
                db.commit()
            await REST_API(conf)(
                {
                    "id": task_id,
                    "connector_id": connector_id,
                    "kb_id": env["kb"],
                    "tenant_id": env["owners"][0],
                    "poll_range_start": start,
                    "reindex": "1" if full else "0",
                    "auto_parse": False,
                    "timeout_secs": 30,
                }
            )
            with env["engine"].connect() as db:
                row = dict(db.execute(sa.select(SyncLogs.__table__).where(SyncLogs.id == task_id)).mappings().one())
                assert row["status"] == (TaskStatus.DONE if success else TaskStatus.FAIL), row["full_exception_trace"]
                if not success:
                    assert row["poll_range_start"] == start and row["docs_removed_from_index"] == 0
                manifest.setdefault("runs", []).append({"task": task_id, "status": row["status"], "checkpoint": str(row["poll_range_start"]), "errors": row["error_msg"]})
            return snapshot(env, connector_id)

        initial = await run(config, True, full=True)
        assert len(initial["documents"]) == 2
        hashes = {d["id"]: d["content_hash"] for d in initial["documents"]}
        for doc in initial["documents"]:
            number = doc["name"].split()[1].split(".")[0]
            assert doc["source_type"] == "rest_api/" + connector_id
            assert read_object(env["storage"], env["bucket"], f"{env['kb']}/{doc['location']}") == f"Title {number}\nbody {number}".encode()
        with env["engine"].connect() as db:
            metadata = list(db.scalars(sa.select(DocumentMetadata.meta_fields).where(DocumentMetadata.id.in_(hashes))))
            assert len(metadata) == 2 and all(value["metadata.category"] == "news" for value in metadata)
        duplicated = await run(config, True)
        assert {d["id"]: d["content_hash"] for d in duplicated["documents"]} == hashes
        assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == 2
        vendor["mode"], vendor["category"] = "failed-page", "updated"
        await run(config, False)
        vendor["mode"] = "ok"
        recovered = await run(config, True)
        assert len(recovered["documents"]) == 2 and all(d["content_hash"] != hashes[d["id"]] for d in recovered["documents"])
        with env["engine"].connect() as db:
            assert all(value["metadata.category"] == "updated" for value in db.scalars(sa.select(DocumentMetadata.meta_fields).where(DocumentMetadata.id.in_(hashes))))
        vendor["calls"].clear()
        before = snapshot(env, connector_id)
        rejected = await run({**config, "url": "http://127.0.0.1:9/private"}, False)
        assert rejected["documents"] == before["documents"] and vendor["calls"] == []
        # POST body goes through a real socket, not a stubbed fetch_page.
        vendor["calls"].clear()
        Rest = rest_module.RestAPIConnector
        Rest.validate_config({**config, "method": "POST", "request_body": '{"filter":"active"}'})
        assert vendor["calls"][0]["method"] == "POST" and json.loads(vendor["calls"][0]["body"]) == {"filter": "active"}
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id.in_(sa.select(Document.id).where(Document.kb_id == env["kb"]))))
            for model in (SyncLogs, Connector2Kb, Connector):
                column = model.id if model is Connector else model.connector_id
                db.execute(sa.delete(model).where(column == connector_id))
        with env["engine"].connect() as db:
            for model in (SyncLogs, Connector2Kb, Connector):
                column = model.id if model is Connector else model.connector_id
                assert db.scalar(sa.select(sa.func.count()).select_from(model).where(column == connector_id)) == 0
        manifest["connector_rows_removed"] = True
        env["record_path"].write_text(json.dumps(env["record"]))
