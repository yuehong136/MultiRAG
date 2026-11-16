"""OpenAI GPT-5/6 limits survive the real LiteLLM/SDK serialization boundary."""

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest

from core.llm import SupportedLiteLLMProvider as Provider
from core.llm import chat


@pytest.mark.parametrize("provider", [Provider.OpenAI, Provider.Azure_OpenAI])
@pytest.mark.parametrize("model", ["gpt-5.4", "gpt-6.1-terra"])
@pytest.mark.parametrize(
    ("configuration", "request_options", "expected"),
    [
        ({"max_tokens": 31}, {}, 31),
        ({}, {"max_tokens": 37}, 37),
        ({"max_tokens": 31}, {"max_tokens": 37}, 37),
        ({"max_completion_tokens": 41}, {"max_tokens": 37}, 41),
        ({"max_tokens": 31}, {"max_completion_tokens": 43}, 43),
        ({"max_completion_tokens": 41}, {"max_completion_tokens": 43, "max_tokens": 37}, 43),
    ],
)
def test_policy_maps_both_layers_with_explicit_modern_priority_and_no_mutation(provider: Provider, model: str, configuration: dict[str, Any], request_options: dict[str, Any], expected: int) -> None:
    before = deepcopy((configuration, request_options))
    generation, options = chat._apply_model_family_policies(model, backend="litellm", provider=provider, gen_conf=configuration, request_kwargs=request_options)
    merged = {**generation, **options}
    assert merged["max_completion_tokens"] == expected and "max_tokens" not in merged
    assert (configuration, request_options) == before


@pytest.mark.parametrize(
    ("backend", "provider", "model"),
    [("base", Provider.OpenAI, "gpt-6.1-terra"), ("litellm", Provider.OpenAI, "gpt-4.1"), ("litellm", Provider.OpenRouter, "gpt-6.1-terra"), ("litellm", Provider.Tongyi_Qianwen, "qwen-flash")],
)
def test_other_model_and_backend_policies_do_not_reinterpret_legacy_limits(backend: str, provider: Provider, model: str) -> None:
    generation, request = chat._apply_model_family_policies(model, backend=backend, provider=provider, gen_conf={"max_tokens": 31}, request_kwargs={"max_tokens": 37})
    assert generation == {"max_tokens": 31} and request == {"max_tokens": 37}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider", [Provider.OpenAI, Provider.Azure_OpenAI])
async def test_real_litellm_sdk_wire_uses_modern_cap(monkeypatch: pytest.MonkeyPatch, stream: bool, provider: Provider) -> None:
    sent: list[dict[str, Any]] = []

    async def send(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        payload = json.loads(request.content)
        sent.append(payload)
        assert payload["max_completion_tokens"] == 53 and "max_tokens" not in payload
        assert not {"temperature", "top_p", "logprobs", "top_logprobs"} & payload.keys()
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
        if stream:
            chunk = {
                "id": "owned-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-6.1-terra",
                "choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": usage,
            }
            return httpx.Response(200, content="data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}, request=request)
        body = {
            "id": "owned-chat",
            "object": "chat.completion",
            "created": 0,
            "model": "gpt-6.1-terra",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": usage,
        }
        return httpx.Response(200, json=body, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    key = json.dumps({"api_key": "owned-fake", "api_version": "2025-04-01-preview"}) if provider == Provider.Azure_OpenAI else "owned-fake"
    model = chat.LiteLLMBase(key, "gpt-6.1-terra", "https://fixture.invalid/v1", provider=provider, max_retries=0)
    config = {"max_tokens": 53, "temperature": 0.1, "top_p": 0.9}
    before = deepcopy(config)
    if stream:
        result = [part async for part in model.async_chat_streamly("", [{"role": "user", "content": "Reply OK"}], config)]
        assert "OK" in "".join(part for part in result if isinstance(part, str))
    else:
        assert (await model.async_chat("", [{"role": "user", "content": "Reply OK"}], config))[0] == "OK"
    assert len(sent) == 1 and config == before


@pytest.mark.parametrize(
    ("configuration", "request_options", "expected"), [({"max_completion_tokens": 41}, {"max_tokens": 37}, 41), ({"max_tokens": 31}, {"max_completion_tokens": 43}, 43), ({}, {"max_tokens": 47}, 47)]
)
async def test_adapter_nonstream_keeps_cross_layer_priority(monkeypatch: pytest.MonkeyPatch, configuration: dict[str, Any], request_options: dict[str, Any], expected: int) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    model = chat.LiteLLMBase("owned-fake", "gpt-6.1-terra", provider=Provider.OpenAI, max_retries=0)

    @asynccontextmanager
    async def completion(options: dict[str, Any]) -> AsyncIterator[Any]:
        assert options["max_completion_tokens"] == expected and "max_tokens" not in options
        yield SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"), finish_reason="stop")], usage=SimpleNamespace(total_tokens=3))

    monkeypatch.setattr(model, "_completion", completion)
    assert await model.async_chat("", [{"role": "user", "content": "Reply OK"}], configuration, **request_options) == ("OK", 3)


async def test_non_openai_kimi_reasoning_disabled_is_not_reapplied_as_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    model = chat.LiteLLMBase("owned-fake", "kimi-k2.6", provider=Provider.Moonshot, max_retries=0)

    @asynccontextmanager
    async def completion(options: dict[str, Any]) -> AsyncIterator[Any]:
        assert options["thinking"] == {"type": "disabled"}
        assert options["temperature"] == 0.6
        yield SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"), finish_reason="stop")], usage=SimpleNamespace(total_tokens=3))

    monkeypatch.setattr(model, "_completion", completion)
    assert await model.async_chat("", [{"role": "user", "content": "Reply OK"}], {"reasoning": False}) == ("OK", 3)
