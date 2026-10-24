"""Exercise SDK decoding and continuation serialization at the HTTP boundary."""

import asyncio
import json
import os
import socket
import threading
from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from core.llm import ChatModel, chat
from core.llm.chat_model.models.deepseek_chat import DeepSeekChat
from core.llm.chat_model.tool_history import ToolHistoryMixin, ToolResult
from core.llm.tool_decorator import tool

CALLS = [
    {"id": "call-first", "type": "function", "function": {"name": "first", "arguments": '{ "query": "中文", "n": 1 }'}},
    {"id": "call-second", "type": "function", "function": {"name": "second", "arguments": '{"query":"two"}'}},
]
TOOLS = [{"type": "function", "function": {"name": call["function"]["name"], "parameters": {"type": "object", "properties": {}}}} for call in CALLS]


class _ReasoningBase(chat.Base):
    def _need_reasoning_content_back(self) -> bool:
        return True


class _Session:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.finished: list[str] = []
        self.arguments: list[dict[str, Any]] = []
        self.second_finished = asyncio.Event()

    def tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, str]:
        self.started.append(name)
        self.arguments.append(arguments)
        self.finished.append(name)
        return {"content": name}

    async def tool_call_async(self, name: str, arguments: dict[str, Any]) -> dict[str, str]:
        self.started.append(name)
        self.arguments.append(arguments)
        if name == "first":
            await asyncio.wait_for(self.second_finished.wait(), timeout=2)
        else:
            self.second_finished.set()
        self.finished.append(name)
        return {"content": name}


def _envelope(message: dict[str, Any], *, stream: bool, final: bool = False) -> dict[str, Any]:
    return {
        "id": "completion-fixture",
        "object": "chat.completion.chunk" if stream else "chat.completion",
        "created": 1,
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta" if stream else "message": message, "finish_reason": "stop" if final else "tool_calls"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


def _http_response(request: httpx.Request, *, stream: bool, final: bool) -> httpx.Response:
    if final:
        messages = [{"role": "assistant", "content": "answer", "reasoning_content": "final reason"}]
    elif not stream:
        messages = [{"role": "assistant", "content": None, "tool_calls": deepcopy(CALLS), "reasoning_content": "tool reason"}]
    else:
        # Reverse the fragment order to require index-based assembly rather than zip/order.
        messages = [
            {
                "role": "assistant",
                "reasoning_content": "tool ",
                "tool_calls": [{**deepcopy(call), "index": i, "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"][:5]}} for i, call in enumerate(CALLS)],
            },
            {"reasoning_content": "reason", "tool_calls": [{"index": i, "function": {"arguments": CALLS[i]["function"]["arguments"][5:]}} for i in [1, 0]]},
        ]
    if not stream:
        return httpx.Response(200, json=_envelope(messages[0], stream=False, final=final), request=request)
    content = "".join(f"data: {json.dumps(_envelope(message, stream=True, final=final))}\n\n" for message in messages) + "data: [DONE]\n\n"
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"}, request=request)


