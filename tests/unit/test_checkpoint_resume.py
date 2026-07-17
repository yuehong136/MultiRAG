"""GraphRAG/RAPTOR 断点续跑（checkpoint resume）钉板测试。

背景：长时 KG/RAPTOR 任务（可达 10+ 小时 LLM 抽取）中断后重跑会重复全部昂贵工作。
- load_subgraph_from_store 在 build_one 抽取前查 doc store 里已持久化的 subgraph
  （generate_subgraph 写入形态：knowledge_graph_kwd="subgraph"、source_id=[doc_id]、
  removed_kwd="N"），命中即复用、跳过 LLM 抽取；
- has_raptor_chunks 在 RAPTOR file-scope 循环前查 raptor_kwd="raptor" 行，
  命中即跳过该文档。
两者查询失败一律 fail-open（返回 None/False → 照常重跑），绝不让检查点错误中断任务。
DB / doc store 全部打桩（纯函数打桩形态），共享 fixtures 见 tests/unit/conftest.py。
"""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import networkx as nx

import core.graphrag.general.index as kg_index
from core.svr import task_executor


@contextmanager
def _fake_db():
    yield object()


class _FakeDocStore:
    """打桩 docStoreConn：记录 search 调用参数，get_fields 返回预置行。"""

    def __init__(self, rows=None, error=None):
        self.rows = rows or {}
        self.error = error
        self.calls = []

    def search(self, fields, highlight, condition, match_exprs, order_by, offset, limit, index_names, kb_ids):
        self.calls.append({"fields": fields, "condition": condition, "offset": offset, "limit": limit, "index_names": index_names, "kb_ids": kb_ids})
        if self.error is not None:
            raise self.error
        return "res-sentinel"

    def get_fields(self, res, fields):
        return self.rows


def _patch_common(monkeypatch, module, store):
    monkeypatch.setattr(module, "db_connection", _fake_db)
    monkeypatch.setattr(kg_index.KnowledgebaseService, "get_by_id", classmethod(lambda cls, db, kb_id: SimpleNamespace(name="kb")))
    monkeypatch.setattr(module.settings, "docStoreConn", store, raising=False)


def _subgraph_content():
    g = nx.Graph()
    g.add_edge("entity-a", "entity-b", weight=1)
    return json.dumps(nx.node_link_data(g, edges="edges"), ensure_ascii=False)


async def test_subgraph_checkpoint_hit_returns_graph_with_source_id(monkeypatch):
    store = _FakeDocStore(rows={"chunk-1": {"content_with_weight": _subgraph_content(), "source_id": ["doc-1"]}})
    _patch_common(monkeypatch, kg_index, store)

    sg = await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")

    assert sg is not None
    assert set(sg.nodes) == {"entity-a", "entity-b"}
    assert sg.graph["source_id"] == ["doc-1"]


async def test_subgraph_checkpoint_query_pins_generate_subgraph_write_contract(monkeypatch):
    """查询条件必须与 generate_subgraph 的写入字段严格对齐，否则续跑永远 miss。"""
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, kg_index, store)

    await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1")

    (call,) = store.calls
    assert call["condition"] == {"knowledge_graph_kwd": ["subgraph"], "removed_kwd": "N", "source_id": ["doc-1"]}
    assert call["limit"] == 1
    assert call["index_names"] == ["multirag_t1_kb"]  # kb_name 参与索引名，回归点：漏查 kb 名会打错索引
    assert call["kb_ids"] == ["kb1"]


async def test_subgraph_checkpoint_miss_returns_none(monkeypatch):
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, kg_index, store)

    assert await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1") is None


async def test_subgraph_corrupt_json_fails_open(monkeypatch):
    store = _FakeDocStore(rows={"chunk-1": {"content_with_weight": "{not-json", "source_id": ["doc-1"]}})
    _patch_common(monkeypatch, kg_index, store)

    assert await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1") is None


async def test_subgraph_store_error_fails_open(monkeypatch):
    store = _FakeDocStore(error=RuntimeError("doc store down"))
    _patch_common(monkeypatch, kg_index, store)

    assert await kg_index.load_subgraph_from_store("t1", "kb1", "doc-1") is None


async def test_raptor_checkpoint_hit_and_query_contract(monkeypatch):
    store = _FakeDocStore(rows={"chunk-1": {"raptor_kwd": "raptor"}})
    _patch_common(monkeypatch, task_executor, store)

    assert await task_executor.has_raptor_chunks("doc-1", "t1", "kb1") is True
    (call,) = store.calls
    assert call["condition"] == {"doc_id": "doc-1", "raptor_kwd": ["raptor"]}
    assert call["limit"] == 1
    assert call["index_names"] == "multirag_t1_kb"
    assert call["kb_ids"] == ["kb1"]


async def test_raptor_checkpoint_miss_returns_false(monkeypatch):
    store = _FakeDocStore(rows={})
    _patch_common(monkeypatch, task_executor, store)

    assert await task_executor.has_raptor_chunks("doc-1", "t1", "kb1") is False


async def test_raptor_store_error_fails_open(monkeypatch):
    store = _FakeDocStore(error=RuntimeError("doc store down"))
    _patch_common(monkeypatch, task_executor, store)

    assert await task_executor.has_raptor_chunks("doc-1", "t1", "kb1") is False


async def test_run_raptor_for_kb_skips_docs_with_existing_chunks(monkeypatch):
    """file-scope 循环：命中检查点的文档不得再拉 chunk（即不再进入生成流程）。"""
    fetched = []
    monkeypatch.setattr(task_executor.settings, "retriever", SimpleNamespace(chunk_list=lambda doc_id, *a, **k: fetched.append(doc_id) or []), raising=False)

    async def fake_has_raptor_chunks(doc_id, tenant_id, kb_id):
        return doc_id == "doc-hit"

    monkeypatch.setattr(task_executor, "has_raptor_chunks", fake_has_raptor_chunks)

    messages, progress = [], []

    def callback(msg=None, prog=None):
        if msg is not None:
            messages.append(msg)
        if prog is not None:
            progress.append(prog)

    row = {"tenant_id": "t1", "kb_id": "kb1", "id": "task-1", "name": "kb", "pagerank": 0}
    res, tk_count = await task_executor.run_raptor_for_kb(row, {"raptor": {"scope": "file"}}, None, None, 768, callback=callback, doc_ids=["doc-hit", "doc-miss"])

    assert fetched == ["doc-miss"]  # 回归点：doc-hit 被跳过，不再拉取 chunk
    assert any("doc:doc-hit already has RAPTOR chunks" in m for m in messages)
    assert 0.5 in progress  # 跳过的文档也要推进进度
    assert (res, tk_count) == ([], 0)
