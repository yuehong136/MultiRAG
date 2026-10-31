"""Cohere chat uses the chat API route for normal, streaming and tool calls."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from litellm import get_llm_provider

from core.llm import ChatModel, EmbeddingModel, RerankModel, chat


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tools", [False, True])
async def test_cohere_chat_completion_routes_explicit_provider(monkeypatch: pytest.MonkeyPatch, stream: bool, tools: bool) -> None:
    requests: list[dict[str, Any]] = []

    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="OK", tool_calls=None), finish_reason="stop")], usage=None)

    async def completion(**kwargs: Any) -> Any:
        requests.append(kwargs)
        return chunks() if stream else SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK", tool_calls=None), finish_reason="stop")], usage=None)

    monkeypatch.setattr(chat.litellm, "acompletion", completion)
    model = ChatModel["Cohere"]("fixture-key", "custom-command", provider="Cohere", max_retries=0)
    model.max_rounds = 1
    model.tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]
    history = [{"role": "user", "content": "Hello"}]
    if stream:
        call = model.async_chat_streamly_with_tools if tools else model.async_chat_streamly
        parts = [part async for part in call("", history, {"temperature": 0})]
        assert "OK" in parts
    else:
        call = model.async_chat_with_tools if tools else model.async_chat
        answer, _usage = await call("", history, {"temperature": 0})
        assert answer == "OK"
    assert len(requests) == 1
    request = requests[0]
    assert request["model"] == "cohere_chat/custom-command"
    assert get_llm_provider(request["model"])[1] == "cohere_chat"
    assert request["api_key"] == "fixture-key" and request["num_retries"] == 0
    assert request["temperature"] == 0 and request["drop_params"] is True
    assert request.get("stream", False) is stream
    if tools:
        assert request["tools"] == model.tools and request["tool_choice"] == "auto"
    else:
        assert "tools" not in request and "tool_choice" not in request


def test_cohere_embedding_and_rerank_keep_dedicated_adapters() -> None:
    assert ChatModel["Cohere"] is chat.LiteLLMBase
    assert EmbeddingModel["Cohere"].__module__ == "core.llm.embedding"
    assert RerankModel["Cohere"].__module__ == "core.llm.rerank"
