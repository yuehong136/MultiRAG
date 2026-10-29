"""AsyncSession 化后的 chat 热路径契约测试(§11 Phase 1)。

覆盖三条经 async_chat 的路由:conversation completion、restful session_completion、
sdk OpenAI 兼容 completion。断言只锁 HTTP 契约(状态码/SSE 帧结构),
service 层 monkeypatch 真实类,async_chat 打桩为假异步生成器。
"""

import json
import sys
import types
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Conversation, Dialog
from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService


def _route_module(name: str):
    """register_page 动态加载的真实路由模块(client fixture 保证已导入)。"""
    return sys.modules[name]


def _fake_conv(dialog_id: str = "dlg-1"):
    return types.SimpleNamespace(
        id="conv-1",
        dialog_id=dialog_id,
        message=[{"role": "user", "content": "hi", "id": "m1"}],
        reference=[],
        to_dict=lambda: {"id": "conv-1", "dialog_id": dialog_id, "message": [{"role": "user", "content": "hi", "id": "m1"}], "reference": []},
    )


def _fake_dialog(dialog_id: str = "dlg-1"):
    return types.SimpleNamespace(
        id=dialog_id,
        tenant_id="tenant-unit",
        llm_id="fake-llm",
        kb_ids=[],
        llm_setting={},
        prompt_config={},
    )


def _fake_async_chat(answers: list[dict], calls: list[dict] | None = None):
    async def fake(dialog, messages, db, stream=True, **kwargs):
        if calls is not None:
            calls.append({"stream": stream, **kwargs})
        for ans in answers:
            yield ans

    return fake


def _sse_frames(text: str) -> list:
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            frames.append("[DONE]")
        else:
            frames.append(json.loads(payload))
    return frames


@pytest.fixture
def chat_route_stubs(monkeypatch):
    """三条路由共用的 service 层桩。"""
    conv = _fake_conv()
    dialog = _fake_dialog()
    updates: list = []

    monkeypatch.setattr(ConversationService, "get_by_id", classmethod(lambda cls, s, cid: conv))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, s, did: dialog))
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, s, **kw: [dialog]))
    monkeypatch.setattr(ConversationService, "update_by_id", classmethod(lambda cls, s, cid, data: updates.append(cid) or True))
    monkeypatch.setattr(ConversationService, "save", classmethod(lambda cls, s, **data: updates.append(data["id"]) or True))
    return types.SimpleNamespace(conv=conv, dialog=dialog, updates=updates)


