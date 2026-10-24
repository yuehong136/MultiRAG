"""Empty provider responses fail at the actual producer without unsafe indexing."""

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

from api.db.services.llm_service import LLMBundle
from core.llm import ChatModel, chat
from core.llm.cv import GptV4, Zhipu4V
from core.llm.tool_decorator import tool


def _empty() -> SimpleNamespace:
    return SimpleNamespace(choices=[], usage=SimpleNamespace(total_tokens=7))


@pytest.mark.parametrize("producer", ["baichuan", "mistral", "gpt_describe", "gpt_prompt", "zhipu_prompt"])
@pytest.mark.parametrize("empty", [True, False])
def test_direct_chat_and_vision_producers_guard_empty_choices(monkeypatch: pytest.MonkeyPatch, producer: str, empty: bool) -> None:
    response = _empty() if empty else SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="caption"), finish_reason="stop")], usage=SimpleNamespace(total_tokens=7))
    create = Mock(return_value=response)
    if producer in {"baichuan", "mistral"}:
        cls = chat.BaiChuanChat if producer == "baichuan" else chat.MistralChat
        model = cls.__new__(cls)
        model.model_name = "controlled-model"
        model.client = SimpleNamespace(chat=create if producer == "mistral" else SimpleNamespace(completions=SimpleNamespace(create=create)))
        invoke = lambda: model._chat([{"role": "user", "content": "hello"}], {})
    else:
        cls = Zhipu4V if producer == "zhipu_prompt" else GptV4
        model = cls.__new__(cls)
        model.model_name = "controlled-model"
        model.extra_body = None
        model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        monkeypatch.setattr(model, "image2base64", lambda image: "controlled-image")
        monkeypatch.setattr(model, "prompt", lambda image: [])
        monkeypatch.setattr(model, "vision_llm_prompt", lambda image, prompt: [])
        invoke = lambda: model.describe(b"image") if producer == "gpt_describe" else model.describe_with_prompt(b"image", "describe")
    if empty:
        with pytest.raises(ValueError, match=r"^LLM returned empty response$"):
            invoke()
    else:
        answer, tokens = invoke()
        assert answer == "caption" and tokens == (1 if producer == "zhipu_prompt" else 7)
    create.assert_called_once()


@pytest.mark.parametrize("shape", ["empty_choices", "null_message", "null_content", "empty_content"])
async def test_litellm_plain_chat_returns_empty_zero_without_retry(monkeypatch: pytest.MonkeyPatch, shape: str) -> None:
    response = _empty()
    if shape != "empty_choices":
        message = None if shape == "null_message" else SimpleNamespace(content=None if shape == "null_content" else "")
        response.choices = [SimpleNamespace(message=message)]
    complete = AsyncMock(return_value=response)
    monkeypatch.setattr(chat.litellm, "acompletion", complete)
    model = chat.LiteLLMBase("controlled-key", "controlled-model", provider="OpenAI", max_retries=2)
    assert await model.async_chat("", [{"role": "user", "content": "hello"}], {}) == ("", 0)
    complete.assert_awaited_once()


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("shape", ["empty_choices", "null_message"])
async def test_tool_loop_structure_error_retains_usage(monkeypatch: pytest.MonkeyPatch, backend: str, shape: str) -> None:
    response = _empty()
    if shape == "null_message":
        response.choices = [SimpleNamespace(message=None)]
    complete = AsyncMock(return_value=response)
    if backend == "base":
        model = chat.Base.__new__(chat.Base)
        model.model_name = "controlled-model"
        model.max_retries, model.max_rounds = 0, 1
        model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    else:
        model = chat.LiteLLMBase("controlled-key", "controlled-model", provider="OpenAI", max_retries=0, max_rounds=1)
        monkeypatch.setattr(chat.litellm, "acompletion", complete)
    model.tools = []
    answer, tokens = await model.async_chat_with_tools("", [{"role": "user", "content": "hello"}], {})
    assert answer.startswith("**ERROR**") and "response structure error" in answer
    assert "IndexError" not in answer and "AttributeError" not in answer and tokens == 7
    complete.assert_awaited_once()


