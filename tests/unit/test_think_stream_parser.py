"""Regression coverage for provider-specific reasoning stream shapes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent.component.llm import LLM
from api.db.services.dialog_service import _stream_with_think_delta
from api.db.services.llm_service import LLMBundle
from core.llm import ChatModel, chat


@dataclass(frozen=True, slots=True)
class _ThinkCase:
    chunks: tuple[str, ...]
    reasoning: str
    answer: str
    markers: tuple[str, ...]


_CASES = {
    # LiteLLM/DeepSeek appends </think> to every reasoning delta. The parser
    # must delay the close until the first ordinary answer delta arrives.
    "deepseek_repeated_close": _ThinkCase(
        chunks=(
            "<think>We</think>",
            " need</think>",
            " to</think>",
            " answer</think>",
            "我也爱你呀～",
        ),
        reasoning="We need to answer",
        answer="我也爱你呀～",
        markers=("<think>", "</think>"),
    ),
    # MiniMax can keep thinking open across deltas and place the real close at
    # the start of the first answer chunk.
    "minimax_open_reasoning": _ThinkCase(
        chunks=(
            "<think>internal reasoning",
            " continues",
            "</think>\n\nVisible answer",
        ),
        reasoning="internal reasoning continues",
        answer="\n\nVisible answer",
        markers=("<think>", "</think>"),
    ),
    "answer_and_close_in_one_delta": _ThinkCase(
        chunks=("<think>先思考完毕</think>答案在这里",),
        reasoning="先思考完毕",
        answer="答案在这里",
        markers=("<think>", "</think>"),
    ),
    "repeated_close_with_answer": _ThinkCase(
        chunks=("<think>r</think>", "</think>V"),
        reasoning="r",
        answer="V",
        markers=("<think>", "</think>"),
    ),
    "repeated_close_preserves_answer_whitespace": _ThinkCase(
        chunks=("<think>r</think> ", "</think>V"),
        reasoning="r",
        answer=" V",
        markers=("<think>", "</think>"),
    ),
    "answer_before_and_after_reasoning": _ThinkCase(
        chunks=("前言", " ", "<think>内部推理</think>", "最终回答", "。"),
        reasoning="内部推理",
        answer="前言 最终回答。",
        markers=("<think>", "</think>"),
    ),
    "markers_split_across_deltas": _ThinkCase(
        chunks=("<thi", "nk>内部推理</thi", "nk>最终回答"),
        reasoning="内部推理",
        answer="最终回答",
        markers=("<think>", "</think>"),
    ),
    "two_reasoning_blocks": _ThinkCase(
        chunks=(
            "<think>第一段推理</think>答案A",
            " <think>第二段推理</think>答案B",
        ),
        reasoning="第一段推理第二段推理",
        answer="答案A 答案B",
        markers=("<think>", "</think>", "<think>", "</think>"),
    ),
    "unclosed_reasoning_is_not_an_answer": _ThinkCase(
        chunks=("<think>只输出思考", "，流在这里结束"),
        reasoning="只输出思考，流在这里结束",
        answer="",
        markers=("<think>",),
    ),
    "close_only_at_eof": _ThinkCase(
        chunks=("<think>reason", "</think>"),
        reasoning="reason",
        answer="",
        markers=("<think>", "</think>"),
    ),
    "whitespace_tail_at_eof": _ThinkCase(
        chunks=("<think>reason</think> \n",),
        reasoning="reason",
        answer=" \n",
        markers=("<think>", "</think>"),
    ),
    "cumulative_close_with_answer": _ThinkCase(
        chunks=("<think>reason", "<think>reason</think>Answer", "<think>reason</think>Answer tail"),
        reasoning="reason",
        answer="Answer tail",
        markers=("<think>", "</think>"),
    ),
}


async def _chunks(values: tuple[str, ...]) -> AsyncIterator[str]:
    for value in values:
        yield value


async def _collect(
    chunks: AsyncIterator[str],
    *,
    min_tokens: int = 16,
) -> tuple[str, str, tuple[str, ...]]:
    section = "answer"
    reasoning: list[str] = []
    answer: list[str] = []
    markers: list[str] = []
    async for kind, value, _state in _stream_with_think_delta(
        chunks,
        min_tokens=min_tokens,
    ):
        if kind == "marker":
            markers.append(value)
            section = "reasoning" if value == "<think>" else "answer"
        elif section == "reasoning":
            reasoning.append(value)
        else:
            answer.append(value)
    return "".join(reasoning), "".join(answer), tuple(markers)


@pytest.mark.parametrize(
    "case",
    _CASES.values(),
    ids=_CASES.keys(),
)
@pytest.mark.parametrize("min_tokens", [0, 16])
async def test_shared_think_stream_parser_matches_provider_shapes(case: _ThinkCase, min_tokens: int) -> None:
    reasoning, answer, markers = await _collect(_chunks(case.chunks), min_tokens=min_tokens)

    assert reasoning == case.reasoning
    assert answer == case.answer
    assert markers == case.markers


async def test_think_stream_parser_rejects_negative_batch_size() -> None:
    with pytest.raises(ValueError, match="min_tokens"):
        await anext(_stream_with_think_delta(_chunks(("answer",)), min_tokens=-1))


async def test_repeated_close_stream_crosses_token_batch_threshold() -> None:
    from common.token_utils import num_tokens_from_string

    pieces = tuple(f" step {index}" for index in range(40))
    reasoning = "Start" + "".join(pieces)
    assert num_tokens_from_string(reasoning) > 16
    chunks = ("<think>Start</think>", *(piece + "</think>" for piece in pieces), "Visible answer")
    assert await _collect(_chunks(chunks), min_tokens=16) == (reasoning, "Visible answer", ("<think>", "</think>"))


class _DeltaOnlyChatModel:
    max_length = 4096

    def __init__(self) -> None:
        self.reasoning_modes: list[bool] = []

    async def async_chat_streamly_delta(
        self,
        system: str,
        history: list[dict[str, str]],
        gen_conf: dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        del system, history, gen_conf
        self.reasoning_modes.append(kwargs.get("with_reasoning", True) is not False)
        for value in _CASES["deepseek_repeated_close"].chunks:
            yield value


class _ReasoningOnlyThenVisibleChatModel:
    max_length = 4096

    def __init__(self, *, close_reasoning: bool) -> None:
        self.close_reasoning = close_reasoning
        self.reasoning_modes: list[bool] = []

    async def async_chat_streamly_delta(
        self,
        system: str,
        history: list[dict[str, str]],
        gen_conf: dict[str, Any],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        del system, history, gen_conf
        with_reasoning = kwargs.get("with_reasoning", True) is not False
        self.reasoning_modes.append(with_reasoning)
        if with_reasoning:
            yield "<think>provider returned only private reasoning"
            if self.close_reasoning:
                yield "</think>"
            return
        yield "重试后可见回答"


def _make_component(model: Any) -> tuple[LLM, dict[str, Any]]:
    component = object.__new__(LLM)
    component._id = "llm-test"
    component._canvas = SimpleNamespace(globals={})
    component.chat_mdl = model
    component.imgs = []
    component._param = SimpleNamespace(gen_conf=lambda: {})
    outputs: dict[str, Any] = {}

    def check_if_canceled(self: LLM, operation: str) -> bool:
        del self, operation
        return False

    def get_exception_default_value(self: LLM) -> None:
        del self
        return None

    def set_output(self: LLM, key: str, value: Any) -> None:
        del self
        outputs[key] = value

    component.check_if_canceled = MethodType(check_if_canceled, component)
    component.get_exception_default_value = MethodType(
        get_exception_default_value,
        component,
    )
    component.set_output = MethodType(set_output, component)
    return component, outputs


async def _run_component(component: LLM) -> list[str]:
    return [
        value
        async for value in component._stream_output_async(
            "system",
            [{"role": "user", "content": "hello"}],
        )
    ]


async def test_agent_canvas_uses_the_shared_delta_parser() -> None:
    """Pin the RAGFlow June fix at the actual Canvas message output seam."""

    model = _DeltaOnlyChatModel()
    component, outputs = _make_component(model)

    emitted = await _run_component(component)
    reasoning, answer, markers = await _collect(_chunks(tuple(emitted)), min_tokens=0)

    assert model.reasoning_modes == [True]
    assert reasoning == "We need to answer"
    assert answer == "我也爱你呀～"
    assert markers == ("<think>", "</think>")
    assert outputs["content"] == "<think>We need to answer</think>我也爱你呀～"


@pytest.mark.parametrize("close_reasoning", [True, False])
async def test_agent_canvas_retries_reasoning_only_stream_without_exposing_cot(
    close_reasoning: bool,
) -> None:
    model = _ReasoningOnlyThenVisibleChatModel(close_reasoning=close_reasoning)
    component, outputs = _make_component(model)

    emitted = await _run_component(component)
    reasoning, answer, markers = await _collect(_chunks(tuple(emitted)), min_tokens=0)

    assert model.reasoning_modes == [True, False]
    assert reasoning == "provider returned only private reasoning"
    assert answer == "重试后可见回答"
    assert markers == ("<think>", "</think>")
    assert outputs["content"] == ("<think>provider returned only private reasoning</think>重试后可见回答")


@pytest.mark.parametrize("case_name", ["minimax_open_reasoning", "markers_split_across_deltas", "cumulative_close_with_answer"])
@pytest.mark.parametrize("images", [[], ["data:image/png;base64,fixture"]])
async def test_agent_minimax_shapes_preserve_final_content_and_request_options(case_name: str, images: list[str]) -> None:
    requests: list[dict[str, Any]] = []
    case = _CASES[case_name]

    class Model:
        max_length = 4096

        async def async_chat_streamly_delta(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            requests.append(kwargs)
            for value in case.chunks:
                yield value

    component, outputs = _make_component(Model())
    component.imgs = images
    component._canvas.globals["sys.chat_template_kwargs"] = {"thinking": {"type": "enabled"}}
    emitted = await _run_component(component)
    assert await _collect(_chunks(tuple(emitted)), min_tokens=0) == (case.reasoning, case.answer, case.markers)
    assert outputs["content"] == "".join(emitted)
    assert requests == [{**({"images": images} if images else {}), "chat_template_kwargs": {"thinking": {"type": "enabled"}}}]


@pytest.mark.parametrize("surface", ["generate", "output"])
async def test_agent_first_reasoning_is_immediate_and_close_reaches_producer(surface: str) -> None:
    resume = asyncio.Event()
    continued = asyncio.Event()
    closed = asyncio.Event()

    class Model:
        max_length = 4096

        async def async_chat_streamly_delta(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
            try:
                yield "<think>reason"
                continued.set()
                await resume.wait()
                yield "</think>answer"
            finally:
                closed.set()

    component, outputs = _make_component(Model())
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
    stream = component._generate_streamly(messages) if surface == "generate" else component._stream_output_async("system", messages[1:])
    try:
        assert await asyncio.wait_for(anext(stream), timeout=2) == "<think>"
        assert await asyncio.wait_for(anext(stream), timeout=2) == "reason"
        assert not continued.is_set()
    finally:
        await stream.aclose()
        resume.set()
    assert closed.is_set(), "Closing an Agent stream must close its owned provider iterator before returning"
    assert "content" not in outputs


@pytest.mark.parametrize("surface", ["generate", "output"])
@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("early_close", [False, True])
async def test_minimax_sdk_stream_through_agent_and_bundle_closes_owned_client(monkeypatch: pytest.MonkeyPatch, surface: str, tools: bool, early_close: bool) -> None:
    clients: list[Any] = []
    real_client = chat.AsyncOpenAI

    def create(**kwargs: Any) -> Any:
        client = real_client(**kwargs)
        clients.append(client)
        return client

    async def send(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        body = json.loads(request.content)
        assert body["stream"] is True and body["model"] == "MiniMax-M2"
        chunks = [
            {"id": "fixture", "created": 1, "model": "MiniMax-M2", "choices": [{"index": 0, "delta": {"content": "<think>reason"}, "finish_reason": None}]},
            {"id": "fixture", "created": 1, "model": "MiniMax-M2", "choices": [{"index": 0, "delta": {"content": "</think>answer"}, "finish_reason": "stop"}], "usage": {"total_tokens": 5}},
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"}, request=request)

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(chat, "AsyncOpenAI", create)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    model = ChatModel["MiniMax"]("fixture-key", "MiniMax-M2", "http://fixture.invalid/v1", provider="MiniMax", max_retries=0)
    model.is_tools = tools
    bundle = object.__new__(LLMBundle)
    bundle.mdl, bundle.langfuse, bundle.is_tools, bundle.verbose_tool_use = model, None, tools, False
    bundle.max_length = 4096
    bundle._record_usage_async = AsyncMock(return_value=True)
    component, outputs = _make_component(bundle)
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
    stream = component._generate_streamly(messages) if surface == "generate" else component._stream_output_async("system", messages[1:])
    try:
        if early_close:
            assert await anext(stream) == "<think>"
            assert await anext(stream) == "reason"
            await stream.aclose()
            assert "content" not in outputs
        else:
            assert "".join([value async for value in stream]) == "<think>reason</think>answer"
            if surface == "output":
                assert outputs["content"] == "<think>reason</think>answer"
        assert len(clients) == 1 and all(client.is_closed() for client in clients)
    finally:
        await stream.aclose()
        for client in clients:
            await client.close()
        await transport.aclose()
