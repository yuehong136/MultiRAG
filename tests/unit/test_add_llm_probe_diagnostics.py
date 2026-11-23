"""The current provider probe classifies failures without leaking credentials."""

import asyncio
from typing import Any

import httpx
import pytest

import core.llm as provider_models
from api.db.model_contract import ModelRegistration
from api.db.services.provider_verification import verify_model

FACTORY = "OpenAI-API-Compatible"
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DECLARATION = ModelRegistration(model_name="glm-4.6", model_types=["chat"], max_tokens=0)


@pytest.fixture
def probe_chat(monkeypatch: pytest.MonkeyPatch) -> type:
    class Chat:
        answer = "OK"
        error: Exception | None = None
        delay = 0.0
        seen: dict[str, Any] = {}
        closed = False

        def __init__(self, **kwargs: Any) -> None:
            Chat.seen = kwargs

        async def _exceptions_async(self, error: BaseException, attempt: int) -> str:
            raise AssertionError("The probe must replace the normal diagnostics handler")

        async def async_chat(self, *_args: Any) -> tuple[str, int]:
            if Chat.delay:
                await asyncio.sleep(Chat.delay)
            if Chat.error:
                return await self._exceptions_async(Chat.error, 0), 0
            return Chat.answer, 1

        async def aclose(self) -> None:
            Chat.closed = True

    monkeypatch.setattr(provider_models, "ChatModel", {FACTORY: Chat})
    return Chat


@pytest.mark.parametrize("status", [401, 429, 529])
async def test_provider_error_preserves_status_without_echoing_credentials(probe_chat: type, status: int) -> None:
    request = httpx.Request("POST", BASE_URL + "?key=private-fixture")
    probe_chat.error = httpx.HTTPStatusError("private-fixture", request=request, response=httpx.Response(status))
    with pytest.raises(ValueError, match=f"HTTP {status}") as failure:
        await verify_model(FACTORY, "private-fixture", BASE_URL, DECLARATION)
    assert "private-fixture" not in str(failure.value)
    assert probe_chat.closed


async def test_probe_timeout_is_bounded_and_cleans_up(probe_chat: type, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "0.05")
    probe_chat.delay = 1
    with pytest.raises(ValueError, match="timed out"):
        await verify_model(FACTORY, "", BASE_URL, DECLARATION)
    assert probe_chat.closed


async def test_probe_rejects_empty_provider_content(probe_chat: type) -> None:
    probe_chat.answer = ""
    with pytest.raises(ValueError, match="empty or invalid"):
        await verify_model(FACTORY, "", BASE_URL, DECLARATION)
    assert probe_chat.closed


async def test_probe_uses_exact_model_and_endpoint_without_sdk_retry(probe_chat: type) -> None:
    assert await verify_model(FACTORY, "", BASE_URL, DECLARATION) == {"verified": True, "model_name": "glm-4.6", "model_type": "chat"}
    assert probe_chat.seen == {"key": "", "model_name": "glm-4.6", "base_url": BASE_URL, "provider": FACTORY, "max_retries": 0}
    assert probe_chat.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream", "expected_code"),
    [
        # 不可重试类：改前就能立刻拿到错误
        ("Error code: 401 - {'error': {'message': 'invalid api key'}}", "AUTH_ERROR"),
        # 可重试类：这两类才是关键——只判「分类可重试」不判「还有剩余次数」时，
        # max_retries=0 也会先睡 20~300s，然后一个 chunk 都不 yield，错误被整条吞掉
        ("Error code: 429 - {'error': {'message': 'Rate limit reached'}}", "RATE_LIMIT_EXCEEDED"),
        ("litellm.InternalServerError: AnthropicException - Overloaded (529)", "SERVER_ERROR"),
    ],
)
async def test_disabled_retry_keeps_real_error_classification_and_does_not_back_off(upstream: str, expected_code: str) -> None:
    """探测用 max_retries=0：既不能退避（20~300s 会睡穿超时），也不能把真实分类盖成 MAX_RETRIES_EXCEEDED。"""
    from core.llm.chat import Base

    class _Failing:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError(upstream)

    mdl = Base(key="k", model_name="glm-4.6", base_url="https://open.bigmodel.cn/api/paas/v4", max_retries=0)
    mdl.async_client = _Failing()

    started = asyncio.get_running_loop().time()
    chunks = [chunk async for chunk in mdl.async_chat_streamly("", [{"role": "user", "content": "Hi"}], {})]
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert chunks, "max_retries=0 时必须 yield 出错误，不能静默结束"
    assert expected_code in chunks[0]
    assert "MAX_RETRIES_EXCEEDED" not in chunks[0]
    assert upstream in chunks[0]


@pytest.mark.asyncio
async def test_retry_still_happens_when_retries_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_retries>0 的生产路径行为不能被上一条的守卫改掉：仍要重试到耗尽再报 MAX_RETRIES_EXCEEDED。"""
    from core.llm import chat as chat_module

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(chat_module.asyncio, "sleep", _fake_sleep)

    class _RateLimited:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("Error code: 429 - Rate limit reached")

    mdl = chat_module.Base(key="k", model_name="glm-4.6", base_url="https://x/v1", max_retries=3)
    mdl.async_client = _RateLimited()

    chunks = [chunk async for chunk in mdl.async_chat_streamly("", [{"role": "user", "content": "Hi"}], {})]

    assert len(slept) == 3
    assert "MAX_RETRIES_EXCEEDED" in chunks[0]
    assert "Rate limit reached" in chunks[0]
