"""Agent retrieval preserves recall limits and explicit keyword-weight boundaries."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.canvas import Canvas
from agent.tools import retrieval as module
from agent.tools.retrieval import Retrieval, RetrievalParam


@pytest.mark.parametrize("top_k", [None, 1, 37, 2048])
@pytest.mark.parametrize("keyword_weight", [0, 0.23, 1])
async def test_canvas_tool_passes_recall_limit_and_complementary_vector_weight(monkeypatch: pytest.MonkeyPatch, top_k: int | None, keyword_weight: float) -> None:
    canvas = Canvas(json.dumps({"components": {}, "retrieval": []}), tenant_id="actor")
    monkeypatch.setattr(canvas, "is_canceled", lambda: False)
    param = RetrievalParam()
    param.dataset_ids = ["dataset"]
    param.top_n = 3
    param.keywords_similarity_weight = keyword_weight
    if top_k is not None:
        param.top_k = top_k
    tool = Retrieval(canvas, "Retrieval:limit", param)
    kb = SimpleNamespace(id="dataset", tenant_id="owner", name="Dataset", embd_id="embedding", tenant_embd_id=None)
    monkeypatch.setattr(tool, "_resolve_kbs", lambda: (["dataset"], ["dataset"], [kb]))
    monkeypatch.setattr(tool, "_rank_feature", lambda *_: {})
    bundle = object()
    monkeypatch.setattr(module, "build_named_bundle_async", AsyncMock(return_value=bundle))
    fetch = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setattr(module.settings, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, _: chunks))
    try:
        await tool.invoke_async(query="user: recall this")
        assert not tool.error()
        fetch.assert_awaited_once()
        assert fetch.call_args.args == ("recall this", "", bundle, ["owner"], ["Dataset"], 1, 3, 0.2, 1 - keyword_weight)
        assert fetch.call_args.kwargs["top"] == (1024 if top_k is None else top_k)
        assert fetch.call_args.kwargs["kb_ids"] == ["dataset"]
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize("query", ["", None])
async def test_empty_query_does_not_recall(monkeypatch: pytest.MonkeyPatch, query: str | None) -> None:
    canvas = Canvas(json.dumps({"components": {}, "retrieval": []}), tenant_id="actor")
    monkeypatch.setattr(canvas, "is_canceled", lambda: False)
    param = RetrievalParam()
    param.dataset_ids = ["dataset"]
    param.top_k = 37
    param.empty_response = "No query"
    tool = Retrieval(canvas, "Retrieval:empty", param)
    resolve = AsyncMock(side_effect=AssertionError("Empty queries must not resolve datasets"))
    monkeypatch.setattr(tool, "_retrieve_kb", resolve)
    try:
        await tool.invoke_async(query=query)
        assert tool.output("formalized_content") == "No query"
        resolve.assert_not_called()
    finally:
        canvas._thread_pool.shutdown(wait=True)
