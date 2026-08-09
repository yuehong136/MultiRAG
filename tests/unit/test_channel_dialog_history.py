"""Channel-specific Dialog history replacement and reasoning isolation contracts."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services import conversation_service
from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService


class _RecordingAsyncSession(AsyncSession):
    def __init__(self) -> None:
        super().__init__()
        self.rollback_calls = 0

    async def run_sync(self, fn, *args, **kwargs):
        return fn(Session(), *args, **kwargs)

    async def rollback(self) -> None:
        self.rollback_calls += 1


def _conversation() -> SimpleNamespace:
    messages = [
        {"role": "assistant", "content": "prologue"},
        {"role": "user", "content": "previous", "id": "turn-1"},
        {"role": "assistant", "content": "<think>private previous</think>previous answer", "id": "turn-1"},
        {"role": "user", "content": "same question", "id": "turn-2"},
        {"role": "assistant", "content": "<think>private old</think>old answer", "id": "turn-2"},
    ]
    conv = SimpleNamespace(
        id="dialog-session",
        dialog_id="dialog-1",
        message=messages,
        reference=[{"chunks": []}, {"chunks": []}],
    )
    conv.to_dict = lambda: {
        "id": conv.id,
        "dialog_id": conv.dialog_id,
        "message": deepcopy(conv.message),
        "reference": deepcopy(conv.reference),
    }
    return conv


async def test_dialog_regenerate_replaces_latest_turn_and_sanitizes_prompt(monkeypatch) -> None:
    conv = _conversation()
    dialog = SimpleNamespace(id="dialog-1", kb_ids=[])
    saved: dict[str, Any] = {}
    prompts: list[list[dict[str, Any]]] = []

    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kwargs: [dialog]))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, db, dialog_id: dialog))
    monkeypatch.setattr(ConversationService, "query", classmethod(lambda cls, db, **kwargs: [conv]))
    monkeypatch.setattr(
        ConversationService,
        "update_by_id",
        classmethod(lambda cls, db, session_id, data: saved.setdefault("payload", deepcopy(data))),
    )

    async def _chat(dia, msg, db, stream, **kwargs):
        del dia, db, stream, kwargs
        prompts.append(deepcopy(msg))
        yield {
            "answer": "<think>private new</think>new answer",
            "reference": {},
            "final": True,
        }

    monkeypatch.setattr(conversation_service, "async_chat", _chat)
    db = _RecordingAsyncSession()

    frames = [
        frame
        async for frame in conversation_service.async_completion(
            db,
            "tenant-1",
            "dialog-1",
            "same question",
            session_id="dialog-session",
            regenerate=True,
            persist_reasoning=False,
            require_visible_answer=True,
        )
    ]

    assert frames[-1].endswith('"data": true}\n\n')
    assert [message["content"] for message in prompts[0]] == ["previous", "previous answer", "same question"]
    payload = saved["payload"]
    assert [message["content"] for message in payload["message"] if message["role"] == "user"].count("same question") == 1
    assert payload["message"][-1]["content"] == "new answer"
    assert "private" not in str(payload)


async def test_dialog_reasoning_only_answer_does_not_commit_history(monkeypatch) -> None:
    conv = _conversation()
    dialog = SimpleNamespace(id="dialog-1", kb_ids=[])
    saved: dict[str, Any] = {}

    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kwargs: [dialog]))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, db, dialog_id: dialog))
    monkeypatch.setattr(ConversationService, "query", classmethod(lambda cls, db, **kwargs: [conv]))
    monkeypatch.setattr(
        ConversationService,
        "update_by_id",
        classmethod(lambda cls, db, session_id, data: saved.setdefault("payload", deepcopy(data))),
    )

    async def _chat(dia, msg, db, stream, **kwargs):
        del dia, msg, db, stream, kwargs
        yield {
            "answer": "<think>private only</think>",
            "reference": {},
            "final": True,
        }

    monkeypatch.setattr(conversation_service, "async_chat", _chat)
    db = _RecordingAsyncSession()

    frames = [
        frame
        async for frame in conversation_service.async_completion(
            db,
            "tenant-1",
            "dialog-1",
            "same question",
            session_id="dialog-session",
            regenerate=True,
            persist_reasoning=False,
            require_visible_answer=True,
        )
    ]

    assert any('"code": 500' in frame for frame in frames)
    assert saved == {}
    assert db.rollback_calls == 1
