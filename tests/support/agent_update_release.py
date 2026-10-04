"""Agent publish/draft HTTP contracts against isolated PostgreSQL and real Redis."""

import asyncio
import json
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
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
    with ExitStack() as cleanup:
        from api import apps
        from api.db import db_models

        owners = [uuid4().hex for _ in range(3)]
        api_keys = [f"release-test-{uuid4().hex}" for _ in owners]
        sync_sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
        task_ids: list[str] = []
        runtime_tenants: list[str | None] = []
        identifiers = list(owners)

        def remove_rows() -> None:
            with sync_sessions() as db:
                canvas_ids = list(db.scalars(sa.select(UserCanvas.id).where(UserCanvas.user_id.in_(owners))))
                session_ids = list(db.scalars(sa.select(API4Conversation.id).where(API4Conversation.dialog_id.in_(canvas_ids))))
                identifiers.extend([*canvas_ids, *session_ids])
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

        def remove_redis() -> None:
            redis = REDIS_CONN.REDIS
            assert redis is not None
            for identifier in [*identifiers, *task_ids]:
                keys = list(redis.scan_iter(match=f"*{identifier}*"))
                if keys:
                    redis.delete(*keys)
                assert not list(redis.scan_iter(match=f"*{identifier}*"))

        cleanup.callback(remove_redis)
        cleanup.callback(remove_rows)
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
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        def scratch_db() -> Iterator[Session]:
            with sync_sessions() as db:
                yield db

        async def scratch_async_db() -> AsyncIterator[AsyncSession]:
            async with async_sessions() as db:
                yield db

        monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
        monkeypatch.setitem(apps.app.dependency_overrides, get_async_db, scratch_async_db)
        original_init = Canvas.__init__

        def track_canvas(self: Canvas, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            task_ids.append(self.task_id)
            runtime_tenants.append(args[1] if len(args) > 1 else kwargs.get("tenant_id"))

        monkeypatch.setattr(Canvas, "__init__", track_canvas)
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop_server() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            assert not thread.is_alive()

        cleanup.callback(stop_server)
        thread.start()
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


def sse_events(text: str) -> list[dict[str, Any]]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:") and "[DONE]" not in line]


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