@pytest.mark.parametrize(("backend", "registration"), [("sync", "legacy"), ("async", "legacy"), ("litellm", "legacy"), ("async", "decorated"), ("litellm", "decorated")])
@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_tool_continuation_wire_contract(monkeypatch: pytest.MonkeyPatch, backend: str, registration: str, stream: bool) -> None:
    requests: list[dict[str, Any]] = []
    decoded_calls: list[Any] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        body = json.loads(request.content)
        assert bool(body.get("stream", False)) == stream
        requests.append(body)
        assert len(requests) <= 2
        return _http_response(request, stream=stream, final=len(requests) == 2)

    def send_sync(client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return respond(request)

    async def send_async(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return respond(request)

    append_batch = ToolHistoryMixin._append_history_batch

    def capture(self: ToolHistoryMixin, history: list[dict[str, Any]], results: list[ToolResult], reasoning_content: str | None = None) -> list[dict[str, Any]]:
        decoded_calls.extend(result[0] for result in results)
        return append_batch(self, history, results, reasoning_content)

    # Keep real completion methods, SDK response decoding and request serialization.
    monkeypatch.setattr(httpx.Client, "send", send_sync)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    monkeypatch.setattr(ToolHistoryMixin, "_append_history_batch", capture)
    monkeypatch.setenv("DEEPSEEK_API_BASE", "http://fixture.invalid/v1")
    model: Any
    if backend == "litellm":
        model = ChatModel["DeepSeek"]("fixture-key", "deepseek-chat", "http://fixture.invalid/v1", provider="DeepSeek", max_retries=0, max_rounds=1)
    else:
        model = (DeepSeekChat if backend == "sync" else _ReasoningBase)("fixture-key", "deepseek-chat", "http://fixture.invalid/v1", max_retries=0, max_rounds=1)
    session = _Session()
    if registration == "legacy":
        model.bind_tools(session, TOOLS)
    else:

        @tool
        async def first(query: str, n: int) -> dict[str, str]:
            return await session.tool_call_async("first", {"query": query, "n": n})

        @tool
        async def second(query: str) -> dict[str, str]:
            return await session.tool_call_async("second", {"query": query})

        model.bind_tools(tools=[first, second])
    history = [{"role": "user", "content": "hello"}]
    try:
        if backend == "sync":
            answer = "".join(part for part in model.chat_streamly_with_tools("", history, {}) if isinstance(part, str)) if stream else model.chat_with_tools("", history, {})[0]
        elif stream:
            answer = "".join([part async for part in model.async_chat_streamly_with_tools("", history, {}) if isinstance(part, str)])
        else:
            answer, tokens = await model.async_chat_with_tools("", history, {})
            assert tokens == 10
        assert "answer" in answer and "final reason" in answer
        assert "**ERROR**" not in answer
        assert session.started == ["first", "second"]
        assert session.finished == (["first", "second"] if backend == "sync" else ["second", "first"])
        assert session.arguments == [{"query": "中文", "n": 1}, {"query": "two"}]
        assert history == [{"role": "user", "content": "hello"}]
        assert len(requests) == 2
        messages = requests[1]["messages"]
        assert [message["role"] for message in messages] == ["user", "assistant", "tool", "tool"]
        assert messages[1]["reasoning_content"] == "tool reason"
        assert messages[1]["tool_calls"] == ([{**call, "index": i} for i, call in enumerate(CALLS)] if stream else CALLS)
        assert messages[2:] == [{"role": "tool", "tool_call_id": call["id"], "content": json.dumps({"content": call["function"]["name"]})} for call in CALLS]
        assert len(decoded_calls) == 2
        if backend != "litellm":
            assert all(isinstance(call, ChoiceDeltaToolCall if stream else ChatCompletionMessageToolCall) for call in decoded_calls)
            if not stream:
                assert all(not hasattr(call, "index") for call in decoded_calls)
        else:
            assert all(type(call).__module__.startswith("litellm.") for call in decoded_calls)
            if not stream:
                assert all(getattr(call, "index", None) is None for call in decoded_calls)

        if not stream:
            # The single-call API has no active loop callers but shares the same contract.
            single_history: list[dict[str, Any]] = []
            model._append_history(single_history, decoded_calls[0], {"content": "first"}, reasoning_content="tool reason")
            assert single_history == [{"role": "assistant", "tool_calls": [CALLS[0]], "reasoning_content": "tool reason"}, messages[2]]
    finally:
        if backend == "sync":
            model.client.close()
        elif backend == "async":
            model.client.close()
            await model.async_client.close()


@pytest.fixture
def loopback_llm(request: pytest.FixtureRequest) -> Iterator[Any]:
    state = SimpleNamespace(requests=[], url="", final_content="answer")
    evidence = Path(os.environ["B_MODEL_EVIDENCE_DIR"]) / f"{request.node.name}.json" if os.getenv("B_MODEL_EVIDENCE_DIR") else None
    if evidence:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"owner": request.node.name, "registered": "127.0.0.1:0", "status": "before-bind"}))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            req = httpx.Request("POST", state.url + self.path)
            response = _http_response(req, stream=bool(body.get("stream")), final=len(state.requests) == 2)
            if len(state.requests) == 2 and not body.get("stream"):
                payload = response.json()
                payload["choices"][0]["message"]["content"] = state.final_content
                response = httpx.Response(200, json=payload)
            self.send_response(200)
            self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, name="test-llm-tool-loopback", daemon=True)
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    if evidence:
        evidence.write_text(json.dumps({"owner": request.node.name, "listener": state.url, "status": "bound"}))
    worker.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        with socket.socket() as probe:
            closed = probe.connect_ex(("127.0.0.1", server.server_port)) != 0
        assert not worker.is_alive() and closed
        if evidence:
            evidence.write_text(
                json.dumps({"owner": request.node.name, "listener": state.url, "status": "closed", "requests": len(state.requests), "threadAlive": worker.is_alive(), "listenerClosed": closed})
            )


