import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component.variable_assigner import VariableAssigner
from agent.dsl_migration import normalize_chunker_dsl
from api.apps.services.canvas_replica_service import CanvasReplicaService
from core.utils.redis_conn import REDIS_CONN

_TEMPLATE_DIRECTORY = Path(__file__).resolve().parents[2] / "agent" / "templates"
_LIST_VARIABLE_TEMPLATES = [path for path in sorted(_TEMPLATE_DIRECTORY.glob("*.json")) if json.loads(path.read_text())["dsl"].get("variables") == []]


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


@pytest.mark.parametrize("template_path", _LIST_VARIABLE_TEMPLATES, ids=lambda path: path.name)
def test_real_templates_with_legacy_empty_variables_reset(monkeypatch: pytest.MonkeyPatch, db: Session, template_path: Path) -> None:
    from agent.component import agent_with_tools, llm

    class ConfiguredModel:
        def bind_tools(self, session: Any, metadata: list[dict[str, Any]]) -> None:
            assert session is not None and metadata

    @contextmanager
    def model_config_db() -> Iterator[Session]:
        yield db

    # Keep actual Graph/Canvas and every component constructor/check/reset.
    # Only model configuration and provider construction use explicit unit
    # boundaries; no missing-model exception is accepted as a passing reset.
    for module in (llm, agent_with_tools):
        monkeypatch.setattr(module, "db_connection", model_config_db)
        monkeypatch.setattr(module, "get_model_config_by_type_and_name", lambda *args: object())
        monkeypatch.setattr(module, "LLMBundle", lambda *args, **kwargs: ConfiguredModel())
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    source = json.loads(template_path.read_text())["dsl"]
    assert source["variables"] == []
    normalized = CanvasReplicaService.normalize_dsl(normalize_chunker_dsl(source))
    assert normalized["variables"] == []
    canvas = Canvas(json.dumps(normalized), tenant_id="template-unit")
    assert set(canvas.components) == set(normalized["components"])
    for component in canvas.components.values():
        if hasattr(component["obj"], "chat_mdl"):
            assert isinstance(component["obj"].chat_mdl, ConfiguredModel)
    for _ in range(2):
        canvas.reset()
        stored = json.loads(str(canvas))
        assert stored["variables"] == [] and source["variables"] == []
        assert stored["history"] == [] and stored["path"] == [] and stored["globals"]["sys.history"] == []
        assert not any(key.startswith("env.") for key in stored["globals"])
    restored = Canvas(json.dumps(stored), tenant_id="template-unit")
    restored.reset()
    assert json.loads(str(restored))["variables"] == []


@pytest.mark.parametrize("shape", ["missing", "empty_dict", "empty_list"])
@pytest.mark.parametrize("has_orphan", [False, True])
def test_legacy_no_variable_shapes_preserve_dsl_and_clear_orphans(monkeypatch: pytest.MonkeyPatch, shape: str, has_orphan: bool) -> None:
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    dsl = {"components": {}, "path": [], "retrieval": [], "globals": {"env.orphan": ["runtime"]} if has_orphan else {}}
    if shape != "missing":
        dsl["variables"] = [] if shape == "empty_list" else {}
    canvas = Canvas(json.dumps(dsl))
    if has_orphan:
        assert canvas.globals["env.orphan"] == ["runtime"]
    for _ in range(2):
        canvas.reset()
        stored = json.loads(str(canvas))
        assert ("variables" in stored) == (shape != "missing")
        if shape != "missing":
            assert stored["variables"] == dsl["variables"]
        if has_orphan:
            assert stored["globals"]["env.orphan"] == ""
        else:
            assert not any(key.startswith("env.") for key in stored["globals"])


@pytest.mark.parametrize("unsupported", [None, ["nonempty"], "", 0])
def test_unsupported_variable_shapes_are_not_silently_normalized(monkeypatch: pytest.MonkeyPatch, unsupported: Any) -> None:
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    canvas = Canvas(json.dumps({"components": {}, "retrieval": [], "variables": unsupported}))
    with pytest.raises(AttributeError, match="has no attribute 'keys'"):
        canvas.reset()
