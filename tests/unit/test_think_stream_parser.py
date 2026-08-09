"""Regression coverage for provider-specific reasoning stream shapes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import MethodType, SimpleNamespace
from typing import Any

import pytest

from agent.component.llm import LLM
from api.db.services.dialog_service import _stream_with_think_delta


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
async def test_shared_think_stream_parser_matches_provider_shapes(case: _ThinkCase) -> None:
    reasoning, answer, markers = await _collect(_chunks(case.chunks))

    assert reasoning == case.reasoning
    assert answer == case.answer
    assert markers == case.markers


async def test_think_stream_parser_rejects_negative_batch_size() -> None:
    with pytest.raises(ValueError, match="min_tokens"):
        await anext(_stream_with_think_delta(_chunks(("answer",)), min_tokens=-1))


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
