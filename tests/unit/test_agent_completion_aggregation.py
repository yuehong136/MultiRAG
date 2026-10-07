"""Public aggregation must retain results and reject empty or opaque success payloads."""

from functools import partial
from typing import Any

import pytest

from api.utils.agent_completion import AgentCompletionAccumulator, normalize_agent_event


def test_complete_result_keeps_terminal_outputs_and_independent_trace() -> None:
    node = {"event": "node_finished", "data": {"component_id": "LLM:one", "outputs": {"structured": {"count": 2}}, "elapsed_time": 0.2}}
    events: list[dict[str, Any]] = [
        {"event": "message", "data": {"content": "Hello "}},
        node,
        {"event": "message", "data": {"content": "world", "reference": {"chunks": {"0": {"id": "chunk"}}}}},
        {"event": "message_end", "data": {"reference": {"doc_aggs": [{"id": "doc"}]}}},
        {"event": "workflow_finished", "data": {"outputs": {"result": "world"}}},
    ]
    aggregate = AgentCompletionAccumulator(return_trace=True)
    for event in events:
        aggregate.add(normalize_agent_event(event))
    node["data"]["outputs"]["structured"]["count"] = 99
    answer = aggregate.finish(require_terminal=True)
    assert answer["event"] == "workflow_finished" and answer["data"]["outputs"] == {"result": "world"}
    assert answer["data"]["content"] == "Hello world"
    assert answer["data"]["reference"] == {"chunks": {"0": {"id": "chunk"}}, "doc_aggs": [{"id": "doc"}]}
    assert answer["data"]["structured"] == {"LLM:one": {"count": 2}}
    assert answer["data"]["trace"][0]["trace"][0]["outputs"]["structured"] == {"count": 2}


@pytest.mark.parametrize(
    "events", [[], [{"event": "workflow_started", "data": {}}], [{"event": "message_end", "data": {}}], [{"event": "workflow_finished", "data": {"outputs": {"content": "", "_elapsed_time": 1}}}]]
)
def test_empty_workflow_cannot_be_reported_as_success(events: list[dict[str, Any]]) -> None:
    aggregate = AgentCompletionAccumulator()
    for event in events:
        aggregate.add(normalize_agent_event(event))
    with pytest.raises(ValueError):
        aggregate.finish(require_terminal=True)


def test_deferred_handles_are_not_strings_and_other_opaque_objects_fail() -> None:
    callback = partial(str, "deferred")
    event = normalize_agent_event({"event": "node_finished", "data": {"outputs": {"content": callback}}})
    assert event["data"]["outputs"]["content"] is None
    with pytest.raises(TypeError):
        normalize_agent_event({"event": "node_finished", "data": {"outputs": {"opaque": object()}}})


def test_null_structured_output_is_not_a_success_result() -> None:
    aggregate = AgentCompletionAccumulator()
    aggregate.add({"event": "node_finished", "data": {"component_id": "LLM:empty", "outputs": {"structured": None}}})
    aggregate.add({"event": "workflow_finished", "data": {"outputs": {"structured": None}}})
    with pytest.raises(ValueError, match="no output"):
        aggregate.finish(require_terminal=True)


def test_nontext_message_and_nonobject_data_are_rejected() -> None:
    with pytest.raises(ValueError):
        normalize_agent_event({"event": "message_end", "data": []})
    with pytest.raises(ValueError):
        AgentCompletionAccumulator().add({"event": "message", "data": {"content": []}})
