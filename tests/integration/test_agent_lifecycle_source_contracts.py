"""Agent labels and source failures on scratch SQL, real Redis and object storage."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from agent.canvas import Canvas
from api.apps.services import agent_background_tasks
from api.db.db_models import Document, Task, Tenant, User, UserCanvas, UserTenant, get_async_db, get_db
from core.svr import task_executor
from core.utils.minio_conn import MultiRAGMinio
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import message_dsl
from tests.support.database import scratch_database


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    path = Path(os.environ.get("B_EVIDENCE", str(tmp_path)))
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def source_env(evidence: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api import apps
    from api.db import db_models

    with scratch_database(evidence / "resources") as engine:
        sessions = sessionmaker(engine, expire_on_commit=False)
        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        identifiers = [uuid4().hex for _ in range(3)]
        (evidence / "identifiers.json").write_text(json.dumps({"owners": identifiers, "database": engine.url.database, "Redis": [f"*{key}*" for key in identifiers]}))
        with sessions() as db:
            for owner in identifiers:
                db.add(User(id=owner, email=f"{owner}@lifecycle.example.com", nickname="Lifecycle", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name="Lifecycle", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=owner, role="owner", invited_by=owner))
            db.commit()

        closed_sessions: list[bool] = []

        def db_dependency() -> Iterator[Session]:
            with sessions() as db:
                try:
                    yield db
                finally:
                    db.expire_all()
                    closed_sessions.append(True)

        async def async_dependency() -> AsyncIterator[AsyncSession]:
            async with async_sessions() as db:
                yield db

        monkeypatch.setattr(apps, "SessionLocal", sessions)
        monkeypatch.setattr(db_models, "SessionLocal", sessions)
        monkeypatch.setattr(db_models, "async_session_factory", async_sessions)
        monkeypatch.setitem(apps.app.dependency_overrides, get_db, db_dependency)
        monkeypatch.setitem(apps.app.dependency_overrides, get_async_db, async_dependency)
        client = TestClient(apps.app)
        tokens = [apps.manager.create_access_token(data={"sub": f"{owner}@lifecycle.example.com"}) for owner in identifiers]
        client.headers["Authorization"] = f"Bearer {tokens[0]}"
        env = {"engine": engine, "sessions": sessions, "client": client, "owners": identifiers, "tokens": tokens, "canvas_ids": [], "closed_sessions": closed_sessions}
        try:
            yield env
        finally:
            client.close()
            asyncio.run(async_engine.dispose())
            redis = REDIS_CONN.REDIS
            assert redis is not None
            for identifier in [*identifiers, *env["canvas_ids"]]:
                keys = list(redis.scan_iter(match=f"*{identifier}*"))
                if keys:
                    redis.delete(*keys)
                assert not list(redis.scan_iter(match=f"*{identifier}*"))
            (evidence / "redis-cleanup.json").write_text(json.dumps({"identifiers": [*identifiers, *env["canvas_ids"]], "absent": True}))


def test_canvas_label_create_update_omit_clear_and_denials(source_env: dict[str, Any], evidence: Path) -> None:
    env = source_env
    client = env["client"]
    created = client.post("/api/v1/agents", json={"title": "Tagged agent", "dsl": message_dsl("hello"), "canvas_type": "Marketing"})
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    canvas_id = created.json()["data"]["id"]
    env["canvas_ids"].append(canvas_id)
    (evidence / "agent.json").write_text(json.dumps({"id": canvas_id, "Redis": f"*{canvas_id}*"}))

    def read() -> tuple[Any, ...]:
        with env["sessions"]() as db:
            row = db.get(UserCanvas, canvas_id)
            assert row is not None
            return row.canvas_type, row.canvas_category, row.title, row.dsl

    assert read()[:2] == ("Marketing", "agent_canvas")
    assert client.get(f"/api/v1/agents/{canvas_id}").json()["data"]["canvas_type"] == "Marketing"
    listed = client.get("/api/v1/agents").json()
    assert listed["retcode"] == 0 and next(row for row in listed["data"]["canvas"] if row["id"] == canvas_id)["canvas_type"] == "Marketing"
    for payload, label in [({"canvas_type": "Finance"}, "Finance"), ({"title": "Renamed"}, "Finance"), ({"canvas_type": None}, "Finance"), ({"canvas_type": ""}, "")]:
        response = client.put(f"/api/v1/agents/{canvas_id}", json=payload)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        assert read()[:2] == (label, "agent_canvas")
    before = read()
    for token in [env["tokens"][1], "invalid", ""]:
        response = client.put(f"/api/v1/agents/{canvas_id}", headers={"Authorization": f"Bearer {token}"}, json={"canvas_type": "Forbidden"})
        assert response.status_code == 401 or response.json()["retcode"] != 0
        assert read() == before
    for invalid in ["x" * 33, 42, {}, []]:
        response = client.put(f"/api/v1/agents/{canvas_id}", json={"canvas_type": invalid})
        assert response.status_code == 422 and read() == before
    response = client.post("/api/v1/agents", json={"title": "Default label", "dsl": message_dsl("hello"), "canvas_type": None})
    assert response.json()["retcode"] == 0, response.text
    default_id = response.json()["data"]["id"]
    env["canvas_ids"].append(default_id)
    with env["sessions"]() as db:
        assert db.get(UserCanvas, default_id).canvas_type == ""


async def test_missing_storage_stops_worker_and_pdf_recovery_with_sql_readback(source_env: dict[str, Any], evidence: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = source_env
    shared = MultiRAGMinio()
    storage = object.__new__(type(shared))
    storage.conn, storage.bucket, storage.prefix_path = shared.conn, None, None
    bucket = f"b-source-{uuid4().hex}"
    doc_id, task_id = uuid4().hex, uuid4().hex
    manifest = {"bucket": bucket, "objects": ["empty.txt", "sibling.txt"], "missing": "missing.txt", "doc": doc_id, "task": task_id, "absent": False}
    manifest_path = evidence / "source-storage.json"
    manifest_path.write_text(json.dumps(manifest))
    with env["sessions"]() as db:
        db.add(Document(id=doc_id, kb_id=bucket, parser_id="naive", type="txt", created_by=env["owners"][0], name="source.txt", location="missing.txt"))
        db.add(Task(id=task_id, doc_id=doc_id))
        db.commit()
    storage.conn.make_bucket(bucket)
    try:
        for key, data in [("empty.txt", b""), ("sibling.txt", b"preserve sibling")]:
            storage.conn.put_object(bucket, key, BytesIO(data), len(data))
        monkeypatch.setattr(task_executor.settings, "STORAGE_IMPL", storage)
        task = {
            "id": task_id,
            "doc_id": doc_id,
            "kb_id": bucket,
            "tenant_id": env["owners"][0],
            "parser_id": "naive",
            "name": "source.txt",
            "location": "missing.txt",
            "size": 0,
            "from_page": 0,
            "to_page": 1,
            "language": "English",
            "parser_config": {},
            "embd_id": "controlled",
            "llm_id": "controlled",
        }
        with env["sessions"]() as db:
            parser = AsyncMock()
            monkeypatch.setitem(task_executor.FACTORY, "naive", type("Parser", (), {"chunk": lambda *args, **kwargs: pytest.fail("missing content parsed")})())
            monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: pytest.fail("missing source bound embedding model"))
            monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args, **kwargs: {})
            monkeypatch.setattr(task_executor, "init_kb", AsyncMock())
            monkeypatch.setattr(task_executor, "embedding", parser)
            inserted = AsyncMock()
            monkeypatch.setattr(task_executor, "insert_chunks", inserted)
            monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *args: type("Dataset", (), {"name": "source", "parser_config": {}, "llm_id": ""})())
            with pytest.raises(FileNotFoundError, match="storage returned no content"):
                await task_executor.do_handle_task(db, task)
            assert not parser.called and not inserted.called
            assert await task_executor.get_storage_binary(bucket, "empty.txt") == b""
            assert not await task_executor._persist_pdf_outline_from_storage(db, doc_id, "source.pdf")
        with env["sessions"]() as db:
            persisted = db.get(Task, task_id)
            document = db.get(Document, doc_id)
            assert persisted.progress == -1 and "Source file not found" in persisted.progress_msg
            assert bucket not in persisted.progress_msg and "missing.txt" not in persisted.progress_msg
            assert document.chunk_num == document.token_num == 0 and document.progress == 0
        assert storage.get_bytes(bucket, "missing.txt") is None
        assert storage.get_bytes(bucket, "sibling.txt") == b"preserve sibling"
        assert {item.object_name for item in storage.conn.list_objects(bucket)} == {"empty.txt", "sibling.txt"}
    finally:
        for obj in storage.conn.list_objects(bucket):
            storage.conn.remove_object(bucket, obj.object_name)
        storage.conn.remove_bucket(bucket)
        assert not storage.conn.bucket_exists(bucket)
        manifest["absent"] = True
        manifest_path.write_text(json.dumps(manifest))


async def test_webhook_http_releases_request_db_before_task_owned_sql_write(source_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api import apps

    env = source_env
    started, proceed, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    canvas_id = uuid4().hex
    env["canvas_ids"].append(canvas_id)
    dsl = {"components": {"begin": {"obj": {"component_name": "Begin", "params": {"mode": "Webhook", "execution_mode": "Immediately"}}}}}
    with env["sessions"]() as db:
        db.add(UserCanvas(id=canvas_id, user_id=env["owners"][0], title="Before", canvas_type="Marketing", dsl=dsl))
        db.commit()
    monkeypatch.setattr(Canvas, "__init__", lambda *args, **kwargs: None)
    monkeypatch.setattr(Canvas, "__str__", lambda self: json.dumps({"executed": True}))

    async def run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        started.set()
        try:
            await proceed.wait()
            yield {"event": "done"}
        finally:
            closed.set()

    monkeypatch.setattr(Canvas, "run", run)
    try:
        before = len(env["closed_sessions"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps.app), base_url="http://scratch") as client:
            response = await client.post(f"/api/v1/agents/{canvas_id}/webhook", json={})
            assert response.status_code == 200
            await started.wait()
            assert len(env["closed_sessions"]) > before
            # A later edit must survive background completion; only runtime DSL
            # belongs to this task, not the request's stale ORM snapshot.
            with env["sessions"]() as db:
                db.execute(sa.update(UserCanvas).where(UserCanvas.id == canvas_id).values(title="Concurrent edit", canvas_type="Finance"))
                db.commit()
            tasks = list(agent_background_tasks._tasks)
            assert len(tasks) == 1
            proceed.set()
            await asyncio.gather(*tasks)
            assert closed.is_set() and not agent_background_tasks._tasks
            with env["sessions"]() as db:
                saved = db.get(UserCanvas, canvas_id)
                assert saved.dsl == {"executed": True}
                assert (saved.title, saved.canvas_type) == ("Concurrent edit", "Finance")
    finally:
        await agent_background_tasks.stop_agent_tasks()
