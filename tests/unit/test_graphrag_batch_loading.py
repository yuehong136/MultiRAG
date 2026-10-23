"""GraphRAG batching and lazy reads retain checkpoint and cancellation contracts."""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, Mock

import networkx as nx
import pytest
from pydantic import ValidationError

from api.utils.api_utils import get_parser_config
from api.utils.validation_utils import GraphragConfig
from common.exceptions import TaskCanceledException
from core.graphrag.general import index


def _graph(doc_id: str) -> nx.Graph:
    graph = nx.Graph()
    graph.add_node("entity", description="entity", source_id=[doc_id])
    graph.graph["source_id"] = [doc_id]
    return graph


@pytest.fixture
def batch_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(index, "has_canceled", lambda *_: False)
    monkeypatch.setattr(index, "get_graph", AsyncMock(return_value=None))
    monkeypatch.setattr(index, "load_subgraph_from_store", AsyncMock(return_value=None))
    monkeypatch.setattr(index, "clear_phase_markers", lambda *_: None)
    monkeypatch.setattr(index, "merge_subgraph", AsyncMock(side_effect=lambda _t, _k, _d, graph, *_: graph))
    generated = AsyncMock(side_effect=lambda _e, _t, _k, doc_id, *_a, **_kw: _graph(doc_id))
    monkeypatch.setattr(index, "generate_subgraph", generated)
    return {"generated": generated, "row": {"tenant_id": "owner", "kb_id": "kb", "id": "task"}}


async def _run(env: dict[str, Any], docs: list[str], config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return await index._run_graphrag_for_kb(env["row"], docs, "English", {"graphrag": config}, None, None, lambda **_: None, with_resolution=False, with_community=False, **kwargs)


@pytest.mark.parametrize("budget", [512, 4096, 8196])
def test_batch_budget_defaults_bounds_and_roundtrip(budget: int) -> None:
    config = GraphragConfig(batch_chunk_token_size=budget)
    assert GraphragConfig.model_validate(config.model_dump()).batch_chunk_token_size == budget
    assert GraphragConfig().batch_chunk_token_size == 4096
    assert get_parser_config("naive", {})["graphrag"]["batch_chunk_token_size"] == 4096
    assert get_parser_config("naive", {"graphrag": {"batch_chunk_token_size": budget}})["graphrag"]["batch_chunk_token_size"] == budget


@pytest.mark.parametrize("budget", [None, False, True, 0, 511, 8197, "512", 512.5, float("nan"), float("inf")])
async def test_invalid_present_budget_fails_before_source_reads(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, budget: Any) -> None:
    with pytest.raises(ValidationError):
        GraphragConfig(batch_chunk_token_size=budget)
    read = Mock(side_effect=AssertionError("invalid budget caused a source read"))
    monkeypatch.setattr(index, "load_graph_source_chunks", read)
    with pytest.raises(ValueError, match="batch_chunk_token_size"):
        await _run(batch_env, ["doc"], {"batch_chunk_token_size": budget})
    read.assert_not_called()


@pytest.mark.parametrize(("method", "budget", "expected"), [("light", 512, 2), ("general", 4096, 1), ("ner", 4096, 2)])
async def test_worker_uses_nondefault_budget_and_ner_keeps_source_boundaries(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, budget: int, expected: int) -> None:
    source = [" alpha" * 300, " beta" * 300]
    monkeypatch.setattr(index, "load_graph_source_chunks", lambda *_: source)
    result = await _run(batch_env, ["doc"], {"method": method, "batch_chunk_token_size": budget})
    chunks = batch_env["generated"].call_args.args[4]
    assert len(chunks) == result["total_chunks"] == expected
    assert "".join(chunks) == "".join(source)
    if method == "ner":
        assert chunks == source


async def test_checkpoint_or_merged_document_never_reads_source(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index, "get_graph", AsyncMock(return_value=_graph("merged")))
    monkeypatch.setattr(index, "load_subgraph_from_store", AsyncMock(side_effect=lambda _t, _k, doc: _graph(doc)))
    source = AsyncMock(side_effect=AssertionError("resumed document source was fetched"))
    monkeypatch.setattr(index, "load_graph_source_chunks", source)
    result = await _run(batch_env, ["merged", "checkpoint"], {})
    assert result["total_chunks"] == 0 and result["ok_docs"] == ["checkpoint"]
    source.assert_not_called()
    batch_env["generated"].assert_not_awaited()


async def test_source_reads_are_bounded_by_document_semaphore(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    release = threading.Event()
    entered = asyncio.Event()
    active, peak = 0, 0
    lock = threading.Lock()

    def source(*_args: Any) -> list[str]:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(5)
            return ["source"]
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(index, "load_graph_source_chunks", source)
    task = asyncio.create_task(_run(batch_env, ["one", "two", "three", "four"], {}, max_parallel_docs=2))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert peak == 2
    finally:
        release.set()
    result = await task
    assert len(result["ok_docs"]) == 4 and peak == 2 and active == 0


async def test_cancelled_read_settles_and_does_not_generate_or_merge(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = False

    def source(*_args: Any) -> list[str]:
        nonlocal cancelled
        cancelled = True
        return ["source"]

    monkeypatch.setattr(index, "has_canceled", lambda *_: cancelled)
    monkeypatch.setattr(index, "load_graph_source_chunks", source)
    with pytest.raises(TaskCanceledException):
        await _run(batch_env, ["doc"], {})
    batch_env["generated"].assert_not_awaited()
    index.merge_subgraph.assert_not_awaited()


async def test_empty_or_failed_source_is_not_a_completed_subgraph(batch_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index, "load_graph_source_chunks", lambda *_: [])
    assert (await _run(batch_env, ["empty"], {}))["failed_docs"] == ["empty"]

    def fail(*_args: Any) -> Any:
        raise RuntimeError("snapshot read failed")

    monkeypatch.setattr(index, "load_graph_source_chunks", fail)
    with pytest.raises(RuntimeError, match="failed to build"):
        await _run(batch_env, ["failure"], {})
    batch_env["generated"].assert_not_awaited()
