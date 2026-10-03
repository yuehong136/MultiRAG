"""Owned HTTP/SQL/Redis/Milvus/MinIO ingest and producer generation regression.

Parser/model output and explicitly named fault boundaries are controlled. Routes,
authentication, locks, queue writes and independent storage reads are real.
"""

import asyncio
import copy
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, File, File2Document, Knowledgebase, PipelineOperationLog, SourceRecoveryRecord, Task, UserCanvas, UserTenant
from api.db.services import document_ingest_service as service
from api.db.services import document_status_service as status_service
from api.db.services.document_ingest_recovery import recovery_key
from api.db.services.document_service import DocumentService
from api.db.services.document_source_recovery import SourceRecovery, source_recovery_key
from api.db.services.document_status_service import insert_source_chunks
from api.db.services.document_task_service import SupersededDocumentTask, cleanup_task_chunks, increment_task_document, put_task_image, token_digest, write_task_metadata
from api.db.services.task_service import TaskService, prepare_parse_tasks
from common import settings
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN
from tests.integration.test_document_parse_retirement import object_snapshot, sql_snapshot
from tests.integration.test_document_parse_retirement import parse_api as parse_api
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api


def evidence(env: dict[str, Any], label: str, value: Any) -> None:
    target = env["record_path"].with_suffix(f".ingest-{label}.json")
    with env.get("evidence_lock", threading.RLock()):
        target.write_text(json.dumps(value, default=str))


@pytest.fixture
def ingest_api(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = parse_api
    env["evidence_lock"] = threading.RLock()
    env.update({key: uuid4().hex for key in ["a", "b", "c", "foreign", "other_kb", "canvas"]})
    env["other_collection"] = search.index_name_one(env["owners"][1], "ingest_" + env["other_kb"])
    env["ingest_manifest"] = {
        "run_id": uuid4().hex,
        "database": env["engine"].url.database,
        "document_ids": [env[key] for key in ["a", "b", "c", "foreign"]],
        "task_ids": [],
        "file_ids": [],
        "link_ids": [],
        "canvas_id": env["canvas"],
        "extra_dataset": env["other_kb"],
        "collections": [env["collection"], env["other_collection"]],
        "redis_keys": [env["queue"], *(recovery_key(env[key]) for key in ["a", "b", "c", "foreign"])],
        "objects": [],
        "http_port": env["record"]["port"],
    }
    evidence(env, "manifest", env["ingest_manifest"])
    env["ingest_manifest"]["listener"] = {"pid": os.getpid(), "command": "pytest with in-process uvicorn scratch listener", "port": env["record"]["port"], "recorded_before_ingest_creation": True}
    evidence(env, "manifest", env["ingest_manifest"])
    with Session(env["engine"]) as db:
        db.add(
            Knowledgebase(
                id=env["other_kb"], tenant_id=env["owners"][1], created_by=env["owners"][1], name="ingest_" + env["other_kb"], parser_id="naive", parser_config={}, embd_id="scratch-embedding"
            )
        )
        kb = db.get(Knowledgebase, env["kb"])
        assert kb is not None
        kb.parser_config = {"llm_id": "kb-chat", "enable_metadata": True, "metadata": {"origin": "kb"}}
        db.add(UserCanvas(id=env["canvas"], user_id=env["owners"][0], title="Ingest pipeline", dsl={"components": {}, "path": []}))
        for key in ["a", "b", "c", "foreign"]:
            doc_id = env[key]
            dataset = env["other_kb"] if key == "foreign" else env["kb"]
            owner = env["owners"][1] if key == "foreign" else env["owners"][0]
            file_id, link_id = uuid4().hex, uuid4().hex
            env["ingest_manifest"]["file_ids"].append(file_id)
            env["ingest_manifest"]["link_ids"].append(link_id)
            env["ingest_manifest"]["objects"].append(f"{dataset}/{key}.txt")
            evidence(env, "manifest", env["ingest_manifest"])
            db.add(
                Document(
                    id=doc_id,
                    kb_id=dataset,
                    created_by=owner,
                    name=key + ".txt",
                    location=key + ".txt",
                    type="doc",
                    parser_id="naive",
                    parser_config={
                        "llm_id": "doc-chat",
                        "enable_metadata": False,
                        "metadata": {"origin": "doc"},
                        "raptor": {"use_raptor": False},
                        "graphrag": {"use_graphrag": False},
                        "custom": "preserve",
                    },
                    status="0" if key == "a" else "1",
                )
            )
            db.add(File(id=file_id, parent_id=dataset, tenant_id=owner, created_by=owner, name=key + ".txt", location=key + ".txt", type="doc"))
            db.add(File2Document(id=link_id, file_id=file_id, document_id=doc_id))
            env["storage_adapter"].put(dataset, key + ".txt", ("Original ingest " + key).encode())
        db.commit()

    def record_tasks(db: Session, *args: Any) -> None:
        for row in db.new:
            if isinstance(row, Task) and row.doc_id in env["ingest_manifest"]["document_ids"]:
                manifest = env["ingest_manifest"]
                if row.id not in manifest["task_ids"]:
                    manifest["task_ids"].append(row.id)
                    manifest["redis_keys"].append(row.id + "-cancel")
                    evidence(env, "manifest", manifest)

    sa.event.listen(Session, "before_flush", record_tasks)
    original_prepare = status_service.prepare_source_recovery

    def register_source_material(bind: Any, document_id: str, task_id: str, **data: Any) -> SourceRecovery:
        assert document_id in env["ingest_manifest"]["document_ids"]
        key = source_recovery_key(document_id, task_id)
        if key not in env["ingest_manifest"]["redis_keys"]:
            env["ingest_manifest"]["redis_keys"].append(key)
            evidence(env, "manifest", env["ingest_manifest"])
        return original_prepare(bind, document_id, task_id, **data)

    monkeypatch.setattr(status_service, "prepare_source_recovery", register_source_material)

    def record_recovery_owner(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if "advisory_xact_lock" in statement:
            with env["evidence_lock"]:
                env["ingest_manifest"].setdefault("advisory_locks", []).append(parameters["key"])
                evidence(env, "manifest", env["ingest_manifest"])

    sa.event.listen(env["engine"], "before_cursor_execute", record_recovery_owner)
    try:
        yield env
    finally:
        sa.event.remove(Session, "before_flush", record_tasks)
        sa.event.remove(env["engine"], "before_cursor_execute", record_recovery_owner)
        with env["engine"].connect() as db:
            task_ids = list(db.scalars(sa.select(Task.id).where(Task.doc_id.in_(env["ingest_manifest"]["document_ids"]))))
        manifest = env["ingest_manifest"]
        manifest["task_ids"] = list(dict.fromkeys([*manifest["task_ids"], *task_ids]))
        keys = [*manifest["redis_keys"], *(f"{identifier}-cancel" for identifier in manifest["task_ids"])]
        keys = list(dict.fromkeys(key for key in keys if key != env["queue"]))
        manifest["redis_keys"] = list(dict.fromkeys([*manifest["redis_keys"], *keys]))
        evidence(env, "manifest", manifest)
        evidence(env, "final", snapshot(env))
        with env["engine"].connect() as db:
            source_rows = [dict(row) for row in db.execute(sa.select(SourceRecoveryRecord.__table__).where(SourceRecoveryRecord.document_id.in_(manifest["document_ids"]))).mappings()]
        source_states = {}
        for key in keys:
            if key.startswith("document-source-recovery:"):
                raw = REDIS_CONN.REDIS.get(key)
                dump = REDIS_CONN.REDIS.dump(key)
                source_states[key] = {"value": raw, "dump_hex": dump.hex() if dump is not None else None, "pttl": REDIS_CONN.REDIS.pttl(key), "absent": raw is None}
        evidence(env, "source-recovery-cleanup-before", {"rows": source_rows, "redis_states": source_states})
        REDIS_CONN.REDIS.delete(*keys) if keys else None
        settings.docStoreConn.delete_idx(env["other_collection"], env["other_kb"])
        with Session(env["engine"]) as db:
            for model, condition in [
                (SourceRecoveryRecord, SourceRecoveryRecord.document_id.in_(manifest["document_ids"])),
                (DocumentMetadata, DocumentMetadata.id.in_(manifest["document_ids"])),
                (PipelineOperationLog, PipelineOperationLog.document_id.in_(manifest["document_ids"])),
                (Task, Task.doc_id == env["foreign"]),
                (File2Document, File2Document.id.in_(manifest["link_ids"])),
                (File, File.id.in_(manifest["file_ids"])),
                (Document, Document.id == env["foreign"]),
                (UserCanvas, UserCanvas.id == env["canvas"]),
                (UserTenant, UserTenant.tenant_id.in_([env["kb"], *env["owners"]]) & UserTenant.user_id.in_(env["owners"])),
                (Knowledgebase, Knowledgebase.id == env["other_kb"]),
            ]:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        assert not settings.docStoreConn.has_collection(env["other_collection"])
        assert not any(REDIS_CONN.REDIS.exists(key) for key in keys)
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(SourceRecoveryRecord).where(SourceRecoveryRecord.document_id.in_(manifest["document_ids"]))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(UserCanvas).where(UserCanvas.id == env["canvas"])) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id.in_(manifest["link_ids"]))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id.in_(manifest["file_ids"]))) == 0
            for key in manifest.get("advisory_locks", []):
                unsigned = key & ((1 << 64) - 1)
                assert (
                    db.scalar(
                        sa.text(
                            "SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND classid=:high AND objid=:low AND objsubid=1 AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
                        ),
                        {"high": unsigned >> 32, "low": unsigned & ((1 << 32) - 1)},
                    )
                    == 0
                )
        manifest["advisory_locks_remaining"] = 0
        manifest["source_recovery_remaining"] = 0
        manifest["source_recovery_keys_removed"] = True
        manifest["extra_resources_removed"] = True
        evidence(env, "manifest", manifest)


