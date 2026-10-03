"""Tool continuations preserve DeepSeek reasoning across actual SDK response shapes."""

from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

from core.llm import ChatModel, chat
from core.llm.chat_model.base import Base as SyncBase
from core.llm.chat_model.models.deepseek_chat import DeepSeekChat


class _ReasoningBase(chat.Base):
    def _need_reasoning_content_back(self) -> bool:
        return True


class _Session:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    def tool_call(self, name: str, arguments: dict[str, Any]) -> dict[str, str]:
        self.calls.append((name, arguments))
        if self.fail and name == "second":
            raise ValueError("tool unavailable")
        return {"content": name}

    async def tool_call_async(self, name: str, arguments: dict[str, Any]) -> dict[str, str]:
        return self.tool_call(name, arguments)


class _Stream:
    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def __aiter__(self) -> AsyncIterator[Any]:
        for chunk in self.chunks:
            yield chunk

    def __iter__(self) -> Iterator[Any]:
        return iter(self.chunks)


def _call(name: str, call_id: str) -> ChatCompletionMessageToolCall:
    return ChatCompletionMessageToolCall(id=call_id, type="function", function={"name": name, "arguments": '{"query":"hello"}'})


def _response(calls: list[Any] | None, reason: str, *, alias: bool = False) -> SimpleNamespace:
    message = SimpleNamespace(content=None if calls else "answer", tool_calls=calls)
    setattr(message, "reasoning" if alias else "reasoning_content", reason)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None)


def _chunk(reason: str = "", content: str | None = None, calls: list[Any] | None = None, *, alias: bool = False) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=calls)
    setattr(delta, "reasoning" if alias else "reasoning_content", reason)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)


def _tool_round(reason: str, calls: list[Any], *, stream: bool, alias: bool) -> Any:
    if not stream:
        return _response(calls, reason, alias=alias)
    deltas = [ChoiceDeltaToolCall(index=i, id=tc.id, type="function", function={"name": tc.function.name, "arguments": tc.function.arguments}) for i, tc in enumerate(calls)]
    # Reasoning can arrive in the same delta as tool_calls. It must not be skipped.
    return _Stream([_chunk(reason[:2], alias=alias), _chunk(reason[2:], calls=deltas, alias=alias)])


def _check_history(requests: list[list[dict[str, Any]]], *, deepseek: bool, fail: bool) -> None:
    assert len(requests) == 3
    for round_index, expected_reason in [(1, "first reason"), (2, "second reason")]:
        messages = requests[round_index]
        assistant = [message for message in messages if message["role"] == "assistant"][-1]
        if deepseek:
            assert assistant["reasoning_content"] == expected_reason
        else:
            assert "reasoning_content" not in assistant
        assert all("index" not in tc or tc["index"] is not None for tc in assistant["tool_calls"])
        ids = [tc["id"] for tc in assistant["tool_calls"]]
        position = messages.index(assistant)
        replies = messages[position + 1 :]
        assert [reply["tool_call_id"] for reply in replies] == ids
        assert all(reply["role"] == "tool" for reply in replies)
    first_assistant = requests[1][1]
    assert len(first_assistant["tool_calls"]) == 2
    assert requests[2][1] == first_assistant
    assert requests[1][2]["content"] == '{"content": "first"}'
    assert requests[1][3]["content"] == ("tool unavailable" if fail else '{"content": "second"}')


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("deepseek", [False, True])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_sync_tool_loop_preserves_round_reasoning_and_protocol(stream: bool, deepseek: bool, alias: bool, fail: bool) -> None:
    model = (DeepSeekChat if deepseek else SyncBase)("fixture-key", "test-model", "http://fixture.invalid/v1", max_retries=0, max_rounds=2)
    session = _Session(fail=fail)
    model.bind_tools(session, [{"type": "function", "function": {"name": "first"}}])
    requests: list[list[dict[str, Any]]] = []
    responses = iter(
        [
            _tool_round("first reason", [_call("first", "a"), _call("second", "b")], stream=stream, alias=alias),
            _tool_round("second reason", [_call("third", "c")], stream=stream, alias=alias),
            _Stream([_chunk("final reason", "answer", alias=alias)]) if stream else _response(None, "final reason", alias=alias),
        ]
    )

    def complete(**kwargs: Any) -> Any:
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    model.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    history = [{"role": "user", "content": "hello"}]
    if stream:
        parts = list(model.chat_streamly_with_tools("", history, {}))
        answer = "".join(part for part in parts if isinstance(part, str))
    else:
        answer, _tokens = model.chat_with_tools("", history, {})
    assert "answer" in answer
    assert "final reason" in answer
    assert "**ERROR**" not in answer
    assert [name for name, _args in session.calls] == ["first", "second", "third"]
    _check_history(requests, deepseek=deepseek, fail=fail)
    assert history == [{"role": "user", "content": "hello"}]


