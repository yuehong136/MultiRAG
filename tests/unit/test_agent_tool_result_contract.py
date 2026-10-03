"""Agent tools retain their schema and expose component output after a void invoke."""

from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from jsonschema import Draft202012Validator

from agent.component.agent_with_tools import Agent, AgentParam
from agent.tools.base import LLMToolPluginCallSession


@pytest.mark.parametrize("prompt", ["", "Summarize {context}"])
def test_agent_prompt_remains_a_string_schema(prompt: str) -> None:
    agent = object.__new__(Agent)
    agent._id = "parent-->research"
    agent._param = AgentParam()
    agent._param.user_prompt = prompt

    meta = agent.get_meta()
    schema = meta["function"]["parameters"]
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"user_prompt": "query", "reasoning": "why", "context": "facts"})
    assert meta["function"]["name"] == "research"
    assert schema["properties"]["user_prompt"]["type"] == "string"
    assert schema["properties"]["user_prompt"].get("default", "") == prompt


class _Tool:
    def __init__(self, result: Any, output: Any) -> None:
        self.result = result
        self.output = Mock(return_value=output)
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **arguments: Any) -> Any:
        self.calls.append(arguments)
        return self.result


class _AsyncTool(_Tool):
    async def invoke_async(self, **arguments: Any) -> Any:
        return self.invoke(**arguments)


@pytest.mark.parametrize("tool_class", [_Tool, _AsyncTool])
@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ({"content": "answer", "_elapsed_time": 1}, "answer"),
        ({"content": {"answer": 42}}, {"answer": 42}),
        ({"content": "", "other": "value"}, {"content": "", "other": "value"}),
        ({"content": None}, {"content": None}),
        ({"content": False}, False),
        ({"content": 0}, 0),
        ("text", "text"),
        ([], []),
        (None, None),
    ],
)
async def test_void_local_tool_uses_output_in_result_and_callback(tool_class: type[_Tool], output: Any, expected: Any) -> None:
    tool = tool_class(None, output)
    callback = Mock()
    session = LLMToolPluginCallSession({"tool": tool}, partial(callback))
    arguments = {"query": "hello"}

    assert await session.tool_call_async("tool", arguments) == expected
    assert tool.calls == [arguments]
    tool.output.assert_called_once_with()
    assert callback.call_args.args == ("tool", arguments, expected)
    assert callback.call_args.kwargs["elapsed_time"] >= 0


@pytest.mark.parametrize("result", ["", False, 0, [], {}, "answer"])
async def test_explicit_falsy_returns_do_not_use_output(result: Any) -> None:
    tool = _AsyncTool(result, "stale output")
    session = LLMToolPluginCallSession({"tool": tool}, partial(Mock()))
    assert await session.tool_call_async("tool", {}) == result
    tool.output.assert_not_called()


def test_sync_tool_session_also_uses_component_output() -> None:
    tool = _AsyncTool(None, {"content": "answer"})
    session = LLMToolPluginCallSession({"tool": tool}, partial(Mock()))
    assert session.tool_call("tool", {}) == "answer"
    assert tool.calls == [{}]


async def test_output_failure_is_reported_as_a_tool_failure() -> None:
    tool = _AsyncTool(None, None)
    tool.output.side_effect = ValueError("output unavailable")
    session = LLMToolPluginCallSession({"tool": tool}, partial(Mock()))
    with pytest.raises(ValueError, match="output unavailable"):
        await session.tool_call_async("tool", {})


async def test_void_tool_without_output_preserves_none() -> None:
    tool = SimpleNamespace(invoke=Mock(return_value=None))
    session = LLMToolPluginCallSession({"tool": tool}, partial(Mock()))
    assert await session.tool_call_async("tool", {}) is None


async def test_explicit_result_does_not_even_resolve_output_accessor() -> None:
    class Tool:
        def invoke(self) -> str:
            return "answer"

        @property
        def output(self) -> Any:
            raise AssertionError("output must not be accessed")

    session = LLMToolPluginCallSession({"tool": Tool()}, partial(Mock()))
    assert await session.tool_call_async("tool", {}) == "answer"
