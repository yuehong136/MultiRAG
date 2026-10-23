"""Variable validation and nested writes through production Canvas components."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from agent.canvas import Canvas, Graph
from agent.component.loop import Loop
from agent.component.variable_assigner import VariableAssigner


def _node(name: str, params: dict[str, Any] | None = None, *, parent: str | None = None, upstream: list[str] | None = None, downstream: list[str] | None = None) -> dict[str, Any]:
    return {"obj": {"component_name": name, "params": params or {}}, "parent_id": parent, "upstream": upstream or [], "downstream": downstream or []}


@contextmanager
def _canvas(components: dict[str, Any], globals_: dict[str, Any] | None = None) -> Iterator[Canvas]:
    canvas = Canvas(
        json.dumps(
            {
                "components": components,
                "globals": {"sys.query": "", "sys.user_id": "", "sys.conversation_turns": 0, "sys.files": [], "sys.history": [], **(globals_ or {})},
                "retrieval": [],
                "path": [],
            }
        )
    )
    try:
        yield canvas
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.fixture(autouse=True)
def _uncanceled(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cancellation is an external Redis boundary; keep real Graph/component execution.
    monkeypatch.setattr(Graph, "is_canceled", lambda self: False)


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"operator": "set", "parameter": 1},
        {"variable": "", "operator": "set", "parameter": 1},
        {"variable": None, "operator": "set", "parameter": 1},
        {"variable": False, "operator": "set", "parameter": 1},
        {"variable": "env.target", "parameter": 1},
        {"variable": "env.target", "operator": "", "parameter": 1},
        {"variable": "env.target", "operator": None, "parameter": 1},
        {"variable": "env.target", "operator": "set"},
        {"variable": "env.target", "operator": "set", "parameter": None},
        None,
        "invalid",
    ],
)
def test_assigner_rejects_incomplete_first_item_before_reads_writes_or_outputs(monkeypatch: pytest.MonkeyPatch, item: Any) -> None:
    with _canvas({"VariableAssigner:test": _node("VariableAssigner", {"variables": [item, {"variable": "env.target", "operator": "set", "parameter": 99}]})}, {"env.target": 7}) as canvas:
        assigner = canvas.get_component_obj("VariableAssigner:test")
        assert isinstance(assigner, VariableAssigner)
        read, write, output = Mock(), Mock(), Mock()
        monkeypatch.setattr(canvas, "get_variable_value", read)
        monkeypatch.setattr(canvas, "set_variable_value", write)
        monkeypatch.setattr(assigner, "set_output", output)
        with pytest.raises(ValueError, match=r"^Variable is not complete\.$"):
            assigner._invoke()
        read.assert_not_called()
        write.assert_not_called()
        output.assert_not_called()
        assert canvas.globals["env.target"] == 7


@pytest.mark.parametrize("before,value", [(5, 0), (True, False), ("seed", ""), ([1], []), ({"old": 1}, {})])
def test_assigner_set_accepts_falsy_parameters_and_preserves_types(before: Any, value: Any) -> None:
    item = {"variable": "env.target", "operator": "set", "parameter": value}
    with _canvas({"VariableAssigner:test": _node("VariableAssigner", {"variables": [item]})}, {"env.target": before}) as canvas:
        result = canvas.get_component_obj("VariableAssigner:test").invoke()
        assert not result.get("_ERROR")
        assert canvas.get_variable_value("env.target") == value
        assert type(canvas.get_variable_value("env.target")) is type(value)


@pytest.mark.parametrize("value", [0, False, "", [], {}, None])
def test_assigner_overwrite_resolves_variable_references(value: Any) -> None:
    item = {"variable": "env.target", "operator": "overwrite", "parameter": "{env.source}"}
    with _canvas({"VariableAssigner:test": _node("VariableAssigner", {"variables": [item]})}, {"env.target": "old", "env.source": value}) as canvas:
        result = canvas.get_component_obj("VariableAssigner:test").invoke()
        assert not result.get("_ERROR")
        assert canvas.get_variable_value("env.target") == value
        assert type(canvas.get_variable_value("env.target")) is type(value)


@pytest.mark.parametrize(
    "operator,before,expected",
    [("clear", "seed", ""), ("clear", {"old": 1}, {}), ("clear", [1], []), ("remove_first", [1, 2, 3], [2, 3]), ("remove_last", [1, 2, 3], [1, 2]), ("remove_first", [], []), ("remove_last", [], [])],
)
@pytest.mark.parametrize("has_parameter", [False, True])
def test_assigner_no_parameter_operators_accept_missing_or_null_parameter(operator: str, before: Any, expected: Any, has_parameter: bool) -> None:
    item = {"variable": "env.target", "operator": operator}
    if has_parameter:
        item["parameter"] = None
    with _canvas({"VariableAssigner:test": _node("VariableAssigner", {"variables": [item]})}, {"env.target": before}) as canvas:
        result = canvas.get_component_obj("VariableAssigner:test").invoke()
        assert not result.get("_ERROR")
        assert canvas.get_variable_value("env.target") == expected


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"type": "string", "input_mode": "constant", "value": "v"},
        {"variable": "", "type": "string", "input_mode": "constant", "value": "v"},
        {"variable": False, "type": "string", "input_mode": "constant", "value": "v"},
        {"variable": "value", "input_mode": "constant", "value": "v"},
        {"variable": "value", "type": "", "input_mode": "constant", "value": "v"},
        {"variable": "value", "type": "string", "value": "v"},
        {"variable": "value", "type": "string", "input_mode": "unknown", "value": "v"},
        {"variable": "value", "type": "string", "input_mode": "constant"},
        {"variable": "value", "type": "string", "input_mode": "constant", "value": None},
        {"variable": "value", "type": "string", "input_mode": "variable", "value": ""},
        {"variable": "value", "type": "string", "input_mode": "variable", "value": False},
        None,
        "invalid",
    ],
)
def test_loop_rejects_incomplete_first_item_before_reads_writes_or_outputs(monkeypatch: pytest.MonkeyPatch, item: Any) -> None:
    with _canvas({"Loop:test": _node("Loop", {"loop_variables": [item, {"variable": "next", "type": "number", "input_mode": "constant", "value": 99}]})}) as canvas:
        loop = canvas.get_component_obj("Loop:test")
        assert isinstance(loop, Loop)
        read, write, output = Mock(), Mock(), Mock()
        monkeypatch.setattr(canvas, "get_variable_value", read)
        monkeypatch.setattr(canvas, "set_variable_value", write)
        monkeypatch.setattr(loop, "set_output", output)
        with pytest.raises(ValueError, match=r"^Loop Variable is not complete\.$"):
            loop._invoke()
        read.assert_not_called()
        write.assert_not_called()
        output.assert_not_called()


@pytest.mark.parametrize("mode", ["constant", "variable"])
@pytest.mark.parametrize("type_,value", [("number", 0), ("boolean", False), ("string", ""), ("object", {}), ("array<string>", [])])
def test_loop_accepts_falsy_constants_and_variable_values(type_: str, value: Any, mode: str) -> None:
    item = {"variable": "result", "type": type_, "input_mode": mode, "value": value if mode == "constant" else "{env.source}"}
    with _canvas({"Loop:test": _node("Loop", {"loop_variables": [item]})}, {"env.source": value}) as canvas:
        loop = canvas.get_component_obj("Loop:test")
        result = loop.invoke()
        assert not result.get("_ERROR")
        assert loop.output("result") == value
        assert type(loop.output("result")) is type(value)


@pytest.mark.parametrize(
    "path,original,expected",
    [
        ("", {"sibling": 1}, 0),
        ("a", {"sibling": 1, "a": "old"}, {"sibling": 1, "a": 0}),
        ("a.b", {"a": {"sibling": 2, "b": "old"}, "root_sibling": 1}, {"a": {"sibling": 2, "b": 0}, "root_sibling": 1}),
        ("a.b.c", {"a": {"sibling": 2, "b": False}}, {"a": {"sibling": 2, "b": {"c": 0}}}),
        ("a.b", {}, {"a": {"b": 0}}),
    ],
)
def test_graph_nested_setter_writes_last_key_and_keeps_dict_semantics(path: str, original: Any, expected: Any) -> None:
    with _canvas({}) as graph:
        assert graph.set_variable_param_value(original, path, 0) == expected


async def test_real_canvas_loop_assignment_and_serialized_readback() -> None:
    initial = {"nested": {"value": "old", "sibling": "kept"}, "root_sibling": [1]}
    loop_variables = [
        {"variable": "object", "type": "object", "input_mode": "variable", "value": "env.source"},
        {"variable": "zero", "type": "number", "input_mode": "constant", "value": 0},
        {"variable": "false", "type": "boolean", "input_mode": "constant", "value": False},
        {"variable": "empty", "type": "string", "input_mode": "constant", "value": ""},
    ]
    assignments = [
        {"variable": "Loop:test@object.nested.value", "operator": "overwrite", "parameter": "Loop:test@zero"},
        {"variable": "Loop:test@object.nested.false", "operator": "overwrite", "parameter": "Loop:test@false"},
        {"variable": "Loop:test@object.nested.empty", "operator": "overwrite", "parameter": "Loop:test@empty"},
        {"variable": "env.items", "operator": "remove_first"},
    ]
    components = {
        "begin": _node("Begin", downstream=["Loop:test"]),
        "Loop:test": _node(
            "Loop",
            {"loop_variables": loop_variables, "maximum_loop_count": 1, "loop_termination_condition": [{"variable": "Loop:test@zero", "operator": "=", "value": -1, "input_mode": "constant"}]},
            upstream=["begin"],
            downstream=["VariableAssigner:done"],
        ),
        "LoopItem:test": _node("LoopItem", parent="Loop:test", downstream=["VariableAssigner:child"]),
        "VariableAssigner:child": _node("VariableAssigner", {"variables": assignments}, parent="Loop:test", upstream=["LoopItem:test"]),
        "VariableAssigner:done": _node("VariableAssigner", {"variables": [{"variable": "env.done", "operator": "set", "parameter": True}]}, upstream=["Loop:test"]),
    }
    with _canvas(components, {"env.source": initial, "env.items": [1, 2], "env.done": False}) as canvas:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        assert any(event["event"] == "workflow_finished" for event in events)
        expected = {"nested": {"value": 0, "false": False, "empty": "", "sibling": "kept"}, "root_sibling": [1]}
        assert canvas.get_variable_value("Loop:test@object") == expected
        assert type(canvas.get_variable_value("Loop:test@object.nested.value")) is int
        assert canvas.get_variable_value("Loop:test@object.nested.false") is False
        assert canvas.get_variable_value("env.items") == [2]
        assert canvas.get_variable_value("env.done") is True
        assert canvas.get_component_obj("LoopItem:test")._idx == -1
        stored = str(canvas)
        with _canvas({}) as restored:
            restored.dsl = json.loads(stored)
            restored.load()
            assert restored.get_variable_value("Loop:test@object") == expected
            assert restored.get_variable_value("env.items") == [2]
            assert restored.get_component_obj("Loop:test")._param.loop_variables == loop_variables
            assert restored.get_component_obj("VariableAssigner:child")._param.variables == assignments
        finished_children = [event for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "VariableAssigner:child"]
        assert len(finished_children) == 1


@pytest.mark.parametrize("name,params,message", [("VariableAssigner", {"variables": [{}]}, "Variable is not complete."), ("Loop", {"loop_variables": [{}]}, "Loop Variable is not complete.")])
def test_production_invoke_records_clear_error_without_changing_variables(name: str, params: dict[str, Any], message: str) -> None:
    with _canvas({"node": _node(name, params)}, {"env.target": {"kept": 1}}) as canvas:
        before = deepcopy(canvas.globals)
        result = canvas.get_component_obj("node").invoke()
        assert result["_ERROR"] == message
        assert canvas.globals == before
        assert all(key.startswith("_") for key in result)
