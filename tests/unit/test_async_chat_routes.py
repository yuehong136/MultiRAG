"""AsyncSession 化后的 chat 热路径契约测试(§11 Phase 1)。

覆盖三条经 async_chat 的路由:conversation completion、restful session_completion、
sdk OpenAI 兼容 completion。断言只锁 HTTP 契约(状态码/SSE 帧结构),
service 层 monkeypatch 真实类,async_chat 打桩为假异步生成器。
"""

import json
import sys
import types

import pytest

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
        to_dict=lambda: {"id": "conv-1"},
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


def test_session_completion_streams(client, monkeypatch, chat_route_stubs):
    chat_api = _route_module("api.apps.restful_apis.chat")
    calls = []

    monkeypatch.setattr(chat_api, "_owned_chat_exists", lambda s, tenant_id, chat_id: True)
    monkeypatch.setattr(chat_api, "async_chat", _fake_async_chat([{"answer": "answer", "reference": {}, "final": True}], calls))

    resp = client.post(
        "/api/v1/chats/dlg-1/sessions/conv-1/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True, "internet": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1]["data"] is True
    answer_frames = [f for f in frames if isinstance(f.get("data"), dict)]
    assert answer_frames and answer_frames[0]["code"] == 0
    assert calls[0]["internet"] is True
    assert chat_route_stubs.updates == ["conv-1"]


def test_chat_completion_openai_like_streams(client, monkeypatch, chat_route_stubs):
    from api.utils.api_utils import async_token_required

    sdk_session = _route_module("api.apps.sdk.session")
    calls = []

    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(
        sdk_session,
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
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True, "internet": True},
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
    sdk_session = _route_module("api.apps.sdk.session")

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
