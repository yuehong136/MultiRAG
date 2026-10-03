"""canvas_service.completion 与 canvas/agent 路由契约（Phase 2 批次二）。

三条不变量：
① setup 产物全是纯 dict/str——ORM 对象不得跨流式期存活；
② 进入分钟级流式前 rollback 释放连接（idle-in-transaction 专项）；
③ Canvas 构造（组件 __init__ 各自开连接查模型）必须在工作线程执行。
"""

import asyncio
import json
import sys
import threading
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
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
    task_ids: list[str | None] = []

    def __init__(
        self,
        dsl: str,
        tenant_id: str | None,
        task_id: str | None = None,
        canvas_id: str | None = None,
        custom_header: object = "",
        run_context: object | None = None,
    ) -> None:
        del tenant_id, canvas_id, custom_header, run_context
        type(self).built_on_worker.append(threading.current_thread() is not threading.main_thread())
        type(self).task_ids.append(task_id)
        self.task_id = task_id
        self.dsl = dsl
        self.error = ""
        self.history: list[tuple[str, object]] = []
        self.globals: dict[str, object] = {"sys.history": []}

    def reset(self):
        pass

    async def run(self, **kwargs):
        yield {"event": "message", "data": {"content": "hello"}}
        yield {"event": "message_end", "data": {}}

    def get_reference(self):
        return {"chunks": []}

    def cancel_task(self) -> None:
        pass

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
def completion_stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    _FakeCanvas.built_on_worker = []
    _FakeCanvas.task_ids = []
    saved: dict[str, object] = {}

    conv_row = {"id": "sess-1", "message": [], "reference": [], "dsl": "{}", "errors": ""}

    class _FakeConv:
        """ORM 替身：只在 run_sync 内存活，to_dict() 是逸出边界。"""

        def __init__(self):
            self.id = "sess-1"
            self.message = []
            self.dsl = "{}"
            self.dialog_id = "agent-1"
            self.source = "agent"

        def to_dict(self):
            return dict(conv_row)

    monkeypatch.setattr(canvas_service, "Canvas", _FakeCanvas)

    async def bind_task(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(canvas_service, "bind_canvas_task", bind_task)
    monkeypatch.setattr(canvas_service, "finish_runtime", lambda task_id: None)
    monkeypatch.setattr(canvas_service, "require_runtime_finish", lambda task_id: None)
    monkeypatch.setattr(API4ConversationService, "get_by_id", classmethod(lambda cls, s, sid: _FakeConv()))

    def append_message(cls: type[API4ConversationService], db: Session, cid: str, conv: dict[str, Any]) -> int:
        saved["payload"] = (cid, conv)
        return 1

    monkeypatch.setattr(API4ConversationService, "append_message", classmethod(append_message))
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


async def test_completion_allocates_unique_task_id_per_execution(completion_stubs):
    async def consume_once() -> None:
        db = _RecordingAsyncSession({"id": "sess-1"})
        [frame async for frame in canvas_service.completion(db, "tenant-unit", "agent-1", session_id="sess-1", query="hi")]

    await asyncio.gather(*(consume_once() for _ in range(4)))

    assert len(_FakeCanvas.task_ids) == 4
    assert None not in _FakeCanvas.task_ids
    task_ids = [task_id for task_id in _FakeCanvas.task_ids if task_id is not None]
    assert len(set(task_ids)) == 4
    assert "agent-1" not in task_ids
    assert all(UUID(hex=task_id).version == 4 for task_id in task_ids)


async def test_close_at_frame_persists_cancelled_round_once(completion_stubs: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(canvas_service, "finish_runtime", lambda task_id: "cancel_requested")
    db = _RecordingAsyncSession({"id": "sess-1"})
    answers = canvas_service.completion(db, "tenant-unit", "agent-1", session_id="sess-1", query="hi")
    assert '"content": "hello"' in await anext(answers)
    await answers.aclose()
    await answers.aclose()
    _, payload = completion_stubs["payload"]
    assert [message["role"] for message in payload["message"]] == ["user"]
    assert "canceled" in payload["errors"] and "disconnected" in payload["errors"]
    assert db.calls.count("run_sync") == 2  # setup and exactly one failed round


async def test_openai_close_propagates_to_suspended_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    retained: list[AsyncGenerator[str, None]] = []

    async def source(**kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield 'data:{"event":"message","data":{"content":"hello"}}\n\n'
        finally:
            closed.append(True)

    def create(**kwargs: Any) -> AsyncGenerator[str, None]:
        result = source(**kwargs)
        retained.append(result)
        return result

    monkeypatch.setattr(canvas_service, "completion", create)
    answers = canvas_service.completion_openai(_RecordingAsyncSession({}), "tenant", "agent", "hi")
    assert "hello" in await anext(answers)
    await answers.aclose()
    assert closed == [True]


@pytest.mark.parametrize("failure", ["cancel", "redis"])
async def test_terminal_failure_restores_history_prefix(completion_stubs: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from common.exceptions import TaskCanceledException

    class HistoryCanvas(_FakeCanvas):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.history = [("user", "old question"), ("assistant", {"content": "old answer"})]
            self.globals = {"sys.history": ["user: old question", "assistant: old answer"]}

        async def run(self, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
            self.history.append(("user", kwargs["query"]))
            self.globals["sys.history"].append("user: " + kwargs["query"])
            yield {"event": "message", "data": {"content": "failed answer"}}
            yield {"event": "workflow_finished", "data": {}}
            self.history.append(("assistant", {"content": "failed answer"}))
            self.globals["sys.history"].append("assistant: failed answer")

        def __str__(self) -> str:
            return json.dumps({"history": self.history, "globals": self.globals})

    def finish(task_id: str) -> None:
        raise TaskCanceledException("cancelled") if failure == "cancel" else ConnectionError("Redis unavailable")

    monkeypatch.setattr(canvas_service, "Canvas", HistoryCanvas)
    monkeypatch.setattr(canvas_service, "require_runtime_finish", finish)
    frames = [frame async for frame in canvas_service.completion(_RecordingAsyncSession({}), "tenant", "agent-1", session_id="sess-1", query="current question")]
    assert '"event": "error"' in frames[-1]
    _, payload = completion_stubs["payload"]
    stored = json.loads(payload["dsl"])
    assert stored["history"] == [["user", "old question"], ["assistant", {"content": "old answer"}], ["user", "current question"]]
    assert stored["globals"]["sys.history"] == ["user: old question", "assistant: old answer", "user: current question"]


# ---------------------------------------------------------------------------
# 路由层（canvas_service 打桩，锁 SSE 帧与鉴权链）
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_route_stubs(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> TestClient:
    async def _fake_completion(db, tenant_id, agent_id, session_id=None, **kwargs):
        yield "data:" + json.dumps({"event": "message", "data": {"content": "hi"}}) + "\n\n"

    for mod in ("api.apps.restful_apis.agent", "api.apps.sdk.session"):
        monkeypatch.setattr(_route_module(mod), "agent_completion", _fake_completion)

    async def fake_prepare(db: AsyncSession, agent_id: str, caller_id: str, **kwargs: Any) -> canvas_service.PreparedAgentRun:
        return canvas_service.PreparedAgentRun(agent_id, caller_id, caller_id, "{}", None)

    monkeypatch.setattr(_route_module("api.apps.restful_apis.agent"), "prepare_agent_run", fake_prepare)

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


@pytest.mark.parametrize("release", [False, True])
@pytest.mark.parametrize("openai_compatible", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_restful_agent_delegation_refusal_is_not_an_unhandled_error(
    agent_route_stubs: TestClient, monkeypatch: pytest.MonkeyPatch, release: bool, openai_compatible: bool, stream: bool, caplog: pytest.LogCaptureFixture
) -> None:
    from api.db import CanvasCategory
    from api.db.db_models import get_async_db
    from api.identity.mcp_delegation.contracts import DelegationErrorCode, McpDelegationError
    from api.utils.api_utils import async_current_user

    module = _route_module("api.apps.restful_apis.agent")
    principal = agent_route_stubs.app.dependency_overrides[async_current_user]()

    class DebugDB(_RecordingAsyncSession):
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(one=lambda: (CanvasCategory.Agent, principal.id))

    def reject_canvas(*args: Any, **kwargs: Any) -> None:
        raise McpDelegationError(DelegationErrorCode.CONTEXT_REQUIRED)

    async def reject_completion(**kwargs: Any) -> AsyncGenerator[str, None]:
        reject_canvas()
        yield "unreachable"

    async def authorized(*args: Any, **kwargs: Any) -> None:
        pass

    db = DebugDB({})
    agent_route_stubs.app.dependency_overrides[get_async_db] = lambda: db
    monkeypatch.setattr(module.CanvasReplicaService, "load_for_run", lambda **kwargs: {"dsl": {}, "title": "debug"})
    monkeypatch.setattr(module, "require_canvas", authorized)
    monkeypatch.setattr(module, "Canvas", reject_canvas)
    monkeypatch.setattr(module, "agent_completion", reject_completion)
    monkeypatch.setattr(canvas_service, "completion", reject_completion)

    response = agent_route_stubs.post(
        "/api/v1/agents/chat/completion",
        json={"agent_id": "agent-1", "query": "hi", "release": release, "openai-compatible": openai_compatible, "stream": stream, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["retcode"] != 0
    assert body["data"] is False
    assert body["error_code"] == "context_required"
    assert response.headers["cache-control"] == "no-store"
    assert "authenticated run context" in body["retmsg"]
    assert "published Agent revision" in body["retmsg"]
    assert "rollback" in db.calls
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.parametrize("window", ["header_error", "header_cancel", "body_error", "success", "setup_cancel"])
async def test_registered_debug_response_owns_bound_runtime(client: TestClient, monkeypatch: pytest.MonkeyPatch, window: str) -> None:
    from starlette.requests import ClientDisconnect

    from api.db import CanvasCategory
    from api.utils.api_utils import async_current_user

    module = _route_module("api.apps.restful_apis.agent")
    principal = client.app.dependency_overrides[async_current_user]()
    calls: list[str] = []

    class DebugDB(_RecordingAsyncSession):
        async def execute(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(one=lambda: (CanvasCategory.Agent, principal.id))

        async def rollback(self) -> None:
            if window == "setup_cancel":
                raise asyncio.CancelledError()
            await super().rollback()

    class DebugCanvas(_FakeCanvas):
        async def run(self, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
            calls.append("run")
            async for answer in super().run(**kwargs):
                yield answer

        def cancel_task(self) -> None:
            calls.append("cleanup")

    async def bind(*args: Any, **kwargs: Any) -> None:
        calls.append("bind")

    async def authorized(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(UserCanvasService, "accessible", classmethod(lambda cls, db, cid, tid: True))
    monkeypatch.setattr(module.CanvasReplicaService, "load_for_run", lambda **kwargs: {"dsl": {}, "title": "debug"})
    monkeypatch.setattr(module.CanvasReplicaService, "commit_after_run", lambda **kwargs: calls.append("commit") or True)
    monkeypatch.setattr(module, "Canvas", DebugCanvas)
    monkeypatch.setattr(module, "bind_canvas_task", bind)
    monkeypatch.setattr(module, "require_canvas", authorized)
    monkeypatch.setattr(module, "finish_runtime", lambda task_id: calls.append("finish"))
    monkeypatch.setattr(module, "require_runtime_finish", lambda task_id: calls.append("finish"))
    assert "/api/v1/agents/chat/completion" in client.app.openapi()["paths"]
    if window == "setup_cancel":
        with pytest.raises(asyncio.CancelledError):
            await module.run({"agent_id": "agent-unit", "query": "hi"}, DebugDB({}), principal)
        assert calls == ["bind", "finish", "cleanup"]
        return
    response = await module.run({"agent_id": "agent-unit", "query": "hi"}, DebugDB({}), principal)
    assert calls == ["bind"]

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start" and window.startswith("header"):
            if window == "header_cancel":
                raise asyncio.CancelledError()
            raise OSError("header failed")
        if message["type"] == "http.response.body" and window == "body_error":
            raise OSError("body failed")

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    if window == "success":
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    else:
        with pytest.raises(asyncio.CancelledError if window == "header_cancel" else ClientDisconnect):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert calls.count("finish") == calls.count("cleanup") == 1
    assert calls.count("run") == (0 if window.startswith("header") else 1)
    assert calls.count("commit") == (1 if window == "success" else 0)


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


@pytest.mark.parametrize("failure", ["event", "code", "exception", "malformed", "nonobject", "partial", "empty", "text_error", "after_end", "bad_data"])
def test_agentbot_nonstream_rejects_incomplete_or_failed_run(agent_route_stubs: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    closed: list[bool] = []

    async def source(**kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            if failure == "empty":
                return
            yield 'data:{"event":"workflow_started","data":{}}\n\n'
            if failure in {"partial", "after_end"}:
                yield 'data:{"event":"message","data":{"content":"partial"}}\n\n'
                if failure == "partial":
                    return
                yield 'data:{"event":"message_end","data":{}}\n\n'
                raise RuntimeError("failure after a tentative end")
            if failure == "exception":
                raise RuntimeError("controlled execution failure")
            if failure == "malformed":
                yield "data:invalid-json\n\n"
            elif failure == "text_error":
                yield "data:**ERROR**: controlled execution failure\n\n"
            elif failure == "nonobject":
                yield "data:[]\n\n"
            elif failure == "bad_data":
                yield 'data:{"event":"message_end","data":false}\n\n'
            else:
                frame = {"event": "error", "data": {"error": "controlled execution failure"}} if failure == "event" else {"retcode": 100, "retmsg": "controlled execution failure", "data": False}
                yield "data:" + json.dumps(frame) + "\n\n"
            yield 'data:{"event":"message_end","data":{"content":"must not succeed"}}\n\n'
        finally:
            closed.append(True)

    monkeypatch.setattr(_route_module("api.apps.sdk.session"), "agent_completion", source)
    response = agent_route_stubs.post("/api/v1/agentbots/agent-1/completions", json={"query": "hi", "stream": False})
    assert response.status_code == 200 and closed == [True]
    assert response.json()["code"] != 0 and response.json()["message"] and "data" not in response.json()
    assert "must not succeed" not in response.text


@pytest.mark.parametrize("terminal", ["message_end", "workflow_finished", "user_inputs"])
def test_agentbot_nonstream_aggregates_answer_and_keeps_pause(agent_route_stubs: TestClient, monkeypatch: pytest.MonkeyPatch, terminal: str) -> None:
    closed: list[bool] = []

    async def source(**kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield 'data:{"event":"workflow_started","data":{},"session_id":"session"}\n\n'
            yield 'data:{"event":"message","data":{"content":"hel","reference":{"chunks":[1]}},"session_id":"session"}\n\n'
            yield 'data:{"event":"message","data":{"content":"lo"},"session_id":"session"}\n\n'
            yield "data:" + json.dumps({"event": terminal, "data": {"inputs": {"answer": {"type": "string"}}, "tips": "Fill answer", "reference": {"doc_aggs": [2]}}, "session_id": "session"}) + "\n\n"
            if terminal == "user_inputs":
                # Shared completion flushes pre-pause message_end after its
                # persisted user_inputs event. This must not hide the form.
                yield 'data:{"event":"message_end","data":{},"session_id":"session"}\n\n'
        finally:
            closed.append(True)

    monkeypatch.setattr(_route_module("api.apps.sdk.session"), "agent_completion", source)
    response = agent_route_stubs.post("/api/v1/agentbots/agent-1/completions", json={"query": "hi", "stream": False})
    assert response.status_code == 200 and closed == [True]
    body = response.json()
    assert body["code"] == 0 and body["data"]["event"] == terminal and body["data"]["session_id"] == "session"
    assert body["data"]["data"]["content"] == "hello"
    assert body["data"]["data"]["reference"] == {"chunks": [1], "doc_aggs": [2]}
    assert body["data"]["data"]["tips"] == "Fill answer" and body["data"]["data"]["inputs"] == {"answer": {"type": "string"}}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["event", "code", "exception", "malformed"])
def test_agent_adapter_preserves_failure_and_closes_generator(agent_route_stubs: TestClient, monkeypatch: pytest.MonkeyPatch, stream: bool, failure: str) -> None:
    closed: list[bool] = []

    async def fail_completion(**kwargs: Any) -> AsyncGenerator[str, None]:
        try:
            yield 'data:{"event":"workflow_started","data":{}}\n\n'
            if failure == "exception":
                raise RuntimeError("controlled execution failure")
            if failure == "malformed":
                yield "data:invalid-json\n\n"
            else:
                frame = {"event": "error", "data": {"error": "controlled execution failure"}} if failure == "event" else {"code": 100, "message": "controlled execution failure", "data": False}
                yield "data:" + json.dumps(frame) + "\n\n"
            yield 'data:{"event":"message_end","data":{"must_not_succeed":true}}\n\n'
        finally:
            closed.append(True)

    monkeypatch.setattr(_route_module("api.apps.restful_apis.agent"), "agent_completion", fail_completion)
    response = agent_route_stubs.post("/api/v1/agents/chat/completion", json={"agent_id": "agent-1", "release": True, "stream": stream})
    assert response.status_code == 200 and closed == [True]
    assert "must_not_succeed" not in response.text
    if stream:
        frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and "[DONE]" not in line]
        assert len(frames) == 1 and frames[0]["event"] == "error" and frames[0]["code"] != 0
        assert "[DONE]" not in response.text
    else:
        assert response.json()["retcode"] != 0 and response.json().get("data") is not True


@pytest.mark.parametrize("failure", ["event", "exception", "node", "after_terminal"])
@pytest.mark.parametrize("prepared_mode", [False, True])
async def test_completion_records_failure_without_success(completion_stubs: dict[str, object], monkeypatch: pytest.MonkeyPatch, failure: str, prepared_mode: bool) -> None:
    saved: list[dict[str, Any]] = []

    async def failing_run(self: _FakeCanvas, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        yield {"event": "workflow_started", "data": {}}
        if failure == "after_terminal":
            yield {"event": "message_end", "data": {}}
        if failure in {"exception", "after_terminal"}:
            raise RuntimeError("controlled execution failure")
        if failure == "node":
            self.error = "unhandled node failure"
            yield {"event": "node_finished", "data": {"error": self.error}}
        else:
            yield {"event": "error", "data": {"error": "controlled execution failure"}}
        yield {"event": "message_end", "data": {}}

    def persist(db: Session, conv_id: str, conv: dict[str, Any]) -> int:
        saved.append(dict(conv))
        return 1

    monkeypatch.setattr(_FakeCanvas, "run", failing_run)
    monkeypatch.setattr(_FakeCanvas, "cancel_task", lambda self: True, raising=False)
    monkeypatch.setattr(API4ConversationService, "append_message", persist)
    prepared = canvas_service.PreparedAgentRun("agent-1", "tenant-unit", "tenant-unit", "{}", None, {"id": "sess-1", "message": [], "dsl": "{}"}) if prepared_mode else None
    db = _RecordingAsyncSession({})
    frames = [json.loads(frame[5:]) async for frame in canvas_service.completion(db, "tenant-unit", "agent-1", session_id="sess-1", prepared_run=prepared, query="test")]
    assert frames[-1]["event"] == "error" and frames[-1]["code"] != 0
    assert not any(frame["event"] == "message_end" for frame in frames)
    assert len(saved) == 1 and saved[0]["errors"]
    assert [message["role"] for message in saved[0]["message"]] == ["user"]


@pytest.mark.parametrize("failure", ["cancel", "redis", "unbound"])
async def test_terminal_decision_precedes_successful_append(completion_stubs: dict[str, object], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from copy import deepcopy

    from common.exceptions import TaskCanceledException

    saved: list[dict[str, Any]] = []

    def reject_finish(task_id: str) -> None:
        assert not saved
        if failure == "cancel":
            raise TaskCanceledException("Task canceled before completion")
        if failure == "redis":
            raise ConnectionError("terminal Redis failure")
        raise RuntimeError("Task runtime ownership unavailable")

    def persist(cls: type[API4ConversationService], db: Session, identifier: str, conversation: dict[str, Any]) -> int:
        saved.append(deepcopy(conversation))
        return 1

    monkeypatch.setattr(canvas_service, "require_runtime_finish", reject_finish)
    monkeypatch.setattr(API4ConversationService, "append_message", classmethod(persist))
    frames = [json.loads(frame[5:]) async for frame in canvas_service.completion(_RecordingAsyncSession({}), "tenant-unit", "agent-1", session_id="sess-1", query="test")]
    assert len(saved) == 1 and saved[0]["errors"] and [message["role"] for message in saved[0]["message"]] == ["user"]
    assert frames[-1]["event"] == "error" and not any(frame["event"] == "message_end" for frame in frames)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["event", "code", "exception", "malformed"])
async def test_openai_agent_adapter_reports_errors_and_closes_source(monkeypatch: pytest.MonkeyPatch, stream: bool, failure: str) -> None:
    closed: list[bool] = []

    async def source(**kwargs: Any) -> AsyncGenerator[str, None]:
        assert kwargs["user_id"] == "caller" and kwargs["query"] == "question"
        try:
            yield 'data:{"event":"workflow_started","data":{}}\n\n'
            if failure == "exception":
                raise RuntimeError("controlled failure")
            if failure == "malformed":
                yield "data:invalid-json\n\n"
            else:
                frame = {"event": "error", "data": {"error": "controlled failure"}} if failure == "event" else {"code": 100, "message": "controlled failure", "data": False}
                yield "data:" + json.dumps(frame) + "\n\n"
            yield 'data:{"event":"message_end","data":{"content":"must not succeed"}}\n\n'
        finally:
            closed.append(True)

    monkeypatch.setattr(canvas_service, "completion", source)
    db = _RecordingAsyncSession({})
    answers = [answer async for answer in canvas_service.completion_openai(db, "tenant", "agent", "question", stream=stream, user_id="caller")]
    assert closed == [True] and len(answers) == 1
    frame = json.loads(str(answers[0])[5:]) if stream else answers[0]
    assert isinstance(frame, dict) and frame["error"]["type"] == "server_error" and frame["error"]["code"] != 0
    assert "choices" not in frame and "[DONE]" not in str(answers)