@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("deepseek", [False, True])
@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_async_tool_loop_preserves_round_reasoning_and_protocol(monkeypatch: pytest.MonkeyPatch, backend: str, stream: bool, deepseek: bool, alias: bool, fail: bool) -> None:
    model: Any
    if backend == "litellm":
        model = chat.LiteLLMBase("fixture-key", "test-model", provider="DeepSeek" if deepseek else "OpenAI", max_retries=0, max_rounds=2)
    else:
        model = (_ReasoningBase if deepseek else chat.Base)("fixture-key", "test-model", "http://fixture.invalid/v1", max_retries=0, max_rounds=2)
    session = _Session(fail=fail)
    model.bind_tools(session, [{"type": "function", "function": {"name": "first"}}])
    requests: list[list[dict[str, Any]]] = []
    responses = iter(
        [
            _tool_round("first reason", [_call("first", "a"), _call("second", "b")], stream=stream, alias=alias),
            _tool_round("second reason", [_call("third", "c")], stream=stream, alias=alias),
            _Stream([_chunk("final reason", "answer", alias=alias)]) if stream else _response(None, "final reason", alias=alias),
        ]
    )

    async def complete(**kwargs: Any) -> Any:
        requests.append(deepcopy(kwargs["messages"]))
        return next(responses)

    if backend == "litellm":
        monkeypatch.setattr(chat.litellm, "acompletion", complete)
    else:
        model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    history = [{"role": "user", "content": "hello"}]
    if stream:
        parts = [part async for part in model.async_chat_streamly_with_tools("", history, {})]
        answer = "".join(part for part in parts if isinstance(part, str))
    else:
        answer, _tokens = await model.async_chat_with_tools("", history, {})
    assert "answer" in answer
    assert "final reason" in answer
    assert "**ERROR**" not in answer
    assert sorted(name for name, _args in session.calls) == ["first", "second", "third"]
    _check_history(requests, deepseek=deepseek, fail=fail)
    assert history == [{"role": "user", "content": "hello"}]


async def test_registered_deepseek_model_uses_reasoning_history() -> None:
    model = ChatModel["DeepSeek"]("fixture-key", "deepseek-reasoner", provider="DeepSeek", max_retries=0)
    assert isinstance(model, chat.LiteLLMBase)
    assert model._need_reasoning_content_back()


@pytest.mark.parametrize("backend", ["base", "litellm"])
async def test_async_tool_loop_keeps_sync_session_fallback(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    if backend == "litellm":
        model = chat.LiteLLMBase("fixture-key", "test-model", provider="DeepSeek", max_retries=0)
    else:
        model = _ReasoningBase("fixture-key", "test-model", "http://fixture.invalid/v1", max_retries=0)
    session = SimpleNamespace(tool_call=Mock(return_value="tool answer"))
    model.bind_tools(session, [{"type": "function", "function": {"name": "first"}}])
    complete = AsyncMock(side_effect=[_response([_call("first", "a")], "reason"), _response(None, "", alias=False)])
    if backend == "litellm":
        monkeypatch.setattr(chat.litellm, "acompletion", complete)
    else:
        model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
    answer, _tokens = await model.async_chat_with_tools("", [{"role": "user", "content": "hello"}], {})
    assert "answer" in answer
    session.tool_call.assert_called_once_with("first", {"query": "hello"})
