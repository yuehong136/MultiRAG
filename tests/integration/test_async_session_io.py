"""Real request/SQL isolation while synchronous feedback work is held open."""

import asyncio
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from api.apps.restful_apis import chat_api
from api.db.db_models import Conversation, Dialog, Tenant, TenantLLM
from api.db.joint_services import tenant_model_service as tms
from api.db.services.llm_service import LLMBundle
from common.constants import RetCode
from tests.support.agent_update_release import release_api as release_api


def messages() -> list[dict[str, Any]]:
    return [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "id": "answer"}]


@pytest.fixture
def feedback_api(release_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    env = release_api
    chats = [uuid4().hex for _ in range(2)]
    sessions = [uuid4().hex for _ in range(5)]
    try:
        with Session(env["engine"]) as db:
            db.add_all([Dialog(id=chats[i], tenant_id=env["owners"][i], name="Feedback scratch", llm_id="unused") for i in range(2)])
            db.add_all(
                Conversation(id=sid, dialog_id=chats[int(i == 4)], user_id=env["owners"][int(i == 4)], message=messages(), reference=[{"chunks": [{"id": "chunk", "dataset_id": "kb"}]}])
                for i, sid in enumerate(sessions)
            )
            db.commit()
        yield dict(env, chats=chats, sessions=sessions)
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Conversation).where(Conversation.id.in_(sessions)))
            db.execute(sa.delete(Dialog).where(Dialog.id.in_(chats)))
            db.commit()
        with Session(env["engine"]) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Conversation).where(Conversation.id.in_(sessions))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(Dialog).where(Dialog.id.in_(chats))) == 0


def test_feedback_requests_leave_event_loop_responsive_and_persist(feedback_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_property: Any) -> None:
    env = feedback_api
    release = threading.Event()
    all_entered = threading.Event()
    lock = threading.Lock()
    calls: list[dict[str, Any]] = []

    def blocking_feedback(**kwargs: Any) -> dict[str, int]:
        with lock:
            calls.append(deepcopy(kwargs))
            if len(calls) == 4:
                all_entered.set()
        assert release.wait(timeout=10), "Feedback worker was never released"
        return {"success_count": 1, "fail_count": 0}

    monkeypatch.setattr(chat_api.ChunkFeedbackService, "apply_feedback", blocking_feedback)
    url = env["base"] + f"/api/v1/chats/{env['chats'][0]}/sessions/{{}}/messages/answer/feedback"
    headers = {"Authorization": f"Bearer {env['jwts'][0]}"}

    def request(sid: str) -> requests.Response:
        return requests.put(url.format(sid), headers=headers, json={"thumbup": True}, timeout=20)

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(request, sid) for sid in env["sessions"][:4]]
        try:
            assert all_entered.wait(timeout=5), "Synchronous feedback serialized the event loop"
            probe = requests.get(env["base"] + f"/api/v1/chats/{env['chats'][0]}/sessions", headers=headers, timeout=2)
            assert probe.status_code == 200 and probe.json()["code"] == 0
            assert not any(future.done() for future in futures)
            record_property("simultaneous_blocked_workers", len(calls))
            record_property("sql_route_probe_while_workers_blocked", probe.status_code)
        finally:
            release.set()
        for future in futures:
            response = future.result(timeout=20)
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
    with Session(env["engine"]) as db:
        rows = list(db.scalars(sa.select(Conversation).where(Conversation.id.in_(env["sessions"][:4]))))
        assert len(rows) == 4 and all(row.message[2]["thumbup"] is True for row in rows)
    assert len(calls) == 4 and all(call["tenant_id"] == env["owners"][0] for call in calls)

    before = []
    with Session(env["engine"]) as db:
        before = [deepcopy(db.get(Conversation, sid).to_dict()) for sid in env["sessions"]]
    for sid, token, body in [
        (env["sessions"][0], env["jwts"][1], {"thumbup": False}),
        (env["sessions"][4], env["jwts"][0], {"thumbup": False}),
        (env["sessions"][0], "invalid", {"thumbup": False}),
        (env["sessions"][0], env["jwts"][0], {"thumbup": "bad"}),
    ]:
        response = requests.put(url.format(sid), headers={"Authorization": f"Bearer {token}"}, json=body, timeout=5)
        assert response.status_code == 401 or response.json()["code"] in {RetCode.DATA_ERROR, RetCode.AUTHENTICATION_ERROR}
    with Session(env["engine"]) as db:
        assert [db.get(Conversation, sid).to_dict() for sid in env["sessions"]] == before
    assert len(calls) == 4


