"""Retired HTTP entry and retained parsers, with scratch SQL/Redis/MinIO/Milvus.

Only model config/provider output and Redis namespace routing are substituted.
Routes, file parsing, service orchestration and all storage operations are real.
No browser, remote model or background parse worker is run.
"""

import builtins
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from api.db import db_models
from api.db.db_models import Conversation, Dialog, Document, File, File2Document, Knowledgebase, Task, WritingChapter, WritingProject, WritingReferenceMaterial, get_db
from api.db.services.file_service import FileService
from api.db.services.reference_service import ReferenceService
from common import settings
from common.constants import TaskStatus
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN
from tests.integration.test_runtime_document_upload import read_object
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def parse_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
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
    with Session(env["engine"]) as db:
        db.add(Knowledgebase(id=env["kb"], tenant_id=owner, created_by=owner, name=name, embd_id="scratch-embedding", parser_id="naive", parser_config={}))
        db.add(Dialog(id=env["dialog"], tenant_id=owner, name="Parse scratch", llm_id="scratch-chat", kb_ids=[env["kb"]]))
        db.add(Conversation(id=env["conversation"], dialog_id=env["dialog"], user_id=owner, name="Parse scratch", message=[]))
        db.add(WritingProject(id=env["project"], user_id=owner, user_input="scratch", content_type="article", language_style="plain", word_count=100))
        db.add(WritingChapter(id=env["chapter"], project_id=env["project"], title="Scratch chapter"))
        db.commit()
    env["record"]["parse"] = {key: env[key] for key in ["kb", "dialog", "conversation", "project", "chapter", "collection", "queue", "cache_prefix"]}
    env["record_path"].write_text(json.dumps(env["record"]))
    try:
        yield env
    finally:
        store.delete_idx(env["collection"], env["kb"])
        assert not store.has_collection(env["collection"])
        keys = list(REDIS_CONN.REDIS.scan_iter(match=env["cache_prefix"] + "*"))
        REDIS_CONN.REDIS.delete(env["queue"], *keys)
        assert not REDIS_CONN.REDIS.exists(env["queue"])
        assert not list(REDIS_CONN.REDIS.scan_iter(match=env["cache_prefix"] + "*"))
        with Session(env["engine"]) as db:
            doc_ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == env["kb"])))
            scoped = [
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
        assert index_snapshot(env) == []
        env["record"]["parse"].update(
            documents=doc_ids,
            owned_rows=owned_rows,
            remaining=remaining,
            collection_removed=True,
            queue_removed=True,
            cache_keys=[key.decode() if isinstance(key, bytes) else key for key in keys],
            cache_removed=True,
        )


def _headers(env: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {env['jwt']}"}


def _counts(env: dict[str, Any]) -> list[int]:
    with Session(env["engine"]) as db:
        return [db.scalar(sa.select(sa.func.count()).select_from(model)) for model in [Document, File, File2Document, Task, WritingReferenceMaterial]]


def test_retired_parse_http_openapi_no_execution(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parse_api
    before = _counts(env)
    download_dir = Path("logs/downloads")
    downloads = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in download_dir.rglob("*")} if download_dir.exists() else None
    imported = builtins.__import__
    execute = REDIS_CONN.REDIS.execute_command
    calls: list[str] = []

    def guard_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("seleniumwire"):
            calls.append(name)
            raise AssertionError("retired route started a browser")
        return imported(name, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("parse or storage execution")
        raise AssertionError("retired route executed work")

    def redis_guard(command: str, *args: Any, **kwargs: Any) -> Any:
        assert command.upper() in {"PING", "GET", "MGET", "EXISTS", "SCAN", "TYPE", "TTL", "XRANGE"}, command
        return execute(command, *args, **kwargs)

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        assert statement.lstrip().split()[0].upper() not in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}

    with monkeypatch.context() as scoped:
        scoped.setattr(builtins, "__import__", guard_import)
        scoped.setattr(FileService, "parse_docs", forbidden)
        scoped.setattr(env["storage_adapter"], "put", forbidden)
        scoped.setattr(REDIS_CONN, "queue_product", forbidden)
        scoped.setattr(REDIS_CONN, "set", forbidden)
        scoped.setattr(REDIS_CONN.REDIS, "execute_command", redis_guard)
        sa.event.listen(env["engine"], "before_cursor_execute", sql_guard)
        try:
            for headers in [{}, _headers(env), {"Authorization": f"Bearer {env['api_key']}"}, {"Authorization": "Bearer invalid-token"}]:
                for kwargs in [{}, {"data": {"url": "https://example.com/retired"}}, {"files": {"files": ("retired.txt", b"must not parse")}}]:
                    response = requests.post(env["base"] + "/v1/document/parse", headers=headers, timeout=30, **kwargs)
                    assert response.status_code == 404
                    assert response.json() == {"code": 404, "message": "Not Found: /v1/document/parse", "data": None, "error": "Not Found"}
        finally:
            sa.event.remove(env["engine"], "before_cursor_execute", sql_guard)
    assert calls == [] and _counts(env) == before
    assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
    assert not REDIS_CONN.REDIS.exists(env["queue"])
    assert not settings.docStoreConn.has_collection(env["collection"])
    after = {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in download_dir.rglob("*")} if download_dir.exists() else None
    assert after == downloads
    paths = requests.get(env["base"] + "/openapi.json", timeout=30).json()["paths"]
    assert "/v1/document/parse" not in paths
    for path in ["/api/v1/datasets/{dataset_id}/documents/parse", "/v1/write/api/reference-materials/parse", "/v1/document/web_parse"]:
        assert "post" in paths[path]
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, text=True, capture_output=True, timeout=60)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_dataset_parse_real_task_and_redis(parse_api: dict[str, Any]) -> None:
    env = parse_api
    with Session(env["engine"]) as db:
        kb = db.get(Knowledgebase, env["kb"])
        errors, files = FileService.upload_document(db, kb, [(b"Queued deterministic document.", "queued.txt")], env["owners"][0])
        assert errors == [] and len(files) == 1
        doc_id = files[0][0]["id"]
    url = f"{env['base']}/api/v1/datasets/{env['kb']}/documents/parse"
    denied = requests.post(url, json={"document_ids": [doc_id]}, timeout=30)
    assert denied.status_code == 401 and denied.json()["code"] == 401
    malformed = requests.post(url, headers=_headers(env), json={"documents": [doc_id]}, timeout=30)
    assert malformed.status_code == 422 and any("document_ids" in error["loc"] for error in malformed.json()["detail"])
    missing = requests.post(url, headers=_headers(env), json={"document_ids": [uuid4().hex]}, timeout=30)
    assert missing.status_code == 200 and missing.json()["code"] == 102
    assert not REDIS_CONN.REDIS.exists(env["queue"])
    response = requests.post(url, headers=_headers(env), json={"document_ids": [doc_id]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0 and response.json()["data"] == {"success_count": 1}
    with Session(env["engine"]) as db:
        doc = db.get(Document, doc_id)
        # begin2parse records a small initial progress while the worker is queued.
        assert doc.run == TaskStatus.RUNNING.value and 0 <= doc.progress < 0.01
        tasks = list(db.scalars(sa.select(Task).where(Task.doc_id == doc_id)))
        assert len(tasks) == 1 and tasks[0].progress == 0
        task_id = tasks[0].id
        assert read_object(env["storage"], env["bucket"], f"{env['kb']}/{doc.location}") == b"Queued deterministic document."
    messages = REDIS_CONN.REDIS.xrange(env["queue"])
    assert len(messages) == 1
    payload = json.loads(messages[0][1]["message"])
    assert payload["id"] == task_id and payload["doc_id"] == doc_id
    assert not settings.docStoreConn.has_collection(env["collection"])


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


def test_retired_conversation_upload_parse_preserves_real_sql_objects_index_queue(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parse_api
    binary = b"Existing conversation dataset source must survive route retirement."
    with Session(env["engine"]) as db:
        errors, files = FileService.upload_document(db, db.get(Knowledgebase, env["kb"]), [(binary, "existing.txt")], env["owners"][0])
        assert errors == [] and len(files) == 1
        doc_id = files[0][0]["id"]
        task_id = uuid4().hex
        db.add(Task(id=task_id, doc_id=doc_id, task_type="Parse"))
        db.commit()
    parent, child = uuid4().hex, uuid4().hex
    chunks = [
        {
            "id": identifier,
            "pk": identifier,
            "doc_id": doc_id,
            "kb_id": env["kb"],
            "content_with_weight": binary.decode(),
            "available_int": available,
            "mom_id": mother,
            "vector": [0.1] * 768,
            "q_768_vec": [0.1] * 768,
        }
        for identifier, available, mother in [(parent, 0, ""), (child, 1, parent)]
    ]
    assert settings.docStoreConn.insert(chunks, env["collection"], env["kb"]) == []
    assert REDIS_CONN.queue_product(env["queue"], {"id": task_id, "doc_id": doc_id, "task_type": "Parse"})
    assert len(index_snapshot(env)) == 2 and len(REDIS_CONN.REDIS.xrange(env["queue"])) == 1
    assert_retired_upload_has_no_work(env, monkeypatch)
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, text=True, capture_output=True, timeout=60)
    env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_write_reference_retains_real_shared_text_parser(parse_api: dict[str, Any]) -> None:
    env = parse_api
    text = "Actual deterministic reference text survives removal of the unused route."
    assert text in FileService.parse_docs([(text.encode(), "reference.txt")], "system")
    assert text in ReferenceService.parse_file_content(text.encode(), "reference.txt")
    url = env["base"] + "/v1/write/api/reference-materials/parse"
    before = _counts(env)
    denied = requests.post(url, data={"chapter_id": env["chapter"]}, timeout=30)
    assert denied.status_code == 401 and denied.json()["code"] == 401
    malformed = requests.post(url, headers=_headers(env), files={"file": ("reference.txt", text.encode())}, timeout=30)
    assert malformed.status_code == 422 and any("chapter_id" in error["loc"] for error in malformed.json()["detail"])
    assert _counts(env) == before
    response = requests.post(url, headers=_headers(env), data={"chapter_id": env["chapter"]}, files={"file": ("reference.txt", text.encode(), "text/plain")}, timeout=30)
    body = response.json()
    assert response.status_code == 200 and body["retcode"] == 0 and text in body["data"]["summary"]
    with Session(env["engine"]) as db:
        reference = db.get(WritingReferenceMaterial, body["data"]["id"])
        assert reference.chapter_id == env["chapter"] and reference.type == "file" and reference.source == "reference.txt"
        assert text in reference.content
    assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
    assert not REDIS_CONN.REDIS.exists(env["queue"])