def snapshot(env: dict[str, Any]) -> dict[str, Any]:
    indexes = {}
    for name in ["collection", "other_collection"]:
        indexes[name] = index_snapshot({**env, "collection": env[name]})
    with env["engine"].connect() as db:
        extra = {model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__)).mappings()] for model in [UserCanvas, PipelineOperationLog, DocumentMetadata]}
    # Failed SQL attempts also register potential keys in the manifest. Only
    # existing Redis values belong to the state; absence is audited at cleanup.
    flags = {key: value for key in env["ingest_manifest"]["redis_keys"] if key != env["queue"] and (value := REDIS_CONN.REDIS.get(key)) is not None}
    return {
        "sql": sql_snapshot(env),
        "extra_sql": extra,
        "index": indexes,
        "objects": {key: binary.hex() for key, binary in object_snapshot(env).items()},
        "queue": REDIS_CONN.REDIS.xrange(env["queue"]),
        "flags": flags,
    }


def index_snapshot(env: dict[str, Any]) -> list[dict[str, Any]]:
    from pymilvus import MilvusClient

    from common.config_utils import CONFIGS

    config = CONFIGS["milvus"]
    client = MilvusClient(uri=config["hosts"], user=config.get("username", ""), password=config.get("password", ""), db_name=config.get("db_name") or "default")
    try:
        if not client.has_collection(env["collection"]):
            return []
        # Include empty primary keys so a producer bug cannot disappear from
        # acceptance. These scratch collections contain at most a few rows.
        rows = client.query(env["collection"], filter="", limit=16384, output_fields=["*", "vector", "q_768_vec"], consistency_level="Strong")
        assert len(rows) < 16384
        return sorted(rows, key=lambda row: row["pk"])
    finally:
        client.close()


def post(env: dict[str, Any], ids: list[str], run: Any = 1, *, path: str = "/api/v1/documents/ingest", token: str | None = None, **options: Any) -> dict[str, Any]:
    response = requests.post(env["base"] + path, headers={"Authorization": "Bearer " + (token or env["jwt"])}, json={"doc_ids": ids, "run": run, **options}, timeout=30)
    body = response.json()
    evidence(env, "last-http", {"status": response.status_code, "body": body})
    with env["evidence_lock"]:
        history = env.setdefault("http_records", [])
        history.append({"path": path, "doc_ids": ids, "run": run, **options, "status": response.status_code, "body": body})
        evidence(env, "http-log", history)
    return body


def tasks(env: dict[str, Any], doc_id: str) -> list[dict[str, Any]]:
    with env["engine"].connect() as db:
        rows = [dict(row) for row in db.execute(sa.select(Task.__table__).where(Task.doc_id == doc_id).order_by(Task.id)).mappings()]
    manifest = env["ingest_manifest"]
    manifest["task_ids"] = list(dict.fromkeys([*manifest["task_ids"], *(row["id"] for row in rows)]))
    manifest["redis_keys"] = list(dict.fromkeys([*manifest["redis_keys"], *(f"{row['id']}-cancel" for row in rows)]))
    evidence(env, "manifest", manifest)
    return rows


def source(env: dict[str, Any], doc_id: str, *, identifier: str | None = None, text: str = "Historical content", mother: str = "") -> dict[str, Any]:
    chunk_id = identifier or uuid4().hex
    return {
        "pk": chunk_id,
        "id": chunk_id,
        "doc_id": doc_id,
        "kb_id": env["kb"],
        "content_with_weight": text,
        "docnm_kwd": "source.txt",
        "available_int": 0 if doc_id == env["a"] else 1,
        "vector": [0.2] * 768,
        "q_768_vec": [0.2] * 768,
        "create_timestamp_flt": 1790920000.0,
        "create_time": "2026-10-02 10:00:00",
        "mom_id": mother,
    }


def completed(env: dict[str, Any], doc_id: str) -> dict[str, Any]:
    row = source(env, doc_id)
    assert settings.docStoreConn.insert([row], env["collection"], env["kb"]) == []
    with Session(env["engine"]) as db:
        doc = db.get(Document, doc_id)
        assert doc is not None
        plan, _ = prepare_parse_tasks(db, doc.to_dict(), doc.kb_id, doc.location, 0)
        assert len(plan) == 1
        task = plan[0]
        task.update(progress=1, chunk_ids=row["id"], progress_msg="Completed", digest=token_digest(task["digest"], 11))
        db.add(Task(**task))
        doc.chunk_num, doc.token_num, doc.run = 1, 11, "3"
        kb = db.get(Knowledgebase, doc.kb_id)
        assert kb is not None
        kb.chunk_num += 1
        kb.token_num += 11
        db.commit()
    tasks(env, doc_id)
    return row


def test_http_keep_clear_apply_reset_cancel_and_canonical_contracts(ingest_api: dict[str, Any]) -> None:
    env = ingest_api
    completed(env, env["a"])
    before = snapshot(env)
    assert post(env, [env["a"], env["a"]]) == {"code": 0, "message": "success", "data": True}
    reused = tasks(env, env["a"])
    after = snapshot(env)
    assert len(reused) == 1 and reused[0]["progress"] == 1
    assert after["index"] == before["index"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    assert post(env, [env["a"]], 2)["code"] == 102
    assert post(env, [env["a"]], 0)["code"] == 0
    assert snapshot(env)["index"] == before["index"]
    assert post(env, [env["a"]], 1, delete=True, apply_kb=True)["code"] == 0
    queued = tasks(env, env["a"])
    assert len(queued) == 1 and queued[0]["progress"] == 0
    assert index_snapshot(env) == []
    with Session(env["engine"]) as db:
        doc, kb = db.get(Document, env["a"]), db.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        assert (doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num, doc.status) == (0, 0, 0, 0, "0")
        assert doc.parser_config == {**before["sql"][Document.__tablename__][0]["parser_config"], "llm_id": "kb-chat", "enable_metadata": True, "metadata": {"origin": "kb"}}
    row = source(env, env["a"], text="Disabled partial source")
    insert_source_chunks(env["engine"], [row], env["collection"], env["kb"], queued[0]["id"])
    increment_task_document(env["engine"], queued[0]["id"], env["a"], env["kb"], 7, 1, 0)
    increment_task_document(env["engine"], queued[0]["id"], env["a"], env["kb"], 7, 1, 0)
    partial = snapshot(env)
    assert post(env, [env["a"]], 2)["code"] == 0
    assert snapshot(env)["index"] == partial["index"]
    assert REDIS_CONN.REDIS.get(queued[0]["id"] + "-cancel")
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["a"])
        assert doc is not None and (doc.chunk_num, doc.token_num) == (1, 7)
    assert post(env, [env["a"]], 2, delete=True)["code"] == 0
    assert not tasks(env, env["a"]) and index_snapshot(env) == []
    assert post(env, [env["a"]], 0, delete=True)["code"] == 0
    assert not tasks(env, env["a"]) and index_snapshot(env) == []
    assert post(env, [env["b"]], 1, path="/v1/document/run")["code"] == 0
    tasks(env, env["b"])
    for operation in ["stop", "parse"]:
        response = requests.post(f"{env['base']}/api/v1/datasets/{env['kb']}/documents/{operation}", headers={"Authorization": f"Bearer {env['jwt']}"}, json={"document_ids": [env["b"]]}, timeout=30)
        assert response.status_code == 200 and response.json()["code"] == 0
        assert response.json()["data"]["success_count"] == 1
        tasks(env, env["b"])
    schema = requests.get(env["base"] + "/openapi.json", timeout=30).json()
    assert schema["paths"]["/v1/document/run"]["post"]["deprecated"] is True
    evidence(env, "keep-clear", {"before": before, "after_reuse": after, "partial": partial, "final": snapshot(env)})
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    evidence(env, "smoke", {"exit": smoke.returncode, "stdout": smoke.stdout, "stderr": smoke.stderr})
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


@pytest.mark.parametrize("role,active,allowed", [("admin", "1", True), ("owner", "1", True), ("normal", "1", False), ("invite", "1", False), ("admin", "0", False), (None, "1", False)])
def test_real_auth_membership_and_global_selection(ingest_api: dict[str, Any], role: str | None, active: str, allowed: bool) -> None:
    env = ingest_api
    if role:
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][0], tenant_id=env["owners"][1], role=role, status=active, invited_by=env["owners"][1]))
            db.commit()
    before = snapshot(env)
    result = post(env, [env["b"], env["foreign"]])
    assert result["code"] == (0 if allowed else 109)
    if allowed:
        assert len(tasks(env, env["b"])) == len(tasks(env, env["foreign"])) == 1
    else:
        assert snapshot(env) == before
    evidence(env, "auth", {"before": before, "response": result, "after": snapshot(env)})


def test_actual_auth_validation_and_preflight_zero_effects(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps import manager

    env = ingest_api
    before = snapshot(env)
    expired = manager.create_access_token(data={"sub": f"{env['owners'][0]}@upload.test"}, expires=timedelta(seconds=-1))
    for token in ["invalid", expired]:
        response = requests.post(env["base"] + "/api/v1/documents/ingest", headers={"Authorization": "Bearer " + token}, json={"doc_ids": [env["b"]], "run": 1}, timeout=30)
        assert response.status_code == 401
    missing = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["b"]], "run": 1}, timeout=30)
    assert missing.status_code == 401
    assert post(env, [env["b"], uuid4().hex])["code"] == 109
    assert snapshot(env) == before
    for payload in [
        {"doc_ids": [], "run": 1},
        {"doc_ids": [env["b"]], "run": True},
        {"doc_ids": [env["b"]], "run": 1.0},
        {"doc_ids": [1], "run": 1},
        {"doc_ids": [" "], "run": 1},
        {"doc_ids": [env["b"]]},
        {"doc_ids": [env["b"]], "run": 0, "apply_kb": True},
        {"doc_ids": [env["b"]], "run": 1, "delete": "true"},
        {"doc_ids": [env["b"]], "run": 1, "unknown": True},
    ]:
        response = requests.post(env["base"] + "/api/v1/documents/ingest", headers={"Authorization": "Bearer " + env["jwt"]}, json=payload, timeout=30)
        assert response.status_code == 422
    assert snapshot(env) == before
    with monkeypatch.context() as scope:
        scope.setenv("DISABLE_SDK", "1")
        response = requests.post(env["base"] + "/api/v1/documents/ingest", headers={"Authorization": "Bearer " + env["api_key"]}, json={"doc_ids": [env["foreign"]], "run": 1}, timeout=30)
        assert response.status_code == 401
    assert post(env, [env["foreign"]], token=env["api_key"])["code"] == 0
    tasks(env, env["foreign"])
    evidence(env, "preflight", {"before": before, "after": snapshot(env)})


