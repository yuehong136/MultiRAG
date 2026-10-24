"""Non-object model tool arguments never reach execution, including streaming."""

import json
from typing import Any

import httpx
import pytest

from agent.tools.base import LLMToolPluginCallSession
from core.llm import chat


@pytest.mark.parametrize("value", [None, [], [1], 0, False, "text"])
async def test_plugin_session_rejects_non_object(value: Any) -> None:
    session = LLMToolPluginCallSession({"execute": object()}, lambda *args: pytest.fail("Callback executed"))
    with pytest.raises(TypeError, match="must be an object"):
        await session.tool_call_async("execute", value)


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("raw", ["[]", "null", "false", '"text"'])
async def test_sdk_rejects_non_object_arguments_before_tool_dispatch(monkeypatch: pytest.MonkeyPatch, backend: str, stream: bool, raw: str) -> None:
    requests: list[dict[str, Any]] = []

    def response(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 2
        message: dict[str, Any] = {"role": "assistant", "content": "finished"}
        if len(requests) == 1:
            call: dict[str, Any] = {"id": "owned-call", "type": "function", "function": {"name": "execute", "arguments": raw}}
            if stream:
                call["index"] = 0
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
        envelope = {
            "id": "owned",
            "object": "chat.completion.chunk" if stream else "chat.completion",
            "created": 1,
            "model": "deepseek-chat",
            "choices": [{"index": 0, "delta" if stream else "message": message, "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        if stream:
            return httpx.Response(200, content=f"data: {json.dumps(envelope)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}, request=request)
        return httpx.Response(200, json=envelope, request=request)

    def send(_client: httpx.Client, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return response(request)

    async def send_async(_client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return response(request)

    class Session:
        async def tool_call_async(self, *args: Any) -> Any:
            pytest.fail("Invalid arguments reached tool dispatch")

    monkeypatch.setattr(httpx.Client, "send", send)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    model = (chat.Base if backend == "base" else chat.LiteLLMBase)(
        "owned-key", "deepseek-chat", "http://owned.invalid/v1", max_retries=0, max_rounds=1, **({"provider": "DeepSeek"} if backend == "litellm" else {})
    )
    model.bind_tools(Session(), [{"type": "function", "function": {"name": "execute", "parameters": {"type": "object", "properties": {}}}}])
    try:
        if stream:
            answer = "".join([item async for item in model.async_chat_streamly_with_tools("", [{"role": "user", "content": "run"}]) if isinstance(item, str)])
        else:
            answer, _ = await model.async_chat_with_tools("", [{"role": "user", "content": "run"}])
        assert "must be a JSON object" in answer
        assert "finished" in answer
        assert len(requests) == 2
        assert "must be a JSON object" in requests[1]["messages"][-1]["content"]
    finally:
        if backend == "base":
            model.client.close()
            await model.async_client.close()