def test_conversation_completion_streams_and_persists(client, monkeypatch, chat_route_stubs):
    conversation_app = _route_module("api.apps.conversation")
    calls = []

    monkeypatch.setattr(conversation_app, "async_chat", _fake_async_chat([{"answer": "你好", "reference": {}, "final": True}], calls))

    resp = client.post(
        "/v1/conversation/completion",
        json={"conversation_id": "conv-1", "messages": [{"role": "user", "content": "hi", "id": "m1"}], "stream": True, "stream_output": "delta", "internet": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1]["data"] is True
    answer_frames = [f for f in frames if isinstance(f.get("data"), dict)]
    assert answer_frames and answer_frames[0]["data"]["answer"] == "你好"
    assert answer_frames[0]["retcode"] == 0
    assert calls[0]["internet"] is True
    assert chat_route_stubs.updates == ["conv-1"]


def test_conversation_completion_rejects_missing_conversation_id(client):
    resp = client.post("/v1/conversation/completion", json={"conversation_id": "", "messages": []})
    assert resp.status_code == 200
    assert resp.json()["retcode"] != 0


@pytest.mark.parametrize("audio_output", [None, "configured", "none"])
@pytest.mark.parametrize("mode", ["delta", "full", "json"])
@pytest.mark.parametrize("tts_enabled", [True, False])
def test_conversation_audio_preference_is_request_local(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    audio_output: str | None,
    mode: str,
    tts_enabled: bool,
) -> None:
    conversation_app = _route_module("api.apps.conversation")
    dialog = Dialog(
        id="dlg-1",
        tenant_id="tenant-unit",
        llm_id="fake-llm",
        kb_ids=["kb-1"],
        llm_setting={"temperature": 0.5},
        prompt_config={"tts": tts_enabled, "parameters": [{"key": "knowledge", "optional": True}]},
    )
    saved_config = deepcopy(dialog.prompt_config)
    conv = Conversation(id="conv-1", dialog_id=dialog.id, message=[], reference=[])
    persisted: list[dict[str, Any]] = []
    monkeypatch.setattr(ConversationService, "get_by_id", classmethod(lambda cls, db, cid: conv))
    monkeypatch.setattr(ConversationService, "update_by_id", classmethod(lambda cls, db, cid, data: persisted.append(deepcopy(data)) or True))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, db, did: dialog))
    synthesized: list[str] = []

    async def generate(request_dialog: Dialog, messages: list[dict[str, Any]], db: AsyncSession, stream: bool = True, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        assert "audio_output" not in kwargs
        assert request_dialog.kb_ids == ["kb-1"]
        audio = None
        if request_dialog.prompt_config["tts"]:
            synthesized.append("answer")
            audio = "synthetic-audio"
        if audio_output == "none":
            assert request_dialog is not dialog
            # Upstream may normalize nested JSON while generating. It must not
            # dirty the saved application or another request's configuration.
            request_dialog.prompt_config["parameters"][0]["optional"] = False
            request_dialog.llm_setting["temperature"] = 0.9
        yield {"answer": "answer [ID:0]", "reference": {"chunks": []}, "audio_binary": audio, "final": True}

    monkeypatch.setattr(conversation_app, "async_chat", generate)
    body: dict[str, Any] = {
        "conversation_id": "conv-1",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": mode != "json",
        "stream_output": "full" if mode == "full" else "delta",
    }
    if audio_output is not None:
        body["audio_output"] = audio_output
    response = client.post("/v1/conversation/completion", json=body)
    assert response.status_code == 200
    if mode == "json":
        result = response.json()
    else:
        frames = _sse_frames(response.text)
        assert frames[-1]["data"] is True
        result = frames[0]
    assert result["retcode"] == 0, result
    assert result["data"]["answer"] == "answer [ID:0]"
    assert synthesized == (["answer"] if tts_enabled and audio_output != "none" else [])
    assert result["data"]["audio_binary"] == ("synthetic-audio" if synthesized else None)
    assert dialog.prompt_config == saved_config
    assert dialog.llm_setting == {"temperature": 0.5}
    assert len(persisted) == 1
    assert persisted[0]["id"] == "conv-1"
    assert persisted[0]["message"][-1]["content"] == "answer [ID:0]"


def test_conversation_audio_preference_rejects_unknown_mode(client: TestClient) -> None:
    response = client.post(
        "/v1/conversation/completion",
        json={"conversation_id": "conv-1", "messages": [{"role": "user", "content": "hi"}], "audio_output": "typo"},
    )
    assert response.status_code == 422


def test_session_completion_streams(client, monkeypatch, chat_route_stubs):
    chat_api = _route_module("api.apps.restful_apis.chat")
    calls = []

    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda s, tenant_id, chat_id: True)
    monkeypatch.setattr(chat_api, "async_chat", _fake_async_chat([{"answer": "answer", "reference": {}, "final": True}], calls))

    resp = client.post(
        "/api/v1/chat/completions",
        json={"chat_id": "dlg-1", "session_id": "conv-1", "messages": [{"role": "user", "content": "hi"}], "stream": True, "internet": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1]["data"] is True
    answer_frames = [f for f in frames if isinstance(f.get("data"), dict)]
    assert answer_frames and answer_frames[0]["code"] == 0
    assert answer_frames[0]["data"]["chat_id"] == "dlg-1"
    assert calls[0]["internet"] is True
    assert chat_route_stubs.updates == ["conv-1"]


def test_chat_completion_autocreates_session_when_missing(client, monkeypatch, chat_route_stubs):
    chat_api = _route_module("api.apps.restful_apis.chat")
    created = {}

    def fake_create(s, chat_id, dialog, user_id):
        created["args"] = (chat_id, user_id)
        return _fake_conv(dialog_id=chat_id)

    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda s, tenant_id, chat_id: True)
    monkeypatch.setattr(chat_api, "_create_session_for_completion", fake_create)
    monkeypatch.setattr(chat_api, "async_chat", _fake_async_chat([{"answer": "ok", "reference": {}, "final": True}]))

    resp = client.post(
        "/api/v1/chat/completions",
        json={"chat_id": "dlg-1", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["chat_id"] == "dlg-1"
    assert body["data"]["session_id"] == "conv-1"
    assert created["args"] == ("dlg-1", "tenant-unit")
    assert chat_route_stubs.updates == ["conv-1"]


def test_chat_completion_direct_mode_without_chat_id(client, monkeypatch, chat_route_stubs):
    """无 chat_id 直连聊天:临时默认 dialog(租户默认模型),不落库。"""
    chat_api = _route_module("api.apps.restful_apis.chat")
    seen = {}

    async def fake_async_chat(dialog, messages, db, stream=True, **kwargs):
        seen["dialog"] = dialog
        yield {"answer": "直答", "reference": {}, "final": True}

    monkeypatch.setattr(chat_api, "async_chat", fake_async_chat)
    monkeypatch.setattr(
        chat_api.TenantService,
        "get_by_id",
        classmethod(lambda cls, s, tid: types.SimpleNamespace(llm_id="default-llm", tenant_llm_id=7)),
    )

    resp = client.post(
        "/api/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["answer"] == "直答"
    assert "chat_id" not in body["data"]
    assert seen["dialog"].llm_id == "default-llm"
    assert seen["dialog"].tenant_llm_id == 7
    assert seen["dialog"].kb_ids == []
    assert chat_route_stubs.updates == []


def test_chat_completion_rejects_session_without_chat(client):
    resp = client.post(
        "/api/v1/chat/completions",
        json={"session_id": "conv-1", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert body["message"] == "`chat_id` is required when `session_id` is provided."


def test_chat_completion_openai_like_streams(client, monkeypatch, chat_route_stubs):
    from api.utils.api_utils import async_token_required

    openai_api = _route_module("api.apps.restful_apis.openai")
    calls = []

    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(
        openai_api,
        "async_chat",
        _fake_async_chat(
            [
                {"answer": "part", "reference": {}, "final": False},
                {"answer": "part done", "reference": {}, "final": True},
            ],
            calls,
        ),
    )

    resp = client.post(
        "/api/v1/chats_openai/dlg-1/chat/completions",
        json={"model": "model", "messages": [{"role": "user", "content": "hi"}], "stream": True, "internet": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1] == "[DONE]"
    chunk_frames = [f for f in frames if isinstance(f, dict)]
    assert chunk_frames and all(f["object"] == "chat.completion.chunk" for f in chunk_frames)
    assert chunk_frames[-1]["choices"][0]["finish_reason"] == "stop"
    assert calls[0]["internet"] is True


def test_sdk_completion_request_models_expose_internet(client):
    sdk_session = _route_module("api.apps.restful_apis.bot")

    chat_request = sdk_session.ChatCompletionRequest(question="hi", internet=True)
    openai_request = sdk_session.ChatCompletionOpenAIRequest(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        internet=True,
    )
    chatbot_request = sdk_session.ChatbotCompletionRequest(question="hi", internet=True)

    assert chat_request.internet is True
    assert openai_request.internet is True
    assert chatbot_request.internet is True
