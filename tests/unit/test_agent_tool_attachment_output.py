from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from agent.component.agent_with_tools import Agent

_ATTACHMENT_CONTENT = "attachment_count: 1\n\nattachment1 (image): chart.png\nparsed artifact"
_ARTIFACT_MARKDOWN = "![chart.png](/artifact/chart.png)"


class _Canvas:
    def __init__(self, *, has_references: bool = False) -> None:
        self.has_references = has_references

    def get_component(self, _component_id: str) -> dict[str, list[str]]:
        return {"downstream": []}

    def get_reference(self) -> dict[str, list[object]]:
        return {"chunks": [object()] if self.has_references else []}


class _CodeExecTool:
    def __init__(self) -> None:
        self._param = SimpleNamespace(
            outputs={
                "_ATTACHMENT_CONTENT": {"value": _ATTACHMENT_CONTENT},
                "_ARTIFACTS": {
                    "value": [
                        {
                            "name": "chart.png",
                            "url": "/artifact/chart.png",
                            "mime_type": "image/png",
                        }
                    ]
                },
            }
        )


def _build_agent(*, cite: bool = False, has_references: bool = False) -> tuple[Agent, dict[str, Any]]:
    agent = object.__new__(Agent)
    outputs: dict[str, Any] = {}
    agent.chat_mdl = object()
    agent._id = "agent"
    agent._param = SimpleNamespace(cite=cite)
    agent._canvas = _Canvas(has_references=has_references)
    agent.tools = {"code": _CodeExecTool()}
    agent.check_if_canceled = lambda _context: False
    agent.get_exception_default_value = lambda: None
    agent.set_output = lambda key, value: outputs.__setitem__(key, value)
    agent.callback = lambda *_args, **_kwargs: None
    return agent, outputs


async def test_non_streaming_agent_keeps_artifact_link_without_attachment_metadata() -> None:
    agent, outputs = _build_agent()
    agent._prepare_prompt_variables = lambda: ("prompt", [], {})
    agent._get_output_schema = lambda: None
    agent.exception_handler = lambda: None
    agent._fit_messages = lambda _prompt, messages: messages
    agent._append_system_prompt = lambda _messages, _prompt: None

    async def generate(_messages: list[dict[str, Any]]) -> str:
        return "answer"

    agent._generate_async = generate

    result = await Agent._invoke_async.__wrapped__(agent)

    assert result == f"answer\n\n{_ARTIFACT_MARKDOWN}"
    assert outputs["content"] == result
    assert _ATTACHMENT_CONTENT not in result


@pytest.mark.parametrize(
    ("cite", "has_references", "fitted_messages", "expected_answer"),
    [
        (False, False, [], "answer"),
        (True, True, [{"role": "user", "content": "message"}] * 7, "cited answer"),
    ],
)
async def test_streaming_agent_keeps_artifact_link_without_attachment_metadata(
    cite: bool,
    has_references: bool,
    fitted_messages: list[dict[str, str]],
    expected_answer: str,
) -> None:
    agent, outputs = _build_agent(cite=cite, has_references=has_references)
    agent._fit_messages = lambda _prompt, _messages: fitted_messages

    async def generate_streamly(_messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        yield "answer"

    async def generate_citations(_answer: str) -> AsyncIterator[str]:
        yield "cited answer"

    agent._generate_streamly = generate_streamly
    agent._gen_citations_async = generate_citations

    chunks = [chunk async for chunk in agent.stream_output_with_tools_async("prompt", [])]
    rendered = "".join(chunks)

    assert rendered == f"{expected_answer}\n\n{_ARTIFACT_MARKDOWN}"
    assert outputs["content"] == rendered
    assert _ATTACHMENT_CONTENT not in rendered
