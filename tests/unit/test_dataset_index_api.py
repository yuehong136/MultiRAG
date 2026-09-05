"""统一索引 API（graph/raptor/mindmap）与数据集管理端点的服务与路由契约。

路由顺序是本文件的核心资产：`DELETE /datasets/{id}/{index_type}` 是 catch-all，
FastAPI 按注册顺序线性匹配，任何把它提前的改动都会静默吞掉 knowledge_graph /
tags / index 三条具体 DELETE 路由。上游 Flask 下静态段天然优先，没有这个约束，
所以这组断言只在我方成立，也只能由我方守。
"""

import types

import pytest

from api.apps.services import dataset_api_service
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import TaskService
from common import settings

# ---------------------------------------------------------------------------
# 服务层：索引类型分派
# ---------------------------------------------------------------------------


@pytest.fixture
def kb_access(monkeypatch, fake_kb):
    kb = fake_kb(mindmap_task_id=None)
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: kb))
    return kb


@pytest.mark.parametrize("index_type", ["graph", "raptor", "mindmap"])
def test_run_index_queues_task_type_and_persists_matching_column(db, kb_access, monkeypatch, index_type):
    queued: dict[str, object] = {}
    saved: dict[str, object] = {}

    monkeypatch.setattr(
        dataset_api_service.DocumentService,
        "get_by_kb_id",
        classmethod(lambda cls, s, **kw: ([{"id": "d1"}], 1)),
    )
    monkeypatch.setattr(dataset_api_service, "queue_raptor_o_graphrag_tasks", lambda s, **kw: queued.update(kw) or "task-new")
    monkeypatch.setattr(KnowledgebaseService, "update_by_id", classmethod(lambda cls, s, kb_id, payload: saved.update(payload) or True))

    success, result = dataset_api_service.run_index(db, "tenant-unit", "kb1", index_type)

    assert (success, result) == (True, {"task_id": "task-new"})
    assert queued["ty"] == {"graph": "graphrag", "raptor": "raptor", "mindmap": "mindmap"}[index_type]
    assert saved == {f"{'graphrag' if index_type == 'graph' else index_type}_task_id": "task-new"}


def test_run_index_rejects_unknown_type(db):
    success, result = dataset_api_service.run_index(db, "tenant-unit", "kb1", "vector")

    assert success is False
    assert "Invalid index type 'vector'" in result


def test_run_index_refuses_while_previous_task_runs(db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: fake_kb(raptor_task_id="task-old")))
    monkeypatch.setattr(TaskService, "get_by_id", classmethod(lambda cls, s, tid: types.SimpleNamespace(progress=0.4)))

    success, result = dataset_api_service.run_index(db, "tenant-unit", "kb1", "raptor")

    assert success is False
    assert "A RAPTOR Task is already running." in result


def test_trace_index_reads_the_column_for_its_type(db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: fake_kb(graphrag_task_id="g1", raptor_task_id=None)))
    monkeypatch.setattr(TaskService, "get_by_id", classmethod(lambda cls, s, tid: types.SimpleNamespace(to_dict=lambda: {"id": tid, "progress": 1})))

    assert dataset_api_service.trace_index(db, "tenant-unit", "kb1", "graph") == (True, {"id": "g1", "progress": 1})
    # 未建立任务的类型返回空 dict，而不是报错
    assert dataset_api_service.trace_index(db, "tenant-unit", "kb1", "raptor") == (True, {})


def test_delete_index_cancels_task_and_wipes_only_graph_artifacts(db, kb_access, monkeypatch):
    cancelled: list[str] = []
    deleted: list[dict] = []
    saved: dict[str, object] = {}

    kb_access.graphrag_task_id = "g1"
    monkeypatch.setattr(dataset_api_service.REDIS_CONN, "set", lambda key, value: cancelled.append(key))
    monkeypatch.setattr(TaskService, "delete_by_id", classmethod(lambda cls, s, tid: 1))
    monkeypatch.setattr(settings, "docStoreConn", types.SimpleNamespace(delete=lambda cond, idx, kb_id: deleted.append(cond)))
    monkeypatch.setattr(KnowledgebaseService, "update_by_id", classmethod(lambda cls, s, kb_id, payload: saved.update(payload) or True))

    success, result = dataset_api_service.delete_index(db, "tenant-unit", "kb1", "graph")

    assert (success, result) == (True, {})
    assert cancelled == ["g1-cancel"]
    assert deleted == [{"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}]
    assert saved == {"graphrag_task_id": "", "graphrag_task_finish_at": None}


def test_delete_index_leaves_doc_store_untouched_for_mindmap(db, kb_access, monkeypatch):
    kb_access.mindmap_task_id = None
    monkeypatch.setattr(settings, "docStoreConn", types.SimpleNamespace())  # 任何 delete 调用都会 AttributeError
    monkeypatch.setattr(KnowledgebaseService, "update_by_id", classmethod(lambda cls, s, kb_id, payload: True))

    assert dataset_api_service.delete_index(db, "tenant-unit", "kb1", "mindmap") == (True, {})


# ---------------------------------------------------------------------------
# 服务层：标签聚合
# ---------------------------------------------------------------------------


def test_aggregate_tags_unpacks_retriever_tuples(db, monkeypatch, fake_kb):
    """retriever.all_tags 产出 (tag, count) 元组，不是 {"value","count"} 字典。

    按字典下标读会直接 TypeError，这条路径一调就炸——上游初版正是这么写的。
    """
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: fake_kb(id=kb_id, tenant_id="t1")))
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(all_tags=lambda tid, kb_ids: [("alpha", 2), ("beta", 3)]))

    success, result = dataset_api_service.aggregate_tags(db, "tenant-unit", ["kb1", "kb2"])

    assert success is True
    # 两个 kb 同属一个租户 → 单次查询，计数不重复累加
    assert result == [{"value": "alpha", "count": 2}, {"value": "beta", "count": 3}]


