from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.llm import chat


def _model() -> chat.LiteLLMBase:
    model = chat.LiteLLMBase("fixture-key", "glm-test", provider="ZHIPU-AI", max_retries=0)
    model.max_rounds = 1
    return model


def _chunk(reason: str, content: str | None, finish: str = "") -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning_content=reason, content=content, tool_calls=None), finish_reason=finish)])


def _stream(model: chat.LiteLLMBase, tools: bool) -> AsyncIterator[str | int]:
    history = [{"role": "user", "content": "hello"}]
    if tools:
        return model.async_chat_streamly_with_tools("", history, {})
    return model.async_chat_streamly("", history, {})


@pytest.mark.parametrize("tools", [False, True])
async def test_stream_preserves_same_chunk_reasoning_then_answer(monkeypatch: pytest.MonkeyPatch, tools: bool) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield _chunk("first reason", "first answer")
        yield _chunk("", " tail", "stop")

    completion = AsyncMock(side_effect=lambda **kwargs: chunks())
    monkeypatch.setattr(chat.litellm, "acompletion", completion)
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 7)
    result = [part async for part in _stream(_model(), tools)]
    assert result == ["<think>first reason</think>", "first answer", " tail", 7 if tools else 14]
    completion.assert_awaited_once()


@pytest.mark.parametrize("tools", [False, True])
async def test_stream_mixed_final_chunk_accumulates_answer_without_another_tool_round(monkeypatch: pytest.MonkeyPatch, tools: bool) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield _chunk("reason", "complete answer", "stop")

    completion = AsyncMock(side_effect=lambda **kwargs: chunks())
    monkeypatch.setattr(chat.litellm, "acompletion", completion)
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 11)
    result = [part async for part in _stream(_model(), tools)]
    assert result == ["<think>reason</think>", "complete answer", 11]
    completion.assert_awaited_once()


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("reason_first", [False, True])
async def test_separate_reasoning_and_content_keep_existing_behavior(monkeypatch: pytest.MonkeyPatch, tools: bool, reason_first: bool) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        if reason_first:
            yield _chunk("reason", None)
        yield _chunk("", "answer", "stop")

    completion = AsyncMock(side_effect=lambda **kwargs: chunks())
    monkeypatch.setattr(chat.litellm, "acompletion", completion)
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 7)
    result = [part async for part in _stream(_model(), tools)]
    expected: list[str | int] = ["<think>reason</think>"] if reason_first else []
    expected.extend(["answer", 7 if tools or not reason_first else 14])
    assert result == expected
    completion.assert_awaited_once()


async def test_reasoning_can_be_hidden_without_dropping_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield _chunk("secret reasoning", "answer", "stop")

    monkeypatch.setattr(chat.litellm, "acompletion", AsyncMock(side_effect=lambda **kwargs: chunks()))
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 7)
    result = [part async for part in _model().async_chat_streamly("", [{"role": "user", "content": "hello"}], {}, with_reasoning=False)]
    assert result == ["answer", 7]


@pytest.mark.parametrize("tools", [False, True])
async def test_missing_usage_counts_content_once_per_provider_chunk(monkeypatch: pytest.MonkeyPatch, tools: bool) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield _chunk("reason", "answer", "stop")

    token_inputs: list[str] = []

    def count_tokens(content: str) -> int:
        token_inputs.append(content)
        return 5

    monkeypatch.setattr(chat.litellm, "acompletion", AsyncMock(side_effect=lambda **kwargs: chunks()))
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 0)
    monkeypatch.setattr(chat, "num_tokens_from_string", count_tokens)
    result = [part async for part in _stream(_model(), tools)]
    assert result == ["<think>reason</think>", "answer", 5]
    assert token_inputs == ["answer"]


@pytest.mark.parametrize("tools", [False, True])
async def test_empty_choices_do_not_drop_later_mixed_chunk(monkeypatch: pytest.MonkeyPatch, tools: bool) -> None:
    async def chunks() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(choices=[])
        yield _chunk("reason", "answer", "stop")

    monkeypatch.setattr(chat.litellm, "acompletion", AsyncMock(side_effect=lambda **kwargs: chunks()))
    monkeypatch.setattr(chat, "total_token_count_from_response", lambda _: 7)
    assert [part async for part in _stream(_model(), tools)] == ["<think>reason</think>", "answer", 7]
