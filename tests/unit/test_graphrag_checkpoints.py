"""Checkpoint failures, resume invalidation, stable reports and merge fidelity."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import networkx as nx
import numpy as np
import pytest

from common import settings
from core.graphrag import persistence, phase_markers, utils
from core.graphrag.entity_resolution import EntityResolution
from core.graphrag.general import index
from core.graphrag.general.extractor import Extractor
from tests.support.graphrag_store import Store


class Lease:
    async def __aenter__(self) -> "Lease":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def check(self) -> None:
        pass


@contextmanager
def fake_db() -> Iterator[object]:
    yield object()


@pytest.fixture
def checkpoint_env(monkeypatch: pytest.MonkeyPatch) -> Store:
    store = Store()
    monkeypatch.setattr(settings, "docStoreConn", store)
    for module in [utils, index]:
        monkeypatch.setattr(module, "db_connection", fake_db)
        monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="scratch"))
    monkeypatch.setattr(index, "GraphRAGLease", lambda *args: Lease())
    monkeypatch.setattr(index, "has_canceled", lambda *args: False)
    monkeypatch.setattr(persistence, "has_canceled", lambda *args: False)
    monkeypatch.setattr(index, "load_graph_source_chunks", lambda *args: ["source"])
    marker_values: dict[str, str] = {}
    monkeypatch.setattr(phase_markers.REDIS_CONN, "get", marker_values.get)
    monkeypatch.setattr(phase_markers.REDIS_CONN, "set", lambda key, value, ttl: marker_values.update({key: value}) or True)
    monkeypatch.setattr(phase_markers.REDIS_CONN, "delete", lambda key: marker_values.pop(key, None))
    return store


def graph(doc: str = "doc") -> nx.Graph:
    result = nx.Graph()
    result.graph["source_id"] = [doc]
    result.add_node("A", entity_type="person", description="A", source_id=[doc])
    result.add_node("B", entity_type="person", description="B", source_id=[doc])
    result.add_edge("A", "B", description="edge", weight=1, keywords=["edge"], source_id=[doc])
    return result


async def seed(store: Store, doc: str = "doc") -> nx.Graph:
    result = graph(doc)
    await utils.set_graph("tenant", "kb", None, result, utils.GraphChange(), None)
    return result


async def test_nullable_fields_are_dispatched_by_graph_row_kind(checkpoint_env: Store) -> None:
    store = checkpoint_env
    null_fields = {"entity_kwd": None, "from_entity_kwd": None, "to_entity_kwd": None, "name_kwd": None}
    entity_id, relation_id = utils.entity_chunk_id("kb", "A"), utils.relation_chunk_id("kb", "A", "B")
    rows = [
        ("old-graph", "graph", {}),
        ("old-subgraph", "subgraph", {"source_id": "doc"}),
        ("foreign-subgraph", "subgraph", {"source_id": "foreign-doc"}),
        (entity_id, "entity", {"entity_kwd": "A"}),
        ("duplicate-entity", "entity", {"entity_kwd": "A"}),
        (relation_id, "relation", {"from_entity_kwd": "A", "to_entity_kwd": "B"}),
        ("duplicate-relation", "relation", {"from_entity_kwd": "A", "to_entity_kwd": "B"}),
        ("removed-entity", "entity", {"entity_kwd": "GONE"}),
        ("removed-relation", "relation", {"from_entity_kwd": "B", "to_entity_kwd": "GONE"}),
    ]
    for pk, kind, fields in rows:
        store.rows[pk] = {**null_fields, "pk": pk, "kb_id": "kb", "knowledge_graph_kwd": kind, **fields}
    preserved = {pk: dict(store.rows[pk]) for pk in [entity_id, relation_id, "foreign-subgraph"]}
    await seed(store)
    assert all(store.rows[pk] == row for pk, row in preserved.items())
    assert not {"old-graph", "old-subgraph", "duplicate-entity", "duplicate-relation", "removed-entity", "removed-relation"} & store.rows.keys()
    assert sum(row["knowledge_graph_kwd"] == "graph" for row in store.rows.values()) == 1


@pytest.mark.parametrize(
    "kind, fields",
    [
        ("entity", {"entity_kwd": None}),
        ("entity", {"entity_kwd": ""}),
        ("relation", {"from_entity_kwd": None, "to_entity_kwd": "B"}),
        ("relation", {"from_entity_kwd": "A", "to_entity_kwd": ""}),
    ],
)
async def test_invalid_identity_in_its_own_graph_kind_is_not_silently_pruned(checkpoint_env: Store, kind: str, fields: dict[str, Any]) -> None:
    store = checkpoint_env
    malformed = {"pk": "malformed", "kb_id": "kb", "knowledge_graph_kwd": kind, **fields}
    store.rows["malformed"] = malformed
    with pytest.raises(ValueError, match="Invalid persisted graph"):
        await seed(store)
    assert store.rows["malformed"] == malformed
    assert "prune" not in store.events


@pytest.mark.parametrize("failure", ["fail_read", "fail_write"])
async def test_failed_checkpoint_preserves_previous_graph_and_does_not_prune(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    store = checkpoint_env
    await seed(store)
    before = dict(store.rows)
    setattr(store, failure, True)
    store.events.clear()

    # Retry delay is irrelevant to this preflight/nontransient failure test.
    async def no_wait(_delay: float) -> None:
        pass

    monkeypatch.setattr(utils.asyncio, "sleep", no_wait)
    with pytest.raises(RuntimeError):
        await seed(store, "new-doc")
    assert "prune" not in store.events
    assert store.rows == before


async def test_resume_loads_graph_skips_merged_docs_and_invalidates_after_new_content(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store = checkpoint_env
    await seed(store)
    resolution = AsyncMock(side_effect=lambda *args, **kwargs: None)
    community = AsyncMock(side_effect=lambda *args, **kwargs: None)
    monkeypatch.setattr(index, "resolve_entities", resolution)
    monkeypatch.setattr(index, "extract_community", community)
    build = AsyncMock(return_value=graph("new-doc"))
    monkeypatch.setattr(index, "generate_subgraph", build)
    row = {"tenant_id": "tenant", "kb_id": "kb", "id": "task"}

    async def run(docs: list[str]) -> dict[str, Any]:
        return await index.run_graphrag_for_kb(row, docs, "English", {}, None, SimpleNamespace(llm_name="scratch", encode=lambda texts: (np.ones((len(texts), 2)), 0)), lambda **kwargs: None)

    await run(["doc"])
    assert resolution.await_count == community.await_count == 1
    assert resolution.call_args.args[1] == {"A", "B"}
    build.assert_not_awaited()
    await run(["doc"])
    assert resolution.await_count == community.await_count == 1
    # A Redis clear failure cannot make a marker from the previous graph valid.
    monkeypatch.setattr(index, "clear_phase_markers", lambda *args: None)
    monkeypatch.setattr(utils, "clear_phase_markers", lambda *args: None)
    await run(["doc", "new-doc"])
    assert resolution.await_count == community.await_count == 2
    assert build.await_count == 1
    persisted = await utils.get_graph("tenant", "kb")
    assert persisted is not None and set(persisted.graph["source_id"]) == {"doc", "new-doc"}


async def test_phase_failure_never_sets_completion_marker(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    persisted = await seed(checkpoint_env)
    monkeypatch.setattr(index, "resolve_entities", AsyncMock(side_effect=RuntimeError("phase failed")))
    with pytest.raises(RuntimeError, match="phase failed"):
        await index.run_graphrag_for_kb({"tenant_id": "tenant", "kb_id": "kb", "id": "task"}, ["doc"], "English", {}, None, None, lambda **kwargs: None)
    assert not phase_markers.has_phase_marker("kb", phase_markers.PHASE_RESOLUTION, index.phase_revision(persisted, {}))


async def test_community_ids_are_stable_and_query_or_prune_failure_propagates(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store = checkpoint_env
    report = SimpleNamespace(structured_output=[{"title": "title", "findings": [], "weight": 1, "entities": ["A", "B"]}], output=["report"])
    extractor = SimpleNamespace()

    class Reports:
        async def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return report

    monkeypatch.setattr(index, "CommunityReportsExtractor", lambda *args: Reports())

    async def run() -> None:
        await index.extract_community.__wrapped__(graph(), "tenant", "kb", None, extractor, None, lambda **kwargs: None)

    await run()
    identifiers = set(store.rows)
    await run()
    assert set(store.rows) == identifiers
    store.fail_read = True
    store.events.clear()
    with pytest.raises(RuntimeError, match="snapshot"):
        await run()
    assert not store.events
    store.fail_read = False
    store.rows["stale"] = {"pk": "stale", "kb_id": "kb", "knowledge_graph_kwd": "community_report", "content_with_weight": "old"}
    store.fail_delete = True
    with pytest.raises(RuntimeError, match="prune"):
        await run()
    assert "stale" in store.rows


async def test_redirected_common_neighbour_merges_every_edge_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    ext = Extractor.__new__(Extractor)
    ext._handle_entity_relation_summary = AsyncMock(side_effect=lambda name, description, **kwargs: description)
    value = nx.Graph()
    for node in ["A", "B", "C", "X"]:
        value.add_node(node, description=node, source_id=[node])
    for node in ["B", "C"]:
        value.add_edge(node, "X", description=node, weight=1, keywords=[node], source_id=[node])
    change = utils.GraphChange()
    await ext._merge_graph_nodes(value, ["A", "B", "C"], change)
    edge = value.edges["A", "X"]
    assert edge["weight"] == 2 and edge["source_id"] == ["B", "C"] and edge["keywords"] == ["B", "C"]
    assert edge["description"] == "B<SEP>C"
    assert ("A", "X") in change.added_updated_edges


async def test_resolution_model_failure_is_not_a_completed_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution = EntityResolution.__new__(EntityResolution)
    resolution._resolution_prompt = "{input_text}"
    resolution._record_delimiter_key = "record_delimiter"
    resolution._entity_index_delimiter_key = "entity_index_delimiter"
    resolution._resolution_result_delimiter_key = "resolution_result_delimiter"
    resolution._input_text_key = "input_text"
    resolution._async_chat = AsyncMock(side_effect=RuntimeError("provider failed"))
    resolution.is_similarity = lambda *args: True
    with pytest.raises(RuntimeError, match="provider failed"):
        await resolution(graph(), {"A"}, callback=lambda **kwargs: None)


def test_marker_redis_failure_and_revision_mismatch_are_cache_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(phase_markers.REDIS_CONN, "get", lambda key: b"old")
    assert not phase_markers.has_phase_marker("kb", phase_markers.PHASE_COMMUNITY, "new")

    def fail(*args: Any) -> Any:
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(phase_markers.REDIS_CONN, "get", fail)
    monkeypatch.setattr(phase_markers.REDIS_CONN, "set", fail)
    assert not phase_markers.has_phase_marker("kb", phase_markers.PHASE_COMMUNITY, "new")
    assert not phase_markers.set_phase_marker("kb", phase_markers.PHASE_COMMUNITY, "new")


async def test_resume_can_finish_phases_without_available_source_chunks(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed(checkpoint_env)
    monkeypatch.setattr(index, "load_graph_source_chunks", lambda *args: [])
    resolution, community = AsyncMock(), AsyncMock()
    monkeypatch.setattr(index, "resolve_entities", resolution)
    monkeypatch.setattr(index, "extract_community", community)
    await index.run_graphrag_for_kb({"tenant_id": "tenant", "kb_id": "kb", "id": "task"}, ["doc"], "English", {}, None, None, lambda **kwargs: None)
    resolution.assert_awaited_once()
    community.assert_awaited_once()


async def test_schema_preflight_rejects_lossy_checkpoint_before_any_write(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store = checkpoint_env
    await seed(store)
    previous = dict(store.rows)
    store.events.clear()
    monkeypatch.setattr(store, "describe_collection", lambda *args: {"fields": [{"name": "content_with_weight", "params": {"max_length": 8}}]})
    with pytest.raises(ValueError, match="exceeds"):
        await seed(store, "new-doc")
    assert store.rows == previous and not store.events


async def test_resolution_serializes_actual_overlapping_neighbourhood_merges() -> None:
    resolver = EntityResolution.__new__(EntityResolution)
    resolver._resolution_prompt = "{input_text}"
    resolver._record_delimiter_key = "record_delimiter"
    resolver._entity_index_delimiter_key = "entity_index_delimiter"
    resolver._resolution_result_delimiter_key = "resolution_result_delimiter"
    resolver._input_text_key = "input_text"
    resolver.is_similarity = lambda *args: True
    resolver._async_chat = AsyncMock(return_value="<|>1<|>&&Yes&&")
    active, peak = 0, 0

    async def summary(name: str, description: str, **kwargs: Any) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return description

    resolver._handle_entity_relation_summary = summary
    value = nx.Graph()
    for node in ["A1", "A2", "B1", "B2", "X"]:
        value.add_node(node, description=node, source_id=[node], entity_type="a" if node.startswith("A") else "b" if node.startswith("B") else "x")
    for node in ["A1", "A2", "B1", "B2"]:
        value.add_edge(node, "X", description=node, weight=1, source_id=[node], keywords=[node])
    result = await resolver(value, set(value.nodes()), callback=lambda **kwargs: None)
    assert peak == 1 and len(result.change.removed_nodes) == 2
    assert len(value.nodes()) == 3 and all(edge["weight"] == 2 for _, _, edge in value.edges(data=True))


async def test_invalidated_graph_rebuild_query_failure_keeps_checkpoint(checkpoint_env: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    store = checkpoint_env
    await seed(store)
    graph_row = next(row for row in store.rows.values() if row["knowledge_graph_kwd"] == "graph")
    graph_row["removed_kwd"] = "Y"
    original = store.graph_rows

    def fail_subgraphs(name: str, kb: str, kinds: list[str]) -> list[dict[str, Any]]:
        if kinds == ["subgraph"]:
            raise RuntimeError("checkpoint inventory unavailable")
        return original(name, kb, kinds)

    monkeypatch.setattr(store, "graph_rows", fail_subgraphs)
    before = dict(store.rows)
    store.events.clear()
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await utils.get_graph("tenant", "kb")
    assert store.rows == before and not store.events


async def test_rebuild_prefers_stable_checkpoint_over_legacy_rows_in_any_order(checkpoint_env: Store) -> None:
    store = checkpoint_env
    await seed(store)
    stable = next(row for row in store.rows.values() if row["knowledge_graph_kwd"] == "subgraph")
    store.rows = {"old-a": {**stable, "pk": "old-a", "content_with_weight": "bad legacy JSON"}, "old-b": {**stable, "pk": "old-b", "content_with_weight": "bad legacy JSON"}, **store.rows}
    rebuilt = await utils.rebuild_graph("tenant", "kb")
    assert rebuilt is not None and set(rebuilt.nodes()) == {"A", "B"}
    assert rebuilt.graph["source_id"] == ["doc"]
