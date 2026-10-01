"""Agent publish/draft HTTP contracts against isolated PostgreSQL and real Redis."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import BackgroundTasks
from requests import Session as HTTPSession
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from agent.canvas import Canvas
from api.db import UserTenantRole
from api.db.db_models import API4Conversation, APIToken, Tenant, User, UserCanvas, UserCanvasVersion, UserTenant, get_async_db, get_db
from common.constants import StatusEnum
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

    owners = [uuid4().hex for _ in range(3)]
    api_keys = [f"release-test-{uuid4().hex}" for _ in owners]
    sync_sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    with sync_sessions() as db:
        for owner, token in zip(owners, api_keys, strict=True):
            db.add(User(id=owner, email=f"{owner}@release.example.com", nickname="Release test", password="unused", access_token="active"))
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
    runtime_tenants: list[str | None] = []
    original_init = Canvas.__init__

    def track_canvas(self: Canvas, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        task_ids.append(self.task_id)
        runtime_tenants.append(args[1] if len(args) > 1 else kwargs.get("tenant_id"))

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
            client.headers["Authorization"] = f"Bearer {apps.manager.create_access_token(data={'sub': f'{owners[0]}@release.example.com'})}"
            yield {
                "base": base,
                "owners": owners,
                "keys": api_keys,
                "client": client,
                "engine": bootstrapped_engine,
                "task_ids": task_ids,
                "runtime_tenants": runtime_tenants,
                "jwts": [apps.manager.create_access_token(data={"sub": f"{owner}@release.example.com"}) for owner in owners],
                "foreign_jwt": apps.manager.create_access_token(data={"sub": f"{owners[1]}@release.example.com"}),
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


def invite_member(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caller: int) -> None:
    scheduled: list[tuple[str, str]] = []

    def record_email(background_tasks: BackgroundTasks, tenant_id: str, to_email: str, inviter: str) -> None:
        scheduled.append((tenant_id, to_email))

    email = f"{env['owners'][caller]}@release.example.com"
    # Exercise real invitation/auth/SQL; intercept only outbound mail scheduling.
    with monkeypatch.context() as patch:
        patch.setattr(sys.modules["api.apps.restful_apis.tenant"], "_schedule_invite_email", record_email)
        response = env["client"].post(f"{env['base']}/api/v1/tenants/{env['owners'][0]}/users", json={"email": email}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert scheduled == [(env["owners"][0], email)]
    with Session(env["engine"]) as db:
        membership = db.scalar(sa.select(UserTenant).where(UserTenant.user_id == env["owners"][caller], UserTenant.tenant_id == env["owners"][0]))
        assert membership is not None and membership.role == UserTenantRole.INVITE and membership.status == StatusEnum.VALID.value
        personal = db.scalar(sa.select(UserTenant).where(UserTenant.user_id == env["owners"][caller], UserTenant.tenant_id == env["owners"][caller]))
        assert personal is not None and personal.role == UserTenantRole.OWNER and personal.status == StatusEnum.VALID.value


def join_member(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caller: int, role: UserTenantRole) -> None:
    invite_member(env, monkeypatch, caller)
    response = env["client"].patch(f"{env['base']}/api/v1/tenants/{env['owners'][0]}", headers={"Authorization": f"Bearer {env['jwts'][caller]}"}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    if role == UserTenantRole.ADMIN:
        response = env["client"].put(f"{env['base']}/api/v1/tenants/{env['owners'][0]}/users/{env['owners'][caller]}/role", json={"role": role}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    with Session(env["engine"]) as db:
        membership = db.scalar(sa.select(UserTenant).where(UserTenant.user_id == env["owners"][caller], UserTenant.tenant_id == env["owners"][0]))
        assert membership is not None and membership.role == role and membership.status == StatusEnum.VALID.value


def denied_membership(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    if case == "invite":
        invite_member(env, monkeypatch, 2)
        return
    role = UserTenantRole.ADMIN if case == "inactive_admin" else UserTenantRole.NORMAL
    join_member(env, monkeypatch, 2, role)
    # Removal APIs delete rows; seed only the legacy invalid status under test.
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][2], UserTenant.tenant_id == env["owners"][0]).values(status=StatusEnum.INVALID.value))
        db.commit()
        membership = db.scalar(sa.select(UserTenant).where(UserTenant.user_id == env["owners"][2], UserTenant.tenant_id == env["owners"][0]))
        assert membership is not None and membership.role == role and membership.status == StatusEnum.INVALID.value


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


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", ["unpublished", "missing", "private", "nonmember", "invite", "inactive_member", "inactive_admin", "invalid_identity", "inactive_identity", "missing_auth"])
def test_published_run_denials_before_stream(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, case: str) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Denied release", "dsl": message_dsl("draft")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    if case != "unpublished":
        update(env, agent_id, {"release": True, "permission": "team" if case in {"nonmember", "invite", "inactive_member", "inactive_admin"} else "me"})
    if case in {"invite", "inactive_member", "inactive_admin"}:
        denied_membership(env, monkeypatch, case)
    if case == "private":
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][2], tenant_id=env["owners"][0], role="normal", invited_by=env["owners"][0]))
            db.commit()
    if case == "inactive_identity":
        with Session(env["engine"]) as db:
            db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][0], UserTenant.tenant_id == env["owners"][0]).values(status="0"))
            db.commit()
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    headers = {}
    if case in {"private", "nonmember", "invite", "inactive_member", "inactive_admin"}:
        headers["Authorization"] = f"Bearer {env['jwts'][2]}"
    if case == "invalid_identity":
        headers["Authorization"] = "Bearer invalid"
    if case == "missing_auth":
        headers["Authorization"] = ""
    requested_agent = uuid4().hex if case == "missing" else agent_id
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completion", headers=headers, json={"agent_id": requested_agent, "release": True, "stream": stream, "user_id": env["owners"][0]}, timeout=30
    )
    auth_failure = case in {"invalid_identity", "inactive_identity", "missing_auth"}
    expected_status = 401 if auth_failure else {"unpublished": 409, "missing": 404}.get(case, 403)
    assert response.status_code == expected_status, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code" if auth_failure else "retcode"] != 0
    assert response.json().get("data") is not True
    if case in {"private", "nonmember", "invite", "inactive_member", "inactive_admin"}:
        assert response.json()["retcode"] == 103 and response.json()["data"] is False
    with Session(env["engine"]) as db:
        assert (
            db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(sa.or_(API4Conversation.dialog_id.in_([agent_id, requested_agent]), API4Conversation.user_id.in_(env["owners"]))))
            == 0
        )
    assert env["task_ids"] == []
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before


def legacy_list_dsl(content: str, has_orphan: bool) -> dict[str, Any]:
    dsl = message_dsl(content)
    dsl["variables"] = []
    dsl["history"] = [["user", "stale"]]
    dsl["globals"] = {"sys.query": "stale", "sys.user_id": "stale", "sys.conversation_turns": 8, "sys.files": [], "sys.history": ["stale"]}
    if has_orphan:
        dsl["globals"]["env.orphan"] = ["stale"]
    return dsl


def assert_legacy_list_reset(dsl: dict[str, Any], has_orphan: bool) -> None:
    assert dsl["variables"] == []
    assert dsl["history"] == [] and dsl["path"] == []
    assert dsl["globals"]["sys.history"] == [] and dsl["globals"]["sys.conversation_turns"] == 0
    if has_orphan:
        assert dsl["globals"]["env.orphan"] == ""
    else:
        assert not any(key.startswith("env.") for key in dsl["globals"])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("has_orphan", [False, True])
def test_http_legacy_empty_list_variables_all_reset_paths(release_api: dict[str, Any], stream: bool, has_orphan: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Legacy list variables", "dsl": legacy_list_dsl("published-list", has_orphan)}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    update(env, agent_id, {"dsl": legacy_list_dsl("draft-list", has_orphan)})
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    session_ids = []
    for release in (True, False):
        response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": release}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        session_ids.append(response.json()["data"]["id"])
        with Session(env["engine"]) as db:
            session = db.get(API4Conversation, session_ids[-1])
            assert session is not None
            assert_legacy_list_reset(session.dsl, has_orphan)
            if not release:
                untouched_before = session.to_dict()
    # A persisted legacy list session already has runtime state. Continuing it
    # must not apply the new-session reset or clear orphan env runtime values.
    with Session(env["engine"]) as db:
        old_session = db.get(API4Conversation, session_ids[0])
        assert old_session is not None
        old_dsl = json.loads(json.dumps(old_session.dsl))
        old_dsl["history"] = [["user", "prior"]]
        old_dsl["globals"]["sys.history"] = ["user: prior"]
        old_dsl["globals"]["sys.conversation_turns"] = 3
        if has_orphan:
            old_dsl["globals"]["env.orphan"] = ["ongoing"]
        old_session.dsl = old_dsl
        db.commit()
    for query, session_id in [("fresh", None), ("next", session_ids[0]), ("again", session_ids[0])]:
        payload = {"agent_id": agent_id, "release": True, "query": query, "stream": stream}
        if session_id:
            payload["session_id"] = session_id
        response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json=payload, timeout=30)
        assert response.status_code == 200, response.text
        if stream:
            events = sse_events(response.text)
            assert not any(event.get("event") == "error" for event in events)
            assert any(event.get("event") == "message_end" for event in events) and "data:[DONE]" in response.text
            content = "".join(event["data"]["content"] for event in events if event.get("event") == "message")
        else:
            assert response.json()["retcode"] == 0, response.text
            assert response.json()["data"]["event"] == "message_end"
            content = response.json()["data"]["data"]["content"]
        assert content == "published-list"
        with Session(env["engine"]) as db:
            if session_id is None:
                session_id = db.scalar(sa.select(API4Conversation.id).where(API4Conversation.dialog_id == agent_id, API4Conversation.id.not_in(session_ids)))
                assert session_id is not None
            session = db.get(API4Conversation, session_id)
            assert session is not None and not session.errors and session.message[-1]["role"] == "assistant"
            stored = json.loads(session.dsl)
            assert stored["variables"] == []
            users = [turn[1] for turn in stored["history"] if turn[0] == "user"]
            expected_users = {"fresh": ["fresh"], "next": ["prior", "next"], "again": ["prior", "next", "again"]}[query]
            assert users == expected_users
            assert stored["globals"]["sys.conversation_turns"] == {"fresh": 1, "next": 4, "again": 5}[query]
            if has_orphan:
                assert stored["globals"]["env.orphan"] == ("" if query == "fresh" else ["ongoing"])
            if stream:
                assert all(event.get("session_id") == session_id for event in events)
            untouched = db.get(API4Conversation, session_ids[1])
            assert untouched is not None and untouched.to_dict() == untouched_before
        assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before
    with Session(env["engine"]) as db:
        conversations_before_reset = {session.id: session.to_dict() for session in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))}
        untouched = db.get(API4Conversation, session_ids[1])
        assert untouched is not None
        assert_legacy_list_reset(untouched.dsl, has_orphan)
    for _ in range(2):
        debug_before = read_state(env, agent_id)
        response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/components/Message:answer/debug", json={"params": {}}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0 and response.json()["data"]["content"] == "draft-list", response.text
        assert read_state(env, agent_id) == debug_before and REDIS_CONN.get(replica_key) == replica_before
        with Session(env["engine"]) as db:
            after_debug = {session.id: session.to_dict() for session in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))}
            assert after_debug == conversations_before_reset
        response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/reset", json={}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        assert_legacy_list_reset(response.json()["data"], has_orphan)
        state = read_state(env, agent_id)
        assert_legacy_list_reset(state["dsl"], has_orphan)
        assert state["versions"] == before["versions"] and state["release"] == before["release"] and state["title"] == before["title"]
        assert REDIS_CONN.get(replica_key) == replica_before
        with Session(env["engine"]) as db:
            after = {session.id: session.to_dict() for session in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))}
            assert after == conversations_before_reset


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("openai_compatible", [False, True])
def test_http_variable_defaults_first_run(release_api: dict[str, Any], stream: bool, openai_compatible: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "First run defaults", "dsl": variable_dsl("published")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    update(env, agent_id, {"dsl": variable_dsl("draft")})
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    seed = "draft" if openai_compatible else "published"
    payload = {"agent_id": agent_id, "stream": stream, "release": not openai_compatible}
    if openai_compatible:
        payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "first"}]})
    else:
        payload["query"] = "first"
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json=payload, timeout=30)
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        if openai_compatible:
            content = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
            assert "data: [DONE]" in response.text
        else:
            assert not any(event.get("event") == "error" for event in events)
            content = "".join(event["data"]["content"] for event in events if event.get("event") == "message")
    elif openai_compatible:
        content = response.json()["choices"][0]["message"]["content"]
    else:
        assert response.json()["retcode"] == 0, response.text
        content = response.json()["data"]["data"]["content"]
    assert json.loads(content) == [seed, "first"]
    with Session(env["engine"]) as db:
        conversations = list(db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id)))
        assert len(conversations) == 1
        session = conversations[0]
        assert not session.errors
        assert_variable_state(json.loads(session.dsl), seed, [seed, "first"], executed=True)
        history = json.loads(session.dsl)["history"]
        assert history[0] == ["user", "first"] and all("stale" not in str(turn) for turn in history)
        expected_title = next(version["title"] for version in before["versions"] if version["release"] == (not openai_compatible))
        assert session.version_title == expected_title
        if stream and not openai_compatible:
            assert all(event.get("session_id") == session.id for event in events)
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before


@pytest.mark.parametrize("release", [False, True])
def test_sdk_session_helper_variable_defaults_preserve_draft(release_api: dict[str, Any], release: bool) -> None:
    from api.apps.sdk.session import CreateAgentSessionRequest, create_agent_session

    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "SDK defaults", "dsl": variable_dsl("published")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    update(env, agent_id, {"dsl": variable_dsl("draft")})
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    # This legacy helper is not registered as an HTTP route. Exercise its real
    # Canvas and transaction against scratch, without inventing an SDK URL.
    with Session(env["engine"]) as db:
        response = create_agent_session(agent_id, CreateAgentSessionRequest(release=release), db=db, tenant_id=env["owners"][0])
        result = json.loads(response.body)
        assert result["code"] == 0, result
        session_id = result["data"]["id"]
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None
        seed = "published" if release else "draft"
        assert_variable_state(session.dsl, seed, [seed], executed=False)
        assert session.dsl["history"] == [] and session.dsl["path"] == []
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before


def test_http_variable_defaults_reset_and_component_debug(release_api: dict[str, Any]) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Reset defaults", "dsl": variable_dsl("seed")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    created_session = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30).json()
    assert created_session["retcode"] == 0
    session_id = created_session["data"]["id"]
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None
        session_before = session.to_dict()
    debug_url = f"{env['base']}/api/v1/agents/{agent_id}/components/Message:answer/debug"
    for _ in range(2):
        response = env["client"].post(debug_url, json={"params": {}}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        assert json.loads(response.json()["data"]["content"]) == ["seed"]
        assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before
    for _ in range(2):
        response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/reset", json={}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        assert_variable_state(response.json()["data"], "seed", ["seed"], executed=False)
        state = read_state(env, agent_id)
        assert_variable_state(state["dsl"], "seed", ["seed"], executed=False)
        assert state["dsl"]["history"] == [] and state["dsl"]["path"] == [] and state["dsl"]["globals"]["sys.history"] == []
        assert state["versions"] == before["versions"] and state["release"] == before["release"]
        # The existing explicit reset contract updates SQL Canvas only.
        assert REDIS_CONN.get(replica_key) == replica_before
        with Session(env["engine"]) as db:
            session = db.get(API4Conversation, session_id)
            assert session is not None and session.to_dict() == session_before
    after_reset = read_state(env, agent_id)
    for url, payload in [(debug_url, {"params": {}}), (f"{env['base']}/api/v1/agents/{agent_id}/reset", {})]:
        response = env["client"].post(url, json=payload, headers={"Authorization": f"Bearer {env['foreign_jwt']}"}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 103 and response.json()["data"] is False, response.text
        assert read_state(env, agent_id) == after_reset and REDIS_CONN.get(replica_key) == replica_before


def sse_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:") and "[DONE]" not in line]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("caller_role", [UserTenantRole.OWNER, UserTenantRole.NORMAL, UserTenantRole.ADMIN])
def test_published_run_owner_team_and_existing_session(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, caller_role: UserTenantRole) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Shared release", "dsl": message_dsl("published-fixed")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True, "permission": "team"})
    update(env, agent_id, {"dsl": message_dsl("current-draft")})
    member = caller_role != UserTenantRole.OWNER
    if member:
        join_member(env, monkeypatch, 1, caller_role)
    caller = 1 if member else 0
    headers = {"Authorization": f"Bearer {env['jwts'][caller]}"}
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", headers=headers, json={"agent_id": agent_id, "release": True, "stream": stream, "query": "first"}, timeout=30)
    assert response.status_code == 200
    if stream:
        events = sse_events(response.text)
        assert "".join(event["data"]["content"] for event in events if event.get("event") == "message") == "published-fixed"
        assert any(event.get("event") == "message_end" for event in events) and "data:[DONE]" in response.text
        assert not any(event.get("event") == "error" for event in events)
    else:
        assert response.json()["retcode"] == 0 and response.json()["data"]["data"]["content"] == "published-fixed"
    with Session(env["engine"]) as db:
        sessions = list(db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id)))
        assert len(sessions) == 1
        session_id = sessions[0].id
        assert sessions[0].user_id == env["owners"][caller]
        assert json.loads(sessions[0].dsl)["components"]["begin"]["obj"]["params"]["prologue"] == "published-fixed"
        assert sessions[0].message[-1]["content"] == "published-fixed" and not sessions[0].errors
        assert sessions[0].version_title == next(v["title"] for v in before["versions"] if v["release"])
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before
    assert env["runtime_tenants"] == [env["owners"][0]]
    if member:
        denied_update = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", headers=headers, json={"release": True}, timeout=30)
        assert denied_update.json()["retcode"] == 103
        denied_create = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", headers=headers, json={"release": True}, timeout=30)
        assert denied_create.json()["retcode"] != 0
        assert read_state(env, agent_id) == before
    update(env, agent_id, {"dsl": message_dsl("new-published"), "release": True})
    next_state = read_state(env, agent_id)
    next_replica = REDIS_CONN.get(replica_key)
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completion", headers=headers, json={"agent_id": agent_id, "session_id": session_id, "release": True, "stream": stream, "query": "continue"}, timeout=30
    )
    assert response.status_code == 200
    if stream:
        assert "".join(event["data"]["content"] for event in sse_events(response.text) if event.get("event") == "message") == "published-fixed"
    else:
        assert response.json()["retcode"] == 0 and response.json()["data"]["data"]["content"] == "published-fixed"
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None and session.message[-1]["content"] == "published-fixed"
        assert json.loads(session.dsl)["components"]["begin"]["obj"]["params"]["prologue"] == "published-fixed" and not session.errors
        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 1
    assert env["runtime_tenants"] == [env["owners"][0], env["owners"][0]]
    assert read_state(env, agent_id) == next_state and REDIS_CONN.get(replica_key) == next_replica


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fault", ["component", "exception", "event"])
def test_published_runtime_failure_has_no_success(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, fault: str) -> None:
    env = release_api
    dsl = message_dsl("must-not-succeed")
    if fault == "component":
        dsl["components"]["Message:answer"]["obj"]["params"].update(content=["{{"], stream=False)
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Failure release", "dsl": dsl}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    before = read_state(env, agent_id)
    original_run = Canvas.run

    async def injected_run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        # Real Canvas setup and initial event; inject the exceptional transport
        # cases at the execution boundary. The component case is fully real.
        async for event in original_run(self, **kwargs):
            yield event
            if fault == "exception":
                raise RuntimeError("injected execution failure")
            yield {"event": "error", "data": {"error": "injected execution error event"}}
            yield {"event": "message_end", "data": {"must_not_succeed": True}}
            return

    if fault != "component":
        monkeypatch.setattr(Canvas, "run", injected_run)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json={"agent_id": agent_id, "release": True, "stream": stream}, timeout=30)
    assert response.status_code == 200
    if stream:
        events = sse_events(response.text)
        assert events[-1]["event"] == "error" and events[-1]["code"] != 0
        assert not any(event.get("event") in {"message", "message_end", "workflow_finished"} for event in events)
        assert "data:[DONE]" in response.text
    else:
        assert response.json()["retcode"] != 0 and response.json().get("data") is not True
    with Session(env["engine"]) as db:
        sessions = list(db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id)))
        assert len(sessions) == 1 and sessions[0].errors
        assert not any(message["role"] == "assistant" for message in sessions[0].message)
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before


@pytest.mark.parametrize("stream", [False, True])
def test_published_run_consumes_the_prepared_snapshot(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Snapshot race", "dsl": message_dsl("selected-published")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    module = sys.modules["api.apps.restful_apis.agent"]
    original_prepare = module.prepare_agent_run
    selected_titles: list[str] = []

    def publish_new_version() -> None:
        with Session(env["engine"]) as db:
            current = db.get(UserCanvas, agent_id)
            assert current is not None
            current.dsl = message_dsl("published-after-selection")
            timestamp = db.scalar(sa.select(sa.func.max(UserCanvasVersion.create_time)).where(UserCanvasVersion.user_canvas_id == agent_id))
            db.add(UserCanvasVersion(id=uuid4().hex, user_canvas_id=agent_id, dsl=current.dsl, release=True, title="later publication", create_time=timestamp + 1))
            db.commit()

    async def prepare_then_publish(db: AsyncSession, selected_id: str, caller_id: str, **kwargs: Any) -> Any:
        prepared = await original_prepare(db, selected_id, caller_id, **kwargs)
        selected_titles.append(prepared.version_title)
        await asyncio.to_thread(publish_new_version)
        return prepared

    monkeypatch.setattr(module, "prepare_agent_run", prepare_then_publish)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json={"agent_id": agent_id, "release": True, "stream": stream}, timeout=30)
    assert response.status_code == 200 and len(selected_titles) == 1
    if stream:
        assert "".join(event["data"]["content"] for event in sse_events(response.text) if event.get("event") == "message") == "selected-published"
    else:
        assert response.json()["retcode"] == 0 and response.json()["data"]["data"]["content"] == "selected-published"
    state = read_state(env, agent_id)
    assert state["dsl"] == message_dsl("published-after-selection") and state["versions"][0]["title"] == "later publication"
    with Session(env["engine"]) as db:
        session = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
        assert session is not None and session.version_title == selected_titles[0]
    assert json.loads(session.dsl)["components"]["begin"]["obj"]["params"]["prologue"] == "selected-published"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("case", ["missing_session", "cross_agent", "private", "nonmember", "invite", "inactive_member", "inactive_admin"])
def test_existing_session_run_denials_are_http_errors(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, case: str) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Session access", "dsl": message_dsl("published")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True, "permission": "team" if case in {"nonmember", "invite", "inactive_member", "inactive_admin"} else "me"})
    if case in {"invite", "inactive_member", "inactive_admin"}:
        denied_membership(env, monkeypatch, case)
    created_session = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": True}, timeout=30).json()
    assert created_session["retcode"] == 0
    session_id = created_session["data"]["id"]
    requested_agent = agent_id
    if case == "cross_agent":
        other = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Other session access", "dsl": message_dsl("other")}, timeout=30).json()
        assert other["retcode"] == 0
        requested_agent = other["data"]["id"]
    if case == "private":
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][2], tenant_id=env["owners"][0], role="normal", invited_by=env["owners"][0]))
            db.commit()
    task_count = len(env["task_ids"])
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None
        before = session.to_dict()
    agent_before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    headers = {"Authorization": f"Bearer {env['jwts'][2]}"} if case in {"private", "nonmember", "invite", "inactive_member", "inactive_admin"} else {}
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completion",
        headers=headers,
        json={"agent_id": requested_agent, "session_id": uuid4().hex if case == "missing_session" else session_id, "release": True, "stream": stream, "user_id": env["owners"][0]},
        timeout=30,
    )
    assert response.status_code == (404 if case == "missing_session" else 403), response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["retcode"] == (102 if case == "missing_session" else 103) and response.json()["data"] is False
    assert len(env["task_ids"]) == task_count
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None and session.to_dict() == before
        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id.in_([agent_id, requested_agent]))) == 1
    assert read_state(env, agent_id) == agent_before and REDIS_CONN.get(replica_key) == replica_before


def variable_dsl(seed: str) -> dict[str, Any]:
    dsl = message_dsl("{env.items}")
    dsl["components"]["begin"]["downstream"] = ["VariableAssigner:append"]
    dsl["components"]["Message:answer"]["upstream"] = ["VariableAssigner:append"]
    dsl["components"]["VariableAssigner:append"] = {
        "obj": {
            "component_name": "VariableAssigner",
            "params": {
                "variables": [{"variable": "{env.items}", "operator": "append", "parameter": "{sys.query}"}, {"variable": "{env.object}", "operator": "set", "parameter": {"runtime": ["changed"]}}]
            },
        },
        "upstream": ["begin"],
        "downstream": ["Message:answer"],
    }
    dsl["variables"] = {
        "items": {"type": "array<string>", "value": [seed]},
        "object": {"type": "object", "value": {"nested": [seed]}},
        "text": {"type": "string", "value": seed},
        "number": {"type": "number", "value": 7},
        "boolean": {"type": "boolean", "value": True},
        "explicit_false": {"type": "number", "value": False},
        "explicit_zero": {"type": "boolean", "value": 0},
        "explicit_empty": {"type": "number", "value": ""},
        "empty_object": {"type": "object", "value": {}},
        "empty_array": {"type": "array<string>", "value": []},
        "fallback_number": {"type": "number", "value": None},
        "fallback_boolean": {"type": "boolean"},
        "fallback_object": {"type": "object", "value": None},
        "fallback_array": {"type": "array<string>"},
        "fallback_string": {"type": "string", "value": None},
        "fallback_unknown": {"type": "unknown", "value": None},
    }
    dsl["globals"] = {"sys.query": "stale", "sys.user_id": "stale", "sys.conversation_turns": 8, "sys.files": [], "sys.history": ["stale"], **{f"env.{name}": "stale" for name in dsl["variables"]}}
    dsl["globals"]["env.items"] = ["stale-runtime"]
    dsl["globals"]["env.object"] = {"stale": []}
    dsl["history"] = [["user", "stale history"]]
    return dsl


def assert_variable_state(dsl: dict[str, Any], seed: str, items: list[str], *, executed: bool) -> None:
    assert dsl["variables"] == variable_dsl(seed)["variables"]
    expected = {
        "env.items": items,
        "env.object": {"runtime": ["changed"]} if executed else {"nested": [seed]},
        "env.text": seed,
        "env.number": 7,
        "env.boolean": True,
        "env.explicit_false": False,
        "env.explicit_zero": 0,
        "env.explicit_empty": "",
        "env.empty_object": {},
        "env.empty_array": [],
        "env.fallback_number": 0,
        "env.fallback_boolean": False,
        "env.fallback_object": {},
        "env.fallback_array": [],
        "env.fallback_string": "",
        "env.fallback_unknown": "",
    }
    for key, value in expected.items():
        assert dsl["globals"][key] == value and type(dsl["globals"][key]) is type(value), key


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("release", [False, True])
def test_http_variable_defaults_new_and_existing_sessions(release_api: dict[str, Any], stream: bool, release: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Variable defaults", "dsl": variable_dsl("published")}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    update(env, agent_id, {"release": True})
    update(env, agent_id, {"dsl": variable_dsl("draft")})
    seed = "published" if release else "draft"
    before = read_state(env, agent_id)
    replica_key = f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"
    replica_before = REDIS_CONN.get(replica_key)
    response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": release}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    session_id = response.json()["data"]["id"]
    untouched = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": release}, timeout=30).json()
    assert untouched["retcode"] == 0
    untouched_id = untouched["data"]["id"]
    with Session(env["engine"]) as db:
        session = db.get(API4Conversation, session_id)
        assert session is not None
        assert_variable_state(session.dsl, seed, [seed], executed=False)
        assert session.dsl["history"] == [] and session.dsl["path"] == [] and session.dsl["globals"]["sys.history"] == []
        unrelated = db.get(API4Conversation, untouched_id)
        assert unrelated is not None
        unrelated_before = unrelated.to_dict()
    for query, expected in [("first", [seed, "first"]), ("second", [seed, "first", "second"])]:
        response = env["client"].post(
            f"{env['base']}/api/v1/agents/chat/completion", json={"agent_id": agent_id, "session_id": session_id, "release": release, "query": query, "stream": stream}, timeout=30
        )
        assert response.status_code == 200, response.text
        if stream:
            events = sse_events(response.text)
            assert not any(e.get("event") == "error" for e in events)
            content = "".join(e["data"]["content"] for e in events if e.get("event") == "message")
            assert all(e.get("session_id") == session_id for e in events)
        else:
            assert response.json()["retcode"] == 0, response.text
            content = response.json()["data"]["data"]["content"]
        assert json.loads(content) == expected
        with Session(env["engine"]) as db:
            session = db.get(API4Conversation, session_id)
            assert session is not None and not session.errors
            assert_variable_state(json.loads(session.dsl), seed, expected, executed=True)
    other = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": release}, timeout=30).json()
    assert other["retcode"] == 0 and other["data"]["id"] != session_id
    with Session(env["engine"]) as db:
        other_session = db.get(API4Conversation, other["data"]["id"])
        assert other_session is not None
        assert_variable_state(other_session.dsl, seed, [seed], executed=False)
        session = db.get(API4Conversation, session_id)
        assert session is not None
        assert_variable_state(json.loads(session.dsl), seed, [seed, "first", "second"], executed=True)
        unrelated = db.get(API4Conversation, untouched_id)
        assert unrelated is not None and unrelated.to_dict() == unrelated_before
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key) == replica_before
