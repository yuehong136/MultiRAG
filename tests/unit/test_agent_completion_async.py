"""canvas_service.completion 与 canvas/agent 路由契约（Phase 2 批次二）。

三条不变量：
① setup 产物全是纯 dict/str——ORM 对象不得跨流式期存活；
② 进入分钟级流式前 rollback 释放连接（idle-in-transaction 专项）；
③ Canvas 构造（组件 __init__ 各自开连接查模型）必须在工作线程执行。
"""

import json
import sys
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services import canvas_service
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService


def _route_module(name: str):
    return sys.modules[name]


class _FakeCanvas:
    """构造即记录所在线程；run() 产出两帧。"""

    built_on_worker: list[bool] = []

    def __init__(self, dsl, tenant_id, agent_id=None, canvas_id=None, custom_header=""):
        type(self).built_on_worker.append(threading.current_thread() is not threading.main_thread())
        self.dsl = dsl
        self.error = ""

    def reset(self):
        pass

    async def run(self, **kwargs):
        yield {"event": "message", "data": {"content": "hello"}}
        yield {"event": "message_end", "data": {}}

    def get_reference(self):
        return {"chunks": []}

    def __str__(self):
        return json.dumps({"graph": {}})


class _RecordingAsyncSession(AsyncSession):
    """继承真类过 beartype；记录 run_sync / rollback 的调用序，锁事务释放时机。"""

    def __init__(self, conv_row: dict):
        super().__init__()
        self.calls: list[str] = []
        self._conv_row = conv_row

    async def run_sync(self, fn, *args, **kwargs):
        self.calls.append("run_sync")
        return fn(Session(), *args, **kwargs)

    async def rollback(self):
        self.calls.append("rollback")


@pytest.fixture
def completion_stubs(monkeypatch):
    _FakeCanvas.built_on_worker = []
    saved: dict[str, object] = {}

    conv_row = {"id": "sess-1", "message": [], "reference": [], "dsl": "{}", "errors": ""}

    class _FakeConv:
        """ORM 替身：只在 run_sync 内存活，to_dict() 是逸出边界。"""

        def __init__(self):
            self.id = "sess-1"
            self.message = []
            self.dsl = "{}"

        def to_dict(self):
            return dict(conv_row)

    monkeypatch.setattr(canvas_service, "Canvas", _FakeCanvas)
    monkeypatch.setattr(API4ConversationService, "get_by_id", classmethod(lambda cls, s, sid: _FakeConv()))
    monkeypatch.setattr(
        API4ConversationService,
        "append_message",
        classmethod(lambda cls, s, cid, conv: saved.setdefault("payload", (cid, conv))),
    )
    return saved


async def test_completion_releases_connection_before_streaming(completion_stubs):
    db = _RecordingAsyncSession({"id": "sess-1"})

    frames = [f async for f in canvas_service.completion(db, "tenant-unit", "agent-1", session_id="sess-1", query="hi")]

    assert any('"content": "hello"' in f for f in frames)
    # ② 事务释放必须发生在流式之前：setup(run_sync) → rollback → 收尾写入(run_sync)
    assert db.calls == ["run_sync", "rollback", "run_sync"]
    # ③ Canvas 构造在工作线程
    assert _FakeCanvas.built_on_worker == [True]
    # ① 收尾写入的是纯 dict（不是 ORM 对象）
    conv_id, payload = completion_stubs["payload"]
    assert conv_id == "sess-1"
    assert isinstance(payload, dict)
    assert [m["role"] for m in payload["message"]] == ["user", "assistant"]
    assert payload["message"][1]["content"] == "hello"


async def test_completion_raises_when_session_missing(monkeypatch):
    monkeypatch.setattr(API4ConversationService, "get_by_id", classmethod(lambda cls, s, sid: None))
    db = _RecordingAsyncSession({})

    with pytest.raises(LookupError, match="Session not found"):
        [f async for f in canvas_service.completion(db, "tenant-unit", "agent-1", session_id="ghost", query="hi")]