async def test_feedback_reads_uncommitted_request_state_without_cross_thread_session(bootstrapped_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    chat_id, session_id, owner_id = (uuid4().hex for _ in range(3))
    loop_thread = threading.get_ident()
    sql_threads: list[int] = []
    feedback_threads: list[int] = []

    def track_sql(*args: Any) -> None:
        sql_threads.append(threading.get_ident())

    def feedback(**kwargs: Any) -> dict[str, int]:
        feedback_threads.append(threading.get_ident())
        return {"success_count": 1, "fail_count": 0}

    sa.event.listen(bootstrapped_async_engine.sync_engine, "before_cursor_execute", track_sql)
    monkeypatch.setattr(chat_api.ChunkFeedbackService, "apply_feedback", feedback)
    try:
        async with AsyncSession(bootstrapped_async_engine, expire_on_commit=False) as db:
            db.add(Dialog(id=chat_id, tenant_id=owner_id, name="Uncommitted request", llm_id="unused", status="1"))
            db.add(Conversation(id=session_id, dialog_id=chat_id, user_id=owner_id, message=messages(), reference=[{"chunks": [{"id": "chunk", "dataset_id": "kb"}]}]))
            await db.flush()
            async with AsyncSession(bootstrapped_async_engine) as independent:
                assert await independent.get(Conversation, session_id) is None
            response = await chat_api.update_message_feedback(chat_id, session_id, "answer", chat_api.ChatRequest(thumbup=True), db, owner_id)
            assert json.loads(response.body)["code"] == 0
        async with AsyncSession(bootstrapped_async_engine) as independent:
            saved = await independent.get(Conversation, session_id)
            assert saved is not None and saved.message[2]["thumbup"] is True
        assert sql_threads and set(sql_threads) == {loop_thread}
        assert feedback_threads and all(thread != loop_thread for thread in feedback_threads)
    finally:
        sa.event.remove(bootstrapped_async_engine.sync_engine, "before_cursor_execute", track_sql)
        async with AsyncSession(bootstrapped_async_engine) as db:
            await db.execute(sa.delete(Conversation).where(Conversation.id == session_id))
            await db.execute(sa.delete(Dialog).where(Dialog.id == chat_id))
            await db.commit()
        async with AsyncSession(bootstrapped_async_engine) as db:
            assert await db.get(Conversation, session_id) is None
            assert await db.get(Dialog, chat_id) is None


_MODEL_DEFAULTS = [
    ("chat", "llm_id", "tenant_llm_id"),
    ("embedding", "embd_id", "tenant_embd_id"),
    ("speech2text", "asr_id", "tenant_asr_id"),
    ("image2text", "img2txt_id", "tenant_img2txt_id"),
    ("rerank", "rerank_id", "tenant_rerank_id"),
    ("tts", "tts_id", "tenant_tts_id"),
]


@pytest.fixture
def model_factory_scope(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    owners = [uuid4().hex, uuid4().hex]
    ids: dict[tuple[str, str], int] = {}
    constructed: list[dict[str, Any]] = []
    closed: list[int] = []

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            try:
                yield db
            finally:
                closed.append(id(db))

    class ProbeBundle(LLMBundle):
        def __init__(self, db: Session, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> None:
            self.db = db
            self.model_config = model_config
            # Actual blocking SQL on the factory's own connection, not a fake
            # await count. A concurrent coroutine must still get scheduled.
            db.execute(sa.text("SELECT pg_sleep(0.05)"))
            constructed.append({"thread": threading.get_ident(), "session": id(db), "tenant": tenant_id, "model": model_config["id"]})

    monkeypatch.setattr(tms.settings, "FACTORY_LLM_INFOS", [{"name": "ScopeFixture"}])
    monkeypatch.setattr(tms, "db_connection", connection)
    monkeypatch.setattr(tms, "LLMBundle", ProbeBundle)
    try:
        with Session(bootstrapped_engine) as db:
            for owner in owners:
                tenant = Tenant(id=owner, llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive")
                db.add(tenant)
                for model_type, name_field, id_field in _MODEL_DEFAULTS:
                    model = TenantLLM(tenant_id=owner, llm_factory="ScopeFixture", llm_name=f"model-{model_type}", mdl_type=model_type, api_key="fixture", api_base="http://fixture.invalid")
                    db.add(model)
                    db.flush()
                    assert model.id is not None
                    ids[owner, model_type] = model.id
                    setattr(tenant, id_field, model.id)
                    setattr(tenant, name_field, f"model-{model_type}@ScopeFixture")
            db.commit()
        yield {"engine": bootstrapped_engine, "owners": owners, "ids": ids, "constructed": constructed, "closed": closed}
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.tenant_id.in_(owners)))
            db.execute(sa.delete(Tenant).where(Tenant.id.in_(owners)))
            db.commit()
        with Session(bootstrapped_engine) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id.in_(owners))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(Tenant).where(Tenant.id.in_(owners))) == 0


