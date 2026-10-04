"""Retired HTTP entry and retained parsers, with scratch SQL/Redis/MinIO/Milvus.

Only model config/provider output and Redis namespace routing are substituted.
Routes, file parsing, service orchestration and all storage operations are real.
No browser, remote model or background parse worker is run."""

import builtins
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, WritingReferenceMaterial
from api.db.services.file_service import FileService
from api.db.services.reference_service import ReferenceService
from common import settings
from common.constants import TaskStatus
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_parse_retirement import _counts, _headers, assert_retired_upload_has_no_work, index_snapshot
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


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
