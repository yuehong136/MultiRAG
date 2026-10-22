"""Blocking feedback work must leave the request session on the event loop."""

import asyncio
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis import chat_api
from common.constants import RetCode


def payload(session_id: str = "session") -> dict[str, Any]:
    return {
        "id": session_id,
        "dialog_id": "chat",
        "message": [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "id": "answer"}],
        "reference": [{"chunks": [{"chunk_id": "chunk", "kb_id": "kb"}]}],
    }


async def test_feedback_work_overlaps_without_moving_sql_session(monkeypatch: pytest.MonkeyPatch) -> None:
    loop_thread = threading.get_ident()
    original = payload()
    writes: list[dict[str, Any]] = []
    worker_threads: list[int] = []
    active = 0
    maximum_active = 0
    probe_samples = 0
    lock = threading.Lock()

    def get(s: Any, cid: str) -> SimpleNamespace:
        assert threading.get_ident() == loop_thread
        return SimpleNamespace(dialog_id="chat", to_dict=lambda: original)

    def save(s: Any, cid: str, data: dict[str, Any]) -> int:
        assert threading.get_ident() == loop_thread
        writes.append(deepcopy(data))
        return 1

    def slow_feedback(**kwargs: Any) -> dict[str, int]:
        nonlocal active, maximum_active
        with lock:
            worker_threads.append(threading.get_ident())
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.08)
        with lock:
            active -= 1
        return {"success_count": 1, "fail_count": 0}

    async def probe() -> None:
        nonlocal probe_samples
        while len(writes) < 4:
            await asyncio.sleep(0.005)
            with lock:
                probe_samples += int(active > 0)

    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *args: True)
    monkeypatch.setattr(chat_api.ConversationService, "get_by_id", get)
    monkeypatch.setattr(chat_api.ConversationService, "update_by_id", save)
    monkeypatch.setattr(chat_api.ChunkFeedbackService, "apply_feedback", slow_feedback)

    async def request() -> Any:
        async with AsyncSession() as db:
            return await chat_api.update_message_feedback("chat", "session", "answer", chat_api.ChatRequest(thumbup=True), db, "owner")

    async with asyncio.timeout(5):
        responses, _ = await asyncio.gather(asyncio.gather(*(request() for _ in range(4))), probe())
    assert all(json.loads(response.body)["code"] == 0 for response in responses)
    assert len(writes) == 4 and all(write["message"][2]["thumbup"] is True for write in writes)
    assert "thumbup" not in original["message"][2]
    assert all(worker != loop_thread for worker in worker_threads)
    assert maximum_active >= 2 and probe_samples >= 2


@pytest.mark.parametrize("case", ["foreign_chat", "foreign_session", "missing_session", "invalid_thumb"])
async def test_rejected_feedback_has_no_external_or_sql_write(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Rejected feedback performed IO")

    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda *args: case != "foreign_chat")
    conv = SimpleNamespace(dialog_id="other" if case == "foreign_session" else "chat", to_dict=payload)
    monkeypatch.setattr(chat_api.ConversationService, "get_by_id", lambda *args: None if case == "missing_session" else conv)
    monkeypatch.setattr(chat_api.ConversationService, "update_by_id", forbidden)
    monkeypatch.setattr(chat_api.ChunkFeedbackService, "apply_feedback", forbidden)
    async with AsyncSession() as db:
        response = await chat_api.update_message_feedback("chat", "session", "answer", chat_api.ChatRequest(thumbup="yes" if case == "invalid_thumb" else True), db, "owner")
    expected = RetCode.AUTHENTICATION_ERROR if case == "foreign_chat" else RetCode.DATA_ERROR
    assert json.loads(response.body)["code"] == expected