def test_aggregate_tags_merges_counts_across_tenants(db, monkeypatch, fake_kb):
    kbs = {"kb1": fake_kb(id="kb1", tenant_id="t1"), "kb2": fake_kb(id="kb2", tenant_id="t2")}
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: kbs[kb_id]))
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(all_tags=lambda tid, kb_ids: [("shared", 1)]))

    success, result = dataset_api_service.aggregate_tags(db, "tenant-unit", ["kb1", "kb2"])

    assert (success, result) == (True, [{"value": "shared", "count": 2}])


def test_aggregate_tags_requires_ids(db):
    assert dataset_api_service.aggregate_tags(db, "tenant-unit", []) == (False, 'Lack of "dataset_ids"')


def test_list_tags_shapes_retriever_tuples_as_objects(db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(dataset_api_service.UserTenantService, "get_tenants_by_user_id", classmethod(lambda cls, s, uid: [{"tenant_id": "t1"}]))
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(all_tags=lambda tid, kb_ids: [("alpha", 7)]))

    assert dataset_api_service.list_tags(db, "tenant-unit", "kb1") == (True, [{"value": "alpha", "count": 7}])


# ---------------------------------------------------------------------------
# 服务层：详情与摄取日志
# ---------------------------------------------------------------------------


def test_get_ingestion_summary_reports_counts_and_status(db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: fake_kb(doc_num=4, chunk_num=40, token_num=400)))
    monkeypatch.setattr(
        dataset_api_service.DocumentService,
        "get_parsing_status_by_kb_ids",
        classmethod(lambda cls, s, kb_ids: {"kb1": {"done_count": 4}}),
    )

    success, result = dataset_api_service.get_ingestion_summary(db, "tenant-unit", "kb1")

    assert (success, result) == (True, {"doc_num": 4, "chunk_num": 40, "token_num": 400, "status": {"done_count": 4}})


def test_get_ingestion_log_scopes_lookup_to_the_dataset(db, monkeypatch):
    """日志必须同时匹配 log_id 与 kb_id，否则可跨数据集读到别人的日志。"""
    seen: dict[str, object] = {}

    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(
        PipelineOperationLogService,
        "get_or_none",
        classmethod(lambda cls, s, **kw: seen.update(kw) or types.SimpleNamespace(to_dict=lambda: {"id": "log1"})),
    )

    success, result = dataset_api_service.get_ingestion_log(db, "tenant-unit", "kb1", "log1")

    assert (success, result) == (True, {"id": "log1"})
    assert seen == {"id": "log1", "kb_id": "kb1"}


