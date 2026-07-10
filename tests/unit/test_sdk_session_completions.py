"""sdk/session 两条 completion 路由的换轨钉板（§11 Phase 2 任务 2/3）。

锁 SSE 帧结构（code/message/data 形态、末帧差异）与非流式 JSON 形态：
completion → async_completion、iframe_completion → async_iframe_completion
换轨前后逐字节不变。桩打在 chat/async_chat 边界（同步/异步双名都桩，
换轨前后同一测试不变绿），帧构造走真实 completion 系生成器。
"""

import json
import sys
import types

import pytest

from api.db.services import conversation_service
from api.db.services.api_service import API4ConversationService
from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService


def _route_module(name: str):
    return sys.modules[name]


def _sse_frames(text: str):
    frames = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            frames.append(json.loads(chunk[len("data:") :]))
    return frames


def _fake_dialog():
    return types.SimpleNamespace(
        id="dlg-1",
        tenant_id="tenant-unit",
        kb_ids=[],
        prompt_config={"prologue": "您好！"},
    )


def _fake_conv():
    return types.SimpleNamespace(
        id="conv-1",
        dialog_id="dlg-1",
        message=[{"role": "user", "content": "hi", "id": "m1"}],
        reference=[],
        to_dict=lambda: {"id": "conv-1"},
    )


def _fake_sync_chat(answers):
    def _chat(dialog, messages, db, stream=True, **kwargs):
        yield from answers

    return _chat


def _fake_async_chat(answers):
    async def _chat(dialog, messages, db, stream=True, **kwargs):
        for answer in answers:
            yield answer

    return _chat


@pytest.fixture
def completion_stubs(monkeypatch):
    dia = _fake_dialog()
    conv = _fake_conv()
    updates: list[str] = []
    answers = [{"answer": "你好", "reference": {}}]
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, s, **kw: [dia]))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, s, did: dia))
    monkeypatch.setattr(ConversationService, "query", classmethod(lambda cls, s, **kw: [conv]))
    monkeypatch.setattr(ConversationService, "save", classmethod(lambda cls, s, **kw: True))
    monkeypatch.setattr(ConversationService, "update_by_id", classmethod(lambda cls, s, cid, data: updates.append(cid) or True))
    monkeypatch.setattr(API4ConversationService, "save", classmethod(lambda cls, s, **kw: True))
    monkeypatch.setattr(conversation_service, "chat", _fake_sync_chat(answers))
    monkeypatch.setattr(conversation_service, "async_chat", _fake_async_chat(answers))
    return types.SimpleNamespace(dialog=dia, conv=conv, updates=updates)


@pytest.fixture
def sdk_auth(client):
    from api.utils.api_utils import async_beta_token_required, async_token_required, beta_token_required, token_required

    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[beta_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    return client


# ---------------------------------------------------------------------------
# chat_completion（任务 2）
# ---------------------------------------------------------------------------


def test_chat_completion_stream_frames(sdk_auth, completion_stubs):
    resp = sdk_auth.post(
        "/api/v1/chats/dlg-1/completions",
        json={"question": "hi", "session_id": "conv-1", "stream": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    # 会话路径末帧无 message 键（与新会话路径不同，钉住差异）
    assert frames[-1] == {"code": 0, "data": True}
    answer_frames = [f for f in frames if isinstance(f.get("data"), dict)]
    assert answer_frames
    assert answer_frames[0]["code"] == 0
    assert answer_frames[0]["data"]["answer"] == "你好"
    assert answer_frames[0]["data"]["session_id"] == "conv-1"
    assert completion_stubs.updates == ["conv-1"]


def test_chat_completion_new_session_prologue_frames(sdk_auth, completion_stubs):
    resp = sdk_auth.post("/api/v1/chats/dlg-1/completions", json={"stream": True})

    assert resp.status_code == 200
    frames = _sse_frames(resp.text)
    # 新会话路径两帧都带 message 键（钉住与会话路径的差异）
    assert frames[-1] == {"code": 0, "message": "", "data": True}
    prologue = frames[0]
    assert prologue["code"] == 0
    assert prologue["message"] == ""
    assert set(prologue["data"].keys()) == {"answer", "reference", "audio_binary", "id", "session_id"}
    assert prologue["data"]["answer"] == "您好！"


def test_chat_completion_non_stream_returns_first_answer(sdk_auth, completion_stubs):
    """非流式分支：换轨前对同步生成器 async for 会 TypeError（预存在 bug），换轨后修复。"""
    resp = sdk_auth.post(
        "/api/v1/chats/dlg-1/completions",
        json={"question": "hi", "session_id": "conv-1", "stream": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["answer"] == "你好"
    assert body["data"]["session_id"] == "conv-1"


# ---------------------------------------------------------------------------
# chatbot_completions（任务 3）
# ---------------------------------------------------------------------------


def test_chatbot_completions_stream_prologue_frames(sdk_auth, completion_stubs):
    """请求模型无 session_id 字段→路由恒走新会话路径（存量契约，钉住）。"""
    resp = sdk_auth.post("/api/v1/chatbots/dlg-1/completions", json={"question": "hi", "stream": True})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1] == {"code": 0, "message": "", "data": True}
    prologue = frames[0]
    assert prologue["code"] == 0
    assert prologue["message"] == ""
    assert set(prologue["data"].keys()) == {"answer", "reference", "audio_binary", "id", "session_id"}
    assert prologue["data"]["answer"] == "您好！"


def test_chatbot_completions_non_stream_returns_prologue_frame_string(sdk_auth, completion_stubs):
    """新会话路径不分流式恒 yield SSE 字符串→非流式 data 是字符串（存量契约，钉住）。

    换轨前 async for 同步生成器 TypeError（预存在 bug），换轨后修复。
    """
    resp = sdk_auth.post("/api/v1/chatbots/dlg-1/completions", json={"question": "hi", "stream": False})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert isinstance(body["data"], str) and body["data"].startswith("data:")
    frame = json.loads(body["data"][len("data:") :])
    assert frame["data"]["answer"] == "您好！"
