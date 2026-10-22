"""Real request/SQL isolation while synchronous feedback work is held open."""

import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from api.apps.restful_apis import chat_api
from api.db.db_models import Conversation, Dialog
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
