"""Retired HTTP entry and retained parsers, with scratch SQL/Redis/MinIO/Milvus.

Only model config/provider output and Redis namespace routing are substituted.
Routes, file parsing, service orchestration and all storage operations are real.
No browser, remote model or background parse worker is run.
"""

import json
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from api.db import db_models
from api.db.db_models import (
    Conversation,
    Dialog,
    Document,
    File,
    File2Document,
    Knowledgebase,
    PipelineOperationLog,
    SourceRecoveryRecord,
    Task,
    WritingChapter,
    WritingProject,
    WritingReferenceMaterial,
    get_db,
)
from api.db.services import document_status_service as status_service
from api.db.services.document_source_recovery import SourceRecovery, source_recovery_key
from api.db.services.file_service import FileService
from common import settings
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def parse_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    with ExitStack() as cleanup:
        from api import apps

        env = runtime_upload_api
        sessions = sessionmaker(env["engine"], expire_on_commit=False)

        def scratch_db() -> Iterator[Session]:
            with sessions() as db:
                yield db

        monkeypatch.setattr(apps, "SessionLocal", sessions)
        monkeypatch.setattr(db_models, "SessionLocal", sessions)
        monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
        owner = env["owners"][0]
        env.update({key: uuid4().hex for key in ["kb", "dialog", "conversation", "project", "chapter"]})
        name = f"parse_retire_{uuid4().hex}"
        env["collection"] = search.index_name_one(owner, name)
        env["queue"] = f"parse-retirement:{uuid4().hex}:queue"
        env["cache_prefix"] = f"parse-retirement:{uuid4().hex}:model:"
        store = settings.docStoreConn
        assert store.db_type() == "milvus"
        assert not store.has_collection(env["collection"])
        assert not REDIS_CONN.REDIS.exists(env["queue"])

        def queue_name(priority: int) -> str:
            assert priority == 0
            return env["queue"]

        monkeypatch.setattr(settings, "get_svr_queue_name", queue_name)
        env["record"]["parse"] = {key: env[key] for key in ["kb", "dialog", "conversation", "project", "chapter", "collection", "queue", "cache_prefix"]}
        manifest = env["record"]["parse"]
        manifest.update(source_document_ids=[], source_task_ids=[], redis_keys=[])

        def remove_index() -> None:
            store.delete_idx(env["collection"], env["kb"])
            assert not store.has_collection(env["collection"])
            assert index_snapshot(env) == []
            manifest["collection_removed"] = True

        def remove_queue() -> None:
            keys = list(REDIS_CONN.REDIS.scan_iter(match=env["cache_prefix"] + "*"))
            REDIS_CONN.REDIS.delete(env["queue"], *keys, *manifest["redis_keys"])
            assert not REDIS_CONN.REDIS.exists(env["queue"])
            assert not any(REDIS_CONN.REDIS.exists(key) for key in manifest["redis_keys"])
            assert not list(REDIS_CONN.REDIS.scan_iter(match=env["cache_prefix"] + "*"))
            manifest.update(queue_removed=True, cache_removed=True, cache_keys=[key.decode() if isinstance(key, bytes) else key for key in keys])

        def remove_sql() -> None:
            with Session(env["engine"]) as db:
                doc_ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == env["kb"])))
                source_doc_ids = sorted({*doc_ids, *manifest["source_document_ids"]})
                scoped = [
                    (SourceRecoveryRecord, SourceRecoveryRecord.document_id.in_(source_doc_ids)),
                    (PipelineOperationLog, PipelineOperationLog.document_id.in_(doc_ids)),
                    (Task, Task.doc_id.in_(doc_ids)),
                    (File2Document, File2Document.document_id.in_(doc_ids)),
                    (Document, Document.kb_id == env["kb"]),
                    (File, File.tenant_id == owner),
                    (WritingReferenceMaterial, WritingReferenceMaterial.chapter_id == env["chapter"]),
                    (WritingChapter, WritingChapter.id == env["chapter"]),
                    (WritingProject, WritingProject.id == env["project"]),
                    (Conversation, Conversation.id == env["conversation"]),
                    (Dialog, Dialog.id == env["dialog"]),
                    (Knowledgebase, Knowledgebase.id == env["kb"]),
                ]
                owned_rows = {model.__tablename__: list(db.scalars(sa.select(model.id).where(condition))) for model, condition in scoped}
                for model, condition in scoped:
                    db.execute(sa.delete(model).where(condition))
                db.commit()
            with env["engine"].connect() as db:
                remaining = {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in scoped}
                assert not any(remaining.values()), remaining
            manifest.update(documents=doc_ids, owned_rows=owned_rows, remaining=remaining)

        def snapshot_source() -> None:
            with Session(env["engine"]) as db:
                doc_ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == env["kb"])))
                source_doc_ids = sorted({*doc_ids, *manifest["source_document_ids"]})
                source_rows = [dict(row) for row in db.execute(sa.select(SourceRecoveryRecord.__table__).where(SourceRecoveryRecord.document_id.in_(source_doc_ids))).mappings()]
                source_states = {}
                for key in manifest["redis_keys"]:
                    raw = REDIS_CONN.REDIS.get(key)
                    dump = REDIS_CONN.REDIS.dump(key)
                    source_states[key] = {"value": raw, "dump_hex": dump.hex() if dump is not None else None, "pttl": REDIS_CONN.REDIS.pttl(key), "absent": raw is None}
                env["record_path"].with_suffix(".source-cleanup-before.json").write_text(
                    json.dumps({"database": env["engine"].url.database, "document_ids": source_doc_ids, "rows": source_rows, "redis_states": source_states}, default=str)
                )

        cleanup.callback(remove_sql)
        cleanup.callback(remove_queue)
        cleanup.callback(remove_index)
        cleanup.callback(snapshot_source)
        with Session(env["engine"]) as db:
            db.add(Knowledgebase(id=env["kb"], tenant_id=owner, created_by=owner, name=name, embd_id="scratch-embedding", parser_id="naive", parser_config={}))
            db.add(Dialog(id=env["dialog"], tenant_id=owner, name="Parse scratch", llm_id="scratch-chat", kb_ids=[env["kb"]]))
            db.add(Conversation(id=env["conversation"], dialog_id=env["dialog"], user_id=owner, name="Parse scratch", message=[]))
            db.add(WritingProject(id=env["project"], user_id=owner, user_input="scratch", content_type="article", language_style="plain", word_count=100))
            db.add(WritingChapter(id=env["chapter"], project_id=env["project"], title="Scratch chapter"))
            db.commit()
        source_lock = threading.RLock()
        original_prepare = status_service.prepare_source_recovery

        def register_source_material(bind: Any, document_id: str, task_id: str, **data: Any) -> SourceRecovery:
            assert data["dataset_id"] == env["kb"] or document_id in env.get("ingest_manifest", {}).get("document_ids", [])
            with source_lock:
                for field, value in [("source_document_ids", document_id), ("source_task_ids", task_id), ("redis_keys", source_recovery_key(document_id, task_id))]:
                    if value not in manifest[field]:
                        manifest[field].append(value)
                env["record_path"].write_text(json.dumps(env["record"]))
            return original_prepare(bind, document_id, task_id, **data)

        monkeypatch.setattr(status_service, "prepare_source_recovery", register_source_material)
        env["record_path"].write_text(json.dumps(env["record"]))
        yield env


