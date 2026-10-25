"""Manual metadata expressions survive repeated calls and real Iteration scheduling."""

import json
from contextlib import nullcontext
from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.tools import retrieval as module
from agent.tools.retrieval import Retrieval, RetrievalParam


def _boundaries(monkeypatch: pytest.MonkeyPatch, db: Session, canvas: Canvas, tool: Retrieval) -> AsyncMock:
    kb = SimpleNamespace(id="dataset", tenant_id="tenant", name="Dataset", embd_id="embedding", tenant_embd_id=None)
    monkeypatch.setattr(canvas, "is_canceled", lambda: False)
    monkeypatch.setattr(tool, "_resolve_kbs", lambda: ([kb.id], [kb.id], [kb]))
    monkeypatch.setattr(tool, "_rank_feature", lambda *_: {})
    monkeypatch.setattr(module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(module, "build_named_bundle_async", AsyncMock(return_value=object()))
    recalled = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setitem(vars(module.settings), "retriever", SimpleNamespace(retrieval=recalled, retrieval_by_children=lambda chunks, _: chunks))
    return recalled


async def test_real_iteration_resolves_each_value_without_rewriting_expression(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    def node(name: str, params: dict[str, Any] | None = None, *, parent: str | None = None, upstream: list[str] | None = None, downstream: list[str] | None = None) -> dict[str, Any]:
        return {"obj": {"component_name": name, "params": params or {}}, "parent_id": parent, "upstream": upstream or [], "downstream": downstream or []}

    config = {"method": "manual", "manual": [{"key": "tag", "op": "in", "value": "{IterationItem:outer@item}"}]}
    components = {
        "begin": node("Begin", downstream=["Iteration:outer"]),
        "Iteration:outer": node("Iteration", {"items_ref": "env.items"}, upstream=["begin"], downstream=["Message:done"]),
        "IterationItem:outer": node("IterationItem", parent="Iteration:outer", downstream=["Retrieval:child"]),
        "Retrieval:child": node("Retrieval", {"query": "query", "dataset_ids": ["dataset"], "meta_data_filter": config}, parent="Iteration:outer", upstream=["IterationItem:outer"]),
        "Message:done": node("Message", {"content": ["done"], "stream": False}, upstream=["Iteration:outer"]),
    }
    globals_ = {"env.items": ["Alice", "Bob"], "sys.query": "", "sys.user_id": "", "sys.conversation_turns": 0, "sys.files": [], "sys.history": []}
    canvas = Canvas(json.dumps({"components": components, "globals": globals_, "retrieval": [], "path": []}), tenant_id="tenant")
    tool = canvas.get_component_obj("Retrieval:child")
    recalled = _boundaries(monkeypatch, db, canvas, tool)
    filters: list[dict[str, Any]] = []

    def select_ids(session: Session, kb_ids: list[str], conditions: list[dict[str, Any]], logic: str) -> list[str]:
        filters.extend(deepcopy(conditions))
        return [conditions[0]["value"]]

    monkeypatch.setattr(module.DocMetadataService, "filter_doc_ids_by_meta_pushdown", select_ids)
    monkeypatch.setattr(canvas.get_component_obj("Message:done"), "_save_to_memory", lambda *_: None)
    try:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        assert len([event for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Retrieval:child"]) == 2
        assert [flt["value"] for flt in filters] == ["Alice", "Bob"]
        assert [call.kwargs["doc_ids"] for call in recalled.call_args_list] == [["Alice"], ["Bob"]]
        assert tool._param.meta_data_filter == config
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0, 0),
        (False, False),
        ([], []),
        ([0, False], [0, False]),
        ({"typed": [0, False]}, {"typed": [0, False]}),
        ("prefix {env.current} suffix", "prefix AliceBob suffix"),
        ("prefix {{env.current}} suffix", "prefix AliceBob suffix"),
        ("prefix { {env.current} } suffix", "prefix AliceBob suffix"),
        ("{env.current}", "AliceBob"),
        ("{env.current} {env.current}", "AliceBob AliceBob"),
        ("{env.current}{env.current}", "AliceBobAliceBob"),
    ],
)
async def test_resolver_returns_a_new_mapping_and_preserves_typed_constants(monkeypatch: pytest.MonkeyPatch, db: Session, value: Any, expected: Any) -> None:
    canvas = Canvas(json.dumps({"components": {}, "retrieval": []}), tenant_id="tenant")
    canvas.globals["env.current"] = partial(lambda: iter(["Alice", "Bob"]))
    param = RetrievalParam()
    param.dataset_ids = ["dataset"]
    param.meta_data_filter = {"method": "manual", "manual": [{"key": "tag", "op": "in", "value": value}]}
    tool = Retrieval(canvas, "Retrieval:manual", param)
    _boundaries(monkeypatch, db, canvas, tool)
    before = deepcopy(param.meta_data_filter)

    async def inspect_resolver(config: dict[str, Any], metas: Any, query: str, model: Any, selected: list[str] | None, resolver: Any, **kwargs: Any) -> list[str]:
        original = config["manual"][0]
        result = resolver(original)
        assert result is not original
        assert config == before
        assert result["value"] == expected
        assert type(result["value"]) is type(expected)
        return ["-999"]

    monkeypatch.setattr(module, "apply_meta_data_filter", inspect_resolver)
    try:
        await tool._retrieve_kb("query")
        assert param.meta_data_filter == before
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize(("pushed", "expected", "loads"), [(None, ["fallback"], 1), ([], ["-999"], 0)])
async def test_unsupported_pushdown_and_authoritative_no_match_stay_distinct(monkeypatch: pytest.MonkeyPatch, db: Session, pushed: list[str] | None, expected: list[str], loads: int) -> None:
    canvas = Canvas(json.dumps({"components": {}, "retrieval": []}), tenant_id="tenant")
    canvas.globals["env.current"] = "Alice"
    param = RetrievalParam()
    param.dataset_ids = ["dataset"]
    param.meta_data_filter = {"method": "manual", "manual": [{"key": "tag", "op": "in", "value": "{env.current}"}]}
    tool = Retrieval(canvas, "Retrieval:manual", param)
    recalled = _boundaries(monkeypatch, db, canvas, tool)
    loader_calls: list[list[str]] = []

    def metadata(kb_ids: list[str]) -> dict[str, Any]:
        loader_calls.append(kb_ids)
        return {"tag": {"Alice": ["fallback"]}}

    monkeypatch.setattr(tool, "_flatted_metas", metadata)
    monkeypatch.setattr(module.DocMetadataService, "filter_doc_ids_by_meta_pushdown", lambda *_: pushed)
    try:
        await tool._retrieve_kb("query")
        assert recalled.call_args.kwargs["doc_ids"] == expected
        assert len(loader_calls) == loads
        assert param.meta_data_filter["manual"][0]["value"] == "{env.current}"
    finally:
        canvas._thread_pool.shutdown(wait=True)