@pytest.mark.parametrize("factory", ["VLLM", "OpenAI"])
@pytest.mark.parametrize("registration", ["legacy", "decorated"])
@pytest.mark.parametrize("stream", [False, True])
async def test_bundle_tool_roundtrip_over_owned_loopback_http(monkeypatch: pytest.MonkeyPatch, db: Session, loopback_llm: Any, factory: str, registration: str, stream: bool) -> None:
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: None))
    usages: list[tuple[int, int]] = []

    async def record(cls: type[TenantLLMService], model_id: int, tokens: int) -> bool:
        usages.append((model_id, tokens))
        return True

    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id_async", classmethod(record))
    config = {"id": 17, "llm_name": "test-model", "llm_factory": factory, "mdl_type": "chat", "api_key": "controlled-key", "api_base": loopback_llm.url, "is_tools": True}
    bundle = LLMBundle(db, "controlled-tenant", config, max_retries=0, max_rounds=1)
    session = _Session()
    if registration == "legacy":
        bundle.bind_tools(session, TOOLS)
    else:

        @tool
        async def first(query: str, n: int) -> dict[str, str]:
            return await session.tool_call_async("first", {"query": query, "n": n})

        @tool
        async def second(query: str) -> dict[str, str]:
            return await session.tool_call_async("second", {"query": query})

        bundle.bind_tools(tools=[first, second])
    try:
        history = [{"role": "user", "content": "hello"}]
        if stream:
            parts = [part async for part in bundle.async_chat_streamly("", history, {})]
            answer = "".join(parts)
        else:
            answer = await bundle.async_chat("", history, {})
        assert "answer" in answer and "**ERROR**" not in answer
        assert session.started == ["first", "second"] and session.finished == ["second", "first"]
        assert history == [{"role": "user", "content": "hello"}]
        assert len(loopback_llm.requests) == 2
        assert [msg["role"] for msg in loopback_llm.requests[1]["messages"]] == ["user", "assistant", "tool", "tool"]
        assert all(item["type"] == "function" for item in loopback_llm.requests[0]["tools"])
        # Preserve each existing backend's accounting for this SSE fixture:
        # Base retains the last usage total; LiteLLM omits chunk usage here and
        # falls back to content tokens. Registration must not change either.
        expected_tokens = (5 if factory == "VLLM" else 1) if stream else 10
        assert usages == [(17, expected_tokens)]
    finally:
        if hasattr(bundle.mdl, "client"):
            bundle.mdl.client.close()
        if hasattr(bundle.mdl, "async_client"):
            await bundle.mdl.async_client.close()


@pytest.fixture(autouse=True)
async def _close_litellm_logging_for_test_loop() -> AsyncIterator[None]:
    """Drain SDK callbacks before pytest closes this test's event loop."""
    yield
    if GLOBAL_LOGGING_WORKER._bound_loop is asyncio.get_running_loop():
        if GLOBAL_LOGGING_WORKER._queue is not None:
            await asyncio.wait_for(GLOBAL_LOGGING_WORKER._queue.join(), timeout=2)
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.stop(), timeout=2)


@pytest.mark.parametrize("factory", ["VLLM", "OpenAI"])
async def test_bundle_empty_final_over_owned_loopback_http(monkeypatch: pytest.MonkeyPatch, db: Session, loopback_llm: Any, factory: str) -> None:
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: None))
    usages: list[tuple[int, int]] = []

    async def record(cls: type[TenantLLMService], model_id: int, tokens: int) -> bool:
        usages.append((model_id, tokens))
        return True

    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id_async", classmethod(record))
    loopback_llm.final_content = None
    config = {"id": 17, "llm_name": "test-model", "llm_factory": factory, "mdl_type": "chat", "api_key": "controlled-key", "api_base": loopback_llm.url, "is_tools": True}
    bundle = LLMBundle(db, "controlled-tenant", config, max_retries=0, max_rounds=1)
    session = _Session()
    bundle.bind_tools(session, TOOLS)
    try:
        answer = await bundle.async_chat("", [{"role": "user", "content": "hello"}], {})
        assert answer.startswith("**ERROR**: MODEL_ERROR") and "without visible content" in answer
        assert session.started == ["first", "second"] and len(loopback_llm.requests) == 2
        assert usages == [(17, 10)]
    finally:
        if hasattr(bundle.mdl, "client"):
            bundle.mdl.client.close()
        if hasattr(bundle.mdl, "async_client"):
            await bundle.mdl.async_client.close()
