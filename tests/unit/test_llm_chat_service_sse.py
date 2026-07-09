"""llm_app.chat_service_sse AsyncSession 化后的契约测试(§11 Phase 1)。

断言只锁 HTTP 契约:SSE 帧结构、非流式 JSON、setup 失败仍以 SSE 错误帧返回
(与旧实现 setup 在生成器内的行为一致)。service 层 monkeypatch 真实类。
"""

import json
import sys
import types

import pytest

from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService


def _llm_module():
    return sys.modules["api.apps.llm"]


class _FakeBundle:
    def __init__(self, *args, **kwargs):
        self.db = "sentinel"

    async def async_chat_streamly_delta(self, system, history, gen_conf, **kwargs):
        yield "你好"
        yield "你好,世界"

    async def async_chat(self, system, history, gen_conf, **kwargs):
        return "非流式回答"


@pytest.fixture
def llm_stubs(monkeypatch):
    monkeypatch.setattr(TenantService, "get_info_by", classmethod(lambda cls, s, uid: [{"tenant_id": "tenant-unit"}]))
    monkeypatch.setattr(TenantLLMService, "get_my_llms", classmethod(lambda cls, s, tid: [types.SimpleNamespace(llm_name="m1", mdl_type="chat")]))
    monkeypatch.setattr(_llm_module(), "get_model_config_by_type_and_name", lambda s, tid, t, n: {"llm_name": "m1", "max_tokens": 1024})
    monkeypatch.setattr(_llm_module(), "LLMBundle", _FakeBundle)


def _sse_frames(text: str) -> list[dict]:
    return [json.loads(line[len("data:") :].strip()) for line in text.splitlines() if line.strip().startswith("data:")]


def test_chat_service_sse_streams(client, llm_stubs):
    resp = client.post(
        "/v1/llm/chat_service_sse",
        json={"prompt": "p", "messages": [{"role": "user", "content": "hi"}], "llm_name": "m1", "stream": True, "gen_conf": {}},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1]["data"] is True
    text_frames = [f for f in frames if isinstance(f.get("data"), str) and f["data"]]
    assert text_frames and all(f["retcode"] == 0 for f in text_frames)
    assert "你好,世界" in text_frames[-1]["data"]


def test_chat_service_non_stream_returns_json(client, llm_stubs):
    resp = client.post(
        "/v1/llm/chat_service_sse",
        json={"prompt": "p", "messages": [{"role": "user", "content": "hi"}], "llm_name": "m1", "stream": False, "gen_conf": {}},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == "非流式回答"


class _FakeChatAgent:
    def __init__(self, *args, **kwargs):
        self.agent = types.SimpleNamespace(tools={})

    async def chat_async(self, **kwargs):
        return "agent 非流式回答"

    async def chat_with_tools_stream_async(self, **kwargs):
        yield "agent"
        yield "agent 流式回答"


def test_enhanced_chat_sse_streams(client, monkeypatch, llm_stubs):
    monkeypatch.setattr(_llm_module(), "ChatAgentAdapter", _FakeChatAgent)

    resp = client.post(
        "/v1/llm/enhanced_chat_sse",
        json={"prompt": "p", "messages": [{"role": "user", "content": "hi"}], "llm_name": "m1", "stream": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[-1]["data"] is True
    text_frames = [f for f in frames if isinstance(f.get("data"), str) and f["data"]]
    assert text_frames and "agent 流式回答" in text_frames[-1]["data"]


def test_enhanced_chat_non_stream_returns_json(client, monkeypatch, llm_stubs):
    monkeypatch.setattr(_llm_module(), "ChatAgentAdapter", _FakeChatAgent)

    resp = client.post(
        "/v1/llm/enhanced_chat_sse",
        json={"prompt": "p", "messages": [{"role": "user", "content": "hi"}], "llm_name": "m1", "stream": False},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"]["answer"] == "agent 非流式回答"


def test_chat_service_sse_setup_failure_keeps_sse_error_contract(client, llm_stubs):
    """未知模型的 setup 失败必须仍以 SSE 错误帧 + 结束帧返回(旧实现行为)。"""
    resp = client.post(
        "/v1/llm/chat_service_sse",
        json={"prompt": "p", "messages": [{"role": "user", "content": "hi"}], "llm_name": "no-such-model", "stream": True, "gen_conf": {}},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[0]["retcode"] == 500
    assert "no-such-model" in frames[0]["retmsg"]
    assert frames[-1]["data"] is True
