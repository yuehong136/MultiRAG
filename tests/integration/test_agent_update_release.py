"""Agent publish/draft HTTP contracts against isolated PostgreSQL and real Redis."""

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn
from requests import Session as HTTPSession
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from agent.canvas import Canvas
from api.db.db_models import API4Conversation, APIToken, Tenant, User, UserCanvas, UserCanvasVersion, UserTenant, get_async_db, get_db
from core.utils.redis_conn import REDIS_CONN


def message_dsl(content: str) -> dict[str, Any]:
    return {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {"prologue": content}}, "downstream": ["Message:answer"], "upstream": []},
            "Message:answer": {"obj": {"component_name": "Message", "params": {"content": [content]}}, "downstream": [], "upstream": ["begin"]},
        },
        "path": [],
        "history": [],
        "retrieval": [],
    }


@pytest.fixture
def release_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api import apps
    from api.db import db_models

    owners = [uuid4().hex, uuid4().hex]
    api_keys = [f"release-test-{uuid4().hex}", f"release-test-{uuid4().hex}"]
    sync_sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    with sync_sessions() as db:
        for owner, token in zip(owners, api_keys, strict=True):
            db.add(User(id=owner, email=f"{owner}@release.test", nickname="Release test", password="unused", access_token="active"))
            db.add(Tenant(id=owner, name="Release scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=owner, role="owner", invited_by=owner))
            db.add(APIToken(tenant_id=owner, token=token, name="release-test"))
        db.commit()
    # Keep real JWT/API-key verification and user loading pointed at scratch.
    monkeypatch.setattr(apps, "SessionLocal", sync_sessions)
    monkeypatch.setattr(db_models, "SessionLocal", sync_sessions)
    async_engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
    async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    def scratch_db() -> Iterator[Session]:
        with sync_sessions() as db:
            yield db

    async def scratch_async_db() -> AsyncIterator[AsyncSession]:
        async with async_sessions() as db:
            yield db

    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
    monkeypatch.setitem(apps.app.dependency_overrides, get_async_db, scratch_async_db)
    task_ids: list[str] = []
    original_init = Canvas.__init__

    def track_canvas(self: Canvas, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        task_ids.append(self.task_id)

    monkeypatch.setattr(Canvas, "__init__", track_canvas)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        with HTTPSession() as client:
            client.headers["Authorization"] = f"Bearer {apps.manager.create_access_token(data={'sub': f'{owners[0]}@release.test'})}"
            yield {
                "base": base,
                "owners": owners,
                "keys": api_keys,
                "client": client,
                "engine": bootstrapped_engine,
                "foreign_jwt": apps.manager.create_access_token(data={"sub": f"{owners[1]}@release.test"}),
            }
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        asyncio.run(async_engine.dispose())
        with sync_sessions() as db:
            canvas_ids = list(db.scalars(sa.select(UserCanvas.id).where(UserCanvas.user_id.in_(owners))))
            session_ids = list(db.scalars(sa.select(API4Conversation.id).where(API4Conversation.dialog_id.in_(canvas_ids))))
            db.execute(sa.delete(API4Conversation).where(API4Conversation.dialog_id.in_(canvas_ids)))
            db.execute(sa.delete(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id.in_(canvas_ids)))
            db.execute(sa.delete(UserCanvas).where(UserCanvas.id.in_(canvas_ids)))
            db.execute(sa.delete(APIToken).where(APIToken.token.in_(api_keys)))
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id.in_(owners)))
            db.execute(sa.delete(Tenant).where(Tenant.id.in_(owners)))
            db.execute(sa.delete(User).where(User.id.in_(owners)))
            db.commit()
            for model, column, ids in [
                (UserCanvas, UserCanvas.id, canvas_ids),
                (UserCanvasVersion, UserCanvasVersion.user_canvas_id, canvas_ids),
                (API4Conversation, API4Conversation.id, session_ids),
                (User, User.id, owners),
                (UserTenant, UserTenant.user_id, owners),
                (Tenant, Tenant.id, owners),
                (APIToken, APIToken.token, api_keys),
            ]:
                assert db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(ids))) == 0
        redis = REDIS_CONN.REDIS
        assert redis is not None
        for identifier in [*owners, *canvas_ids, *session_ids, *task_ids]:
            keys = list(redis.scan_iter(match=f"*{identifier}*"))
            if keys:
                redis.delete(*keys)
            assert not list(redis.scan_iter(match=f"*{identifier}*"))
        assert not thread.is_alive()
        print("release acceptance cleanup: scratch users/tokens/canvases/versions/sessions, owned Redis keys and HTTP listener removed")


