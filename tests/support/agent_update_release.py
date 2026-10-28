"""Agent publish/draft HTTP contracts against isolated PostgreSQL and real Redis."""

import asyncio
import errno
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
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
from tests.support.event_loop import UVICORN_LOOP
from tests.support.resource_journal import ResourceJournal
from tests.support.uvicorn_runtime import run_server


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

        journal = ResourceJournal("agent-release-http")
        evidence = Path(os.environ.get("AGENT_RELEASE_EVIDENCE", str(journal.path.parent))) / (journal.run_id + ".release.json")
        report: dict[str, Any] = {"pid": os.getpid(), "database": bootstrapped_engine.url.database, "rows": {}, "redis_keys": [], "cleanup": {}}
        journal.event("reserve-fixture", pid=os.getpid(), database=bootstrapped_engine.url.database, host=bootstrapped_engine.url.host, port=bootstrapped_engine.url.port, evidence=str(evidence))

        def save_report() -> None:
            evidence.write_text(json.dumps(report, indent=2))
            evidence.chmod(0o600)

        cleanup.callback(save_report)
        owners = [uuid4().hex for _ in range(3)]
        api_keys = [f"release-test-{uuid4().hex}" for _ in owners]
        sync_sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
        task_ids: list[str] = []
        runtime_tenants: list[str | None] = []
        identifiers = list(owners)
        registered_rows: dict[str, set[str]] = {}
        owned_redis: set[str] = set()
        lock = threading.RLock()

        def reserve_row(table: str, values: dict[str, Any]) -> None:
            identifier = values.get("id")
            if not identifier:
                return
            with lock:
                known = registered_rows.setdefault(table, set())
                if str(identifier) in known:
                    return
                journal.event("reserve-sql-row", database=bootstrapped_engine.url.database, table=table, id=str(identifier))
                known.add(str(identifier))
                report["rows"][table] = sorted(known)
                if table in {UserCanvas.__tablename__, API4Conversation.__tablename__}:
                    identifiers.append(str(identifier))

        def reserve_flush(session: Session, flush_context: Any, instances: Any) -> None:
            bind = session.get_bind()
            if getattr(bind, "engine", bind) not in sql_engines:
                return
            for row in session.new:
                reserve_row(row.__tablename__, {"id": getattr(row, "id", None)})

        def reserve_insert(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            compiled = getattr(context, "compiled", None)
            if compiled is None or not getattr(context, "isinsert", False):
                return
            table = compiled.statement.table
            for values in context.compiled_parameters:
                reserve_row(table.name, values)

        sql_engines: list[sa.Engine] = [bootstrapped_engine]
        sa.event.listen(Session, "before_flush", reserve_flush)
        sa.event.listen(bootstrapped_engine, "before_cursor_execute", reserve_insert)
        cleanup.callback(sa.event.remove, Session, "before_flush", reserve_flush)
        cleanup.callback(sa.event.remove, bootstrapped_engine, "before_cursor_execute", reserve_insert)
        journal.event(
            "reserve-owner-scope", owners=owners, api_tokens=[{"tenant_id": owner, "token_sha256": hashlib.sha256(token.encode()).hexdigest()} for owner, token in zip(owners, api_keys, strict=True)]
        )

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
            with sync_sessions() as reader:
                remaining = {}
                for table_name, row_ids in registered_rows.items():
                    table = db_models.Base.metadata.tables["usr_ai." + table_name]
                    remaining[table_name] = reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.id.in_(sorted(row_ids))))
                assert not any(remaining.values()), remaining
            report["cleanup"]["sql_rows_absent"] = True
            report["cleanup"]["remaining_rows"] = remaining
            journal.event("sql-cleanup-readback", database=bootstrapped_engine.url.database, owned_rows_absent=True, remaining=remaining)

        def remove_redis() -> None:
            redis = REDIS_CONN.REDIS
            assert redis is not None
            for key in sorted(owned_redis):
                redis.delete(key)
            assert not any(redis.exists(key) for key in owned_redis)
            for identifier in [*identifiers, *task_ids]:
                keys = list(redis.scan_iter(match=f"*{identifier}*"))
                assert not keys, "Unregistered Redis resource remains in the owned scope"
            report["cleanup"]["redis_keys_absent"] = True
            journal.event("redis-cleanup-readback", keys=sorted(owned_redis), absent=True)

        cleanup.callback(remove_redis)
        cleanup.callback(remove_rows)
        with sync_sessions() as db:
            for owner, token in zip(owners, api_keys, strict=True):
                db.add(User(id=owner, email=f"{owner}@release.example.com", nickname="Release test", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name="Release scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=owner, role="owner", invited_by=owner))
                journal.event("reserve-api-token", tenant_id=owner, token_sha256=hashlib.sha256(token.encode()).hexdigest())
                db.add(APIToken(tenant_id=owner, token=token, name="release-test"))
            db.commit()
        # Keep real JWT/API-key verification and user loading pointed at scratch.
        monkeypatch.setattr(apps, "SessionLocal", sync_sessions)
        monkeypatch.setattr(db_models, "SessionLocal", sync_sessions)
        async_engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
        sql_engines.append(async_engine.sync_engine)
        sa.event.listen(async_engine.sync_engine, "before_cursor_execute", reserve_insert)
        cleanup.callback(sa.event.remove, async_engine.sync_engine, "before_cursor_execute", reserve_insert)
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
        redis = REDIS_CONN.REDIS
        assert redis is not None
        redis_endpoint = {key: redis.connection_pool.connection_kwargs.get(key) for key in ["host", "port", "db"]}
        original_command, original_pipeline = redis.execute_command, redis.pipeline

        def reserve_command(arguments: tuple[Any, ...]) -> None:
            if not arguments:
                return
            command = arguments[0].decode() if isinstance(arguments[0], bytes) else str(arguments[0])
            command = command.upper()
            keys = []
            if command in {"EVAL", "EVALSHA"}:
                keys = list(arguments[3 : 3 + int(arguments[2])])
            elif command in {"DEL", "UNLINK"}:
                keys = list(arguments[1:])
            elif command in {"SET", "SETEX", "PSETEX", "SETNX", "HSET", "HMSET", "HDEL", "LPUSH", "RPUSH", "SADD", "ZADD", "INCR", "XADD", "EXPIRE", "PEXPIRE"}:
                keys = list(arguments[1:2])
            with lock:
                for raw_key in keys:
                    key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
                    if key in owned_redis:
                        continue
                    assert not redis.exists(key), "Fixture refuses to overwrite a borrowed Redis key"
                    journal.event("reserve-redis-key", key=key, endpoint=redis_endpoint, before_absent=True)
                    owned_redis.add(key)
                    report["redis_keys"] = sorted(owned_redis)

        def execute_command(*arguments: Any, **options: Any) -> Any:
            reserve_command(arguments)
            return original_command(*arguments, **options)

        def pipeline(*arguments: Any, **options: Any) -> Any:
            instance = original_pipeline(*arguments, **options)
            original_execute = instance.execute

            def execute(*args: Any, **kwargs: Any) -> Any:
                for command, _options in instance.command_stack:
                    reserve_command(command)
                return original_execute(*args, **kwargs)

            instance.execute = execute
            return instance

        monkeypatch.setattr(redis, "execute_command", execute_command)
        monkeypatch.setattr(redis, "pipeline", pipeline)
        original_init = Canvas.__init__
        canvases: list[Canvas] = []

        def track_canvas(self: Canvas, *args: Any, **kwargs: Any) -> None:
            arguments = list(args)
            task_id = (arguments[2] if len(arguments) > 2 else kwargs.get("task_id")) or uuid4().hex
            if len(arguments) > 2:
                arguments[2] = task_id
            else:
                kwargs["task_id"] = task_id
            journal.event("reserve-canvas-task", pid=os.getpid(), task_id=task_id)
            task_ids.append(task_id)
            runtime_tenants.append(args[1] if len(args) > 1 else kwargs.get("tenant_id"))
            canvases.append(self)
            original_init(self, *arguments, **kwargs)

        def close_canvas_pools() -> None:
            for canvas in canvases:
                pool = getattr(canvas, "_thread_pool", None)
                if pool is not None:
                    pool.shutdown(wait=True, cancel_futures=True)
            report["cleanup"]["canvas_executors_closed"] = len(canvases)

        monkeypatch.setattr(Canvas, "__init__", track_canvas)
        cleanup.callback(close_canvas_pools)
        candidate_port = 20000 + secrets.randbelow(40000)
        journal.event("reserve-listener", pid=os.getpid(), host="127.0.0.1", port=candidate_port)
        listener = socket.socket()

        def close_listener() -> None:
            port = listener.getsockname()[1] if listener.fileno() != -1 else report.get("port")
            listener.close()
            if port:
                with socket.socket() as probe:
                    probe.settimeout(1)
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
            report["cleanup"]["listener_absent"] = True
            journal.event("listener-cleanup-readback", host="127.0.0.1", port=port, absent=True)

        cleanup.callback(close_listener)
        for attempt in range(32):
            if attempt:
                candidate_port = 20000 + secrets.randbelow(40000)
                journal.event("reserve-listener", pid=os.getpid(), host="127.0.0.1", port=candidate_port)
            try:
                listener.bind(("127.0.0.1", candidate_port))
                break
            except OSError as error:
                journal.event("listener-bind-failed", host="127.0.0.1", port=candidate_port, errno=error.errno)
                if error.errno != errno.EADDRINUSE:
                    raise
        else:
            raise RuntimeError("Unable to bind an owned Agent test listener")
        report["port"] = listener.getsockname()[1]
        journal.event("listener-bound", pid=os.getpid(), host="127.0.0.1", port=report["port"])
        base = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error", loop=UVICORN_LOOP))
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True)

        def stop_server() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            assert not thread.is_alive()
            report["cleanup"]["server_thread_absent"] = True
            journal.event("server-thread-cleanup-readback", pid=os.getpid(), thread_id=thread.ident, absent=True)

        cleanup.callback(stop_server)
        journal.event("reserve-server-thread", pid=os.getpid(), listener_port=report["port"])
        thread.start()
        journal.event("server-thread-started", pid=os.getpid(), thread_id=thread.ident)
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
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "release": True, "query": "test", "stream": False}, timeout=30)
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
