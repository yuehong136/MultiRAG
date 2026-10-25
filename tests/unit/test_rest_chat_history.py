"""Latest-message REST history, explicit branches, and failure isolation."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def history_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Any, dict[str, Any]]:
    route = sys.modules["api.apps.restful_apis.chat"]
    state: dict[str, Any] = {
        "calls": [],
        "writes": [],
        "saved": [],
        "failure": None,
        "row": {
            "id": "session",
            "dialog_id": "chat",
            "user_id": "tenant-unit",
            "name": "stored",
            "message": [
                {"role": "assistant", "content": "prologue"},
                {"role": "user", "content": "stored question", "id": "old"},
                {"role": "assistant", "content": "stored answer", "id": "old"},
            ],
            "reference": [{"chunks": [], "doc_aggs": []}],
        },
    }
    row = SimpleNamespace(**deepcopy(state["row"]))
    row.to_dict = lambda: deepcopy(state["row"])
    state["object"] = row
    dialog = SimpleNamespace(id="chat", tenant_id="tenant-unit", llm_id="model", llm_setting={}, prompt_config={}, kb_ids=[])
    monkeypatch.setattr(route, "_owned_chat_exists", lambda *_: True)
    monkeypatch.setattr(route.DialogService, "get_by_id", lambda *_: dialog)
    monkeypatch.setattr(route.ConversationService, "get_by_id", lambda *_: row)
    monkeypatch.setattr(route.ConversationService, "update_by_id", lambda db, id, payload: state["writes"].append(deepcopy(payload)) or 1)
    monkeypatch.setattr(route.ConversationService, "save", lambda db, **payload: state["saved"].append(deepcopy(payload)))

    async def chat(dialog: Any, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        state["calls"].append({"messages": deepcopy(messages), "stream": stream, "kwargs": kwargs})
        if state["failure"]:
            raise state["failure"]
        yield {"answer": "answer", "reference": {"chunks": []}, "final": True}

    monkeypatch.setattr(route, "async_chat", chat)
    return client, route, state


def _send(ctx: tuple[TestClient, Any, dict[str, Any]], **body: Any) -> Response:
    return ctx[0].post("/api/v1/chat/completions", json={"chat_id": "chat", "session_id": "session", "stream": False, **body})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_server_history_default_and_explicit_submitted_branch(history_route: tuple[TestClient, Any, dict[str, Any]], stream: bool, full: bool) -> None:
    _, _, state = history_route
    submitted = [
        {"role": "system", "content": "ignored"},
        {"role": "user", "content": "client question", "id": "client"},
        {"role": "assistant", "content": "client answer", "id": "client"},
        {"role": "user", "content": "latest", "id": "latest"},
    ]
    response = _send(history_route, messages=submitted, stream=stream, pass_all_history_messages=full, user_id="forged")
    assert response.status_code == 200
    if stream:
        assert json.loads(response.text.splitlines()[-2][5:])["data"] is True
    else:
        assert response.json()["code"] == 0
    assert [m["content"] for m in state["calls"][0]["messages"]] == (["client question", "client answer", "latest"] if full else ["stored question", "stored answer", "latest"])
    saved = state["writes"][0]
    assert saved["message"][-2]["id"] == saved["message"][-1]["id"] == "latest"
    assert saved["user_id"] == "tenant-unit"
    assert saved["reference"][-1] == {"chunks": []}
    assert "pass_all_history_messages" not in state["calls"][0]["kwargs"]
    assert "user_id" not in state["calls"][0]["kwargs"]
    assert state["object"].message == state["row"]["message"]


@pytest.mark.parametrize("session_id", [None, "", "session"])
def test_conversation_id_fallback_with_question(history_route: tuple[TestClient, Any, dict[str, Any]], session_id: str | None) -> None:
    response = _send(history_route, question="latest", session_id=session_id, conversation_id="session")
    assert response.json()["code"] == 0
    assert response.json()["data"]["session_id"] == "session"
    assert history_route[2]["calls"][0]["messages"][-1]["content"] == "latest"
    assert "conversation_id" not in history_route[2]["calls"][0]["kwargs"]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"messages": None, "question": "fallback"},
        {"messages": []},
        {"messages": ["bad"]},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"messages": [{"role": [], "content": "x"}]},
        {"messages": [{"role": {}, "content": "x"}]},
        {"messages": [{"role": "user"}]},
        {"messages": [{"role": "user", "content": 1}]},
        {"messages": [{"role": "assistant", "content": "x"}]},
        {"messages": [{"role": "user", "content": "x", "id": 3}]},
        {"question": "x", "session_id": "other", "conversation_id": "session"},
        *({"question": "x", "pass_all_history_messages": value} for value in ("false", "true", 0, 1, None, [], {})),
        {"question": "x", "pass_all_history": True},
        {"question": "x", "stream": "true"},
        *({"question": "x", "files": value} for value in (None, "bad", {}, ["id"], [{"id": "bad"}], [{"id": 3}])),
        {"messages": [{"role": "user", "content": "x", "files": None}]},
    ],
)
def test_invalid_contract_rejects_before_provider_or_writes(history_route: tuple[TestClient, Any, dict[str, Any]], extra: dict[str, Any]) -> None:
    response = _send(history_route, **extra)
    assert response.status_code == 422, response.text
    state = history_route[2]
    assert not state["writes"] and not state["saved"] and not state["calls"]
    assert state["object"].message == state["row"]["message"]


@pytest.mark.parametrize("stream", [False, True])
def test_failed_provider_does_not_mutate_history_or_autocreate_session(history_route: tuple[TestClient, Any, dict[str, Any]], stream: bool) -> None:
    state = history_route[2]
    state["failure"] = RuntimeError("failure")
    for session in ("session", None):
        response = _send(history_route, question="x", session_id=session, stream=stream)
        assert response.status_code == 200
        if stream:
            assert not any(json.loads(line[5:]).get("data") is True for line in response.text.splitlines() if line.startswith("data:"))
        else:
            assert response.json()["code"] != 0
    assert state["writes"] == state["saved"] == []
    assert state["object"].message == state["row"]["message"]


@pytest.mark.parametrize("stream", [False, True])
async def test_cancellation_does_not_persist_partial_history(history_route: tuple[TestClient, Any, dict[str, Any]], stream: bool) -> None:
    _, route, state = history_route
    state["failure"] = asyncio.CancelledError()
    db = AsyncSession()
    request = route.SessionCompletionRequest(question="cancel", chat_id="chat", session_id="session", stream=stream)
    with pytest.raises(asyncio.CancelledError):
        response = await route.session_completion(request, db=db, tenant_id="tenant-unit")
        if stream:
            async for _ in response.body_iterator:
                pass
    assert state["writes"] == state["saved"] == []
    assert state["object"].message == state["row"]["message"]


def test_question_files_use_authenticated_descriptors(history_route: tuple[TestClient, Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, route, state = history_route
    descriptor = {"id": "a" * 32, "created_by": "tenant-unit", "name": "trusted.txt", "mime_type": "text/plain"}
    restore = AsyncMock(return_value=[descriptor])
    monkeypatch.setattr(route.FileService, "resolve_runtime_uploads", restore)
    response = _send(history_route, question="read", files=[{"id": "a" * 32, "created_by": "victim", "name": "forged"}])
    assert response.json()["code"] == 0
    restore.assert_awaited_once_with("tenant-unit", ["a" * 32])
    assert state["calls"][0]["messages"][-1]["files"] == [descriptor]
    assert state["writes"][0]["message"][-2]["files"] == [descriptor]


def test_chat_denial_precedes_attachment_io(history_route: tuple[TestClient, Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, route, state = history_route
    restore = AsyncMock(side_effect=AssertionError("forbidden storage access"))
    monkeypatch.setattr(route.FileService, "resolve_runtime_uploads", restore)
    monkeypatch.setattr(route, "_owned_chat_exists", lambda *_: False)
    response = _send(history_route, question="x", files=[{"id": "a" * 32}])
    assert response.json()["code"] != 0
    restore.assert_not_awaited()
    assert state["writes"] == state["saved"] == state["calls"] == []


@pytest.mark.parametrize("message_id", ["old", "new-branch"])
def test_submitted_branch_keeps_only_matching_persisted_references(history_route: tuple[TestClient, Any, dict[str, Any]], message_id: str) -> None:
    state = history_route[2]
    reference = {"chunks": [{"document_id": "stored-reference"}]}
    state["row"]["reference"] = [reference]
    submitted = [
        {"role": "user", "content": "earlier", "id": message_id},
        {"role": "assistant", "content": "earlier answer", "id": message_id},
        {"role": "user", "content": "latest"},
    ]
    response = _send(history_route, messages=submitted, pass_all_history_messages=True)
    assert response.json()["code"] == 0
    assert state["writes"][0]["reference"] == [reference if message_id == "old" else {"chunks": [], "doc_aggs": []}, {"chunks": []}]


def test_failed_persistence_has_no_stream_success_tail(history_route: tuple[TestClient, Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, route, state = history_route
    monkeypatch.setattr(route.ConversationService, "update_by_id", lambda *_: 0)
    response = _send(history_route, question="answer without commit", stream=True)
    frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert frames[-1]["code"] != 0 and not any(frame.get("data") is True for frame in frames)
    assert state["object"].message == state["row"]["message"]


async def test_cancellation_after_delta_does_not_flush_partial_turn(history_route: tuple[TestClient, Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, route, state = history_route

    async def chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"answer": "partial", "reference": {}, "final": False}
        raise asyncio.CancelledError()

    monkeypatch.setattr(route, "async_chat", chat)
    response = await route.session_completion(route.SessionCompletionRequest(question="cancel", chat_id="chat", session_id="session"), db=AsyncSession(), tenant_id="tenant-unit")
    first = await anext(response.body_iterator)
    assert '"partial"' in first
    with pytest.raises(asyncio.CancelledError):
        await anext(response.body_iterator)
    assert state["writes"] == state["saved"] == []
    assert state["object"].message == state["row"]["message"]


@pytest.mark.parametrize("stream", [False, True])
async def test_completion_closes_owned_generator_on_early_return_or_disconnect(history_route: tuple[TestClient, Any, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    _, route, state = history_route
    closed: list[bool] = []

    async def chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"answer": "first", "reference": {}, "final": not stream}
            yield {"answer": "later", "reference": {}, "final": True}
        finally:
            closed.append(True)

    monkeypatch.setattr(route, "async_chat", chat)
    response = await route.session_completion(route.SessionCompletionRequest(question="close", chat_id="chat", session_id="session", stream=stream), db=AsyncSession(), tenant_id="tenant-unit")
    if stream:
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert state["writes"] == []
    else:
        assert len(state["writes"]) == 1
    assert closed == [True]
