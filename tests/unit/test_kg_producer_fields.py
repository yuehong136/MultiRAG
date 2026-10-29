"""Index-time graph scores and N-hop paths survive the actual producer chain."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import networkx as nx
import pytest

from common import settings
from common.doc_store.doc_store_base import DocStoreConnection
from core.graphrag import search, utils
from tests.support.graphrag_store import Store


def _graph() -> nx.Graph:
    graph = nx.Graph()
    graph.graph["source_id"] = ["document"]
    for name in ["A", "B", "C"]:
        graph.add_node(name, entity_type="person", description=name, source_id=["document"])
    # Reverse insertion order checks undirected lookup and output stability.
    graph.add_edge("B", "A", weight=3, description="A knows B", keywords=["knows"], source_id=["document"])
    graph.add_edge("C", "B", weight=7, description="B knows C", keywords=["knows"], source_id=["document"])
    nx.set_node_attributes(graph, nx.pagerank(graph), "pagerank")
    return graph


@pytest.mark.parametrize(
    "hops, expected", [(0, []), (1, [{"path": ("A", "B"), "weights": [3.0]}]), (2, [{"path": ("A", "B", "C"), "weights": [3.0, 7.0]}]), (3, [{"path": ("A", "B", "C"), "weights": [3.0, 7.0]}])]
)
def test_nhop_paths_keep_one_weight_per_edge(hops: int, expected: list[dict[str, Any]]) -> None:
    graph = _graph()
    before = deepcopy(nx.node_link_data(graph, edges="edges"))
    assert utils.n_neighbor(graph, "A", hops) == expected
    assert nx.node_link_data(graph, edges="edges") == before


def test_isolated_missing_weight_and_insertion_order() -> None:
    graph = nx.Graph()
    graph.add_node("alone")
    graph.add_edges_from([("D", "B"), ("C", "B"), ("B", "A")])
    assert utils.n_neighbor(graph, "alone") == []
    expected = [{"path": ("A", "B", "C"), "weights": [0.0, 0.0]}, {"path": ("A", "B", "D"), "weights": [0.0, 0.0]}]
    assert utils.n_neighbor(graph, "A") == expected
    assert utils.n_neighbor(nx.Graph(reversed(list(graph.edges()))), "A") == expected


@pytest.fixture
def embedding_boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    model = SimpleNamespace(llm_name="kg-producer-unit", encode=Mock(return_value=([[0.25, 0.75]], 0)))
    monkeypatch.setattr(utils, "get_embed_cache", lambda *_: None)
    monkeypatch.setattr(utils, "set_embed_cache", lambda *_: None)
    return model


@pytest.mark.parametrize("rank", [0, None, 0.25])
async def test_entity_serializes_real_rank_and_paths(embedding_boundary: SimpleNamespace, rank: float | None) -> None:
    graph = _graph()
    meta = {**graph.nodes["A"], "pagerank": rank}
    before = deepcopy(meta)
    chunks: list[dict[str, Any]] = []
    await utils.graph_node_to_chunk("kb", embedding_boundary, "A", meta, chunks, utils.n_neighbor(graph, "A"))
    row = json.loads(json.dumps(chunks, allow_nan=False))[0]
    assert row["rank_flt"] == (rank or 0)
    assert json.loads(row["n_hop_with_weight"]) == [{"path": ["A", "B", "C"], "weights": [3.0, 7.0]}]
    assert row["id"] == utils.entity_chunk_id("kb", "A")
    assert row["source_id"] == ["document"] and row["kb_id"] == "kb" and row["available_int"] == 0
    assert row["q_2_vec"] == [0.25, 0.75] and meta == before


async def test_legacy_entity_defaults_to_zero_rank_and_json_empty_paths(embedding_boundary: SimpleNamespace) -> None:
    chunks: list[dict[str, Any]] = []
    await utils.graph_node_to_chunk("kb", embedding_boundary, "alone", {"entity_type": "person", "description": "alone", "source_id": ["doc"]}, chunks)
    assert chunks[0]["rank_flt"] == 0 and chunks[0]["n_hop_with_weight"] == "[]"


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
async def test_nonfinite_scores_fail_before_embedding_or_append(embedding_boundary: SimpleNamespace, invalid: float) -> None:
    graph = _graph()
    chunks: list[dict[str, Any]] = []
    with pytest.raises(ValueError, match="pagerank must be finite"):
        await utils.graph_node_to_chunk("kb", embedding_boundary, "A", {**graph.nodes["A"], "pagerank": invalid}, chunks)
    graph.edges["A", "B"]["weight"] = invalid
    with pytest.raises(ValueError, match="edge weight must be finite"):
        utils.n_neighbor(graph, "A")
    embedding_boundary.encode.assert_not_called()
    assert chunks == []


@contextmanager
def _db() -> Iterator[object]:
    yield object()


async def test_set_graph_passes_real_paths_and_persists_before_independent_readback(monkeypatch: pytest.MonkeyPatch, embedding_boundary: SimpleNamespace, tmp_path: Path) -> None:
    store = Store()
    graph = _graph()
    monkeypatch.setattr(settings, "docStoreConn", store)
    monkeypatch.setattr(utils, "db_connection", _db)
    monkeypatch.setattr(utils.KnowledgebaseService, "get_by_id", lambda *_: SimpleNamespace(name="scratch"))
    monkeypatch.setattr(utils, "clear_phase_markers", lambda *_: None)
    change = utils.GraphChange(added_updated_nodes=set(graph.nodes), added_updated_edges=set(graph.edges))
    await utils.set_graph("tenant", "kb", embedding_boundary, graph, change, None)
    # Readback uses the store query API separately from producer output lists.
    actual = store.query("scratch", expr='kb_id == "kb" && pk in ' + json.dumps([utils.entity_chunk_id("kb", "A"), utils.relation_chunk_id("kb", "A", "B")]))
    saved = tmp_path / "actual-graph-rows.json"
    saved.write_text(json.dumps(actual, allow_nan=False))
    entity, relation = json.loads(saved.read_text())
    assert entity["rank_flt"] == graph.nodes["A"]["pagerank"] > 0
    assert json.loads(entity["n_hop_with_weight"]) == [{"path": ["A", "B", "C"], "weights": [3.0, 7.0]}]
    assert relation["weight_int"] == 3
    assert all(row["available_int"] == 0 and row["kb_id"] == "kb" and row["source_id"] == ["document"] for row in actual)
    assert store.events.index("graph") > store.events.index("entity") and store.events.index("graph") > store.events.index("relation")
    assert not any(not task.done() and task is not asyncio.current_task() for task in asyncio.all_tasks())


@pytest.mark.parametrize("reverse", [False, True])
async def test_duplicate_nhop_edges_keep_max_rank_and_accumulate_similarity(monkeypatch: pytest.MonkeyPatch, reverse: bool) -> None:
    retriever = search.KGSearch(Mock(spec=DocStoreConnection, db_type=Mock(return_value="milvus")))
    contributors = [("A", 7), ("D", 3)]
    if reverse:
        contributors.reverse()
    entities = {name: {"sim": 1.0, "pagerank": 1.0, "description": '{"description":"entity"}', "n_hop_ents": [{"path": ["B", "C"], "weights": [weight]}]} for name, weight in contributors}
    monkeypatch.setattr(search, "db_connection", _db)
    monkeypatch.setattr(utils.KnowledgebaseService, "get_by_ids", lambda *_args, **_kwargs: [SimpleNamespace(name="scratch")])
    monkeypatch.setattr(retriever, "query_rewrite", AsyncMock(return_value=([], ["A"])))
    monkeypatch.setattr(retriever, "get_relevant_ents_by_keywords", AsyncMock(return_value=entities))
    monkeypatch.setattr(retriever, "get_relevant_ents_by_types", lambda *_: {})
    monkeypatch.setattr(retriever, "get_relevant_relations_by_txt", AsyncMock(return_value={}))
    monkeypatch.setattr(search, "get_relation", AsyncMock(return_value={"description": "relation"}))
    monkeypatch.setattr(retriever, "_community_retrieval_", lambda *_: "")
    result = await retriever.retrieval("question", "tenant", ["kb"], None, None)
    # Similarity = 1/2 + 1/2, PageRank = max(7, 3), independent of order.
    assert "0,B,C,7.00,relation" in result["content_with_weight"]


async def test_keyword_projection_includes_nhop_and_legacy_empty_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock(
        spec=DocStoreConnection,
        db_type=Mock(return_value="milvus"),
        search=Mock(return_value={"entity": {"entity_kwd": "A", "rank_flt": 0.25, "_score": 1, "n_hop_with_weight": ""}}),
        get_fields=lambda result, _: result,
    )
    retriever = search.KGSearch(store)
    monkeypatch.setattr(retriever, "get_vector", AsyncMock(return_value=object()))
    result = await retriever.get_relevant_ents_by_keywords(["A"], {}, ["index"], ["kb"], None)
    assert "n_hop_with_weight" in store.search.call_args.args[0]
    assert result["A"]["n_hop_ents"] == []