def _headers(env: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {env['jwt']}"}


def _counts(env: dict[str, Any]) -> list[int]:
    with Session(env["engine"]) as db:
        return [db.scalar(sa.select(sa.func.count()).select_from(model)) for model in [Document, File, File2Document, Task, WritingReferenceMaterial]]


class _ModelOutput:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.llm_name = name
        self.max_length = 8192
        self.calls = calls

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        self.calls.append("encode")
        # The production mapping's standard vector field has 768 dimensions.
        return np.asarray([[0.1] * 768 for _ in texts]), len(texts)

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> str:
        self.calls.append("chat")
        return "# Deterministic document\n## Verified text\n- Retained upload and parse content\n"


def sql_snapshot(env: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Independent connection, all columns including relationships and counters."""
    with env["engine"].connect() as db:
        return {
            model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).order_by(model.id)).mappings()]
            for model in [Document, File, File2Document, Task, Conversation, Dialog, Knowledgebase]
        }


def object_snapshot(env: dict[str, Any]) -> dict[str, bytes]:
    return {item.object_name: read_object(env["storage"], env["bucket"], item.object_name) for item in env["storage"].list_objects(env["bucket"], recursive=True)}


def index_snapshot(env: dict[str, Any]) -> list[dict[str, Any]]:
    from pymilvus import MilvusClient

    from common.config_utils import CONFIGS

    config = CONFIGS["milvus"]
    client = MilvusClient(uri=config["hosts"], user=config.get("username", ""), password=config.get("password", ""), db_name=config.get("db_name") or "default")
    try:
        if not client.has_collection(env["collection"]):
            return []
        return sorted(client.query(env["collection"], filter='pk != ""', output_fields=["*", "vector", "q_768_vec"], consistency_level="Strong"), key=lambda row: row["pk"])
    finally:
        client.close()


def assert_retired_upload_has_no_work(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Actual old requests cannot authenticate, parse, write or create temp files."""
    import tempfile
    from datetime import timedelta

    import starlette.formparsers

    from api.apps import manager
    from api.db.services import document_service, llm_service
    from core.app import naive

    before = sql_snapshot(env), object_snapshot(env), index_snapshot(env), REDIS_CONN.REDIS.xrange(env["queue"])
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("execution")
        raise AssertionError("retired upload producer executed work")

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        assert statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}

    execute = REDIS_CONN.REDIS.execute_command

    def redis_guard(command: str, *args: Any, **kwargs: Any) -> Any:
        assert command.upper() in {"PING", "GET", "MGET", "EXISTS", "SCAN", "TYPE", "TTL", "XRANGE"}, command
        return execute(command, *args, **kwargs)

    expired = manager.create_access_token(data={"sub": f"{env['owners'][0]}@upload.test"}, expires=timedelta(seconds=-1))
    url = env["base"] + "/v1/document/upload_and_parse"
    with monkeypatch.context() as scoped:
        for target, attribute in [
            (FileService, "upload_document"),
            (FileService, "parse_docs"),
            (llm_service, "LLMBundle"),
            (naive, "chunk"),
            (env["storage_adapter"], "put"),
            (settings.docStoreConn, "insert"),
            (REDIS_CONN, "queue_product"),
            (tempfile, "NamedTemporaryFile"),
            (starlette.formparsers, "SpooledTemporaryFile"),
        ]:
            scoped.setattr(target, attribute, forbidden)
        scoped.setattr(REDIS_CONN.REDIS, "execute_command", redis_guard)
        sa.event.listen(env["engine"], "before_cursor_execute", sql_guard)
        try:
            for credential in [None, env["jwt"], env["api_key"], "invalid-token", expired]:
                headers = {"Authorization": f"Bearer {credential}"} if credential else {}
                for kwargs in [
                    {},
                    {"files": {"files": ("wrong-field.txt", b"must not write")}},
                    {"data": {"conversation_id": env["conversation"]}, "files": {"file": ("retired.txt", b"must not parse", "text/plain")}},
                ]:
                    response = requests.post(url, headers=headers, timeout=30, **kwargs)
                    assert response.status_code == 404
                    assert response.json() == {"code": 404, "message": "Not Found: /v1/document/upload_and_parse", "data": None, "error": "Not Found"}
        finally:
            sa.event.remove(env["engine"], "before_cursor_execute", sql_guard)
    assert calls == []
    after = sql_snapshot(env), object_snapshot(env), index_snapshot(env), REDIS_CONN.REDIS.xrange(env["queue"])
    assert after == before
    readback = {}
    for label, snapshot in [("before", before), ("after", after)]:
        readback[label] = {"sql": snapshot[0], "objects_hex": {key: binary.hex() for key, binary in snapshot[1].items()}, "index": snapshot[2], "queue": snapshot[3]}
    readback_path = env["record_path"].with_suffix(".retirement-readback.json")
    readback_path.write_text(json.dumps(readback, default=str))
    paths = requests.get(env["base"] + "/openapi.json", timeout=30).json()["paths"]
    assert "/v1/document/upload_and_parse" not in paths
    assert not hasattr(document_service, "doc_upload_and_parse") and not hasattr(document_service, "doc_upload_and_parse_in_session")
    env["record"].setdefault("retirement", []).append(
        {
            "requests": 15,
            "sql_rows": {table: len(rows) for table, rows in before[0].items()},
            "objects": sorted(before[1]),
            "index_rows": len(before[2]),
            "queue_messages": len(before[3]),
            "unchanged": True,
            "execution_calls": calls,
            "readback": str(readback_path),
        }
    )
