"""GraphRAG retries checked, replay-safe upserts before pruning."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import networkx as nx
import pytest

from common import settings
from core.graphrag import utils as graphrag_utils
from tests.support.graphrag_store import Store


class _FlakyDocStore(Store):
    def __init__(self) -> None:
        super().__init__()
        self.rows["edge-old"] = {"pk": "edge-old", "knowledge_graph_kwd": "relation", "from_entity_kwd": "entity-a", "to_entity_kwd": "entity-b", "kb_id": "kb-1"}
        self.edge_delete_attempts = 0
        self.insert_attempts = 0

    def graph_delete_rows(self, _index_name: str, _kb_id: str, ids: list[str]) -> None:
        self.edge_delete_attempts += 1
        for cid in ids:
            self.rows.pop(cid, None)

    def insert(self, chunks: list[dict[str, Any]], index_name: str, kb_id: str) -> list[str]:
        self.insert_attempts += 1
        if self.insert_attempts < 3:
            raise RuntimeError("TOO_MANY_CONNECTIONS")
        return super().insert(chunks, index_name, kb_id)


@contextmanager
def _db_connection() -> Iterator[SimpleNamespace]:
    yield SimpleNamespace()


async def test_set_graph_retries_transient_edge_deletes_and_batch_inserts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FlakyDocStore()
    waits: list[int] = []

    async def run_in_thread(function: Callable[..., Any], *args: Any) -> Any:
        return function(*args)

    async def record_sleep(delay: int) -> None:
        waits.append(delay)

    monkeypatch.setattr(settings, "docStoreConn", store)
    monkeypatch.setattr(graphrag_utils, "db_connection", _db_connection)
    monkeypatch.setattr(graphrag_utils.KnowledgebaseService, "get_by_id", lambda _db, _kb_id: SimpleNamespace(name="kb-name"))
    monkeypatch.setattr(graphrag_utils, "thread_pool_exec", run_in_thread)
    monkeypatch.setattr(graphrag_utils, "clear_phase_markers", lambda *args: None)
    monkeypatch.setattr(graphrag_utils.asyncio, "sleep", record_sleep)

    graph = nx.Graph()
    graph.graph["source_id"] = []
    change = graphrag_utils.GraphChange(removed_edges={("entity-a", "entity-b")})

    await graphrag_utils.set_graph("tenant-1", "kb-1", None, graph, change, None)

    assert store.edge_delete_attempts == 1
    assert store.insert_attempts == 3
    assert waits == [1, 2]
