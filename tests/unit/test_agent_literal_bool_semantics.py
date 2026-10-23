"""Literal evaluation and boolean operations through actual Canvas components."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from agent.canvas import Canvas, Graph


def _node(name: str, params: dict[str, Any] | None = None, *, upstream: list[str] | None = None, downstream: list[str] | None = None, parent: str | None = None) -> dict[str, Any]:
    return {"obj": {"component_name": name, "params": params or {}}, "upstream": upstream or [], "downstream": downstream or [], "parent_id": parent}


@contextmanager
def _canvas(components: dict[str, Any], values: dict[str, Any] | None = None) -> Iterator[Canvas]:
    canvas = Canvas(
        json.dumps(
            {
                "components": components,
                "globals": {"sys.query": "", "sys.user_id": "", "sys.files": [], "sys.conversation_turns": 0, "sys.history": [], **(values or {})},
                "path": [],
                "history": [],
                "retrieval": [],
            }
        )
    )
    try:
        yield canvas
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.fixture(autouse=True)
def uncanceled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Graph, "is_canceled", lambda self: False)


@pytest.mark.parametrize("wrap_list", [False, True])
def test_literal_eval_dispatch_keeps_normalized_objects_and_nested_values(wrap_list: bool) -> None:
    source = {"zero": "0", "false": "False", "empty": "''", "nested": {"items": "[0, False, '']"}, "plain": "unparsed"}
    expected = {"zero": 0, "false": False, "empty": "", "nested": {"items": [0, False, ""]}, "plain": "unparsed"}
    values = {"env.source": [source, None, 2, "skip"] if wrap_list else source, "env.missing": None}
    with _canvas({"DataOperations:parse": _node("DataOperations", {"operations": "literal_eval", "query": ["env.source", "env.missing"]})}, values) as canvas:
        component = canvas.get_component_obj("DataOperations:parse")
        result = component.invoke()
        assert not result.get("_ERROR") and component.output("result") == [expected]
        assert component.output("result")[0]["false"] is False
        assert type(component.output("result")[0]["zero"]) is int
        assert canvas.get_variable_value("env.source") == values["env.source"]


@pytest.mark.parametrize("value,expected", [(True, False), (False, False), (8, 0), (2.5, 0.0), ("text", ""), ({"old": 1}, {}), ([1], [])])
def test_clear_preserves_boolean_and_numeric_types_in_serialized_canvas(value: Any, expected: Any) -> None:
    with _canvas({"VariableAssigner:clear": _node("VariableAssigner", {"variables": [{"variable": "env.target", "operator": "clear"}]})}, {"env.target": value}) as canvas:
        result = canvas.get_component_obj("VariableAssigner:clear").invoke()
        assert not result.get("_ERROR")
        actual = canvas.get_variable_value("env.target")
        assert actual == expected and type(actual) is type(expected)
        stored = json.loads(str(canvas))
        assert stored["globals"]["env.target"] == expected and type(stored["globals"]["env.target"]) is type(expected)


@pytest.mark.parametrize(
    "variable,operator,value,expected",
    [
        (True, "is", True, True),
        (False, "is", False, True),
        (False, "is", 0, False),
        (True, "is not", False, True),
        (False, "is not", False, False),
        (True, "empty", None, False),
        (False, "not empty", None, True),
        (4, "≥", 3, True),
        (0, "=", 0, True),
        (2.5, "<", 2, False),
    ],
)
def test_loop_conditions_dispatch_boolean_before_numeric(variable: Any, operator: str, value: Any, expected: bool) -> None:
    with _canvas({"LoopItem:test": _node("LoopItem")}) as canvas:
        assert canvas.get_component_obj("LoopItem:test").evaluate_condition(variable, operator, value) is expected


def test_boolean_loop_condition_rejects_numeric_operator() -> None:
    with _canvas({"LoopItem:test": _node("LoopItem")}) as canvas:
        with pytest.raises(Exception, match="Invalid operator: ="):
            canvas.get_component_obj("LoopItem:test").evaluate_condition(True, "=", 1)


async def test_canvas_boolean_clear_terminates_loop_and_roundtrips_values() -> None:
    components = {
        "begin": _node("Begin", downstream=["DataOperations:parse"]),
        "DataOperations:parse": _node("DataOperations", {"operations": "literal_eval", "query": ["env.source"]}, upstream=["begin"], downstream=["Loop:repeat"]),
        "Loop:repeat": _node(
            "Loop",
            {
                "loop_variables": [{"variable": "flag", "type": "boolean", "input_mode": "constant", "value": True}],
                "maximum_loop_count": 3,
                "loop_termination_condition": [{"variable": "Loop:repeat@flag", "operator": "is", "value": False, "input_mode": "constant"}],
            },
            upstream=["DataOperations:parse"],
            downstream=["Message:done"],
        ),
        "LoopItem:start": _node("LoopItem", parent="Loop:repeat", downstream=["VariableAssigner:clear"]),
        "VariableAssigner:clear": _node(
            "VariableAssigner",
            {"variables": [{"variable": "Loop:repeat@flag", "operator": "clear"}, {"variable": "env.iterations", "operator": "+=", "parameter": 1}]},
            parent="Loop:repeat",
            upstream=["LoopItem:start"],
        ),
        "Message:done": _node("Message", {"content": ["done"]}, upstream=["Loop:repeat"]),
    }
    with _canvas(components, {"env.source": {"flag": "False", "zero": "0", "empty": "''"}, "env.iterations": 0}) as canvas:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        assert any(event["event"] == "workflow_finished" for event in events)
        assert canvas.get_variable_value("Loop:repeat@flag") is False and canvas.get_variable_value("env.iterations") == 1
        assert canvas.get_component_obj("DataOperations:parse").output("result") == [{"flag": False, "zero": 0, "empty": ""}]
        stored = str(canvas)
    restored = Canvas(stored)
    try:
        assert restored.get_variable_value("Loop:repeat@flag") is False and restored.get_variable_value("env.iterations") == 1
        assert restored.get_component_obj("DataOperations:parse").output("result")[0] == {"flag": False, "zero": 0, "empty": ""}
    finally:
        restored._thread_pool.shutdown(wait=True)