async def _final_rounds(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    content: str | None,
    *,
    finish_reason: str = "stop",
    tool_round: bool = True,
    verbose: bool = False,
    reasoning_field: str = "reasoning_content",
    reasoning: str = "",
    max_retries: int = 0,
) -> tuple[str, int, list[dict[str, Any]]]:
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if tool_round and len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call-search", "type": "function", "function": {"name": "search", "arguments": "{}"}}]}
            reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": content, reasoning_field: reasoning}
            reason = finish_reason
        assert len(requests) <= (2 if tool_round else 1)
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "controlled-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": reason}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    async def send(client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        return respond(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    model = (
        chat.Base("controlled-key", "test-model", "http://controlled.invalid/v1", max_retries=max_retries)
        if backend == "base"
        else ChatModel["OpenAI"]("controlled-key", "test-model", "http://controlled.invalid/v1", provider="OpenAI", max_retries=max_retries)
    )

    @tool
    async def search() -> str:
        return "Synthetic document."

    model.bind_tools(tools=[search])
    if not verbose:
        model._verbose_tool_use = lambda *_args: ""
    try:
        answer, tokens = await model.async_chat_with_tools("", [{"role": "user", "content": "Find it."}], {})
        return answer, tokens, requests
    finally:
        if hasattr(model, "client"):
            model.client.close()
        if hasattr(model, "async_client"):
            await model.async_client.close()


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("content", [None, "", "   "])
@pytest.mark.parametrize("finish_reason", ["stop", "length"])
@pytest.mark.parametrize("tool_round", [False, True])
async def test_empty_final_tool_answer_is_explicit_model_error(monkeypatch: pytest.MonkeyPatch, backend: str, content: str | None, finish_reason: str, tool_round: bool) -> None:
    answer, tokens, requests = await _final_rounds(monkeypatch, backend, content, finish_reason=finish_reason, tool_round=tool_round, max_retries=2)
    assert answer.startswith("**ERROR**: MODEL_ERROR") and "empty final answer" in answer and finish_reason in answer
    assert tokens == (6 if tool_round else 3) and len(requests) == (2 if tool_round else 1)
    if tool_round:
        assert requests[1]["messages"][-1] == {"role": "tool", "tool_call_id": "call-search", "content": "Synthetic document."}


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("reasoning_field", ["reasoning_content", "reasoning"])
async def test_reasoning_only_first_final_is_still_empty_answer(monkeypatch: pytest.MonkeyPatch, backend: str, reasoning_field: str) -> None:
    answer, tokens, requests = await _final_rounds(monkeypatch, backend, None, tool_round=False, reasoning_field=reasoning_field, reasoning="Reasoning.")
    assert answer.startswith("**ERROR**: MODEL_ERROR") and tokens == 3 and len(requests) == 1


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_final_preserves_accumulated_tool_output_and_reasoning(monkeypatch: pytest.MonkeyPatch, backend: str, content: str | None) -> None:
    answer, tokens, requests = await _final_rounds(monkeypatch, backend, content, verbose=True, reasoning="Reasoning.")
    assert answer == '<tool_call>{\n  "name": "search",\n  "args": {},\n  "result": "Synthetic document."\n}</tool_call><think>Reasoning.</think>'
    assert tokens == 6 and len(requests) == 2


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("reasoning_field", ["reasoning_content", "reasoning"])
async def test_nonempty_final_preserves_reasoning_and_length_notice(monkeypatch: pytest.MonkeyPatch, backend: str, reasoning_field: str) -> None:
    answer, tokens, requests = await _final_rounds(monkeypatch, backend, "Found a document.", finish_reason="length", reasoning_field=reasoning_field, reasoning="Reasoning.")
    assert answer == "<think>Reasoning.</think>Found a document." + chat.LENGTH_NOTIFICATION_EN
    assert tokens == 6 and len(requests) == 2


@pytest.mark.parametrize("backend", ["base", "litellm"])
async def test_cancelled_decorated_tool_loop_stops_before_next_request(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    started, stopped = asyncio.Event(), asyncio.Event()
    complete = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="call-wait", function=SimpleNamespace(name="waiting", arguments="{}"))]))], usage=None
        )
    )
    if backend == "base":
        model = chat.Base.__new__(chat.Base)
        model.model_name = "controlled-model"
        model.max_retries, model.max_rounds = 0, 1
        model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    else:
        model = chat.LiteLLMBase("controlled-key", "controlled-model", provider="OpenAI", max_retries=0, max_rounds=1)
        monkeypatch.setattr(chat.litellm, "acompletion", complete)

    @tool
    async def waiting() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    model.bind_tools(tools=[waiting])
    task = asyncio.create_task(model.async_chat_with_tools("", [{"role": "user", "content": "wait"}], {}))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    complete.assert_awaited_once()


