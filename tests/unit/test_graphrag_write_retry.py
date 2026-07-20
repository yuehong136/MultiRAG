"""GraphRAG doc-store write retry regression tests."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import networkx as nx
import pytest

from common import settings
from core.graphrag import utils as graphrag_utils


class _FlakyDocStore:
    def __init__(self) -> None:
        self.edge_delete_attempts = 0
        self.insert_attempts = 0

    def delete(self, condition: dict[str, Any], _index_name: str, _kb_id: str) -> None:
        if condition.get("knowledge_graph_kwd") == ["relation"]:
            self.edge_delete_attempts += 1
            if self.edge_delete_attempts < 3:
                raise RuntimeError("TOO_MANY_CONNECTIONS")

    def insert(self, _chunks: list[dict[str, Any]], _index_name: str, _kb_id: str) -> None:
        self.insert_attempts += 1
        if self.insert_attempts < 3:
            raise RuntimeError("TOO_MANY_CONNECTIONS")


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
    monkeypatch.setattr(graphrag_utils.asyncio, "sleep", record_sleep)

    graph = nx.Graph()
    graph.graph["source_id"] = []
    change = graphrag_utils.GraphChange(removed_edges={("entity-a", "entity-b")})

    await graphrag_utils.set_graph("tenant-1", "kb-1", None, graph, change, None)

    assert store.edge_delete_attempts == 3
    assert store.insert_attempts == 3
    assert waits == [1, 2, 1, 2]
