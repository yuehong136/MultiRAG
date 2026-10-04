"""Actual status HTTP, PostgreSQL, Milvus and source write acceptance.

Only model/provider output and specifically named failure boundaries are
controlled. Authentication, routes, transactions, index writes and reads are real.
"""

import json
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, Knowledgebase, Task, UserTenant
from api.db.services import document_status_service as status_service
from common import settings
from tests.support.document_parse_retirement import object_snapshot, sql_snapshot
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def status_api(parse_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    with ExitStack() as cleanup:
        env = parse_api
        env.update({name: uuid4().hex for name in ["doc", "zero", "second", "other_kb", "foreign_doc", "task", "second_task"]})
        env["owner_key"] = f"status-{uuid4().hex}"
        owner = env["owners"][0]
        env["record"]["status"] = {}

        def remove_owned() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.delete(APIToken).where(APIToken.token == env["owner_key"]))
                db.execute(sa.delete(Document).where(Document.id == env["foreign_doc"]))
                db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == env["other_kb"]))
                db.execute(sa.delete(UserTenant).where(UserTenant.tenant_id == owner, UserTenant.user_id == env["owners"][1]))
                db.commit()
                assert db.get(Document, env["foreign_doc"]) is None
                assert db.get(Knowledgebase, env["other_kb"]) is None
            with env["engine"].connect() as db:
                remaining = {
                    model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(model.id == identifier))
                    for model, identifier in [(Document, env["foreign_doc"]), (Knowledgebase, env["other_kb"])]
                }
                assert not any(remaining.values())
            env["record"]["status"]["remaining"] = remaining

        cleanup.callback(remove_owned)
        with Session(env["engine"]) as db:
            db.add(APIToken(tenant_id=owner, token=env["owner_key"], name="status scratch"))
            db.add(Knowledgebase(id=env["other_kb"], tenant_id=owner, created_by=owner, name="other_status", embd_id="scratch-embedding", parser_id="naive", parser_config={}))
            for key, kb, count in [("doc", env["kb"], 2), ("zero", env["kb"], 0), ("second", env["kb"], 1), ("foreign_doc", env["other_kb"], 0)]:
                db.add(Document(id=env[key], kb_id=kb, created_by=owner, name=key + ".txt", type="doc", parser_id="naive", parser_config={}, status="1", chunk_num=count, location=key + ".txt"))
            db.add(Task(id=env["task"], doc_id=env["zero"], task_type="Parse"))
            db.add(Task(id=env["second_task"], doc_id=env["second"], task_type="Parse"))
            db.execute(sa.update(Document).where(Document.id.in_([env["zero"], env["second"]])).values(run="1"))
            db.commit()
        rows = [chunk(env, env["doc"], "first"), chunk(env, env["doc"], "second"), chunk(env, env["second"], "sibling")]
        assert settings.docStoreConn.insert(rows, env["collection"], env["kb"]) == []
        env["storage_adapter"].put(env["kb"], "doc.txt", b"Owned original source bytes.")
        env["record"]["status"] = {key: env[key] for key in ["doc", "zero", "second", "other_kb", "foreign_doc", "task", "second_task"]}
        yield env


def chunk(env: dict[str, Any], doc: str, text: str) -> dict[str, Any]:
    identifier = uuid4().hex
    return {
        "id": identifier,
        "pk": identifier,
        "doc_id": doc,
        "kb_id": env["kb"],
        "docnm_kwd": "source.txt",
        "content_with_weight": text,
        "available_int": 1,
        "vector": [0.1] * 768,
        "q_768_vec": [0.1] * 768,
        "create_time": "2026-10-02 10:00:00",
        "create_timestamp_flt": 1790920000.0,
        "tag_kwd": "stable",
    }


def change(env: dict[str, Any], ids: list[str], status: Any, *, key: str | None = None, dataset: str | None = None) -> requests.Response:
    return requests.post(
        f"{env['base']}/api/v1/datasets/{dataset or env['kb']}/documents/batch-update-status",
        headers={"Authorization": f"Bearer {key or env['owner_key']}"},
        json={"doc_ids": ids, "status": status},
        timeout=30,
    )


def sql_rows(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as connection:
        table = Document.__table__
        return {row["id"]: dict(row) for row in connection.execute(sa.select(table).where(table.c.kb_id.in_([env["kb"], env["other_kb"]]))).mappings()}


def index_rows(env: dict[str, Any], doc: str | None = None) -> list[dict[str, Any]]:
    # A separate client with strong consistency, independent of the write wrapper.
    from pymilvus import MilvusClient

    from common.config_utils import CONFIGS

    config = CONFIGS["milvus"]
    client = MilvusClient(uri=config["hosts"], user=config.get("username", ""), password=config.get("password", ""), db_name=config.get("db_name") or "default")
    try:
        return sorted(
            client.query(env["collection"], filter=f'doc_id == "{doc}"' if doc else 'pk != ""', output_fields=["*", "vector", "q_768_vec"], consistency_level="Strong"), key=lambda row: row["pk"]
        )
    finally:
        client.close()


def assert_retired_status_has_no_work(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps import manager
    from core.utils.redis_conn import REDIS_CONN

    before = sql_snapshot(env), index_rows(env), object_snapshot(env), REDIS_CONN.REDIS.xrange(env["queue"])
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("status write")
        raise AssertionError("retired status route executed a write")

    expired = manager.create_access_token(data={"sub": f"{env['owners'][0]}@upload.test"}, expires=timedelta(seconds=-1))
    with monkeypatch.context() as scoped:
        for target, attribute in [
            (status_service, "change_document_status"),
            (status_service, "change_document_status_sync"),
            (settings.docStoreConn, "update"),
            (settings.docStoreConn, "insert"),
            (env["storage_adapter"], "put"),
            (REDIS_CONN, "queue_product"),
        ]:
            scoped.setattr(target, attribute, forbidden)
        for token in [None, env["jwt"], env["owner_key"], env["api_key"], "invalid-token", expired]:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            for body in [None, {"doc_id": env["doc"], "status": 1}, {"doc_ids": [env["doc"]], "status": "1"}, {"doc_ids": env["doc"], "status": 1}, {"doc_ids": [env["doc"]], "status": True}]:
                response = requests.post(env["base"] + "/v1/document/change_status", headers=headers, json=body, timeout=30)
                assert response.status_code == 404
                assert response.json() == {"code": 404, "message": "Not Found: /v1/document/change_status", "data": None, "error": "Not Found"}
    assert calls == []
    after = sql_snapshot(env), index_rows(env), object_snapshot(env), REDIS_CONN.REDIS.xrange(env["queue"])
    assert after == before
    readback = {
        label: {"sql": snapshot[0], "index": snapshot[1], "objects_hex": {key: binary.hex() for key, binary in snapshot[2].items()}, "queue": snapshot[3]}
        for label, snapshot in [("before", before), ("after", after)]
    }
    path = env["record_path"].with_suffix(".legacy-status-readback.json")
    path.write_text(json.dumps(readback, default=str))
    env["record"]["legacy_status_retirement"] = {"requests": 30, "unchanged": True, "execution_calls": calls, "readback": str(path)}
    schema = requests.get(env["base"] + "/openapi.json", timeout=30).json()
    assert "/v1/document/change_status" not in schema["paths"] and "ChangeStatusRequest" not in schema["components"]["schemas"]
    assert "post" in schema["paths"]["/api/v1/datasets/{dataset_id}/documents/batch-update-status"]
