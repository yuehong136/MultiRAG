"""Switch branches run through the real Canvas scheduler without model calls."""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from agent.canvas import Canvas
from core.utils.redis_conn import REDIS_CONN


def switch_dsl(conditions: list[dict[str, Any]]) -> dict[str, Any]:
    targets = ["Message:case", "Message:later", "Message:else"]
    components = {
        "begin": {"obj": {"component_name": "Begin", "params": {}}, "upstream": [], "downstream": ["Switch:route"]},
        "Switch:route": {
            "obj": {"component_name": "Switch", "params": {"conditions": conditions, "end_cpn_ids": ["Message:else"]}},
            "upstream": ["begin"],
            "downstream": targets,
        },
    }
    for target in targets:
        components[target] = {
            "obj": {"component_name": "Message", "params": {"content": [target], "stream": False}},
            "upstream": ["Switch:route"],
            "downstream": [],
        }
    return {"components": components, "path": [], "history": [], "retrieval": [], "graph": {"nodes": [{"id": target, "data": {"name": target}} for target in targets]}}


@pytest.fixture
def canvases(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Canvas]]:
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    created: list[Canvas] = []
    yield created
    for canvas in created:
        canvas._thread_pool.shutdown(wait=True)


def load(canvases: list[Canvas], conditions: list[dict[str, Any]]) -> Canvas:
    canvas = Canvas(json.dumps(switch_dsl(conditions)), tenant_id="switch-unit")
    canvases.append(canvas)
    return canvas


def assert_branch(canvas: Canvas, events: list[dict[str, Any]], target: str) -> None:
    assert events[-1]["event"] == "workflow_finished"
    assert [event["data"]["content"] for event in events if event["event"] == "message"] == [target]
    assert canvas.path == ["begin", "Switch:route", target]
    branch = canvas.get_component_obj("Switch:route")
    assert branch.output("_next") == [target]
    assert branch.output("next") == [target]
    assert branch.error() is None


@pytest.mark.parametrize("logical_operator", ["and", "or"])
@pytest.mark.parametrize("items", [[], [{"cpn_id": "", "operator": "=", "value": "yes"}]])
async def test_empty_condition_reaches_else(canvases: list[Canvas], logical_operator: str, items: list[dict[str, Any]]) -> None:
    canvas = load(canvases, [{"logical_operator": logical_operator, "items": items, "to": ["Message:case"]}])
    events = [event async for event in canvas.run(query="question")]
    assert_branch(canvas, events, "Message:else")


async def test_empty_condition_does_not_hide_later_matching_branch(canvases: list[Canvas]) -> None:
    canvas = load(
        canvases,
        [
            {"logical_operator": "and", "items": [{"cpn_id": "", "operator": "=", "value": "yes"}], "to": ["Message:case"]},
            {"logical_operator": "and", "items": [{"cpn_id": "begin@answer", "operator": "=", "value": "yes"}], "to": ["Message:later"]},
        ],
    )
    events = [event async for event in canvas.run(query="question", inputs={"answer": {"value": "yes"}})]
    assert_branch(canvas, events, "Message:later")


@pytest.mark.parametrize("answer, expected", [(0, "0"), ("", "")])
async def test_evaluated_falsy_value_still_matches(canvases: list[Canvas], answer: int | str, expected: str) -> None:
    canvas = load(canvases, [{"logical_operator": "and", "items": [{"cpn_id": "begin@answer", "operator": "=", "value": expected}], "to": ["Message:case"]}])
    events = [event async for event in canvas.run(query="question", inputs={"answer": {"value": answer}})]
    assert_branch(canvas, events, "Message:case")


@pytest.mark.parametrize("logical_operator, target", [("and", "Message:else"), ("or", "Message:case")])
async def test_evaluated_and_or_conditions_keep_their_logic(canvases: list[Canvas], logical_operator: str, target: str) -> None:
    canvas = load(
        canvases,
        [
            {
                "logical_operator": logical_operator,
                "items": [{"cpn_id": "sys.query", "operator": "=", "value": value} for value in ["no", "yes"]],
                "to": ["Message:case"],
            }
        ],
    )
    events = [event async for event in canvas.run(query="yes")]
    assert_branch(canvas, events, target)


def test_canceled_switch_does_not_choose_else(canvases: list[Canvas], monkeypatch: pytest.MonkeyPatch) -> None:
    canvas = load(canvases, [{"logical_operator": "and", "items": [], "to": ["Message:case"]}])
    monkeypatch.setattr(canvas, "is_canceled", lambda: True)
    branch = canvas.get_component_obj("Switch:route")
    branch.invoke()
    assert branch.error() == "Task has been canceled"
    assert not branch.output("_next")
    assert not branch.output("next")