@pytest.mark.parametrize("fault", ["false", "zero", "exception", "sql_before", "sql_after", "restore_failure"])
def test_clear_fault_preserves_full_history_and_atomic_ledgers(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = ingest_api
    completed(env, env["a"])
    before = snapshot(env)
    original_delete = settings.docStoreConn.delete
    original_commit = Session.commit
    attempts = 0

    def failed_delete(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1 or fault == "restore_failure":
            if fault in {"exception", "restore_failure"}:
                original_delete(*args, **kwargs)
                raise RuntimeError("Controlled store deletion failure")
            return False if fault == "false" else 0
        return original_delete(*args, **kwargs)

    def failed_commit(db: Session) -> None:
        nonlocal attempts
        if db.info.get("ingest_fault"):
            attempts += 1
            if attempts == 1:
                if fault == "sql_after":
                    original_commit(db)
                raise RuntimeError("Controlled SQL commit failure")
        original_commit(db)

    from contextlib import contextmanager

    original_connection = service.db_connection

    @contextmanager
    def connection() -> Iterator[Session]:
        with original_connection() as db:
            db.info["ingest_fault"] = True
            yield db

    with monkeypatch.context() as scope:
        if fault.startswith("sql_"):
            scope.setattr(service, "db_connection", connection)
            scope.setattr(Session, "commit", failed_commit)
        else:
            scope.setattr(settings.docStoreConn, "delete", failed_delete)
        body = post(env, [env["a"]], delete=True)
    assert body["code"] == 500 and set(body["data"]["results"]) == {env["a"]}
    after = snapshot(env)
    assert after["sql"] == before["sql"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    if fault != "restore_failure":
        assert after["index"] == before["index"]
        assert post(env, [env["a"]], delete=True)["code"] == 0
        tasks(env, env["a"])
    else:
        assert "recovery could not" in body["data"]["results"][env["a"]]["error"]
        assert REDIS_CONN.REDIS.exists(recovery_key(env["a"]))
        assert post(env, [env["a"]], 0)["code"] == 0
        assert snapshot(env)["index"] == before["index"]
        assert not REDIS_CONN.REDIS.exists(recovery_key(env["a"]))
        with Session(env["engine"]) as db:
            doc, kb = db.get(Document, env["a"]), db.get(Knowledgebase, env["kb"])
            assert doc is not None and kb is not None and (doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == (1, 11, 1, 11)
    evidence(env, "clear-fault", {"fault": fault, "before": before, "response": body, "after": after})


@pytest.mark.parametrize("fault", ["false", "exception", "accepted_then_exception", "readback_failure"])
def test_real_enqueue_ack_and_partial_results(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = ingest_api
    original = REDIS_CONN.REDIS.xadd
    original_range = REDIS_CONN.REDIS.xrange
    attempts = 0

    def enqueue(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            if fault in {"accepted_then_exception", "readback_failure"}:
                original(*args, **kwargs)
            if fault == "false":
                return False
            raise ConnectionError("Controlled enqueue transport failure")
        return original(*args, **kwargs)

    def readback(*args: Any, **kwargs: Any) -> Any:
        if attempts == 2 and fault == "readback_failure":
            raise ConnectionError("Controlled readback failure")
        return original_range(*args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(REDIS_CONN.REDIS, "xadd", enqueue)
        scope.setattr(REDIS_CONN.REDIS, "xrange", readback)
        body = post(env, [env["b"], env["c"]])
    assert body["code"] == (0 if fault == "accepted_then_exception" else 500)
    first, second = tasks(env, env["b"]), tasks(env, env["c"])
    assert len(first) == 1
    queued = [json.loads(fields.get("message", fields.get(b"message"))) for _, fields in REDIS_CONN.REDIS.xrange(env["queue"])]
    assert queued[0]["id"] == first[0]["id"]
    if fault in {"readback_failure", "accepted_then_exception"}:
        assert len(second) == 1 and queued[1]["id"] == second[0]["id"]
    else:
        assert second == [] and len(queued) == 1
    if body["code"]:
        assert set(body["data"]["results"]) == {env["b"], env["c"]}
        assert body["data"]["results"][env["b"]] == {"run": "1"}
    evidence(env, "enqueue", {"fault": fault, "response": body, "readback": snapshot(env)})


@pytest.mark.parametrize("fault", ["false", "exception", "sql"])
def test_cancel_flag_ack_and_owned_nonce_compensation(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = ingest_api
    assert post(env, [env["b"]])["code"] == 0
    task = tasks(env, env["b"])[0]
    key = task["id"] + "-cancel"
    REDIS_CONN.REDIS.set(key, "earlier", px=60000)
    before = snapshot(env)
    original_set, original_commit = REDIS_CONN.REDIS.set, Session.commit
    failed_sql = False

    def flag(*args: Any, **kwargs: Any) -> Any:
        if fault == "false":
            return False
        if fault == "exception":
            original_set(*args, **kwargs)
            raise ConnectionError("Controlled cancel transport failure")
        return original_set(*args, **kwargs)

    def commit(db: Session) -> None:
        nonlocal failed_sql
        if fault == "sql" and not failed_sql and db.scalar(sa.select(Task.progress).where(Task.id == task["id"])) == -1:
            failed_sql = True
            raise RuntimeError("Controlled cancellation SQL failure")
        original_commit(db)

    with monkeypatch.context() as scope:
        scope.setattr(REDIS_CONN.REDIS, "set", flag)
        scope.setattr(Session, "commit", commit)
        body = post(env, [env["b"]], 2)
    assert body["code"] == 500
    after = snapshot(env)
    assert after == before and REDIS_CONN.REDIS.get(key) in {"earlier", b"earlier"}
    evidence(env, "cancel-fault", {"before": before, "response": body, "after": after})


@pytest.mark.parametrize("boundary", ["insert", "count", "cleanup", "image", "metadata"])
def test_superseded_worker_cannot_mutate_identical_new_ids(ingest_api: dict[str, Any], boundary: str) -> None:
    env = ingest_api
    assert post(env, [env["a"]])["code"] == 0
    old = tasks(env, env["a"])[0]
    with Session(env["engine"]) as db:
        # This producer has started. An equivalent unstarted queue retry must
        # keep its original task; this test requires an intentional new generation.
        TaskService.update_progress(db, old["id"], {"progress": 0.1, "progress_msg": "Controlled old producer started."})
        db.commit()
    assert tasks(env, env["a"])[0]["progress"] == pytest.approx(0.1)
    row = source(env, env["a"], text="Old paused worker")
    if boundary != "insert":
        insert_source_chunks(env["engine"], [row], env["collection"], env["kb"], old["id"])
    assert post(env, [env["a"]], delete=True)["code"] == 0
    new = tasks(env, env["a"])[0]
    assert new["id"] != old["id"]
    row["content_with_weight"] = "Current generation with identical chunk ID"
    insert_source_chunks(env["engine"], [row], env["collection"], env["kb"], new["id"])
    increment_task_document(env["engine"], new["id"], env["a"], env["kb"], 5, 1, 0)
    env["ingest_manifest"]["objects"].append(f"{env['kb']}/{row['id']}")
    evidence(env, "manifest", env["ingest_manifest"])
    put_task_image(new["id"], env["a"], env["kb"], env["owners"][0], bucket=env["kb"], fnm=row["id"], binary=b"New image bytes")
    before = snapshot(env)
    operation = {
        "insert": lambda: insert_source_chunks(env["engine"], [{**row, "content_with_weight": "Late old insertion"}], env["collection"], env["kb"], old["id"]),
        "count": lambda: increment_task_document(env["engine"], old["id"], env["a"], env["kb"], 99, 1, 0),
        "cleanup": lambda: cleanup_task_chunks(env["engine"], old["id"], env["a"], env["kb"], env["collection"], [row["id"]]),
        "image": lambda: put_task_image(old["id"], env["a"], env["kb"], env["owners"][0], bucket=env["kb"], fnm=row["id"], binary=b"Late old image"),
        "metadata": lambda: write_task_metadata(env["engine"], old["id"], env["a"], env["kb"], {"stale": True}),
    }[boundary]
    if boundary == "cleanup":
        operation()
    else:
        with pytest.raises(SupersededDocumentTask):
            operation()
    assert snapshot(env) == before
    evidence(env, "generation", {"boundary": boundary, "before": before, "after": snapshot(env)})


def test_same_document_race_and_independent_document_progress(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = ingest_api
    entered, release = threading.Event(), threading.Event()
    original = service._plan

    def plan(db: Session, doc: Document, kb: Knowledgebase, previous: list[Task]) -> Any:
        if doc.id == env["b"]:
            entered.set()
            assert release.wait(10)
        return original(db, doc, kb, previous)

    monkeypatch.setattr(service, "_plan", plan)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(post, env, [env["b"]])
        assert entered.wait(5)
        second = pool.submit(post, env, [env["b"]])
        independent = pool.submit(post, env, [env["c"]])
        try:
            assert independent.result(5)["code"] == 0
            assert not first.done() and not second.done()
        finally:
            release.set()
        assert first.result(10)["code"] == 0 and second.result(10)["code"] != 0
    assert len(tasks(env, env["b"])) == len(tasks(env, env["c"])) == 1
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 2
    evidence(env, "concurrency", snapshot(env))


@pytest.mark.parametrize("boundary", ["insert", "count", "finally", "rollback"])
def test_actual_paused_classic_worker_and_toc_generation(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    from types import SimpleNamespace

    from core.svr import task_executor as worker

    env = ingest_api
    assert post(env, [env["a"]])["code"] == 0
    old_id = tasks(env, env["a"])[0]["id"]
    with Session(env["engine"]) as db:
        old = TaskService.get_task(db, old_id)
        assert old is not None
        old.update(task_type="", parser_config={"toc_extraction": True})
    settings.docStoreConn.create_idx(env["collection"], env["kb"], 768)
    row = source(env, env["a"], text="Old worker child")
    row["mom"] = "Old worker mother"
    entered, release = threading.Event(), threading.Event()
    original_insert, original_count = worker.insert_chunks, worker.increment_task_document
    original_store = settings.docStoreConn.insert
    failed_store = False

    async def parsed(*args: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(row)]

    async def embedding(*args: Any) -> int:
        return 7

    async def toc(*args: Any) -> list[dict[str, Any]]:
        return [{"title": "Controlled TOC", "chunk_id": 0}]

    async def insert(db: Session, task_id: str, *args: Any) -> bool:
        if task_id == old_id and boundary in {"insert", "finally"}:
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
            if boundary == "finally":
                raise RuntimeError("Controlled old worker error before finally")
        result = await original_insert(db, task_id, *args)
        if task_id == old_id and boundary == "rollback":
            assert result is False
            entered.set()
            assert await asyncio.to_thread(release.wait, 10)
        return result

    def count(bind: Any, task_id: str, *args: Any) -> None:
        if task_id == old_id and boundary == "count":
            entered.set()
            assert release.wait(10)
        original_count(bind, task_id, *args)

    def store_insert(chunks: Any, *args: Any) -> Any:
        nonlocal failed_store
        result = original_store(chunks, *args)
        if boundary == "rollback" and not failed_store:
            failed_store = True
            raise RuntimeError("Controlled partial old worker insertion")
        return result

    monkeypatch.setattr(worker, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(worker, "LLMBundle", lambda *args, **kwargs: SimpleNamespace(encode=lambda texts: ([[0.2] * 768], 7)))
    monkeypatch.setattr(worker, "build_chunks", parsed)
    monkeypatch.setattr(worker, "embedding", embedding)
    monkeypatch.setattr(worker, "run_toc_from_text", toc)
    monkeypatch.setattr(worker, "insert_chunks", insert)
    monkeypatch.setattr(worker, "increment_task_document", count)
    monkeypatch.setattr(settings.docStoreConn, "insert", store_insert)

    def execute() -> Any:
        with Session(env["engine"]) as db:
            return asyncio.run(worker.do_handle_task(db, old))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            if not entered.wait(8):
                import sys
                import traceback

                evidence(env, "worker-stacks", {str(identifier): traceback.format_stack(frame) for identifier, frame in sys._current_frames().items()})
                pytest.fail(str((boundary, future.exception() if future.done() else "pending")))
            assert post(env, [env["a"]], delete=True)["code"] == 0
            current = tasks(env, env["a"])[0]["id"]
            new_row = {**source(env, env["a"], identifier=row["id"], text="New same-ID child"), "mom": "New same-ID mother"}
            with Session(env["engine"]) as db:
                assert asyncio.run(
                    original_insert(
                        db,
                        current,
                        env["owners"][0],
                        env["kb"],
                        [new_row],
                        lambda *args, **kwargs: None,
                        env["collection"],
                        settings.docStoreConn._get_connection().describe_collection(env["collection"]),
                    )
                )
            increment_task_document(env["engine"], current, env["a"], env["kb"], 5, 1, 0)
            before = snapshot(env)
        finally:
            release.set()
        if boundary in {"count", "finally"}:
            with pytest.raises((SupersededDocumentTask, RuntimeError)):
                future.result(10)
        else:
            future.result(10)
    assert snapshot(env) == before
    rows = index_snapshot(env)
    assert len(rows) == 2 and all(row["available_int"] == 0 for row in rows)
    assert {row["content_with_weight"] for row in rows} == {"New same-ID child", "New same-ID mother"}
    evidence(env, "real-worker", {"boundary": boundary, "before": before, "after": snapshot(env)})


def test_real_pipeline_queue_worker_and_reuse(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from core.flow.pipeline import Pipeline
    from core.svr import task_executor as worker

    env = ingest_api
    with Session(env["engine"]) as db:
        db.execute(sa.update(Document).where(Document.id == env["a"]).values(pipeline_id=env["canvas"]))
        db.commit()
    assert post(env, [env["a"]])["code"] == 0
    queued = tasks(env, env["a"])[0]
    messages = [json.loads(fields.get("message", fields.get(b"message"))) for _, fields in REDIS_CONN.REDIS.xrange(env["queue"])]
    assert messages[0]["tenant_id"] == env["owners"][0] and messages[0]["dataflow_id"] == env["canvas"]

    async def model_output(canvas: Pipeline, *args: Any, **kwargs: Any) -> dict[str, Any]:
        assert canvas._source_document_id == env["a"] and canvas.task_id == queued["id"]
        return {"chunks": [{"text": "Controlled pipeline output", "q_768_vec": [0.2] * 768}], "embedding_token_consumption": 9}

    monkeypatch.setattr(Pipeline, "run", model_output)
    settings.docStoreConn.create_idx(env["collection"], env["kb"], 768)
    with Session(env["engine"]) as db:
        task = TaskService.get_task(db, queued["id"])
        assert task is not None
        task.update(task_type="dataflow", dataflow_id=env["canvas"], file=None)
        asyncio.run(worker.run_dataflow(db, task))
    before = snapshot(env)
    assert len(index_snapshot(env)) == 1 and index_snapshot(env)[0]["available_int"] == 0
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["a"])
        assert doc is not None and (doc.chunk_num, doc.token_num) == (1, 9)
    assert tasks(env, env["a"])[0]["progress"] == 1
    assert post(env, [env["a"]])["code"] == 0
    assert tasks(env, env["a"])[0]["progress"] == 1
    after = snapshot(env)
    assert before["index"] == after["index"] and before["queue"] == after["queue"]
    evidence(env, "pipeline", {"before": before, "after": after})


def test_deferred_postgresql_failure_restores_index_before_retry(ingest_api: dict[str, Any]) -> None:
    env = ingest_api
    completed(env, env["a"])
    function = "ingest_fault_" + uuid4().hex
    env["ingest_manifest"]["sql_functions"] = ["usr_ai." + function]
    env["ingest_manifest"]["sql_triggers"] = [function]
    evidence(env, "manifest", env["ingest_manifest"])
    before = snapshot(env)
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['a']}' AND NEW.run = '1' THEN RAISE EXCEPTION 'controlled deferred ingest failure'; END IF; RETURN NEW; END $$"
            )
        )
        db.execute(sa.text(f"CREATE CONSTRAINT TRIGGER {function} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION usr_ai.{function}()"))
    try:
        body = post(env, [env["a"]], delete=True)
        assert body["code"] == 500 and snapshot(env) == before
        evidence(env, "deferred", {"before": before, "body": body, "after": snapshot(env)})
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text(f"DROP TRIGGER {function} ON usr_ai.t_ai_documents"))
            db.execute(sa.text(f"DROP FUNCTION usr_ai.{function}()"))
        with env["engine"].connect() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM pg_trigger WHERE tgname=:name"), {"name": function}) == 0
            assert db.scalar(sa.text("SELECT count(*) FROM pg_proc WHERE proname=:name"), {"name": function}) == 0
    assert post(env, [env["a"]], delete=True)["code"] == 0
    tasks(env, env["a"])


def test_admin_actual_methods_http_and_sync_wait(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    from admin.client.http_client import HttpClient
    from admin.client.multirag_client import MultiRAGClient

    env = ingest_api
    http = HttpClient(host="127.0.0.1", port=env["record"]["port"])
    http.login_token = "Bearer " + env["jwt"]
    client = MultiRAGClient(http, "user")
    with Session(env["engine"]) as db:
        name = db.scalar(sa.select(Knowledgebase.name).where(Knowledgebase.id == env["kb"]))
    client.parse_dataset_docs({"dataset_name": name, "document_names": ["b.txt"]})
    assert "Parsing submitted" in capsys.readouterr().out
    assert len(tasks(env, env["b"])) == 1
    client.parse_dataset({"dataset_name": name, "method": "async"})
    assert "Parsing submitted" in capsys.readouterr().out
    for key in ["a", "b", "c"]:
        tasks(env, env[key])
    original_list = client._list_documents
    polls = 0

    def finish_after_submission(dataset_name: str, dataset_id: str, document_ids: list[str] | None = None) -> Any:
        nonlocal polls
        polls += 1
        if polls == 3:
            with Session(env["engine"]) as db:
                db.execute(sa.update(Document).where(Document.kb_id == env["kb"]).values(run="3"))
                db.commit()
        return original_list(dataset_name, dataset_id, document_ids)

    monkeypatch.setattr(client, "_list_documents", finish_after_submission)
    client.parse_dataset({"dataset_name": name, "method": "sync"})
    assert polls == 3 and "Success to parse dataset" in capsys.readouterr().out
    for key in ["a", "b", "c"]:
        tasks(env, env[key])
    evidence(env, "admin", {"polls": polls, "readback": snapshot(env)})


@pytest.mark.parametrize("fault", ["false", "none", "exception", "sql_before", "sql_after", "restore_failure"])
def test_source_batch_failure_restores_full_rows_and_task_ledger(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = ingest_api
    assert post(env, [env["b"]])["code"] == 0
    task_id = tasks(env, env["b"])[0]["id"]
    original_row = {**source(env, env["b"]), "ingest_tokens_int": 3}
    insert_source_chunks(env["engine"], [original_row], env["collection"], env["kb"], task_id)
    before = snapshot(env)
    replacement = {**original_row, "content_with_weight": "Controlled overwrite", "vector": [0.4] * 768, "q_768_vec": [0.4] * 768, "ingest_tokens_int": 5}
    original_insert, original_commit = settings.docStoreConn.insert, Session.commit
    attempts = 0

    def insert(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        result = original_insert(*args, **kwargs)
        attempts += 1
        if attempts == 1:
            if fault == "false":
                return False
            if fault == "none":
                return None
            raise RuntimeError("Controlled source insert failure")
        return result

    def commit(db: Session) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if fault == "sql_after":
                original_commit(db)
            raise RuntimeError("Controlled source commit failure")
        original_commit(db)

    with monkeypatch.context() as scope:
        if fault.startswith("sql_"):
            scope.setattr(Session, "commit", commit)
        else:
            scope.setattr(settings.docStoreConn, "insert", insert)
        if fault == "restore_failure":
            scope.setattr(settings.docStoreConn, "delete", lambda *args, **kwargs: False)
        with pytest.raises(RuntimeError):
            insert_source_chunks(env["engine"], [replacement], env["collection"], env["kb"], task_id)
    after = snapshot(env)
    evidence(env, "source-failure-readback", {"fault": fault, "before": before, "after": after})
    if fault == "restore_failure":
        assert after["sql"] == before["sql"] and after["index"] != before["index"]
        assert post(env, [env["b"]], delete=True)["code"] == 0
        tasks(env, env["b"])
    else:
        assert after == before
        insert_source_chunks(env["engine"], [replacement], env["collection"], env["kb"], task_id)
        with Session(env["engine"]) as db:
            doc, kb = db.get(Document, env["b"]), db.get(Knowledgebase, env["kb"])
            assert doc is not None and kb is not None
            assert (doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == (1, 5, 1, 5)
    evidence(env, "source-fault", {"fault": fault, "before": before, "after_failure": after, "after_retry": snapshot(env)})


@pytest.mark.parametrize("boundary", ["metadata", "insert", "count", "log"])
def test_actual_pipeline_late_generation_cannot_mutate_new_rows(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    from core.flow.pipeline import Pipeline
    from core.svr import task_executor as worker

    env = ingest_api
    with Session(env["engine"]) as db:
        db.execute(sa.update(Document).where(Document.id == env["a"]).values(pipeline_id=env["canvas"]))
        db.commit()
    assert post(env, [env["a"]])["code"] == 0
    old_id = tasks(env, env["a"])[0]["id"]
    row_id = uuid4().hex
    entered, release = threading.Event(), threading.Event()
    original_insert = worker.insert_chunks
    originals = {name: getattr(worker, name) for name in ["write_task_metadata", "increment_task_document", "record_task_pipeline"]}

    async def output(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"chunks": [{"id": row_id, "text": "Late old pipeline", "q_768_vec": [0.2] * 768, "metadata": {"generation": "old"}}], "embedding_token_consumption": 9}

    async def insert(db: Session, task_id: str, *args: Any) -> bool:
        if boundary == "insert" and task_id == old_id:
            entered.set()
            assert await asyncio.to_thread(release.wait, 15)
        return await original_insert(db, task_id, *args)

    def fenced(name: str, bind: Any, task_id: str, *args: Any) -> Any:
        selected = {"metadata": "write_task_metadata", "count": "increment_task_document", "log": "record_task_pipeline"}[boundary] if boundary != "insert" else None
        if name == selected and task_id == old_id:
            entered.set()
            assert release.wait(15)
        return originals[name](bind, task_id, *args)

    monkeypatch.setattr(Pipeline, "run", output)
    monkeypatch.setattr(worker, "insert_chunks", insert)
    for name in originals:
        from functools import partial

        monkeypatch.setattr(worker, name, partial(fenced, name))
    settings.docStoreConn.create_idx(env["collection"], env["kb"], 768)

    def execute() -> None:
        with Session(env["engine"]) as db:
            task = TaskService.get_task(db, old_id)
            assert task is not None
            task.update(task_type="dataflow", dataflow_id=env["canvas"], file=None)
            asyncio.run(worker.run_dataflow(db, task))

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute)
        try:
            assert entered.wait(10)
            assert post(env, [env["a"]], delete=True)["code"] == 0
            current = tasks(env, env["a"])[0]["id"]
            new_row = source(env, env["a"], identifier=row_id, text="Current same-ID pipeline")
            insert_source_chunks(env["engine"], [new_row], env["collection"], env["kb"], current)
            increment_task_document(env["engine"], current, env["a"], env["kb"], 5, 1, 0)
            write_task_metadata(env["engine"], current, env["a"], env["kb"], {"generation": "new"})
            before = snapshot(env)
        finally:
            release.set()
        if boundary in {"metadata", "count"}:
            with pytest.raises(SupersededDocumentTask):
                future.result(10)
        else:
            future.result(10)
    assert snapshot(env) == before
    assert index_snapshot(env)[0]["content_with_weight"] == "Current same-ID pipeline"
    evidence(env, "late-pipeline", {"boundary": boundary, "before": before, "after": snapshot(env)})


def test_http_handler_repeated_cancellation_drains_owned_sql_work(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = ingest_api
    entered, release, captured = threading.Event(), threading.Event(), threading.Event()
    handler: dict[str, Any] = {}
    original_finish, original_plan = service.finish_status_write, service._plan

    async def finish(work: Any) -> Any:
        handler.update(loop=asyncio.get_running_loop(), task=asyncio.current_task())
        captured.set()
        return await original_finish(work)

    def plan(*args: Any) -> Any:
        entered.set()
        assert release.wait(15)
        return original_plan(*args)

    monkeypatch.setattr(service, "finish_status_write", finish)
    monkeypatch.setattr(service, "_plan", plan)

    def locked() -> None:
        with Session(env["engine"]) as db:
            with pytest.raises(sa.exc.OperationalError):
                db.scalar(sa.select(Document).where(Document.id == env["b"]).with_for_update(nowait=True))
            db.rollback()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(requests.post, env["base"] + "/api/v1/documents/ingest", headers={"Authorization": "Bearer " + env["jwt"]}, json={"doc_ids": [env["b"]], "run": 1}, timeout=30)
        try:
            assert captured.wait(5) and entered.wait(5)
            locked()
            for _ in range(2):
                delivered = threading.Event()
                handler["loop"].call_soon_threadsafe(handler["task"].cancel)
                handler["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(3)
                locked()
                assert not future.done()
        finally:
            release.set()
        response = future.result(10)
        assert response.status_code == 500
    current = tasks(env, env["b"])
    assert len(current) == 1 and REDIS_CONN.REDIS.xlen(env["queue"]) == 1
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(Document.id).where(Document.id == env["b"]).with_for_update(nowait=True)) == env["b"]
    evidence(env, "http-cancellation", {"status": response.status_code, "cancellation_count": 2, "readback": snapshot(env)})


def test_real_classic_multiple_tasks_partial_queue_and_history_retry(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from pypdf import PdfWriter

    env = ingest_api
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=100, height=100)
    binary = io.BytesIO()
    writer.write(binary)
    env["storage_adapter"].put(env["kb"], "b.txt", binary.getvalue())
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["b"])
        assert doc is not None
        doc.type, doc.name = "pdf", "b.pdf"
        doc.parser_config = {**doc.parser_config, "task_page_size": 1, "pages": [[1, 3]]}
        db.commit()
    original = REDIS_CONN.REDIS.xadd
    calls = 0

    def enqueue(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return False if calls == 2 else original(*args, **kwargs)

    with monkeypatch.context() as scope:
        scope.setattr(REDIS_CONN.REDIS, "xadd", enqueue)
        response = post(env, [env["b"]])
    assert response["code"] == 500
    queued = response["data"]["results"][env["b"]]["queued_task_ids"]
    current = tasks(env, env["b"])
    assert len(current) == 2 and len(queued) == 1
    assert {row["progress"] for row in current} == {0, -1}
    row = {**source(env, env["b"]), "ingest_tokens_int": 4}
    insert_source_chunks(env["engine"], [row], env["collection"], env["kb"], queued[0])
    with Session(env["engine"]) as db:
        db.execute(sa.update(Task).where(Task.id == queued[0]).values(progress=1))
        db.commit()
    before = snapshot(env)
    assert post(env, [env["b"]])["code"] == 0
    after = snapshot(env)
    assert before["index"] == after["index"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 2
    retried = tasks(env, env["b"])
    assert len(retried) == 2 and {task["progress"] for task in retried} == {0, 1}
    assert next(task for task in retried if task["progress"] == 1)["digest"].endswith(":ingest-tokens:4")
    evidence(env, "multi-task", {"response": response, "before_retry": before, "after_retry": after})


@pytest.mark.parametrize("enabled", [True, False])
def test_actual_classic_worker_completes_child_mother_toc_ledgers(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    from types import SimpleNamespace

    from core.svr import task_executor as worker

    env = ingest_api
    doc_id = env["b"] if enabled else env["a"]
    assert post(env, [doc_id])["code"] == 0
    task_id = tasks(env, doc_id)[0]["id"]
    row = {**source(env, doc_id), "mom": "Controlled mother"}

    async def parsed(*args: Any) -> list[dict[str, Any]]:
        return [copy.deepcopy(row)]

    async def embedding(*args: Any) -> int:
        return 7

    async def toc(*args: Any) -> list[dict[str, Any]]:
        return [{"title": "Controlled TOC", "chunk_id": 0}]

    monkeypatch.setattr(worker, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(worker, "LLMBundle", lambda *args, **kwargs: SimpleNamespace(encode=lambda texts: ([[0.2] * 768], 7)))
    monkeypatch.setattr(worker, "build_chunks", parsed)
    monkeypatch.setattr(worker, "embedding", embedding)
    monkeypatch.setattr(worker, "run_toc_from_text", toc)
    settings.docStoreConn.create_idx(env["collection"], env["kb"], 768)
    with Session(env["engine"]) as db:
        task = TaskService.get_task(db, task_id)
        assert task is not None
        task.update(task_type="", parser_config={"toc_extraction": True})
        asyncio.run(worker.do_handle_task(db, task))
    rows = index_snapshot(env)
    assert len(rows) == 3
    for item in rows:
        assert item["available_int"] == (1 if enabled and item["id"] == row["id"] else 0)
    current = tasks(env, doc_id)[0]
    assert current["progress"] == 1 and len(current["chunk_ids"].split()) == 2
    with Session(env["engine"]) as db:
        doc, kb = db.get(Document, doc_id), db.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        assert (doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == (2, 7, 2, 7)
    evidence(env, "classic-completion", {"enabled": enabled, "readback": snapshot(env)})


@pytest.mark.parametrize("change", ["document_deleted", "dataset_inactive", "admin_revoked"])
def test_fresh_authorization_and_resource_revalidation_before_write(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    env = ingest_api
    doc_id = env["foreign"] if change == "admin_revoked" else env["b"]
    member_id = uuid4().hex
    if change == "admin_revoked":
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=member_id, user_id=env["owners"][0], tenant_id=env["owners"][1], role="admin", status="1", invited_by=env["owners"][1]))
            db.commit()
    original = service.preflight
    expected: dict[str, Any] = {}

    def preflight(*args: Any, **kwargs: Any) -> Any:
        selected = original(*args, **kwargs)
        with Session(env["engine"]) as db:
            if change == "document_deleted":
                db.execute(sa.delete(Document).where(Document.id == doc_id))
            elif change == "dataset_inactive":
                db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(status="0"))
            else:
                db.execute(sa.update(UserTenant).where(UserTenant.id == member_id).values(status="0"))
            db.commit()
        expected.update(snapshot(env))
        return selected

    monkeypatch.setattr(service, "preflight", preflight)
    response = post(env, [doc_id])
    assert response["code"] == (102 if change == "document_deleted" else 109)
    assert snapshot(env) == expected
    evidence(env, "fresh-revalidation", {"change": change, "response": response, "expected_external_change": expected, "after": snapshot(env)})


def test_table_configuration_after_commit_failure_is_compensated(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = ingest_api
    env["storage_adapter"].put(env["kb"], "b.txt", b"name,value\nexample,1\n")
    with Session(env["engine"]) as db:
        doc, kb = db.get(Document, env["b"]), db.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        doc.parser_id, doc.name = "table", "b.csv"
        kb.parser_config = {**kb.parser_config, "field_map": {"original": "mapping"}}
        db.commit()
    before = snapshot(env)
    original_commit = Session.commit
    failed = False

    def commit(db: Session) -> None:
        nonlocal failed
        original_commit(db)
        if not failed:
            failed = True
            raise RuntimeError("Controlled table SQL acknowledgement failure")

    with monkeypatch.context() as scope:
        scope.setattr(Session, "commit", commit)
        response = post(env, [env["b"]])
    assert response["code"] == 500 and snapshot(env) == before
    assert post(env, [env["b"]])["code"] == 0
    tasks(env, env["b"])
    with Session(env["engine"]) as db:
        assert "field_map" not in db.get(Knowledgebase, env["kb"]).parser_config
    evidence(env, "table-compensation", {"response": response, "before": before, "after_retry": snapshot(env)})


def test_source_deferred_commit_restores_full_history_and_ledgers(ingest_api: dict[str, Any]) -> None:
    env = ingest_api
    assert post(env, [env["b"]])["code"] == 0
    task_id = tasks(env, env["b"])[0]["id"]
    row = {**source(env, env["b"]), "ingest_tokens_int": 3}
    insert_source_chunks(env["engine"], [row], env["collection"], env["kb"], task_id)
    before = snapshot(env)
    function = "ingest_source_fault_" + uuid4().hex
    env["ingest_manifest"].update(sql_functions=["usr_ai." + function], sql_triggers=[function])
    evidence(env, "manifest", env["ingest_manifest"])
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{function}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['b']}' AND NEW.token_num > 3 THEN RAISE EXCEPTION 'controlled source deferred failure'; END IF; RETURN NEW; END $$"
            )
        )
        db.execute(sa.text(f"CREATE CONSTRAINT TRIGGER {function} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION usr_ai.{function}()"))
    try:
        with pytest.raises(sa.exc.DBAPIError):
            insert_source_chunks(env["engine"], [{**row, "content_with_weight": "Deferred overwrite", "ingest_tokens_int": 5}], env["collection"], env["kb"], task_id)
        assert snapshot(env) == before
        evidence(env, "source-deferred", {"before": before, "after": snapshot(env)})
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text(f"DROP TRIGGER {function} ON usr_ai.t_ai_documents"))
            db.execute(sa.text(f"DROP FUNCTION usr_ai.{function}()"))
        with env["engine"].connect() as db:
            assert db.scalar(sa.text("SELECT count(*) FROM pg_trigger WHERE tgname=:name"), {"name": function}) == 0
            assert db.scalar(sa.text("SELECT count(*) FROM pg_proc WHERE proname=:name"), {"name": function}) == 0
    insert_source_chunks(env["engine"], [{**row, "content_with_weight": "Deferred retry", "ingest_tokens_int": 5}], env["collection"], env["kb"], task_id)


@pytest.mark.parametrize("winner", ["cancel", "task_cancel", "disable"])
def test_committed_ingest_revalidates_later_cancel_and_availability(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, winner: str) -> None:
    from contextlib import contextmanager

    env = ingest_api
    completed(env, env["b"])
    # Force a new task with history clearing, then pause after real SQL COMMIT.
    entered, release = threading.Event(), threading.Event()
    original_commit, original_connection = Session.commit, service.db_connection
    paused = False

    @contextmanager
    def connection() -> Iterator[Session]:
        with original_connection() as db:
            db.info["ingest_pause"] = True
            yield db

    def commit(db: Session) -> None:
        nonlocal paused
        original_commit(db)
        if db.info.get("ingest_pause") and not paused:
            paused = True
            entered.set()
            assert release.wait(15)

    monkeypatch.setattr(service, "db_connection", connection)
    monkeypatch.setattr(Session, "commit", commit)
    if winner == "disable":
        monkeypatch.setattr(REDIS_CONN.REDIS, "xadd", lambda *args, **kwargs: False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(post, env, [env["b"]], delete=True)
        try:
            assert entered.wait(8)
            current = tasks(env, env["b"])[0]["id"]
            if winner == "cancel":
                assert post(env, [env["b"]], 2)["code"] == 0
            elif winner == "task_cancel":
                response = requests.post(f"{env['base']}/api/v1/tasks/{current}/cancel", headers={"Authorization": "Bearer " + env["jwt"]}, timeout=30)
                assert response.status_code == 200 and response.json()["retcode"] == 0
            else:
                response = requests.post(
                    f"{env['base']}/api/v1/datasets/{env['kb']}/documents/batch-update-status",
                    headers={"Authorization": "Bearer " + env["jwt"]},
                    json={"doc_ids": [env["b"]], "status": "0"},
                    timeout=30,
                )
                assert response.status_code == 200 and response.json()["code"] == 0
            before_resume = snapshot(env)
        finally:
            release.set()
        response = future.result(10)
    assert response["code"] != 0 and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    after = snapshot(env)
    if winner == "disable":
        with Session(env["engine"]) as db:
            doc = db.get(Document, env["b"])
            assert doc is not None and doc.status == "0" and (doc.chunk_num, doc.token_num) == (1, 11)
        assert len(index_snapshot(env)) == 1 and index_snapshot(env)[0]["available_int"] == 0
    else:
        # Recovery material is private bookkeeping, and may be discarded when
        # a later generation wins. Every actual business resource stays exact.
        before_resume["flags"].pop(recovery_key(env["b"]), None)
        assert after == before_resume
    evidence(env, "commit-interleaving", {"winner": winner, "response": response, "before_resume": before_resume, "after": after})


def test_second_table_preserves_shared_field_mapping(ingest_api: dict[str, Any]) -> None:
    env = ingest_api
    completed(env, env["a"])
    env["storage_adapter"].put(env["kb"], "b.txt", b"name,value\nexample,1\n")
    with Session(env["engine"]) as db:
        doc, kb = db.get(Document, env["b"]), db.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        doc.parser_id, doc.name = "table", "b.csv"
        kb.parser_config = {**kb.parser_config, "field_map": {"original": "mapping"}}
        original = copy.deepcopy(kb.parser_config)
        db.commit()
    assert post(env, [env["b"]])["code"] == 0
    tasks(env, env["b"])
    with Session(env["engine"]) as db:
        assert db.get(Knowledgebase, env["kb"]).parser_config == original
    evidence(env, "second-table", snapshot(env))


def test_real_pipeline_components_finish_before_index_terminal(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from agent import canvas as canvas_module
    from core.flow.base import ProcessBase, ProcessParamBase
    from core.svr import task_executor as worker

    env = ingest_api
    names = ["File", "Parser", "Chunker", "Tokenizer"]
    dsl = {
        "components": {name: {"obj": {"component_name": "IngestControlled", "params": {}}, "downstream": names[i + 1 : i + 2], "upstream": names[max(i - 1, 0) : i]} for i, name in enumerate(names)},
        "path": [],
    }

    class ControlledParam(ProcessParamBase):
        def check(self) -> None:
            pass

    class Controlled(ProcessBase):
        component_name = "IngestControlled"

        async def _invoke(self, **kwargs: Any) -> None:
            if self._id == "Tokenizer":
                self.set_output("chunks", [{"text": "Actual pipeline component output", "q_768_vec": [0.2] * 768, "metadata": {"origin": "controlled component"}}])
                self.set_output("embedding_token_consumption", 8)

    original_factory, original_progress = canvas_module.component_class, TaskService.update_progress
    stages = []

    def factory(name: str) -> Any:
        return ControlledParam if name == "IngestControlledParam" else Controlled if name == "IngestControlled" else original_factory(name)

    def progress(db: Session, task_id: str, info: dict[str, Any]) -> Any:
        result = original_progress(db, task_id, info)
        if "Done" in info.get("progress_msg", ""):
            stages.append(info["progress"])
        return result

    monkeypatch.setattr(canvas_module, "component_class", factory)
    monkeypatch.setattr(TaskService, "update_progress", progress)
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserCanvas).where(UserCanvas.id == env["canvas"]).values(dsl=dsl))
        db.execute(sa.update(Document).where(Document.id == env["a"]).values(pipeline_id=env["canvas"]))
        db.commit()
    assert post(env, [env["a"]])["code"] == 0
    task_id = tasks(env, env["a"])[0]["id"]
    env["ingest_manifest"]["redis_keys"].append(f"{env['canvas']}-{task_id}-logs")
    evidence(env, "manifest", env["ingest_manifest"])
    settings.docStoreConn.create_idx(env["collection"], env["kb"], 768)
    with Session(env["engine"]) as db:
        task = TaskService.get_task(db, task_id)
        assert task is not None
        task.update(task_type="dataflow", dataflow_id=env["canvas"], file=None)
        asyncio.run(worker.run_dataflow(db, task))
    assert stages == pytest.approx([0.2, 0.4, 0.6, 0.8])
    assert tasks(env, env["a"])[0]["progress"] == 1
    assert len(index_snapshot(env)) == 1 and index_snapshot(env)[0]["available_int"] == 0
    with Session(env["engine"]) as db:
        doc = db.get(Document, env["a"])
        assert doc is not None and (doc.chunk_num, doc.token_num) == (1, 8)
    evidence(env, "actual-pipeline-components", {"component_progress": stages, "readback": snapshot(env)})


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
def test_actual_bulk_visibility_reconciles_sql_and_commit_compensation(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    import inspect
    import logging
    from types import SimpleNamespace

    from common.doc_store.document_history import document_history
    from core.utils.es_conn import ESConnection
    from core.utils.opensearch_conn import OSConnection

    env = ingest_api
    document_id, dataset_id, task_id = env["c"], env["kb"], uuid4().hex
    with Session(env["engine"]) as db:
        document = db.get(Document, document_id)
        assert document is not None
        document.run = "1"
        db.add(Task(id=task_id, doc_id=document_id, digest="native-visibility", progress=0))
        db.commit()
    cls = inspect.getclosurevars(ESConnection if backend == "elasticsearch" else OSConnection).nonlocals["cls"]
    store = object.__new__(cls)
    store.logger = logging.getLogger("ingest-native-visibility")
    pending: dict[str, Any] = {}
    visible: dict[str, Any] = {}
    calls = []

    def bulk(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["refresh"] is False
        operations = kwargs.get("operations", kwargs.get("body"))
        for meta, payload in zip(operations[::2], operations[1::2], strict=True):
            identifier = meta["index"]["_id"]
            pending[identifier] = {"id": identifier, **copy.deepcopy(payload)}
        calls.append("bulk")
        return {"errors": False}

    def refresh(**kwargs: Any) -> dict[str, Any]:
        visible.clear()
        visible.update(copy.deepcopy(pending))
        calls.append("refresh")
        return {"_shards": {"total": 2, "successful": 1, "failed": 0}}

    def search_rows(**kwargs: Any) -> dict[str, Any]:
        rows = list(visible.values())
        return {
            "timed_out": False,
            "_scroll_id": "owned-cursor",
            "_shards": {"total": 1, "successful": 1, "failed": 0},
            "hits": {"total": {"value": len(rows), "relation": "eq"}, "hits": [{"_id": row["id"], "_source": copy.deepcopy(row)} for row in rows]},
        }

    def scroll(**kwargs: Any) -> dict[str, Any]:
        return {"timed_out": False, "_scroll_id": "owned-cursor", "_shards": {"total": 1, "successful": 1, "failed": 0}, "hits": {"total": {"value": len(visible), "relation": "eq"}, "hits": []}}

    def delete_by_query(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["refresh"] is True
        filters = kwargs["body"]["query"]["bool"].get("filter", [])
        ids = next((item["ids"]["values"] for item in filters if "ids" in item), list(pending))
        count = sum(identifier in pending for identifier in ids)
        for identifier in ids:
            pending.pop(identifier, None)
        refresh(index=env["collection"])
        return {"deleted": count}

    client = SimpleNamespace(
        bulk=bulk, indices=SimpleNamespace(refresh=refresh, exists=lambda **kwargs: True), search=search_rows, scroll=scroll, clear_scroll=lambda **kwargs: None, delete_by_query=delete_by_query
    )
    store.es = store.os = client
    original_store = settings.docStoreConn
    with monkeypatch.context() as native_patch:
        native_patch.setattr(settings, "docStoreConn", store)
        row = {
            "id": "native-original",
            "doc_id": document_id,
            "kb_id": dataset_id,
            "q_2_vec": [0.25, 0.5],
            "content_with_weight": "full native source",
            "create_time": "2026-10-03 00:00:01",
            "ingest_tokens_int": 5,
        }
        insert_source_chunks(env["engine"], [copy.deepcopy(row)], env["collection"], dataset_id, task_id)
        with env["engine"].connect() as db:
            doc = dict(db.execute(sa.select(Document.__table__).where(Document.id == document_id)).mappings().one())
            kb = dict(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.id == dataset_id)).mappings().one())
        assert (doc["chunk_num"], doc["token_num"], kb["chunk_num"], kb["token_num"]) == (1, 5, 1, 5)
        saved = document_history(store, env["collection"], dataset_id, document_id)
        before = sql_snapshot(env)
        original_commit = Session.commit
        failed = False

        def accepted_then_failed(db: Session) -> None:
            nonlocal failed
            original_commit(db)
            if not failed:
                failed = True
                raise RuntimeError("controlled accepted SQL commit response failure")

        native_patch.setattr(Session, "commit", accepted_then_failed)
        with pytest.raises(RuntimeError, match="accepted SQL commit response failure"):
            insert_source_chunks(env["engine"], [{**row, "content_with_weight": "uncommitted replacement", "q_2_vec": [0.75, 1.0], "ingest_tokens_int": 8}], env["collection"], dataset_id, task_id)
        assert sql_snapshot(env) == before
        assert document_history(store, env["collection"], dataset_id, document_id) == saved
        evidence(
            env,
            "native-search-visibility",
            {"backend": backend, "controlled_transport": True, "actual_connector_and_sql": True, "calls": calls, "sql_before": before, "sql_after": sql_snapshot(env), "full_rows": saved},
        )
    assert settings.docStoreConn is original_store


@pytest.mark.parametrize("state", ["restore_retry", "submitted", "later_generation"])
def test_active_durable_material_reconciles_actual_owner_queue_and_generation(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, state: str) -> None:
    from contextlib import contextmanager

    from api.db.services.document_ingest_recovery import StoredRecovery

    env = ingest_api
    completed(env, env["b"])
    before = snapshot(env)
    old_task = tasks(env, env["b"])[0]["id"]
    original_commit, original_connection = Session.commit, service.db_connection
    entered, release = threading.Event(), threading.Event()
    failed = False

    @contextmanager
    def connection() -> Iterator[Session]:
        with original_connection() as db:
            db.info["active_recovery_fault"] = True
            yield db

    def commit(db: Session) -> None:
        nonlocal failed
        original_commit(db)
        if db.info.get("active_recovery_fault") and not failed:
            failed = True
            if state == "later_generation":
                entered.set()
                assert release.wait(15)
            else:
                raise RuntimeError("Controlled accepted first SQL commit response failure")

    def restore_unavailable(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("Controlled unavailable store recovery")

    original_update = StoredRecovery.update

    def failed_phase_missing(saved: StoredRecovery, **changes: Any) -> None:
        if changes.get("phase") == "failed":
            raise ConnectionError("Controlled Redis failed-phase write did not reach server")
        original_update(saved, **changes)

    if state == "later_generation":
        with monkeypatch.context() as scope:
            scope.setattr(service, "db_connection", connection)
            scope.setattr(Session, "commit", commit)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(post, env, [env["b"]], delete=True)
                try:
                    assert entered.wait(8)
                    active = StoredRecovery.load(env["b"])
                    assert active is not None and active.data["phase"] == "active"
                    assert post(env, [env["b"]], 2)["code"] == 0
                    assert post(env, [env["b"]])["code"] == 0
                    new_task = tasks(env, env["b"])[0]["id"]
                    winner = source(env, env["b"], identifier=before["index"]["collection"][0]["pk"], text="New generation owns identical chunk ID")
                    insert_source_chunks(env["engine"], [winner], env["collection"], env["kb"], new_task)
                    latest = snapshot(env)
                finally:
                    release.set()
                response = pending.result(10)
        assert response["code"] != 0 and snapshot(env) == latest
        assert not REDIS_CONN.REDIS.exists(recovery_key(env["b"]))
        assert tasks(env, env["b"])[0]["id"] == new_task != old_task
    else:
        with monkeypatch.context() as scope:
            if state == "restore_retry":
                scope.setattr(service, "db_connection", connection)
                scope.setattr(Session, "commit", commit)
                scope.setattr(service, "restore_document_history", restore_unavailable)
                scope.setattr(StoredRecovery, "update", failed_phase_missing)
            else:
                scope.setattr(StoredRecovery, "clear", lambda saved: (_ for _ in ()).throw(ConnectionError("Controlled journal clear did not reach Redis")))
            response = post(env, [env["b"]], delete=True)
        assert response["code"] == 500
        active = StoredRecovery.load(env["b"])
        assert active is not None and active.data["phase"] == "active"
        applied = snapshot(env)
        assert applied["index"]["collection"] == []
        current_task = tasks(env, env["b"])[0]["id"]
        assert current_task != old_task
        if state == "restore_retry":
            assert applied["queue"] == before["queue"] == []
            assert post(env, [env["b"]], 0)["code"] == 0
            recovered = snapshot(env)
            assert recovered["index"] == before["index"] and recovered["objects"] == before["objects"]
            assert tasks(env, env["b"])[0] == next(row for row in before["sql"][Task.__tablename__] if row["id"] == old_task)
            with Session(env["engine"]) as db:
                doc, kb = db.get(Document, env["b"]), db.get(Knowledgebase, env["kb"])
                assert doc is not None and kb is not None
                assert (doc.run, doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == ("0", 1, 11, 1, 11)
        else:
            assert len(applied["queue"]) == 1
            assert post(env, [env["b"]], 2)["code"] == 0
            assert tasks(env, env["b"])[0]["id"] == current_task
            assert snapshot(env)["index"] == applied["index"] and snapshot(env)["queue"] == applied["queue"]
        assert not REDIS_CONN.REDIS.exists(recovery_key(env["b"]))
    evidence(env, "active-journal-recovery", {"state": state, "before": before, "response": response, "after": snapshot(env)})


@pytest.mark.parametrize("status", ["0", "1"])
def test_same_source_status_winner_survives_full_history_compensation(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    from contextlib import contextmanager

    env = ingest_api
    row = completed(env, env["b"])
    parent = source(env, env["b"], identifier="same-status-parent", mother="same-status-parent")
    child = {**row, "mom_id": parent["id"], "available_int": 1 if status == "0" else 0}
    toc = {**source(env, env["b"], identifier="same-status-toc"), "toc_kwd": "toc", "available_int": 0}
    special = {**source(env, env["b"], identifier="same-status-special"), "raptor_kwd": "raptor", "available_int": 1}
    parent["available_int"] = 0
    assert settings.docStoreConn.insert([parent, child, toc, special], env["collection"], env["kb"]) == []
    with Session(env["engine"]) as db:
        doc, kb = db.get(Document, env["b"]), db.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        doc.status, doc.chunk_num, kb.chunk_num = status, 3, 3
        db.commit()
    before = snapshot(env)
    entered, release = threading.Event(), threading.Event()
    original_commit, original_connection = Session.commit, service.db_connection
    paused = False

    @contextmanager
    def connection() -> Iterator[Session]:
        with original_connection() as db:
            db.info["same_status_pause"] = True
            yield db

    def commit(db: Session) -> None:
        nonlocal paused
        original_commit(db)
        if db.info.get("same_status_pause") and not paused:
            paused = True
            entered.set()
            assert release.wait(15)

    monkeypatch.setattr(service, "db_connection", connection)
    monkeypatch.setattr(Session, "commit", commit)
    monkeypatch.setattr(REDIS_CONN.REDIS, "xadd", lambda *args, **kwargs: False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post, env, [env["b"]], delete=True)
        try:
            assert entered.wait(8)
            response = requests.post(
                f"{env['base']}/api/v1/datasets/{env['kb']}/documents/batch-update-status",
                headers={"Authorization": "Bearer " + env["jwt"]},
                json={"doc_ids": [env["b"]], "status": status},
                timeout=30,
            )
            assert response.status_code == 200 and response.json()["code"] == 0
        finally:
            release.set()
        result = pending.result(10)
    assert result["code"] == 500
    after = snapshot(env)
    expected = copy.deepcopy(before["index"])
    for item in expected["collection"]:
        if item["pk"] == child["id"]:
            item["available_int"] = int(status)
    assert after["index"] == expected and after["sql"] == before["sql"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    evidence(env, "same-status-winner", {"status": status, "before": before, "response": result, "after": after})


@pytest.mark.parametrize("fault", ["connection_entry", "session_exit", "owner_close"])
@pytest.mark.parametrize("path", ["/api/v1/documents/ingest", "/v1/document/run"])
def test_http_batch_boundary_failure_preserves_real_effects_and_retry(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str, path: str) -> None:
    from contextlib import contextmanager

    from api.db.services.document_ingest_recovery import RecoveryOwner

    env = ingest_api
    original_connection, original_close = service.db_connection, RecoveryOwner.close
    entries, closes = 0, 0

    @contextmanager
    def connection() -> Iterator[Session]:
        nonlocal entries
        entries += 1
        current = entries
        if current == 2 and fault == "connection_entry":
            raise ConnectionError("Controlled second document connection entry failure")
        with original_connection() as db:
            yield db
        if current == 2 and fault == "session_exit":
            raise RuntimeError("Controlled second document session close failure")

    def close(owner: RecoveryOwner) -> None:
        nonlocal closes
        closes += 1
        original_close(owner)
        if closes == 2:
            raise RuntimeError("Controlled second document recovery owner close failure")

    ids = [env["a"], env["b"], env["c"]]
    with monkeypatch.context() as scope:
        scope.setattr(service, "db_connection", connection)
        if fault == "owner_close":
            scope.setattr(RecoveryOwner, "close", close)
        response = post(env, ids, path=path, delete=True)
    assert response["code"] == 500
    results = response["data"]["results"]
    assert set(results) == set(ids) and results[env["a"]] == results[env["c"]] == {"run": "1"}
    assert "error" in results[env["b"]] and results[env["b"]]["effect"] == ("unknown" if fault == "connection_entry" else "confirmed")
    for key in ["a", "b", "c"]:
        tasks(env, env[key])
    after_failure = snapshot(env)
    queue_before = len(after_failure["queue"])
    assert queue_before == (2 if fault == "connection_entry" else 3)
    first_task = tasks(env, env["a"])[0]["id"]
    assert post(env, ids, path=path, delete=True)["code"] == 0
    after_retry = snapshot(env)
    assert len(after_retry["queue"]) == 3
    assert tasks(env, env["a"])[0]["id"] == first_task
    if fault != "connection_entry":
        assert after_retry == after_failure
    evidence(env, "batch-boundary-escape", {"fault": fault, "path": path, "response": response, "after_failure": after_failure, "after_retry": after_retry})


@pytest.mark.parametrize("change", ["inactive_kb", "missing_submitted", "paged_done"])
def test_admin_sync_real_poll_errors_selection_and_pagination(ingest_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], change: str) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    from admin.client.http_client import HttpClient
    from admin.client.multirag_client import MultiRAGClient

    env = ingest_api
    if change == "paged_done":
        with Session(env["engine"]) as db:
            for ordinal in range(98):
                identifier, file_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
                name = f"paged-{ordinal}.txt"
                manifest = env["ingest_manifest"]
                manifest["document_ids"].append(identifier)
                manifest["file_ids"].append(file_id)
                manifest["link_ids"].append(link_id)
                manifest["objects"].append(env["kb"] + "/" + name)
                evidence(env, "manifest", manifest)
                db.add(Document(id=identifier, kb_id=env["kb"], created_by=env["owners"][0], name=name, location=name, type="doc", parser_id="naive", parser_config={}))
                db.add(File(id=file_id, parent_id=env["kb"], tenant_id=env["owners"][0], created_by=env["owners"][0], name=name, location=name, type="doc"))
                db.add(File2Document(id=link_id, file_id=file_id, document_id=identifier))
                env["storage_adapter"].put(env["kb"], name, b"Owned controlled parsing status fixture")
            db.commit()
    http = HttpClient(host="127.0.0.1", port=env["record"]["port"])
    http.login_token = "Bearer " + env["jwt"]
    client = MultiRAGClient(http, "user")
    original_request = http.request
    requests_seen: list[dict[str, Any]] = []
    submitted: list[str] = []

    def request(method: str, route: str, **kwargs: Any) -> Any:
        response = original_request(method, route, **kwargs)
        body = response.json()
        requests_seen.append({"method": method, "route": route, "params": kwargs.get("params"), "status": response.status_code, "body": body})
        if method == "POST" and route == "documents/ingest":
            assert response.status_code == 200 and body["code"] == 0 and body["data"] is True
            submitted.extend(kwargs["json_body"]["doc_ids"])
            with Session(env["engine"]) as db:
                if change == "inactive_kb":
                    db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(status="0"))
                elif change == "missing_submitted":
                    db.execute(sa.delete(Task).where(Task.doc_id == env["b"]))
                    db.execute(sa.delete(Document).where(Document.id == env["b"]))
                else:
                    # Controlled completion boundary: commit real task rows,
                    # then invoke the production document progress reconciler.
                    db.execute(sa.update(Task).where(Task.doc_id.in_(submitted)).values(progress=1))
                db.commit()
                if change == "paged_done":
                    DocumentService.update_progress_immediately(db, [{"id": identifier} for identifier in submitted])
        return response

    monkeypatch.setattr(http, "request", request)
    with Session(env["engine"]) as db:
        name = db.scalar(sa.select(Knowledgebase.name).where(Knowledgebase.id == env["kb"]))
    client.parse_dataset({"dataset_name": name, "method": "sync"})
    output = capsys.readouterr().out
    evidence(env, "admin-sync-polling", {"change": change, "requests": requests_seen, "submitted_ids": submitted, "output": output, "readback": snapshot(env)})
    assert len([item for item in requests_seen if item["method"] == "POST"]) == 1
    queue = REDIS_CONN.REDIS.xrange(env["queue"])
    assert len(queue) == len(submitted) == (101 if change == "paged_done" else 3)
    if change == "inactive_kb":
        assert requests_seen[-1]["status"] == 200 and requests_seen[-1]["body"]["code"] == 102
        assert "Fail to list documents" in output and "Success to parse" not in output
    elif change == "missing_submitted":
        assert "Parsing status is unavailable" in output and "Success to parse" not in output
    else:
        assert "Success to parse dataset" in output
        polls = [item for item in requests_seen if item["params"] and "ids" in item["params"]]
        assert [item["params"]["page"] for item in polls] == [1, 2]
        assert {doc["id"] for item in polls for doc in item["body"]["data"]["docs"]} == set(submitted)
        assert all(doc["run"] == "DONE" for item in polls for doc in item["body"]["data"]["docs"])
    for identifier in submitted:
        tasks(env, identifier)
