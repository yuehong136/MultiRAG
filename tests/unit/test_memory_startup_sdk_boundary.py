"""Real OpenAI adapter/SDK requests stay budgeted through bound observers."""

from __future__ import annotations

import inspect
import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from core.llm import SupportedLiteLLMProvider
from core.llm.chat import Base, LiteLLMBase
from tests.support.memory_startup_longitudinal import SYSTEM, canonical, request_budget_guard
from tests.support.memory_workload_models import ProviderObserver
from tests.support.resource_journal import ResourceJournal


@pytest.mark.parametrize("oversize", [False, True])
@pytest.mark.parametrize("cached_raw_response", [False, True])
async def test_actual_sdk_budget_precedes_payment_and_survives_bound_usage_observer(oversize: bool, cached_raw_response: bool) -> None:
    wire: list[dict[str, Any]] = []

    def response(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "owned-completion",
                "created": 1,
                "object": "chat.completion",
                "model": "gpt-6.1-terra",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "仅演练内网，禁止公网。"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35, "prompt_tokens_details": {"cached_tokens": 0}, "completion_tokens_details": {"reasoning_tokens": 2}},
            },
        )

    context = {"items": [{"content": "禁止公网。" + (" keep all conditions " * 30000 if oversize else "")}]}
    parts = [("retrieval", canonical(context)), ("question", "允许公网吗？")]
    observer = ProviderObserver(ResourceJournal("startup-sdk-unit"))
    row: dict[str, Any] = {}
    adapter = Base("owned-test-key", "gpt-6.1-terra", "https://unit.invalid/v1", max_retries=0)
    await adapter.async_client.close()
    adapter.async_client = AsyncOpenAI(api_key="owned-test-key", base_url="https://unit.invalid/v1", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
    # LiteLLM can cache this bound raw callable before a new bundle exists.
    original = adapter.async_client.chat.completions.with_raw_response.create if cached_raw_response else adapter.async_client.chat.completions.create

    async def observed(*args: Any, **options: Any) -> Any:
        value = await original(*args, **options)
        if cached_raw_response:
            parsed = value.parse()
            value = await parsed if inspect.isawaitable(parsed) else parsed
        observer._chat_usage(row, value)
        return value

    adapter.async_client.chat.completions.create = observed
    with request_budget_guard(None, parts) as budgets:
        try:
            text, _ = await adapter.async_chat(SYSTEM, [{"role": "user", "content": content} for _, content in parts], {"max_completion_tokens": 512})
            if oversize:
                assert not wire and budgets[0]["allowed"] is False
            else:
                assert text == "仅演练内网，禁止公网。" and len(wire) == len(budgets) == 1
                assert budgets[0]["allowed"] is True and budgets[0]["request_bytes"] > 0
                assert row["provider_total_tokens"] == 35 and row["usage"]["output_tokens"] == 5
                assert row["reasoning_tokens"] == 2
        finally:
            await adapter.async_client.close()
            adapter.client.close()


@pytest.mark.parametrize("oversize", [False, True])
async def test_prewarmed_litellm_sdk_client_is_checked_at_final_send(monkeypatch: pytest.MonkeyPatch, oversize: bool) -> None:
    wire: list[dict[str, Any]] = []
    clients: list[int] = []

    async def transport(client: httpx.AsyncClient, wire_request: httpx.Request) -> httpx.Response:
        clients.append(id(client))
        wire.append(json.loads(wire_request.content))
        return httpx.Response(
            200,
            request=wire_request,
            json={
                "id": "owned-cached-sdk",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-6.1-terra",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "_send_single_request", transport)
    model = LiteLLMBase("owned-warm-key-" + str(oversize), "gpt-6.1-terra", "https://prewarm.invalid/v1", provider=SupportedLiteLLMProvider.OpenAI, max_retries=0)
    warm = await model.async_chat(SYSTEM, [{"role": "user", "content": "warm owned SDK"}], {"max_completion_tokens": 512})
    assert warm[0] == "OK" and len(wire) == 1
    parts = [("retrieval", canonical({"items": ["retain conditions " * 30000 if oversize else "仅演练内网"]})), ("question", "允许公网吗？")]
    with request_budget_guard(None, parts) as budgets:
        result = await model.async_chat(SYSTEM, [{"role": "user", "content": content} for _, content in parts], {"max_completion_tokens": 512})
    assert len(budgets) == 1
    if oversize:
        assert not budgets[0]["allowed"] and len(wire) == 1
    else:
        assert result[0] == "OK" and budgets[0]["allowed"] and len(wire) == 2
        assert clients[0] == clients[1], "the same cached HTTP/SDK client must be exercised"
