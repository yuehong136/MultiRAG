"""Canonical index casing, real task mapping and scoped artifact deletion."""

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
import redis
import sqlalchemy as sa
from pymilvus import DataType, MilvusClient
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Document, File, File2Document, Knowledgebase, PipelineOperationLog, Task
from api.db.services import document_service
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID
from common import settings
from common.config_utils import CONFIGS
from core.nlp import search
from tests.integration.test_dataset_management_http import management_api as management_api
from tests.integration.test_dataset_management_http import request_api, sql_state

CASES = [("graph", "graphrag", "graphrag"), ("raptor", "raptor", "raptor"), ("mindmap", "mindmap", "mindmap")]


@pytest.fixture
def index_api(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = management_api
    name = "index_" + uuid4().hex
    queue = "index-test-" + uuid4().hex
    collections = [search.index_name_one(owner, name) for owner in env["users"][:2]]
    task_ids: list[str] = []
    file_ids = [uuid4().hex for _ in env["documents"]]
    links = [uuid4().hex for _ in env["documents"]]
    record_path = env["record_path"].with_suffix(".index.json")
    record = json.loads(env["record_path"].read_text())
    record.update(index_collections=collections, index_queue=queue, index_tasks=task_ids, index_files=file_ids, index_links=links)
    record_path.write_text(json.dumps(record))

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    def task_id() -> str:
        identifier = uuid4().hex
        task_ids.append(identifier)
        record_path.write_text(json.dumps(record))
        return identifier

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_db)
    monkeypatch.setattr(document_service, "get_uuid", task_id)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    cfg = CONFIGS["milvus"]
    assert settings.docStoreConn.db_type() == "milvus", "This regression requires the local Milvus backend"
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    redis_cfg = CONFIGS["redis"]
    host, port = redis_cfg["host"].rsplit(":", 1)
    redis_reader = redis.Redis(host=host, port=int(port), password=redis_cfg.get("password"), db=redis_cfg.get("db", 1))
    rows: list[dict[str, Any]] = []
    try:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id.in_(env["datasets"])).values(name=name))
            for i, (identifier, link, doc) in enumerate(zip(file_ids, links, env["documents"], strict=True)):
                owner = env["users"][0 if i < 2 else 1]
                db.add(File(id=identifier, parent_id=owner, tenant_id=owner, created_by=owner, name="source.txt", type="txt"))
                db.add(File2Document(id=link, file_id=identifier, document_id=doc))
            db.commit()
        for collection in collections:
            schema = reader.create_schema(auto_id=False)
            schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
            for field in ["kb_id", "doc_id", "knowledge_graph_kwd", "raptor_kwd", "content_with_weight"]:
                schema.add_field(field, DataType.VARCHAR, max_length=512)
            schema.add_field("vector", DataType.FLOAT_VECTOR, dim=2)
            indexes = reader.prepare_index_params()
            indexes.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
            reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong")
        # The sibling deliberately shares the owner's physical index. Every
        # deletion must also restrict kb_id; the foreign dataset has its own index.
        for dataset, doc in zip(env["datasets"], env["documents"], strict=True):
            for kind in ["graph", "subgraph", "entity", "relation", "raptor", "source", "mind_map"]:
                rows.append(
                    {
                        "pk": uuid4().hex,
                        "kb_id": dataset,
                        "doc_id": doc,
                        "knowledge_graph_kwd": kind if kind in {"graph", "subgraph", "entity", "relation", "mind_map"} else "",
                        "raptor_kwd": "raptor" if kind == "raptor" else "",
                        "content_with_weight": kind,
                        "vector": [0.25, 0.75],
                    }
                )
        for i, collection in enumerate(collections):
            selected = env["datasets"][:2] if i == 0 else env["datasets"][2:]
            reader.insert(collection, [row for row in rows if row["kb_id"] in selected])
            reader.flush(collection)
        yield {**env, "reader": reader, "redis": redis_reader, "collections": collections, "queue": queue, "rows": rows}
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(Task).where(Task.id.in_(task_ids)))
            db.execute(sa.delete(File2Document).where(File2Document.id.in_(links)))
            db.execute(sa.delete(File).where(File.id.in_(file_ids)))
        with env["engine"].connect() as db:
            for model, identifiers in [(Task, task_ids), (File2Document, links), (File, file_ids)]:
                assert db.scalar(sa.select(sa.func.count()).select_from(model).where(model.id.in_(identifiers))) == 0
        keys = [queue, *[identifier + "-cancel" for identifier in task_ids]]
        redis_reader.delete(*keys)
        assert not any(redis_reader.exists(key) for key in keys)
        for collection in collections:
            if reader.has_collection(collection):
                reader.drop_collection(collection)
            assert not reader.has_collection(collection)
        reader.close()
        redis_reader.close()
        # management_api subsequently cleans its own SQL rows and listener.
        record.update(index_collections_removed=True, index_redis_removed=True, index_tasks_removed=True)
        record_path.write_text(json.dumps(record))


