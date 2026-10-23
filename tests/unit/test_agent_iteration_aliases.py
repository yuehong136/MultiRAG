"""Iteration aliases across real components and the production Canvas scheduler."""

import json
from functools import partial
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.canvas import Canvas
from agent.component.message import Message
from agent.component.string_transform import StringTransform, StringTransformParam


def _node(name: str, params: dict[str, Any] | None = None, *, parent: str | None = None, upstream: list[str] | None = None, downstream: list[str] | None = None) -> dict[str, Any]:
    return {"obj": {"component_name": name, "params": params or {}}, "parent_id": parent, "upstream": upstream or [], "downstream": downstream or []}


def _canvas(items: list[Any], content: str = "{item}/{index}/{result}", *, stream: bool = True) -> Canvas:
    components = {
        "begin": _node("Begin", downstream=["Iteration:outer"]),
        "Iteration:outer": _node("Iteration", {"items_ref": "env.items"}, upstream=["begin"], downstream=["Message:done"]),
        "IterationItem:outer": _node("IterationItem", parent="Iteration:outer", downstream=["Message:child"]),
        "Message:child": _node("Message", {"content": [content], "stream": stream}, parent="Iteration:outer", upstream=["IterationItem:outer"]),
        "Message:done": _node("Message", {"content": ["done"], "stream": stream}, upstream=["Iteration:outer"]),
    }
    return Canvas(
        json.dumps(
            {
                "components": components,
                "globals": {"env.items": items, "sys.query": "", "sys.user_id": "", "sys.conversation_turns": 0, "sys.files": [], "sys.history": []},
                "retrieval": [],
                "path": [],
            }
        )
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_real_canvas_runs_every_iteration_with_aliases(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    canvas = _canvas([0, False, "", {"value": 1}], stream=stream)
    # Memory persistence is an external boundary; no model/storage/component is replaced.
    monkeypatch.setattr(Message, "_save_to_memory", AsyncMock() if stream else lambda *args: None)
    try:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        outputs = [event["data"]["outputs"]["content"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Message:child"]
        assert outputs == ["0/0/0", "false/1/false", "/2/", '{"value": 1}/3/{"value": 1}']
        assert any(event["event"] == "workflow_finished" for event in events)
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize("value", [0, False, "", {}, [], {"nested": [False, 0]}])
@pytest.mark.parametrize("reference", ["{item}", "{result}", "{IterationItem:outer@item}", "{IterationItem:outer@result}"])
def test_exact_references_preserve_objects_and_scalar_types(value: Any, reference: str) -> None:
    canvas = _canvas([value])
    marker = canvas.get_component_obj("IterationItem:outer")
    marker._invoke()
    child = canvas.get_component_obj("Message:child")
    child._param.inputs = {"query": {"value": None}}
    child._param.query = reference
    # Exercise base get_input independently of Message's dynamic input declaration.
    child.get_input_elements = lambda: child._param.inputs
    result = child.get_input()["query"]
    assert result is marker.output("item")
    assert type(result) is type(value)
    canvas._thread_pool.shutdown(wait=True)


def test_nested_and_sibling_scopes_never_resolve_another_iteration() -> None:
    canvas = _canvas(["outer"])
    canvas.components["IterationItem:outer"]["obj"]._invoke()
    extra = {
        "Iteration:inner": _node("Iteration", {"items_ref": "env.inner"}, parent="Iteration:outer"),
        "IterationItem:inner": _node("IterationItem", parent="Iteration:inner"),
        "Message:inner": _node("Message", {"content": ["{item}"]}, parent="Iteration:inner"),
        "Iteration:sibling": _node("Iteration", {"items_ref": "env.sibling"}),
        "IterationItem:sibling": _node("IterationItem", parent="Iteration:sibling"),
        "Message:sibling": _node("Message", {"content": ["{item}"]}, parent="Iteration:sibling"),
    }
    dsl = json.loads(str(canvas))
    dsl["components"].update(extra)
    dsl["globals"].update({"env.inner": [False], "env.sibling": [0]})
    nested = Canvas(json.dumps(dsl))
    try:
        for scope, value in [("outer", "outer"), ("inner", False), ("sibling", 0)]:
            nested.get_component_obj(f"IterationItem:{scope}")._invoke()
            child = nested.get_component_obj("Message:child" if scope == "outer" else f"Message:{scope}")
            elements = child.get_input_elements_from_text("{item}/{index}/{result}")
            assert elements["item"]["value"] == value
            assert elements["result"]["_cpn_id"] == f"IterationItem:{scope}"
            assert elements["index"]["value"] == 0
        assert nested.get_component_obj("Message:done").get_input_elements_from_text("{item}/{index}/{result}") == {}
        nested.get_component_obj("Message:inner")._param.inputs = {"query": {"value": None}}
        assert nested.get_component_obj("Message:inner").get_input_elements_from_text("{Item}") == {}
    finally:
        canvas._thread_pool.shutdown(wait=True)
        nested._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize("stream", [False, True])
async def test_message_keeps_falsy_values_and_literal_backslashes(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    canvas = _canvas([0], "{env.zero}|{env.false}|{env.empty}|{env.none}|{env.path}", stream=stream)
    canvas.globals.update({"env.zero": 0, "env.false": False, "env.empty": "", "env.none": None, "env.path": r"C:\10\report"})
    message = canvas.get_component_obj("Message:child")
    monkeypatch.setattr(message, "_save_to_memory", AsyncMock() if stream else lambda *args: None)
    try:
        message._invoke()
        result = message.output("content")
        if isinstance(result, partial):
            result = "".join([part async for part in result()])
        assert result == r"0|false|||C:\10\report"
    finally:
        canvas._thread_pool.shutdown(wait=True)


def test_string_transform_merge_isolated_and_split_preserves_empty_input() -> None:
    canvas = _canvas([0])
    param = StringTransformParam()
    param.method = "merge"
    param.script = "{env.zero}|{env.false}|{env.none}|{env.path}"
    transform = StringTransform(canvas, "StringTransform:test", param)
    canvas.components[transform._id] = {"obj": transform, "parent_id": None}
    canvas.globals.update({"env.zero": 0, "env.false": False, "env.none": None, "env.path": r"C:\10\report"})
    try:
        kwargs = {"env.zero": 0, "env.false": False}
        transform._merge(kwargs)
        assert kwargs == {"env.zero": 0, "env.false": False}
        assert transform.output("result") == r"0|false||C:\10\report"
        transform._merge()
        assert transform.output("result") == r"0|false||C:\10\report"
        canvas.globals["env.zero"] = 7
        transform._merge()
        assert transform.output("result") == r"7|false||C:\10\report"
        param.split_ref = "env.path"
        transform._split("")
        assert transform.output("result") == [""]
        canvas.globals["env.path"] = False
        with pytest.raises(AssertionError, match="not a string"):
            transform._split()
    finally:
        canvas._thread_pool.shutdown(wait=True)
