"""GraphRAG/RAPTOR 断点续跑（checkpoint resume）钉板测试。

背景：长时 KG/RAPTOR 任务（可达 10+ 小时 LLM 抽取）中断后重跑会重复全部昂贵工作。
- load_subgraph_from_store 在 build_one 抽取前查 doc store 里已持久化的 subgraph
  （generate_subgraph 写入形态：knowledge_graph_kwd="subgraph"、source_id=[doc_id]、
  removed_kwd="N"），命中即复用、跳过 LLM 抽取；
- has_raptor_chunks 在 RAPTOR file-scope 循环前查 raptor_kwd="raptor" 行，
  命中即跳过该文档。
GraphRAG 查询失败或损坏检查点必须失败并保留原数据；RAPTOR 完整历史读取失败同样传播。
DB / doc store 全部打桩（纯函数打桩形态），共享 fixtures 见 tests/unit/conftest.py。
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from typing import Any

import networkx as nx
import numpy as np
import pytest

import core.graphrag.general.index as kg_index
from common import settings
from common.doc_store import document_history
from core import raptor as raptor_module
from core.svr import task_executor


@contextmanager
def _fake_db() -> Iterator[object]:
    yield object()


class _FakeDocStore:
    """Control complete graph/document snapshots at the active storage boundary."""

    def __init__(self, rows: dict[str, dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or {}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def db_type(self) -> str:
        return "milvus"

    def graph_rows(self, index_name: str, kb_id: str, kinds: list[str]) -> list[dict[str, Any]]:
        self.calls.append({"index_names": index_name, "kb_ids": [kb_id], "kinds": kinds})
        if self.error is not None:
            raise self.error
        return [{"pk": cid, "kb_id": kb_id, "knowledge_graph_kwd": "subgraph", **row} for cid, row in self.rows.items()]

    def graph_subgraph_rows(self, name: str, kb_id: str, doc_id: str) -> list[dict[str, Any]]:
        rows = self.graph_rows(name, kb_id, ["subgraph"])
        self.calls[-1]["doc_id"] = doc_id
        return [row for row in rows if row.get("source_id") in ([doc_id], doc_id)]

    def history(self, store: object, index_name: str, kb_id: str, doc_id: str) -> list[dict[str, Any]]:
        assert store is self
        self.calls.append({"index_names": index_name, "kb_ids": [kb_id], "doc_id": doc_id, "complete_history": True})
        if self.error is not None:
            raise self.error
        return [deepcopy({"id": cid, "doc_id": doc_id, **row}) for cid, row in self.rows.items() if row.get("doc_id", "doc-1") == doc_id]


def _patch_common(monkeypatch: pytest.MonkeyPatch, module: ModuleType, store: _FakeDocStore) -> None:
    monkeypatch.setattr(module, "db_connection", _fake_db)
    monkeypatch.setattr(kg_index.KnowledgebaseService, "get_by_id", classmethod(lambda cls, db, kb_id: SimpleNamespace(name="kb", tenant_id="t1")))
    monkeypatch.setattr(settings, "docStoreConn", store)
    monkeypatch.setattr(document_history, "document_history", store.history)


def _subgraph_content() -> str:
    g = nx.Graph()
    g.add_edge("entity-a", "entity-b", weight=1)
    return json.dumps(nx.node_link_data(g, edges="edges"), ensure_ascii=False)


async def test_subgraph_checkpoint_hit_returns_graph_with_source_id(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(rows={"chunk-1": {"content_with_weight": _subgraph_content(), "source_id": ["doc-1"]}})
    _patch_common(monkeypatch, kg_index, store)

    sg = await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")

    assert sg is not None
    assert set(sg.nodes) == {"entity-a", "entity-b"}
    assert sg.graph["source_id"] == ["doc-1"]


async def test_subgraph_checkpoint_query_pins_generate_subgraph_write_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """查询条件必须与 generate_subgraph 的写入字段严格对齐，否则续跑永远 miss。"""
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, kg_index, store)

    await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")

    (call,) = store.calls
    assert call["kinds"] == ["subgraph"]
    assert call["doc_id"] == "doc-1"
    assert call["index_names"] == "multirag_t1_kb"  # kb_name 参与索引名，回归点：漏查 kb 名会打错索引
    assert call["kb_ids"] == ["kb1"]


async def test_subgraph_checkpoint_miss_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, kg_index, store)

    assert await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1") is None


async def test_subgraph_corrupt_json_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(rows={"chunk-1": {"content_with_weight": "{not-json", "source_id": ["doc-1"]}})
    _patch_common(monkeypatch, kg_index, store)

    with pytest.raises(ValueError):
        await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")


async def test_subgraph_store_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(error=RuntimeError("doc store down"))
    _patch_common(monkeypatch, kg_index, store)

    with pytest.raises(RuntimeError, match="doc store down"):
        await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")


async def test_raptor_checkpoint_hit_and_query_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(rows={"chunk-1": {"raptor_kwd": "raptor"}})
    _patch_common(monkeypatch, task_executor, store)

    assert await task_executor.has_raptor_chunks("doc-1", "t1", "kb1") is True
    (call,) = store.calls
    assert call["doc_id"] == "doc-1" and call["complete_history"] is True
    assert call["index_names"] == "multirag_t1_kb"
    assert call["kb_ids"] == ["kb1"]


async def test_raptor_checkpoint_miss_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, task_executor, store)

    assert await task_executor.has_raptor_chunks("doc-1", "t1", "kb1") is False


async def test_raptor_store_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(error=RuntimeError("doc store down"))
    _patch_common(monkeypatch, task_executor, store)

    with pytest.raises(RuntimeError, match="doc store down"):
        await task_executor.has_raptor_chunks("doc-1", "t1", "kb1")


async def test_run_raptor_for_kb_skips_docs_with_existing_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the complete snapshot, reuse matching trees, generate only missing trees."""
    store = _FakeDocStore(
        rows={
            "summary": {"doc_id": "doc-hit", "raptor_kwd": "raptor", "content_with_weight": "existing summary", "q_2_vec": [1.0, 0.0]},
            "first": {"doc_id": "doc-miss", "content_with_weight": "first source", "q_2_vec": [1.0, 0.0], "chunk_order_int": 0},
            "second": {"doc_id": "doc-miss", "content_with_weight": "second source", "q_2_vec": [0.0, 1.0], "chunk_order_int": 1},
        }
    )
    _patch_common(monkeypatch, task_executor, store)
    before = deepcopy(store.rows)
    generated: list[list[str]] = []

    class Raptor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert kwargs["tree_builder"] == "raptor" and kwargs["clustering_method"] == "gmm"

        async def __call__(self, inputs: list[tuple[str, np.ndarray]], seed: int, callback: Any, task_id: str) -> tuple[list[tuple[str, np.ndarray]], list[tuple[int, int]]]:
            assert task_id == "task-1"
            generated.append([content for content, _vector in inputs])
            return [*inputs, ("new summary", np.array([0.5, 0.5]))], [(0, 2), (2, 3)]

    monkeypatch.setattr(raptor_module, "RecursiveAbstractiveProcessing4TreeOrganizedRetrieval", Raptor)
    monkeypatch.setattr(task_executor.rag_tokenizer, "tokenize", lambda text: text)
    monkeypatch.setattr(task_executor.rag_tokenizer, "fine_grained_tokenize", lambda text: text)
    messages: list[dict[str, Any]] = []
    row = {"tenant_id": "t1", "kb_id": "kb1", "id": "task-1", "name": "kb", "pagerank": 0}
    chunks, tokens, previous = await task_executor.run_raptor_for_kb(
        row,
        {"raptor": {"scope": "file", "prompt": "controlled", "max_token": 64, "threshold": 0.5}},
        None,
        None,
        2,
        callback=lambda **kwargs: messages.append(kwargs),
        doc_ids=["doc-hit", "doc-miss"],
    )
    assert generated == [["first source", "second source"]]
    assert len(chunks) == 1 and chunks[0]["doc_id"] == "doc-miss" and chunks[0]["content_with_weight"] == "new summary"
    assert tokens > 0 and previous == {"doc-miss": {}}
    assert len(messages) == 1 and "doc:doc-hit checkpoint" in messages[0]["msg"] and "prog" not in messages[0]
    assert [call["doc_id"] for call in store.calls] == ["doc-hit", "doc-miss", task_executor.GRAPH_RAPTOR_FAKE_DOC_ID]
    assert store.rows == before