# ---------------------------------------------------------------------------
# 路由层（canvas_service 打桩，锁 SSE 帧与鉴权链）
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_route_stubs(monkeypatch, client):
    async def _fake_completion(db, tenant_id, agent_id, session_id=None, **kwargs):
        yield "data:" + json.dumps({"event": "message", "data": {"content": "hi"}}) + "\n\n"

    for mod in ("api.apps.restful_apis.agent", "api.apps.sdk.session"):
        monkeypatch.setattr(_route_module(mod), "agent_completion", _fake_completion)

    monkeypatch.setattr(API4ConversationService, "get_by_id", classmethod(lambda cls, s, sid: SimpleNamespace(dialog_id="agent-1")))
    monkeypatch.setattr(UserCanvasService, "accessible", classmethod(lambda cls, s, cid, tid: True))

    from api.utils.api_utils import async_beta_token_required, async_token_required

    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    return client


def test_restful_agent_completions_stream_frames(agent_route_stubs):
    resp = agent_route_stubs.post(
        "/api/v1/agents/chat/completion",
        json={"agent_id": "agent-1", "session_id": "sess-1", "query": "hi", "stream": True},
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"content": "hi"' in resp.text
    assert "data:[DONE]" in resp.text


def test_legacy_agent_completion_routes_are_not_registered(agent_route_stubs):
    assert agent_route_stubs.post("/api/v1/agents/agent-1/completions", json={"query": "hi"}).status_code == 404
    assert agent_route_stubs.post("/v1/canvas/agent-1/completion", json={"query": "hi"}).status_code == 404


def test_restful_agent_routes_replace_legacy_canvas_and_sdk_surfaces(client):
    registered = {(method.upper(), path) for path, operations in client.app.openapi()["paths"].items() for method in operations}
    assert {
        ("GET", "/api/v1/agents"),
        ("POST", "/api/v1/agents"),
        ("GET", "/api/v1/agents/{canvas_id}"),
        ("PUT", "/api/v1/agents/{agent_id}"),
        ("DELETE", "/api/v1/agents/{agent_id}"),
        ("POST", "/api/v1/agents/chat/completion"),
        ("GET", "/api/v1/agents/{canvas_id}/sessions"),
        ("POST", "/api/v1/agents/{canvas_id}/sessions"),
        ("GET", "/api/v1/agents/{canvas_id}/sessions/{session_id}"),
        ("DELETE", "/api/v1/agents/{canvas_id}/sessions/{session_id}"),
    } <= registered
    assert ("POST", "/v1/canvas/set") not in registered
    assert ("POST", "/api/v1/agents/{agent_id}/completions") not in registered


def test_canvas_run_rejects_non_owner(client, monkeypatch):
    monkeypatch.setattr(UserCanvasService, "accessible", classmethod(lambda cls, s, cid, tid: False))

    resp = client.post("/api/v1/agents/chat/completion", json={"agent_id": "c1", "query": "hi"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] != 0
    assert "authorized" in body["retmsg"]


def test_restful_agents_openai_mode_streams(agent_route_stubs, monkeypatch):
    async def _fake_completion_openai(db, tenant_id, agent_id, question, session_id=None, stream=True, **kw):
        yield 'data: {"choices": []}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(_route_module("api.apps.restful_apis.agent"), "completion_openai", _fake_completion_openai)

    resp = agent_route_stubs.post(
        "/api/v1/agents/chat/completion",
        json={"agent_id": "agent-1", "openai-compatible": True, "model": "m", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    assert agent_route_stubs.post("/api/v1/agents_openai/agent-1/chat/completions", json={}).status_code == 404


def test_sdk_agent_bot_completions_stream_frames(agent_route_stubs):
    resp = agent_route_stubs.post("/api/v1/agentbots/agent-1/completions", json={"question": "hi", "stream": True})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert '"content": "hi"' in resp.text
