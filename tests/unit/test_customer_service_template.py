"""Run the shipped customer-service graph at controlled model/store boundaries."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import agent_with_tools, categorize, llm
from agent.tools.retrieval import Retrieval
from core.utils.redis_conn import REDIS_CONN

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "agent/templates/smart_customer_service_specialist.json"
AGGREGATOR = "VariableAggregator:GentleLawsSneeze"
MESSAGE = "Message:KhakiSymbolsMarry"
CATEGORIES = {
    "Product Feature Comparison": "Agent:PlentyCandiesRefuse",
    "Product Usage Guide": "Agent:ShinyCooksCall",
    "Book Installation": "Agent:DeepCoatsDress",
}


def template_dsl() -> dict[str, Any]:
    return json.loads(TEMPLATE.read_text())["dsl"]


def test_template_graph_and_runtime_share_selectors_and_require_local_binding() -> None:
    dsl = template_dsl()
    nodes = {node["id"]: node for node in dsl["graph"]["nodes"]}
    edges = {(edge["source"], edge["target"]) for edge in dsl["graph"]["edges"]}
    runtime_edges = set()
    for identifier, component in dsl["components"].items():
        params = component["obj"]["params"]
        form = nodes[identifier]["data"]["form"]
        for key in ("llm_id", "kb_ids", "groups", "content", "meta_data_filter", "top_k"):
            if key in params:
                assert form[key] == params[key]
        runtime_edges.update((identifier, target) for target in component["downstream"])
        if "llm_id" in params:
            assert params["llm_id"] == ""
        if "kb_ids" in params:
            assert params["kb_ids"] == []
    assert runtime_edges == edges
    assert dsl["components"][MESSAGE]["obj"]["params"]["content"] == [f"{{{AGGREGATOR}@answer}}"]
    assert dsl["components"][MESSAGE]["upstream"] == [AGGREGATOR]
    assert set(dsl["components"][AGGREGATOR]["upstream"]) == set(CATEGORIES.values())
    with pytest.raises(ValueError, match=r"\[Agent\] LLM"):
        Canvas(json.dumps(dsl), tenant_id="template-owner")


def test_active_template_loader_reads_the_same_graph(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    from api.db import init_data

    loaded: list[dict[str, Any]] = []
    monkeypatch.setattr(init_data.os, "listdir", lambda path: [TEMPLATE.name])
    monkeypatch.setattr(init_data.CanvasTemplateService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(init_data.CanvasTemplateService, "get_by_id", lambda *args: None)
    monkeypatch.setattr(init_data.CanvasTemplateService, "save", lambda session, **row: loaded.append(row))
    init_data.add_graph_templates(db)
    assert len(loaded) == 1 and loaded[0]["dsl"] == template_dsl()


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, db: Session) -> Iterator[tuple[list[Canvas], list[tuple[str, str, Any]]]]:
    calls: list[tuple[str, str, Any]] = []

    class Model:
        max_length = 16000

        async def async_chat(self, system: str, history: list[dict[str, Any]], config: dict[str, Any], **kwargs: Any) -> str:
            query = history[-1]["content"]
            if "classification system" in system:
                # Categorize includes older turns in its prompt. Classify the
                # current user entry, so this fake cannot hide a wrong branch.
                return query.rsplit('USER: "', 1)[-1].split('"', 1)[0]
            category = "Product Feature Comparison" if "Specification Comparison Agent" in system else "Product Usage Guide" if "Usage Guide Agent" in system else "Book Installation"
            calls.append(("answer", category, query))
            if "provider failure" in query:
                return "**ERROR** controlled provider failure"
            return "answer:" + category

    @contextmanager
    def model_db() -> Iterator[Session]:
        yield db

    def resolve(session: Session, tenant_id: str, model_ref: str) -> list[str]:
        assert session is db and tenant_id == "template-owner" and model_ref == "314"
        calls.append(("model", tenant_id, model_ref))
        return ["chat"]

    async def classify_model(tenant_id: str, model_type: str, model_ref: str) -> Model:
        assert (tenant_id, model_type, model_ref) == ("template-owner", "chat", "314")
        return Model()

    async def retrieve(self: Retrieval, query: str) -> None:
        assert self._canvas.get_tenant_id() == "template-owner"
        assert self._dataset_ids == ["selected-" + self._id]
        calls.append(("retrieval", self._id, list(self._dataset_ids)))
        self.set_output("formalized_content", "schema:" + self._id)

    for module in (llm, agent_with_tools):
        monkeypatch.setattr(module, "db_connection", model_db)
        monkeypatch.setattr(module, "resolve_model_type", resolve)
        monkeypatch.setattr(module, "get_model_config_by_type_and_name", lambda *args: object())
        monkeypatch.setattr(module, "LLMBundle", lambda *args, **kwargs: Model())
    monkeypatch.setattr(categorize, "build_named_bundle_async", classify_model)
    monkeypatch.setattr(Retrieval, "_retrieve_kb", retrieve)
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    canvases: list[Canvas] = []
    yield canvases, calls
    for canvas in canvases:
        canvas._thread_pool.shutdown(wait=True)


def load(canvases: list[Canvas], source: dict[str, Any] | None = None) -> Canvas:
    dsl = deepcopy(source) if source is not None else template_dsl()
    for identifier, component in dsl["components"].items():
        params = component["obj"]["params"]
        if "llm_id" in params:
            params["llm_id"] = "314"
        if "kb_ids" in params:
            params["kb_ids"] = ["selected-" + identifier]
    canvas = Canvas(json.dumps(dsl), tenant_id="template-owner")
    canvases.append(canvas)
    return canvas


async def test_real_graph_selects_each_branch_and_clears_prior_turn_answers(runtime: tuple[list[Canvas], list[tuple[str, str, Any]]]) -> None:
    canvases, calls = runtime
    canvas = load(canvases)
    for category, branch in CATEGORIES.items():
        calls.clear()
        events = [event async for event in canvas.run(query=category)]
        answer = "".join(event["data"]["content"] for event in events if event["event"] == "message")
        assert events[-1]["event"] == "workflow_finished" and not canvas.error
        assert answer == "answer:" + category
        assert canvas.get_variable_value(AGGREGATOR + "@answer") == answer
        assert canvas.get_variable_value(branch + "@content") == answer
        assert [call[1] for call in calls if call[0] == "answer"] == [category]
        retrievals = [call for call in calls if call[0] == "retrieval"]
        assert len(retrievals) == (0 if category == "Book Installation" else 1)
        if retrievals:
            prompt = next(call[2] for call in calls if call[0] == "answer")
            assert "schema:" + retrievals[0][1] in prompt
        canvas = load(canvases, json.loads(str(canvas)))


async def test_branch_failure_does_not_replay_another_branch_answer(runtime: tuple[list[Canvas], list[tuple[str, str, Any]]]) -> None:
    canvases, _ = runtime
    canvas = load(canvases)
    [event async for event in canvas.run(query="Product Feature Comparison")]
    canvas = load(canvases, json.loads(str(canvas)))
    events = [event async for event in canvas.run(query="Product Usage Guide provider failure")]
    assert "controlled provider failure" in canvas.error
    assert not any(event["event"] in {"message", "workflow_finished"} for event in events)
    assert canvas.get_variable_value(AGGREGATOR + "@answer") is None
