import json
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.variable_assigner import VariableAssigner
from core.utils.redis_conn import REDIS_CONN


@pytest.mark.parametrize(
    ("definition", "expected"),
    [
        ({"type": "string", "value": "configured"}, "configured"),
        ({"type": "number", "value": 7}, 7),
        ({"type": "boolean", "value": True}, True),
        ({"type": "number", "value": False}, False),
        ({"type": "boolean", "value": 0}, 0),
        ({"type": "number", "value": ""}, ""),
        ({"type": "object", "value": {}}, {}),
        ({"type": "array<string>", "value": []}, []),
        ({"type": "object", "value": {"nested": ["seed"]}}, {"nested": ["seed"]}),
        ({"type": "array<object>", "value": [{"seed": []}]}, [{"seed": []}]),
        ({"type": "number", "value": None}, 0),
        ({"type": "boolean"}, False),
        ({"type": "object", "value": None}, {}),
        ({"type": "array<string>"}, []),
        ({"type": "string", "value": None}, ""),
        ({"type": "unknown", "value": None}, ""),
        ({}, ""),
    ],
)
@pytest.mark.parametrize("has_runtime_key", [False, True])
def test_reset_restores_defaults_without_rewriting_definitions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], definition: dict[str, Any], expected: Any, has_runtime_key: bool
) -> None:
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    dsl = {"components": {}, "path": [], "retrieval": [], "variables": {"configured": definition}, "globals": {"env.orphan": "old"}}
    if has_runtime_key:
        dsl["globals"]["env.configured"] = "stale runtime"
    canvas = Canvas(json.dumps(dsl))
    canvas.reset()
    assert canvas.globals["env.configured"] == expected and type(canvas.globals["env.configured"]) is type(expected)
    assert canvas.globals["env.orphan"] == ""
    assert canvas.variables == dsl["variables"]
    serialized = json.loads(str(canvas))
    assert serialized["variables"] == dsl["variables"] and serialized["globals"]["env.configured"] == expected
    assert capsys.readouterr().out == ""


def test_mutable_defaults_survive_actual_assigner_and_repeated_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    definitions = {"items": {"type": "array<string>", "value": ["seed"]}, "object": {"type": "object", "value": {"nested": ["seed"]}}}
    dsl = {
        "components": {"VariableAssigner:test": {"obj": {"component_name": "VariableAssigner", "params": {}}, "upstream": [], "downstream": []}},
        "path": [],
        "retrieval": [],
        "variables": definitions,
    }
    canvas = Canvas(json.dumps(dsl))
    canvas.reset()
    canvas.globals["sys.query"] = "runtime"
    assigner = canvas.get_component_obj("VariableAssigner:test")
    assert isinstance(assigner, VariableAssigner)
    assigner._append(canvas.globals["env.items"], "{sys.query}")
    assigner._append(canvas.globals["env.object"]["nested"], "{sys.query}")
    assert canvas.globals["env.items"] == ["seed", "runtime"] and canvas.globals["env.object"]["nested"] == ["seed", "runtime"]
    stored = json.loads(str(canvas))
    assert stored["variables"] == definitions
    continued = Canvas(json.dumps(stored))
    assert continued.globals["env.items"] == ["seed", "runtime"]
    continued.reset()
    assert continued.globals["env.items"] == ["seed"] and continued.globals["env.object"] == {"nested": ["seed"]}
    assert json.loads(str(continued))["variables"] == definitions