@pytest.mark.parametrize(("model_type", "name_field", "id_field"), _MODEL_DEFAULTS)
async def test_foreign_model_ids_fail_before_provider_or_name_fallback(model_factory_scope: dict[str, Any], monkeypatch: pytest.MonkeyPatch, model_type: str, name_field: str, id_field: str) -> None:
    env = model_factory_scope
    owner, foreign = env["owners"]
    foreign_id = env["ids"][foreign, model_type]
    with Session(env["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == owner).values({id_field: foreign_id}))
        db.commit()

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("An explicit foreign model ID fell back to a name")

    monkeypatch.setattr(tms, "get_model_config_by_type_and_name", forbidden)
    with Session(env["engine"]) as db, pytest.raises(LookupError, match="not found"):
        tms.get_tenant_default_model_by_type(db, owner, model_type)
    with pytest.raises(LookupError, match="not found"):
        await tms.build_default_bundle_async(owner, model_type)
    with pytest.raises(LookupError, match="not found"):
        await tms.build_bundle_by_id_async(owner, foreign_id)
    assert env["constructed"] == []
    with Session(env["engine"]) as db:
        tenant = db.get(Tenant, owner)
        assert tenant is not None and getattr(tenant, id_field) == foreign_id
        assert getattr(tenant, name_field) == f"model-{model_type}@ScopeFixture"
        assert not any(db.scalars(sa.select(TenantLLM.used_tokens).where(TenantLLM.tenant_id.in_(env["owners"]))))


async def test_owned_model_factories_keep_offload_detachment_and_no_id_name_resolution(model_factory_scope: dict[str, Any]) -> None:
    env = model_factory_scope
    owner = env["owners"][0]
    model_id = env["ids"][owner, "chat"]
    loop_thread = threading.get_ident()
    task = asyncio.create_task(tms.build_bundle_by_id_async(owner, model_id))
    probes = 0
    while not task.done():
        await asyncio.sleep(0.005)
        probes += 1
    bundle = await task
    assert probes >= 2 and bundle.db is None
    default_bundle = await tms.build_default_bundle_async(owner, "chat")
    assert default_bundle.db is None and default_bundle.model_config["id"] == model_id
    with Session(env["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == owner).values(tenant_llm_id=None))
        db.commit()
    by_name = await tms.build_default_bundle_async(owner, "chat")
    assert by_name.db is None and by_name.model_config["id"] == model_id
    assert len(env["constructed"]) == 3
    assert all(item["thread"] != loop_thread and item["tenant"] == owner and item["model"] == model_id for item in env["constructed"])
    assert len(env["closed"]) == 3
    assert all(item["session"] in env["closed"] for item in env["constructed"])