@pytest.mark.parametrize("kb", [None, SimpleNamespace(name="kb", tenant_id="foreign")])
async def test_raptor_foreign_or_missing_dataset_never_reads_history(monkeypatch: pytest.MonkeyPatch, kb: Any) -> None:
    store = _FakeDocStore()
    _patch_common(monkeypatch, task_executor, store)
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", classmethod(lambda cls, db, kb_id: kb))
    with pytest.raises(ValueError, match="dataset binding unavailable"):
        await task_executor.has_raptor_chunks("doc-1", "t1", "kb1")
    assert store.calls == []


async def test_run_raptor_history_failure_does_not_generate_or_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeDocStore(error=RuntimeError("history unavailable"))
    _patch_common(monkeypatch, task_executor, store)
    monkeypatch.setattr(raptor_module, "RecursiveAbstractiveProcessing4TreeOrganizedRetrieval", lambda *args, **kwargs: pytest.fail("failed history must not invoke a model"))
    with pytest.raises(RuntimeError, match="history unavailable"):
        await task_executor.run_raptor_for_kb({"tenant_id": "t1", "kb_id": "kb1", "id": "task-1", "name": "kb"}, {"raptor": {"scope": "file"}}, None, None, 2, doc_ids=["doc-1"])
    assert len(store.calls) == 1 and store.rows == {}
