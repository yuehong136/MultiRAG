"""dataset_api.get_knowledge_graph 服务与路由契约（restful_apis AsyncSession 收口）。

服务层：同步 DB 面走 run_sync，doc-store 探测经 to_thread 外移（不阻塞事件循环）；
路由层：REST 的 code/data 成功形状与 AUTHENTICATION_ERROR 失败形状钉板。
"""

import json
import threading
import types

import pytest

from api.apps.services import dataset_api_service
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.constants import RetCode


def _fake_kb():
    return types.SimpleNamespace(id="kb1", tenant_id="tenant-unit", name="kb")


@pytest.fixture
def kg_service_stubs(monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: _fake_kb()))


async def test_get_knowledge_graph_denies_without_access(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: False))

    assert await dataset_api_service.get_knowledge_graph(async_db, "tenant-unit", "kb1") == (False, "No authorization.")


async def test_get_knowledge_graph_empty_when_index_missing(async_db, kg_service_stubs, monkeypatch):
    probe: dict[str, bool] = {}

    def _index_exist(index_name, kb_id):
        probe["off_loop"] = threading.current_thread() is not threading.main_thread()
        return False

    monkeypatch.setattr(settings, "docStoreConn", types.SimpleNamespace(index_exist=_index_exist))

    success, result = await dataset_api_service.get_knowledge_graph(async_db, "tenant-unit", "kb1")

    assert (success, result) == (True, {"graph": {}, "mind_map": {}})
    assert probe["off_loop"] is True  # 同步 doc-store 探测必须在工作线程执行


async def test_get_knowledge_graph_parses_and_trims_graph(async_db, kg_service_stubs, monkeypatch):
    graph = {
        "nodes": [{"id": "a", "pagerank": 2}, {"id": "b", "pagerank": 1}],
        "edges": [
            {"source": "a", "target": "b", "weight": 3},
            {"source": "a", "target": "a", "weight": 9},  # 自环应被过滤
            {"source": "a", "target": "ghost", "weight": 5},  # 悬挂边应被过滤
        ],
    }
    sres = types.SimpleNamespace(ids=["c1"], field={"c1": {"knowledge_graph_kwd": "graph", "content_with_weight": json.dumps(graph)}})

    async def _search(req, index_name, kb_ids):
        return sres

    monkeypatch.setattr(settings, "docStoreConn", types.SimpleNamespace(index_exist=lambda index_name, kb_id: True))
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(search=_search))

    success, result = await dataset_api_service.get_knowledge_graph(async_db, "tenant-unit", "kb1")

    assert success is True
    assert [n["id"] for n in result["graph"]["nodes"]] == ["a", "b"]
    assert result["graph"]["edges"] == [{"source": "a", "target": "b", "weight": 3}]


# ---------------------------------------------------------------------------
# 路由层（service 打桩，锁响应形状）
# ---------------------------------------------------------------------------


def test_route_success_shape(client, monkeypatch):
    async def _fake(db, tenant_id, dataset_id):
        return True, {"graph": {"nodes": []}, "mind_map": {}}

    monkeypatch.setattr(dataset_api_service, "get_knowledge_graph", _fake)

    resp = client.get("/api/v1/datasets/kb1/knowledge_graph")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"graph": {"nodes": []}, "mind_map": {}}


def test_route_denied_shape(client, monkeypatch):
    async def _fake(db, tenant_id, dataset_id):
        return False, "No authorization."

    monkeypatch.setattr(dataset_api_service, "get_knowledge_graph", _fake)

    resp = client.get("/api/v1/datasets/kb1/knowledge_graph")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."