def test_get_ingestion_log_missing(db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(PipelineOperationLogService, "get_or_none", classmethod(lambda cls, s, **kw: None))

    assert dataset_api_service.get_ingestion_log(db, "tenant-unit", "kb1", "nope") == (False, "Log not found")


# ---------------------------------------------------------------------------
# 路由层：注册顺序契约（catch-all 不得吞掉具体路由）
# ---------------------------------------------------------------------------


def test_catch_all_delete_does_not_swallow_knowledge_graph(client, monkeypatch):
    reached: list[str] = []

    async def _delete_kg(db, tenant_id, dataset_id):
        reached.append("knowledge_graph")
        return True, True

    async def _delete_index(tenant_id, dataset_id, index_type):
        reached.append(f"index:{index_type}")
        return True, {}

    monkeypatch.setattr(dataset_api_service, "delete_knowledge_graph", _delete_kg)
    monkeypatch.setattr(dataset_api_service, "delete_index_async", _delete_index)

    assert client.delete("/api/v1/datasets/kb1/knowledge_graph").status_code == 200
    assert reached == ["knowledge_graph"]


def test_catch_all_delete_does_not_swallow_tags(client, monkeypatch):
    reached: list[str] = []

    async def _delete_tags(tenant_id, dataset_id, tags):
        reached.append(f"tags:{tags}")
        return True, {}

    async def _delete_index(tenant_id, dataset_id, index_type):
        reached.append(f"index:{index_type}")
        return True, {}

    monkeypatch.setattr(dataset_api_service, "delete_tags_async", _delete_tags)
    monkeypatch.setattr(dataset_api_service, "delete_index_async", _delete_index)

    resp = client.request("DELETE", "/api/v1/datasets/kb1/tags", json={"tags": ["a"]})

    assert resp.status_code == 200
    assert reached == ["tags:['a']"]


def test_catch_all_delete_serves_index_types(client, monkeypatch):
    seen: list[str] = []

    async def _delete_index(tenant_id, dataset_id, index_type):
        seen.append(index_type)
        return True, {}

    monkeypatch.setattr(dataset_api_service, "delete_index_async", _delete_index)

    assert client.delete("/api/v1/datasets/kb1/raptor").status_code == 200
    assert client.delete("/api/v1/datasets/kb1/index?type=graph").status_code == 200
    assert seen == ["raptor", "graph"]


def test_catch_all_delete_rejects_unknown_segment(client):
    resp = client.delete("/api/v1/datasets/kb1/bogus")

    assert resp.status_code == 200
    assert "Invalid index type 'bogus'" in resp.json()["message"]


def test_ingestions_summary_is_not_read_as_a_log_id(client, monkeypatch):
    reached: list[str] = []

    monkeypatch.setattr(dataset_api_service, "get_ingestion_summary", lambda s, t, d: reached.append("summary") or (True, {}))
    monkeypatch.setattr(dataset_api_service, "get_ingestion_log", lambda s, t, d, log_id: reached.append(f"log:{log_id}") or (True, {}))

    assert client.get("/api/v1/datasets/kb1/ingestions/summary").status_code == 200
    assert client.get("/api/v1/datasets/kb1/ingestions/log-7").status_code == 200
    assert reached == ["summary", "log:log-7"]


def test_static_collection_paths_win_over_dataset_id(client, monkeypatch):
    reached: list[str] = []

    async def _aggregate(tenant_id, ids):
        reached.append(f"aggregate:{ids}")
        return True, []

    async def _flattened(tenant_id, ids):
        reached.append(f"flattened:{ids}")
        return True, {}

    monkeypatch.setattr(dataset_api_service, "aggregate_tags_async", _aggregate)
    monkeypatch.setattr(dataset_api_service, "get_flattened_metadata_async", _flattened)
    monkeypatch.setattr(dataset_api_service, "get_dataset", lambda s, t, d: reached.append(f"detail:{d}") or (True, {}))

    assert client.get("/api/v1/datasets/tags/aggregation?dataset_ids=kb1,kb2").status_code == 200
    assert client.get("/api/v1/datasets/metadata/flattened?dataset_ids=kb1").status_code == 200
    assert client.get("/api/v1/datasets/kb1").status_code == 200
    assert reached == ["aggregate:['kb1', 'kb2']", "flattened:['kb1']", "detail:kb1"]


def test_collection_paths_require_dataset_ids(client):
    resp = client.get("/api/v1/datasets/tags/aggregation")

    assert resp.status_code == 200
    assert resp.json()["message"] == "Lack of dataset_ids in query parameters"


def test_index_routes_pass_the_type_query_through(client, monkeypatch):
    seen: list[str] = []

    async def _run(tenant_id, dataset_id, index_type):
        seen.append(f"run:{index_type}")
        return True, {"task_id": "t1"}

    monkeypatch.setattr(dataset_api_service, "run_index_async", _run)
    monkeypatch.setattr(dataset_api_service, "trace_index", lambda s, t, d, index_type: seen.append(f"trace:{index_type}") or (True, {}))

    assert client.post("/api/v1/datasets/kb1/index?type=mindmap").json()["data"] == {"task_id": "t1"}
    assert client.get("/api/v1/datasets/kb1/index?type=raptor").status_code == 200
    assert seen == ["run:mindmap", "trace:raptor"]


def test_metadata_config_and_legacy_auto_metadata_share_the_service(client, monkeypatch):
    """新路径与 deprecated 旧路径必须同源，否则前端迁移期两边会漂移。"""
    calls: list[str] = []

    monkeypatch.setattr(dataset_api_service, "get_auto_metadata", lambda s, t, d: calls.append(d) or (True, {"enabled": True, "fields": []}))

    new = client.get("/api/v1/datasets/kb1/metadata/config")
    legacy = client.get("/api/v1/datasets/kb1/auto_metadata")

    assert new.json()["data"] == legacy.json()["data"] == {"enabled": True, "fields": []}
    assert calls == ["kb1", "kb1"]


def test_rename_tag_rejects_blank_names(client):
    resp = client.put("/api/v1/datasets/kb1/tags", json={"from_tag": "  ", "to_tag": "b"})

    assert resp.status_code == 200
    assert resp.json()["message"] == "from_tag and to_tag must not be empty"