@pytest.fixture(autouse=True)
async def _close_litellm_logging_for_test_loop() -> AsyncIterator[None]:
    """Drain SDK callbacks before pytest closes this test's event loop."""
    yield
    if GLOBAL_LOGGING_WORKER._bound_loop is asyncio.get_running_loop():
        if GLOBAL_LOGGING_WORKER._queue is not None:
            await asyncio.wait_for(GLOBAL_LOGGING_WORKER._queue.join(), timeout=2)
        await asyncio.wait_for(GLOBAL_LOGGING_WORKER.stop(), timeout=2)


@pytest.mark.parametrize("content", ["", "   ", '<tool_call>{"result":"document"}</tool_call>', '<tool_call>{"result":"document"}</tool_call><think>Reasoning.</think>'])
async def test_bundle_reports_empty_visible_answer_after_hiding_tool_metadata(content: str) -> None:
    model = SimpleNamespace(is_tools=True, async_chat_with_tools=AsyncMock(return_value=(content, 6)))
    bundle = LLMBundle.__new__(LLMBundle)
    bundle.mdl, bundle.is_tools, bundle.langfuse, bundle.verbose_tool_use = model, True, None, False
    bundle._record_usage_async = AsyncMock(return_value=True)
    answer = await bundle.async_chat("", [{"role": "user", "content": "Find it."}], {})
    assert answer.startswith("**ERROR**: MODEL_ERROR") and "without visible content" in answer
    bundle._record_usage_async.assert_awaited_once_with(6)
    model.async_chat_with_tools.assert_awaited_once()


async def test_bundle_preserves_visible_verbose_tool_result() -> None:
    content = '<tool_call>{"result":"document"}</tool_call>'
    model = SimpleNamespace(is_tools=True, async_chat_with_tools=AsyncMock(return_value=(content, 6)))
    bundle = LLMBundle.__new__(LLMBundle)
    bundle.mdl, bundle.is_tools, bundle.langfuse, bundle.verbose_tool_use = model, True, None, True
    bundle._record_usage_async = AsyncMock(return_value=True)
    assert await bundle.async_chat("", [{"role": "user", "content": "Find it."}], {}) == content
    bundle._record_usage_async.assert_awaited_once_with(6)


async def test_plain_chat_keeps_empty_response_contract() -> None:
    model = SimpleNamespace(is_tools=False, async_chat=AsyncMock(return_value=("", 0)))
    bundle = LLMBundle.__new__(LLMBundle)
    bundle.mdl, bundle.is_tools, bundle.langfuse, bundle.verbose_tool_use = model, False, None, False
    bundle._record_usage_async = AsyncMock(return_value=True)
    assert await bundle.async_chat("", [{"role": "user", "content": "hello"}], {}) == ""
    bundle._record_usage_async.assert_not_awaited()
