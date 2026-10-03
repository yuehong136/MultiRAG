"""Persist real Agent completions, then read history and owned raster bytes.

Only the reference producer is controlled; Canvas Message execution, authenticated
HTTP, append_message, PostgreSQL, MinIO, Milvus and Redis are real.
"""

import copy
import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from io import BytesIO
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- load the runner before legacy nest_asyncio patches
from PIL import Image
from redis import Redis
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import AgentExecutionOrigin, API4Conversation, APIToken, UserCanvas, UserCanvasVersion
from api.db.services.api_service import API4ConversationService
from common.config_utils import CONFIGS
from core.utils.redis_conn import REDIS_CONN
from tests.integration.test_agent_update_release import message_dsl
from tests.integration.test_document_image_http import _save, _setup
from tests.integration.test_document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.integration.test_document_image_http import image_http_api as image_http_api
from tests.integration.test_document_image_http import image_http_database as image_http_database
from tests.integration.test_document_image_read_service import _snapshot
from tests.integration.test_document_image_read_service import image_resources as image_resources


@contextmanager
def history_environment(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api import apps

    manifest = env["manifest"]
    manifest.update(history_sql_ids={}, redis_keys=[], advisory_locks=[], os_start=subprocess.check_output(["ps", "-p", str(os.getpid()), "-o", "lstart="], text=True).strip())
    pools: list[Any] = []
    lock = threading.RLock()

    def register_keys(keys: list[str]) -> None:
        with lock:
            manifest["redis_keys"] = sorted(set(manifest["redis_keys"]) | set(keys))
            env["register"]()

    redis = REDIS_CONN.REDIS
    assert redis is not None
    owner_api = "history-owner-" + uuid4().hex
    listener_installed = False
    engines = (env["engine"], env["async_engine"].sync_engine)
    try:
        env["register"]()
        for method in ["set", "setex", "delete", "eval"]:
            original = getattr(redis, method)

            def tracked(*args: Any, _method: str = method, _original: Any = original, **kwargs: Any) -> Any:
                keys = args[2 : 2 + int(args[1])] if _method == "eval" else args if _method == "delete" else args[:1]
                register_keys([key.decode() if isinstance(key, bytes) else str(key) for key in keys])
                return _original(*args, **kwargs)

            monkeypatch.setattr(redis, method, tracked)

        original_init = Canvas.__init__

        def track_canvas(self: Canvas, *args: Any, **kwargs: Any) -> None:
            original_init(self, *args, **kwargs)
            pools.append(self._thread_pool)
            register_keys([f"{self.task_id}-logs", f"{self.task_id}-cancel", f"task-runtime:v1:{self.task_id}"])

        monkeypatch.setattr(Canvas, "__init__", track_canvas)

        def register_sql(db: Session, flush_context: Any, instances: Any) -> None:
            if all(db.get_bind() is not engine for engine in engines):
                return
            with lock:
                for obj in db.new:
                    if isinstance(obj, (UserCanvas, UserCanvasVersion, API4Conversation, AgentExecutionOrigin)):
                        ids = manifest["history_sql_ids"].setdefault(obj.__tablename__, [])
                        if obj.id not in ids:
                            ids.append(obj.id)
                env["register"]()

        sa.event.listen(Session, "before_flush", register_sql)
        listener_installed = True
        env["history_listener"] = register_sql
        env["tokens"]["owner_api"] = owner_api
        # The image fixture registers its expired/malformed strings as valid API
        # keys for image endpoint anti-downgrade checks. Agent manager supports that
        # fallback; use genuinely unregistered invalid credentials here.
        env["tokens"]["expired"] = apps.manager.create_access_token(data={"sub": f"{env['ids']['owner']}@image.test"}, expires=timedelta(minutes=-2))
        env["tokens"]["malformed"] = f"unregistered.{uuid4().hex}.invalid"
        manifest["history_api_token_owner"] = env["ids"]["owner"]
        manifest["history_api_token_sha256"] = hashlib.sha256(owner_api.encode()).hexdigest()
        manifest["history_api_token_name"] = "history-owner"
        env["register"]()
        with Session(env["engine"]) as db:
            db.add(APIToken(tenant_id=env["ids"]["owner"], token=owner_api, name="history-owner"))
            db.commit()
        yield env
    finally:
        if listener_installed:
            sa.event.remove(Session, "before_flush", register_sql)
        manifest["history_listener_removed"] = not listener_installed or not sa.event.contains(Session, "before_flush", register_sql)
        for pool in pools:
            pool.shutdown(wait=True, cancel_futures=True)
            assert all(not thread.is_alive() for thread in pool._threads)
        with Session(env["engine"]) as db:
            conditions = [
                (API4Conversation, API4Conversation.id.in_(manifest["history_sql_ids"].get(API4Conversation.__tablename__, []))),
                (UserCanvasVersion, UserCanvasVersion.id.in_(manifest["history_sql_ids"].get(UserCanvasVersion.__tablename__, []))),
                (UserCanvas, UserCanvas.id.in_(manifest["history_sql_ids"].get(UserCanvas.__tablename__, []))),
                (APIToken, sa.and_(APIToken.tenant_id == env["ids"]["owner"], APIToken.token == owner_api)),
            ]
            for model, condition in conditions:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        with env["engine"].connect() as db:
            remaining = {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in conditions}
            remaining[AgentExecutionOrigin.__tablename__] = db.scalar(
                sa.select(sa.func.count())
                .select_from(AgentExecutionOrigin)
                .where(
                    AgentExecutionOrigin.id.in_(
                        sorted(set(manifest["history_sql_ids"].get(API4Conversation.__tablename__, [])) | set(manifest["history_sql_ids"].get(AgentExecutionOrigin.__tablename__, [])))
                    )
                )
            )
        assert not any(remaining.values()), remaining
        keys = list(manifest["redis_keys"])
        if keys:
            redis.delete(*keys)
        assert all(not redis.exists(key) for key in keys)
        manifest.update(history_cleanup=True, history_remaining=remaining, history_keys_absent=True, history_pools_closed=True)
        env["register"]()


@pytest.fixture
def history_api(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    with history_environment(image_http_api, monkeypatch) as env:
        yield env


def history_snapshot(env: dict[str, Any]) -> dict[str, Any]:
    result = _snapshot(env)
    with env["engine"].connect() as db:
        for model in [UserCanvas, UserCanvasVersion, API4Conversation, AgentExecutionOrigin]:
            result["sql"][model.__tablename__] = [dict(row) for row in db.execute(sa.select(model.__table__).order_by(model.id)).mappings()]
        result["advisory_locks"] = [
            dict(row)
            for row in db.execute(
                sa.text(
                    "SELECT classid,objid,objsubid,mode,granted FROM pg_locks WHERE locktype='advisory' AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) ORDER BY classid,objid,objsubid,mode"
                )
            ).mappings()
        ]
    cfg = CONFIGS["redis"]
    host, _, port = cfg["host"].rpartition(":")
    client = Redis(host=host, port=int(port), db=int(cfg.get("db", 1)), username=cfg.get("username") or None, password=cfg.get("password") or None)
    try:
        result["redis"] = {}
        for key in sorted(set(env["manifest"]["redis_keys"]) | {env["queue"]}):
            kind, dump, ttl = client.type(key), client.dump(key), client.pttl(key)
            result["redis"][key] = {"type": kind.decode(), "dump_hex": dump.hex() if dump else None, "pttl": ttl, "value_hex": client.get(key).hex() if kind == b"string" else None}
    finally:
        client.close()
    return result


def assert_read_only(before: dict[str, Any], after: dict[str, Any]) -> None:
    assert {key: value for key, value in before.items() if key != "redis"} == {key: value for key, value in after.items() if key != "redis"}
    assert before["redis"].keys() == after["redis"].keys()
    for key, prior in before["redis"].items():
        current = after["redis"][key]
        assert {k: v for k, v in prior.items() if k != "pttl"} == {k: v for k, v in current.items() if k != "pttl"}
        assert current["pttl"] == prior["pttl"] if prior["pttl"] < 0 else 0 < current["pttl"] <= prior["pttl"]


@pytest.mark.parametrize("stream", [False, True])
def test_saved_history_types_and_authenticated_images_are_read_only(history_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env, ids = history_api, history_api["ids"]
    setup = _setup(env)
    record: dict[str, Any] = {
        "stream": stream,
        "calls": [],
        "controlled_boundary": "Canvas.get_reference producer only; real Message Canvas execution, HTTP auth, append_message, SQL/index/object/Redis",
    }
    path = env["evidence"] / f"{ids['kb']}.agent-history.json"

    def call(method: str, route: str, role: str | None = "owner", **kwargs: Any) -> requests.Response:
        headers = {"Authorization": "Bearer " + env["tokens"][role]} if role else {}
        response = requests.request(method, env["base"] + route, headers=headers, timeout=40, **kwargs)
        record["calls"].append({"method": method, "path": route, "principal": role, "status": response.status_code, "headers": dict(response.headers), "body": response.content})
        _save(path, record)
        return response

    created = call("POST", "/api/v1/agents", json={"title": "History references", "dsl": message_dsl("real Message answer")})
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    agent_id = created.json()["data"]["id"]
    route = f"/api/v1/agents/{agent_id}/sessions"
    empty = call("POST", route, json={"name": "empty preserved"}).json()
    assert empty["retcode"] == 0 and all("reference" not in message for message in empty["data"]["messages"])
    created_session = call("POST", route, json={"name": "saved raster history"}).json()
    assert created_session["retcode"] == 0
    session_id = created_session["data"]["id"]
    record["http_created_session_ids"] = http_ids = [empty["data"]["id"], session_id]
    record["registered_async_sql_ids"] = copy.deepcopy(env["manifest"]["history_sql_ids"])
    for model in [API4Conversation, AgentExecutionOrigin]:
        assert set(http_ids) <= set(record["registered_async_sql_ids"][model.__tablename__])
    raster_key = next(iter(setup["cases"]))
    source = {
        "chunk_id": setup["chunks"]["kb"][0]["id"],
        "content_with_weight": "image caption",
        "doc_id": env["manifest"]["documents"][raster_key],
        "docnm_kwd": "source image",
        "kb_id": ids["kb"],
        "img_id": f"{ids['kb']}-{raster_key}",
        "position_int": [[1, 2, 3, 4, 5]],
        "private_metadata": "must not leak",
    }
    chunks = [
        {**source, "doc_type": "image"},
        {**source, "chunk_id": "table-reference", "doc_type_kwd": "table"},
        {**source, "chunk_id": "text-reference", "doc_type": "text"},
        {**source, "chunk_id": "untyped-reference"},
    ]
    references = {"chunks": chunks, "doc_aggs": [{"doc_id": source["doc_id"], "count": 4}]}
    monkeypatch.setattr(Canvas, "get_reference", lambda self: copy.deepcopy(references))
    completion = call("POST", "/api/v1/agents/chat/completion", json={"agent_id": agent_id, "session_id": session_id, "stream": stream, "query": "show saved references"})
    assert completion.status_code == 200
    if stream:
        assert '"event": "message_end"' in completion.text and '"event": "error"' not in completion.text
    else:
        assert completion.json()["retcode"] == 0, completion.text
    with env["engine"].connect() as db:
        saved = record["saved_sql"] = dict(db.execute(sa.select(API4Conversation.__table__).where(API4Conversation.id == session_id)).mappings().one())
        origin = record["saved_execution_origin"] = dict(db.execute(sa.select(AgentExecutionOrigin.__table__).where(AgentExecutionOrigin.id == session_id)).mappings().one())
    assert origin["agent_id"] == agent_id and origin["platform_user_id"] == ids["owner"] and origin["tenant_id"] == ids["owner"]
    assert origin["execution_mode"] == "draft" and origin["agent_revision_id"] is None
    assert saved["reference"] == references and saved["round"] == 1 and not saved["errors"]
    assert saved["message"][-1]["role"] == "assistant" and saved["message"][-1]["content"] == "real Message answer"
    # Explicit persisted fixtures exercise historic array/keyed reference
    # formats separately from the real completion above.
    seeded: dict[str, str] = {}
    with Session(env["engine"]) as db:
        for shape in ["array", "numeric_map", "no_messages"]:
            identifier = seeded[shape] = uuid4().hex
            packages = [{"chunks": [chunks[0]]}, {"chunks": [chunks[1]]}]
            API4ConversationService.save(
                db,
                id=identifier,
                name="persisted format " + shape,
                dialog_id=agent_id,
                user_id=ids["owner"],
                exp_user_id=ids["owner"],
                source="agent",
                dsl=saved["dsl"],
                message=[]
                if shape == "no_messages"
                else [{"role": "assistant", "content": "prologue", "prompt": "private"}, {"role": "user"}, {"role": "assistant"}, {"role": "user"}, {"role": "assistant"}],
                reference=[] if shape == "no_messages" else packages if shape == "array" else {"10": packages[1], "2": packages[0]},
            )
    record["seeded_history_fixtures"] = seeded
    record["before"] = before = history_snapshot(env)
    writes: list[str] = []

    def no_sql_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            writes.append(statement)
            raise AssertionError("History read attempted SQL write")

    for engine in [env["engine"], env["async_engine"].sync_engine]:
        sa.event.listen(engine, "before_cursor_execute", no_sql_write)
    try:
        for role in ["owner", "owner_api"]:
            for _ in range(2):
                response = call("GET", route + "/" + session_id, role)
                assert response.status_code == 200 and response.json()["retcode"] == 0
                messages = response.json()["data"]["messages"]
                actual = messages[-1]["reference"]
                assert [chunk["id"] for chunk in actual] == [chunk["chunk_id"] for chunk in chunks]
                for current, original in zip(actual, chunks, strict=True):
                    assert current == {
                        "id": original["chunk_id"],
                        "content": original["content_with_weight"],
                        "document_id": original["doc_id"],
                        "document_name": original["docnm_kwd"],
                        "dataset_id": original["kb_id"],
                        "image_id": original["img_id"],
                        "positions": original["position_int"],
                        **{key: original[key] for key in ("doc_type", "doc_type_kwd") if key in original},
                    }
                image_requests = [chunk for chunk in actual if (chunk.get("doc_type") if "doc_type" in chunk else chunk.get("doc_type_kwd")) in {"image", "table"}]
                assert len(image_requests) == 2
                for chunk in image_requests:
                    image = call("GET", "/api/v1/documents/images/" + quote(chunk["image_id"], safe=""), role)
                    assert image.status_code == 200 and image.content == setup["cases"][raster_key][0]
                    assert image.headers["content-type"].startswith("image/png") and image.headers["cache-control"] == "no-store" and image.headers["x-content-type-options"] == "nosniff"
                    with Image.open(BytesIO(image.content)) as decoded:
                        decoded.load()
                        assert decoded.size == (3, 3)
            listed = call("GET", route, role, params={"dsl": "false", "page": 1, "page_size": 30}).json()
            assert listed["retcode"] == 0 and listed["data"]["total"] == 5
            returned = next(item for item in listed["data"]["sessions"] if item["id"] == session_id)
            assert "dsl" not in returned and returned["messages"][-1]["reference"] == actual
        names = call("GET", route, params={"exp_user_id": ids["owner"]}).json()
        assert names["retcode"] == 0 and names["data"]["total"] == 5 and all(set(item) == {"id", "name"} for item in names["data"]["sessions"])
        page = call("GET", route, params={"page": 2, "page_size": 1}).json()
        assert page["retcode"] == 0 and page["data"]["total"] == 5 and len(page["data"]["sessions"]) == 1
        for shape, identifier in seeded.items():
            fixture = call("GET", route + "/" + identifier).json()
            assert fixture["retcode"] == 0
            messages = fixture["data"]["messages"]
            if shape == "no_messages":
                assert messages == []
            else:
                assert all("prompt" not in message for message in messages)
                assert messages[2]["reference"][0]["doc_type"] == "image"
                assert messages[4]["reference"][0]["doc_type_kwd"] == "table"
        for role in [None, "expired", "malformed", "unknown"]:
            denied = call("GET", route + "/" + session_id, role)
            assert denied.status_code == 401 and denied.json().get("retcode", denied.json().get("code")) != 0
            image = call("GET", "/api/v1/documents/images/" + quote(source["img_id"], safe=""), role)
            assert image.status_code == 401 and image.content != setup["cases"][raster_key][0]
        for denied_route, role, code in [(route + "/" + session_id, "other", 103), (route + "/" + uuid4().hex, "owner", 102), (f"/api/v1/agents/{uuid4().hex}/sessions/{session_id}", "owner", 103)]:
            denied = call("GET", denied_route, role)
            assert denied.status_code == 200 and denied.json()["retcode"] == code and not denied.json().get("data")
        for key in [f"{ids['foreign']}-other.png", f"{ids['kb']}-missing-object"]:
            denied = call("GET", "/api/v1/documents/images/" + quote(key, safe=""))
            assert denied.status_code == 404 and denied.json()["code"] != 0
        record["after"] = after = history_snapshot(env)
        assert_read_only(before, after)
        assert not writes
        record["read_phase_sql_writes"] = writes
        record["read_phase_verified"] = True
        if not stream:
            smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True)
            record["smoke"] = {"exit": smoke.returncode, "stdout": smoke.stdout, "stderr": smoke.stderr}
            assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    finally:
        for engine in [env["engine"], env["async_engine"].sync_engine]:
            sa.event.remove(engine, "before_cursor_execute", no_sql_write)
        _save(path, record)


@pytest.mark.parametrize("commit_accepted", [False, True])
def test_history_setup_commit_failure_cleans_registered_material(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, commit_accepted: bool) -> None:
    """Exercise rollback and committed-but-lost-reply setup failures on real stores."""
    env = image_http_api
    canvas_id, session_id = uuid4().hex, uuid4().hex
    key = "history-setup-failure:" + uuid4().hex
    failure = RuntimeError("controlled history setup commit failure")
    original_commit = Session.commit
    pools: list[Any] = []
    record: dict[str, Any] = {"commit_accepted": commit_accepted, "canvas_id": canvas_id, "session_id": session_id, "redis_key": key}

    def fail_setup_commit(db: Session) -> None:
        token = next((row for row in db.new if isinstance(row, APIToken) and row.name == "history-owner"), None)
        if db.get_bind() is not env["engine"] or token is None:
            original_commit(db)
            return
        db.add(UserCanvas(id=canvas_id, user_id=env["ids"]["owner"], title="setup failure scratch", dsl=message_dsl("unused")))
        db.add(API4Conversation(id=session_id, user_id=env["ids"]["owner"], dialog_id=canvas_id, source="agent"))
        db.flush()
        canvas = Canvas(json.dumps(message_dsl("unused")), tenant_id=env["ids"]["owner"])
        pools.append(canvas._thread_pool)
        assert canvas._thread_pool.submit(lambda: "real pool").result(timeout=5) == "real pool"
        assert REDIS_CONN.REDIS is not None
        REDIS_CONN.REDIS.set(key, "registered material")
        record["registered_before_failure"] = copy.deepcopy(env["manifest"]["history_sql_ids"])
        record["transaction_rows_before_failure"] = {
            "token": db.scalar(sa.select(sa.func.count()).select_from(APIToken).where(APIToken.tenant_id == token.tenant_id, APIToken.token == token.token)),
            "session": db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.id == session_id)),
            "canvas": db.scalar(sa.select(sa.func.count()).select_from(UserCanvas).where(UserCanvas.id == canvas_id)),
        }
        if commit_accepted:
            original_commit(db)
            with env["engine"].connect() as readback:
                assert readback.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.id == session_id)) == 1
            record["independent_committed_session_readback"] = 1
        raise failure

    with env["engine"].connect() as db:
        preserved_tokens = list(db.execute(sa.select(APIToken.__table__).order_by(APIToken.token)).mappings())
    with monkeypatch.context() as patches:
        patches.setattr(Session, "commit", fail_setup_commit)
        with pytest.raises(RuntimeError) as raised, history_environment(env, patches):
            pytest.fail("setup failure must prevent yield")
        assert raised.value is failure
        assert not sa.event.contains(Session, "before_flush", env["history_listener"])
    with env["engine"].connect() as db:
        remaining = {
            "session": db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.id == session_id)),
            "origin": db.scalar(sa.select(sa.func.count()).select_from(AgentExecutionOrigin).where(AgentExecutionOrigin.id == session_id)),
            "canvas": db.scalar(sa.select(sa.func.count()).select_from(UserCanvas).where(UserCanvas.id == canvas_id)),
            "token": db.scalar(sa.select(sa.func.count()).select_from(APIToken).where(APIToken.tenant_id == env["ids"]["owner"], APIToken.token == env["tokens"]["owner_api"])),
        }
        assert list(db.execute(sa.select(APIToken.__table__).order_by(APIToken.token)).mappings()) == preserved_tokens
    assert not any(remaining.values()), remaining
    assert env["manifest"]["history_cleanup"] and env["manifest"]["history_listener_removed"]
    assert record["transaction_rows_before_failure"] == {"token": 1, "session": 1, "canvas": 1}
    assert session_id in record["registered_before_failure"][API4Conversation.__tablename__]
    assert canvas_id in record["registered_before_failure"][UserCanvas.__tablename__]
    assert REDIS_CONN.REDIS is not None and not REDIS_CONN.REDIS.exists(key)
    assert pools and all(pool._shutdown and all(not thread.is_alive() for thread in pool._threads) for pool in pools)
    record.update(remaining=remaining, listener_removed=True, redis_absent=True, pools_closed=True, original_exception_preserved=True, unrelated_tokens_preserved=True)
    _save(env["evidence"] / f"{env['ids']['kb']}.history-setup-fault.json", record)
