"""Unified index API, explicit compatibility routes and dataset contracts."""

import types
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from api.apps.services import dataset_api_service
from api.db.db_models import Knowledgebase
from api.db.services.knowledgebase_service import KnowledgebaseService
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


async def test_trace_index_reads_the_column_for_its_type(async_db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    kb = fake_kb(graphrag_task_id="g1", raptor_task_id=None)

    async def get(model, key):
        return kb if model is Knowledgebase else types.SimpleNamespace(to_dict=lambda: {"id": key, "progress": 1})

    monkeypatch.setattr(async_db, "get", get)
    assert await dataset_api_service.trace_index(async_db, "tenant-unit", "kb1", "graph") == (True, {"id": "g1", "progress": 1})
    assert await dataset_api_service.trace_index(async_db, "tenant-unit", "kb1", "raptor") == (True, {})


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


def test_list_tags_preserves_upstream_pairs(db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(dataset_api_service.UserTenantService, "get_tenants_by_user_id", classmethod(lambda cls, s, uid: [{"tenant_id": "t1"}]))
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(all_tags=lambda tid, kb_ids: [("alpha", 7)]))

    assert dataset_api_service.list_tags(db, "tenant-unit", "kb1") == (True, [("alpha", 7)])


# ---------------------------------------------------------------------------
# 服务层：详情与摄取日志
# ---------------------------------------------------------------------------


async def test_get_ingestion_summary_reports_counts_and_status(async_db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "get", AsyncMock(return_value=fake_kb(doc_num=4, chunk_num=40, token_num=400)))
    monkeypatch.setattr(async_db, "execute", AsyncMock(return_value=[("3", 4)]))
    success, result = await dataset_api_service.get_ingestion_summary(async_db, "tenant-unit", "kb1")
    assert success and result["doc_num"] == 4 and result["chunk_num"] == 40 and result["token_num"] == 400
    assert result["status"] == {"unstart_count": 0, "running_count": 0, "cancel_count": 0, "done_count": 4, "fail_count": 0}


async def test_get_ingestion_log_scopes_lookup_to_the_dataset(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    executed = AsyncMock(return_value=types.SimpleNamespace(mappings=lambda: types.SimpleNamespace(first=lambda: {"id": "log1"})))
    monkeypatch.setattr(async_db, "execute", executed)
    assert await dataset_api_service.get_ingestion_log(async_db, "tenant-unit", "kb1", "log1") == (True, {"id": "log1"})
    params = executed.call_args.args[0].compile().params
    assert "kb1" in params.values() and "log1" in params.values()


async def test_get_ingestion_log_missing(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "execute", AsyncMock(return_value=types.SimpleNamespace(mappings=lambda: types.SimpleNamespace(first=lambda: None))))
    assert await dataset_api_service.get_ingestion_log(async_db, "tenant-unit", "kb1", "nope") == (False, "Log not found")


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

    assert resp.status_code == 404


def test_ingestions_summary_is_not_read_as_a_log_id(client, monkeypatch):
    reached: list[str] = []

    monkeypatch.setattr(dataset_api_service, "get_ingestion_summary", AsyncMock(side_effect=lambda s, t, d: reached.append("summary") or (True, {})))
    monkeypatch.setattr(dataset_api_service, "get_ingestion_log", AsyncMock(side_effect=lambda s, t, d, log_id: reached.append(f"log:{log_id}") or (True, {})))

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
    monkeypatch.setattr(dataset_api_service, "get_dataset", AsyncMock(side_effect=lambda s, t, d: reached.append(f"detail:{d}") or (True, {})))

    assert client.get("/api/v1/datasets/tags/aggregation?dataset_ids=kb1,kb2").status_code == 200
    assert client.get("/api/v1/datasets/metadata/flattened?dataset_ids=kb1").status_code == 200
    assert client.get("/api/v1/datasets/kb1").status_code == 200
    assert reached == ["aggregate:['kb1', 'kb2']", "flattened:['kb1']", "detail:kb1"]


def test_collection_paths_require_dataset_ids(client):
    resp = client.get("/api/v1/datasets/tags/aggregation")

    assert resp.status_code == 200
    assert resp.json()["message"] == "Lack of dataset_ids in query parameters"


@pytest.mark.parametrize("index_type", ["graph", "Graph", "GRAPH", "raptor", "RAPTOR", "RaPtOr", "mindmap", "MindMap", "MINDMAP"])
def test_index_routes_normalize_legal_type_queries(client: TestClient, monkeypatch: pytest.MonkeyPatch, index_type: str) -> None:
    seen: list[str] = []

    async def run(tenant_id: str, dataset_id: str, type_name: str) -> tuple[bool, Any]:
        seen.append(f"run:{type_name}")
        return True, {"task_id": "t1"}

    async def trace(db: Any, tenant_id: str, dataset_id: str, type_name: str) -> tuple[bool, Any]:
        seen.append(f"trace:{type_name}")
        return True, {"id": "t1", "task_type": "graphrag" if type_name == "graph" else type_name}

    async def delete(tenant_id: str, dataset_id: str, type_name: str) -> tuple[bool, Any]:
        seen.append(f"delete:{type_name}")
        return True, {}

    monkeypatch.setattr(dataset_api_service, "run_index_async", run)
    monkeypatch.setattr(dataset_api_service, "trace_index", trace)
    monkeypatch.setattr(dataset_api_service, "delete_index_async", delete)
    path = f"/api/v1/datasets/kb1/index?type={index_type}"
    assert client.post(path).json() == {"code": 0, "data": {"task_id": "t1"}}
    body = client.get(path).json()
    assert body["code"] == 0 and body["data"]["id"] == "t1"
    assert body["data"]["task_type"] == ("graphrag" if index_type.lower() == "graph" else index_type.lower())
    assert client.delete(path).json() == {"code": 0, "data": {}}
    assert seen == [f"{operation}:{index_type.lower()}" for operation in ["run", "trace", "delete"]]


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


def test_document_delete_is_not_shadowed_when_dataset_routes_register_first(client, monkeypatch):
    from fastapi.responses import JSONResponse

    def contains(route, suffix):
        children = getattr(getattr(route, "original_router", None), "routes", [route])
        return any(getattr(child, "path", "").endswith(suffix) for child in children)

    routes = client.app.router.routes
    dataset_branch = next(route for route in routes if contains(route, "/datasets/{dataset_id}/index"))
    document_branch = next(route for route in routes if contains(route, "/datasets/{dataset_id}/documents"))
    reordered = [dataset_branch, *[route for route in routes if route is not dataset_branch]]
    assert reordered.index(dataset_branch) < reordered.index(document_branch)
    monkeypatch.setattr(client.app.router, "routes", reordered)
    # Invalid document body must reach Document API validation, never the index handler.
    response = client.request("DELETE", "/api/v1/datasets/kb1/documents", json=["invalid-body"])
    assert response.status_code == 422

    # Mutation check: restoring the old catch-all must make this same request miss Documents.
    async def shadow():
        return JSONResponse(status_code=418, content={"shadowed": True})

    dataset_router = dataset_branch.original_router
    monkeypatch.setattr(dataset_router, "routes", list(dataset_router.routes))
    # Preserve FastAPI's route-cache version as well as its routes after the mutation.
    monkeypatch.setattr(dataset_router, "_routes_version", dataset_router._routes_version)
    dataset_router.add_api_route("/datasets/{dataset_id}/{index_type}", shadow, methods=["DELETE"])
    assert client.request("DELETE", "/api/v1/datasets/kb1/documents", json=["invalid-body"]).status_code == 418


@pytest.mark.parametrize(("legacy", "index_type", "key"), [("run_graphrag", "graph", "graphrag_task_id"), ("run_raptor", "raptor", "raptor_task_id")])
def test_legacy_run_adapts_only_task_id_field(db, monkeypatch, legacy, index_type, key):
    seen = []
    monkeypatch.setattr(dataset_api_service, "run_index", lambda s, t, d, kind: seen.append(kind) or (True, {"task_id": "task1"}))
    assert getattr(dataset_api_service, legacy)(db, "t1", "kb1") == (True, {key: "task1"})
    assert seen == [index_type]


async def test_legacy_trace_keeps_missing_raptor_error(async_db, monkeypatch, fake_kb):
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "get", AsyncMock(side_effect=lambda model, key: fake_kb(raptor_task_id="missing") if model is Knowledgebase else None))
    assert await dataset_api_service.trace_index(async_db, "t1", "kb1", "raptor") == (True, {})
    assert await dataset_api_service.trace_raptor(async_db, "t1", "kb1") == (False, "RAPTOR Task Not Found or Error Occurred")


def test_ingestion_dates_are_validated_before_database_access(client):
    response = client.get("/api/v1/datasets/kb1/ingestions?create_date_from=not-a-date")
    assert response.status_code == 422


def test_single_dataset_tags_serialize_as_upstream_pairs(client, monkeypatch):
    monkeypatch.setattr(dataset_api_service, "list_tags_async", AsyncMock(return_value=(True, [("alpha", 2)])))
    assert client.get("/api/v1/datasets/kb1/tags").json()["data"] == [["alpha", 2]]