def native_state(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read complete fixture rows independently of the production adapter."""
    return {row["pk"]: row for collection in env["collections"] for row in env["reader"].query(collection, filter='pk != ""', output_fields=["*"], consistency_level="Strong")}


def task_state(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as db:
        return {row["id"]: dict(row) for row in db.execute(sa.select(Task.__table__)).mappings()}


@pytest.mark.parametrize("credential_kind", ["jwt", "keys"])
def test_index_case_lifecycle_and_independent_readback(index_api: dict[str, Any], credential_kind: str) -> None:
    env = index_api
    token = env[credential_kind][0]
    path = f"/datasets/{env['datasets'][0]}/index"
    original = sql_state(env)
    for canonical, task_type, column in CASES:
        for supplied in [canonical, canonical.upper(), canonical.title()]:
            # Restore only this test's target artifacts for another casing pass.
            target = [row for row in env["rows"] if row["kb_id"] == env["datasets"][0]]
            env["reader"].upsert(env["collections"][0], target)
            before = native_state(env)
            result = request_api(env, "POST", path, credential=token, params={"type": supplied})
            assert result.status_code == 200 and result.json()["code"] == 0, result.text
            task_id = result.json()["data"]["task_id"]
            tasks = task_state(env)
            assert tasks[task_id]["task_type"] == task_type and tasks[task_id]["doc_id"] == GRAPH_RAPTOR_FAKE_DOC_ID
            assert sql_state(env)[Knowledgebase.__tablename__][env["datasets"][0]][column + "_task_id"] == task_id
            queued = json.loads(env["redis"].xrevrange(env["queue"], count=1)[0][1][b"message"])
            assert queued["id"] == task_id and queued["task_type"] == task_type
            assert queued["doc_id"] == GRAPH_RAPTOR_FAKE_DOC_ID and queued["doc_ids"] == [env["documents"][0]]
            for variant in [canonical, canonical.upper(), canonical.title()]:
                trace = request_api(env, "GET", path, credential=token, params={"type": variant})
                assert trace.status_code == 200 and trace.json()["code"] == 0, trace.text
                assert trace.json()["data"]["id"] == task_id and trace.json()["data"]["task_type"] == task_type
            duplicate = request_api(env, "POST", path, credential=token, params={"type": supplied})
            assert duplicate.json()["code"] != 0 and "already running" in duplicate.json()["message"]
            assert task_state(env) == tasks
            with env["engine"].begin() as db:
                db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["datasets"][0]).values({column + "_task_finish_at": datetime(2025, 1, 1)}))
            result = request_api(env, "DELETE", path, credential=token, params={"type": supplied})
            assert result.status_code == 200 and result.json() == {"code": 0, "data": {}}, result.text
            assert env["redis"].get(task_id + "-cancel") == b"x"
            assert task_id not in task_state(env)
            kb = sql_state(env)[Knowledgebase.__tablename__][env["datasets"][0]]
            assert kb[column + "_task_id"] == "" and kb[column + "_task_finish_at"] is None
            assert request_api(env, "GET", path, credential=token, params={"type": supplied}).json() == {"code": 0, "data": {}}
            removed = {
                identifier
                for identifier, row in before.items()
                if row["kb_id"] == env["datasets"][0]
                and ((canonical == "graph" and row["knowledge_graph_kwd"] in {"graph", "subgraph", "entity", "relation"}) or (canonical == "raptor" and row["raptor_kwd"] == "raptor"))
            }
            assert native_state(env) == {identifier: row for identifier, row in before.items() if identifier not in removed}
            state = sql_state(env)
            assert state[Document.__tablename__] == original[Document.__tablename__]
            assert state[PipelineOperationLog.__tablename__] == original[PipelineOperationLog.__tablename__]


def test_index_rejections_do_not_write(index_api: dict[str, Any]) -> None:
    env = index_api
    path = f"/datasets/{env['datasets'][0]}/index"
    before_sql, before_tasks, before_native = sql_state(env), task_state(env), native_state(env)
    for method in ["POST", "GET", "DELETE"]:
        for value in ["", "vector", "GraphRAG", " graph ", "graph\x00"]:
            response = request_api(env, method, path, credential=env["jwt"][0], params={"type": value})
            assert response.status_code == 200 and response.json()["code"] != 0, response.text
            assert "Invalid index type" in response.json()["message"]
        for credential, expected_status in [(None, 401), ("invalid", 401), (env["jwt"][1], 200), (env["keys"][1], 200)]:
            response = request_api(env, method, path, credential=credential, params={"type": "GRAPH"})
            assert response.status_code == expected_status
            if expected_status == 200:
                assert response.json()["code"] != 0 and response.json()["message"] == "No authorization."
    assert sql_state(env) == before_sql and task_state(env) == before_tasks and native_state(env) == before_native
    assert not env["redis"].exists(env["queue"])


def test_index_fixture_listener_smoke(index_api: dict[str, Any]) -> None:
    result = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": index_api["base"]}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