def read_state(env: dict[str, Any], agent_id: str) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        canvas = db.get(UserCanvas, agent_id)
        assert canvas is not None
        versions = list(db.scalars(sa.select(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == agent_id).order_by(UserCanvasVersion.create_time.desc())))
        return {"title": canvas.title, "release": canvas.release, "dsl": canvas.dsl, "versions": [{"id": v.id, "title": v.title, "release": v.release, "dsl": v.dsl} for v in versions]}


def update(env: dict[str, Any], agent_id: str, payload: dict[str, Any]) -> None:
    response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json=payload, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.json()["data"] is True


def assert_published_runtime(env: dict[str, Any], agent_id: str, published: str, draft: str) -> None:
    before = read_state(env, agent_id)
    response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": True}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    session_id = response.json()["data"]["id"]
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None
        assert session.dsl["components"]["begin"]["obj"]["params"]["prologue"] == published
        assert session.message[0]["content"] == published
        assert session.version_title == next(v["title"] for v in before["versions"] if v["release"])
    # A fresh published run must use the stored snapshot even while Redis holds
    # the current draft. Execute real Begin/Message components, no remote LLM.
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json={"agent_id": agent_id, "release": True, "query": "test", "stream": False}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.json()["data"]["data"]["content"] == published
    replica = json.loads(REDIS_CONN.get(f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"))
    assert replica["dsl"]["components"]["begin"]["obj"]["params"]["prologue"] == draft
    assert read_state(env, agent_id) == before


def test_http_agent_publish_draft_and_readback(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Release contract", "dsl": message_dsl("published-v1")}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    agent_id = response.json()["data"]["id"]
    update(env, agent_id, {"dsl": json.dumps(message_dsl("published-v1")), "release": True})
    published = read_state(env, agent_id)
    detail = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}", timeout=30).json()
    assert detail["retcode"] == 0 and detail["data"]["release"] is True and detail["data"]["last_publish_time"] is not None
    assert published["release"] is True and published["versions"][0]["release"] is True
    update(env, agent_id, {"dsl": message_dsl("published-v1"), "release": True})
    assert read_state(env, agent_id) == published
    update(env, agent_id, {"title": " renamed ", "release": None})
    metadata = read_state(env, agent_id)
    assert metadata["title"] == "renamed" and metadata["release"] is True and metadata["versions"] == published["versions"]
    update(env, agent_id, {"release": "false"})
    draft = read_state(env, agent_id)
    assert draft["release"] is False and draft["versions"][0]["release"] is False and draft["versions"][1]["release"] is True
    update(env, agent_id, {"dsl": message_dsl("published-v1")})
    assert read_state(env, agent_id) == draft
    update(env, agent_id, {"dsl": message_dsl("draft-v2"), "release": None})
    draft = read_state(env, agent_id)
    assert draft["release"] is False and draft["versions"][0]["release"] is False
    assert draft["versions"][-1] == published["versions"][0]
    detail = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}", timeout=30).json()["data"]
    assert detail["release"] is False and detail["last_publish_time"] is not None
    assert_published_runtime(env, agent_id, "published-v1", "draft-v2")
    update(env, agent_id, {"release": "true"})
    assert read_state(env, agent_id)["versions"][0]["release"] is True
    update(env, agent_id, {"dsl": message_dsl("published-v3"), "release": True})
    published = read_state(env, agent_id)
    assert published["release"] is True and published["versions"][0]["dsl"] == message_dsl("published-v3")
    for invalid in ["yes", "arbitrary", 1, 0, [], {}]:
        response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json={"title": "must not save", "release": invalid}, timeout=30)
        assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["body", "release"]
        assert read_state(env, agent_id) == published
    for auth in [None, "invalid", env["foreign_jwt"], env["keys"][1]]:
        headers = {"Authorization": f"Bearer {auth}"} if auth else {"Authorization": ""}
        response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", headers=headers, json={"release": False}, timeout=30)
        assert response.json()["code" if response.status_code == 401 else "retcode"] != 0 and read_state(env, agent_id) == published
    response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json={"dsl": "[]", "release": True}, timeout=30)
    assert response.json()["retcode"] != 0 and read_state(env, agent_id) == published
    # The same update route accepts the owner's SDK API key.
    response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", headers={"Authorization": f"Bearer {env['keys'][0]}"}, json={"release": False}, timeout=30)
    assert response.json()["retcode"] == 0
    assert_published_runtime(env, agent_id, "published-v3", "published-v3")
    versions = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/versions", timeout=30).json()
    assert versions["retcode"] == 0
    assert {v["id"]: v["release"] for v in versions["data"]} == {v["id"]: v["release"] for v in read_state(env, agent_id)["versions"]}
    from api.apps.services.canvas_replica_service import CanvasReplicaService

    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    with monkeypatch.context() as patch:
        patch.setattr(CanvasReplicaService, "replace_for_set", lambda **kwargs: False)
        response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json={"dsl": message_dsl("replica-retry-draft")}, timeout=30)
    assert response.json()["retcode"] != 0 and response.json()["retmsg"] == "agent saved, but replica sync failed."
    assert read_state(env, agent_id)["release"] is False
    assert read_state(env, agent_id)["dsl"] == message_dsl("replica-retry-draft")
    assert REDIS_CONN.get(replica_key) == replica_before
    update(env, agent_id, {"dsl": message_dsl("replica-retry-draft")})
    assert_published_runtime(env, agent_id, "published-v3", "replica-retry-draft")
    smoke = subprocess.run(["make", "smoke"], cwd=Path(__file__).resolve().parents[2], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    print(
        "release HTTP acceptance: JWT/API-key publish/draft/metadata/string normalization, independent PostgreSQL canvas/version/session + Redis draft, real constant Message runtime, make smoke passed"
    )


@pytest.mark.parametrize(("table", "event"), [("t_ai_user_canvas_version", "INSERT"), ("t_ai_user_canvases", "UPDATE"), ("t_ai_user_canvas_version", "DELETE")])
def test_http_agent_write_failure_rolls_back(release_api: dict[str, Any], table: str, event: str) -> None:
    env = release_api
    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Rollback contract", "dsl": message_dsl("original")}, timeout=30)
    assert response.json()["retcode"] == 0, response.text
    agent_id = response.json()["data"]["id"]
    if event == "DELETE":
        # Make trimming necessary, then fail its real PostgreSQL DELETE after
        # the new version was flushed in the request transaction.
        with Session(env["engine"]) as db:
            for index in range(21):
                db.add(UserCanvasVersion(id=uuid4().hex, user_canvas_id=agent_id, dsl=message_dsl(f"draft-{index}"), release=False, create_time=index))
            db.commit()
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica = REDIS_CONN.get(replica_key)
    identifier = f"release_fail_{uuid4().hex}"
    with env["engine"].begin() as conn:
        conn.execute(sa.text(f"CREATE FUNCTION usr_ai.{identifier}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'release acceptance write failure'; END $$"))
        conn.execute(sa.text(f"CREATE TRIGGER {identifier} BEFORE {event} ON usr_ai.{table} FOR EACH ROW EXECUTE FUNCTION usr_ai.{identifier}()"))
    try:
        response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json={"title": "must roll back", "dsl": message_dsl("must roll back"), "release": True}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] != 0, response.text
        assert response.json().get("data") is not True
        assert read_state(env, agent_id) == before
        assert REDIS_CONN.get(replica_key) == replica
    finally:
        with env["engine"].begin() as conn:
            conn.execute(sa.text(f"DROP TRIGGER {identifier} ON usr_ai.{table}"))
            conn.execute(sa.text(f"DROP FUNCTION usr_ai.{identifier}()"))
    update(env, agent_id, {"release": True})
    assert read_state(env, agent_id)["release"] is True
    assert sum(not version["release"] for version in read_state(env, agent_id)["versions"]) <= 20
