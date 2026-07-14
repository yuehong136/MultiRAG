"""dataset RESTful API 契约测试（Phase 2.5 批次 2：AsyncSession 收口）。

11 条路由分三种形态，测试按形态锁类型契约：
- 纯 DB 路由（create/list/auto_metadata/trace×2）：路由层 run_sync——桩断言
  service 收到同步 facade（``sqlalchemy.orm.Session``）；
- 混轨路由（delete/update/run_graphrag/run_raptor）：service 层 ``*_async`` 包装
  整块进工作线程 + ``db_connection`` 自开短会话——桩断言在非主线程收到同步 Session；
- delete_knowledge_graph：service 原位 async（DB run_sync + doc-store to_thread），
  服务级测试断言 doc-store 删除在工作线程执行。
get_knowledge_graph 已于 9004fabf 转换，测试在 test_dataset_knowledge_graph_route.py。
"""

import threading
from types import SimpleNamespace

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import get_async_db, get_db
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common import settings
from common.constants import RetCode


def _record(records, s):
    records.append({"session": s, "off_loop": threading.current_thread() is not threading.main_thread()})


def _assert_sync_facade(records, *, off_loop=None):
    assert records
    for r in records:
        assert isinstance(r["session"], Session), f"同步 service 收到 {type(r['session']).__name__}，应为 sqlalchemy.orm.Session"
        if off_loop is not None:
            assert r["off_loop"] is off_loop


# ---------------------------------------------------------------------------
# 纯 DB 形态（路由层 run_sync）
# ---------------------------------------------------------------------------


def test_dataset_create_success_envelope(client, monkeypatch):
    records: list[dict] = []

    def _create(s, tenant_id, req):
        _record(records, s)
        assert tenant_id == "tenant-unit"
        assert req["name"] == "ds-1"
        return True, {"id": "kb-1", "name": "ds-1"}

    monkeypatch.setattr(dataset_api_service, "create_dataset", _create)

    resp = client.post("/api/v1/datasets", json={"name": "ds-1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "kb-1", "name": "ds-1"}
    _assert_sync_facade(records)


def test_dataset_create_error_and_response_passthrough(client, monkeypatch):
    monkeypatch.setattr(dataset_api_service, "create_dataset", lambda s, t, r: (False, "Dataset name 'ds-1' already exists"))
    dup = client.post("/api/v1/datasets", json={"name": "ds-1"}).json()
    assert dup["code"] == int(RetCode.DATA_ERROR)
    assert dup["message"] == "Dataset name 'ds-1' already exists"

    # 遗留 helper 失败时返回已构造好的 HTTP 响应：_respond 原样透传
    monkeypatch.setattr(dataset_api_service, "create_dataset", lambda s, t, r: (False, JSONResponse(content={"legacy": True})))
    legacy = client.post("/api/v1/datasets", json={"name": "ds-1"})
    assert legacy.json() == {"legacy": True}


def test_dataset_list_envelope_with_total(client, monkeypatch):
    records: list[dict] = []

    def _list(s, tenant_id, args):
        _record(records, s)
        assert args["page"] == 2
        assert args["include_parsing_status"] is False
        return True, {"data": [{"id": "kb-1"}], "total": 41}

    monkeypatch.setattr(dataset_api_service, "list_datasets", _list)

    resp = client.get("/api/v1/datasets?page=2")

    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == [{"id": "kb-1"}]
    assert body["total_datasets"] == 41
    _assert_sync_facade(records)


def test_dataset_auto_metadata_roundtrip(client, monkeypatch):
    records: list[dict] = []
    cfg = {"enabled": True, "fields": [{"name": "author", "type": "str", "description": None, "examples": None, "restrict_values": False}]}
    monkeypatch.setattr(dataset_api_service, "get_auto_metadata", lambda s, t, d: _record(records, s) or (True, cfg))
    monkeypatch.setattr(dataset_api_service, "update_auto_metadata", lambda s, t, d, c: _record(records, s) or (True, {"enabled": c["enabled"], "fields": c["fields"]}))

    got = client.get("/api/v1/datasets/kb-1/auto_metadata").json()
    put = client.put("/api/v1/datasets/kb-1/auto_metadata", json={"enabled": False, "fields": []}).json()

    assert got["code"] == 0 and got["data"] == cfg
    assert put["code"] == 0 and put["data"] == {"enabled": False, "fields": []}
    _assert_sync_facade(records)


def test_dataset_trace_routes_use_sync_facade(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(dataset_api_service, "trace_graphrag", lambda s, t, d: _record(records, s) or (True, {"progress": 0.5}))
    monkeypatch.setattr(dataset_api_service, "trace_raptor", lambda s, t, d: _record(records, s) or (False, "No authorization."))

    graph = client.get("/api/v1/datasets/kb-1/trace_graphrag").json()
    raptor = client.get("/api/v1/datasets/kb-1/trace_raptor").json()

    assert graph["code"] == 0 and graph["data"] == {"progress": 0.5}
    assert raptor["code"] == int(RetCode.DATA_ERROR) and raptor["message"] == "No authorization."
    _assert_sync_facade(records)


# ---------------------------------------------------------------------------
# 混轨形态（service *_async 包装：工作线程 + db_connection 自开短会话）
# ---------------------------------------------------------------------------


def test_dataset_delete_runs_off_loop_with_own_sync_session(client, monkeypatch):
    records: list[dict] = []

    def _delete(s, tenant_id, ids, delete_all):
        _record(records, s)
        assert (tenant_id, ids, delete_all) == ("tenant-unit", ["kb-1"], False)
        return True, None

    monkeypatch.setattr(dataset_api_service, "delete_datasets", _delete)

    resp = client.request("DELETE", "/api/v1/datasets", json={"ids": ["kb-1"]})

    body = resp.json()
    assert body["code"] == 0
    _assert_sync_facade(records, off_loop=True)  # 混轨块必须在工作线程执行


def test_dataset_update_runs_off_loop_with_own_sync_session(client, monkeypatch):
    records: list[dict] = []

    def _update(s, tenant_id, dataset_id, req):
        _record(records, s)
        assert (dataset_id, req) == ("kb-1", {"name": "renamed"})
        return True, {"id": "kb-1", "name": "renamed"}

    monkeypatch.setattr(dataset_api_service, "update_dataset", _update)

    resp = client.put("/api/v1/datasets/kb-1", json={"name": "renamed"})

    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "kb-1", "name": "renamed"}
    _assert_sync_facade(records, off_loop=True)


def test_dataset_run_tasks_run_off_loop(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(dataset_api_service, "run_graphrag", lambda s, t, d: _record(records, s) or (True, {"graphrag_task_id": "t-1"}))
    monkeypatch.setattr(dataset_api_service, "run_raptor", lambda s, t, d: _record(records, s) or (True, {"raptor_task_id": "t-2"}))

    graph = client.post("/api/v1/datasets/kb-1/run_graphrag").json()
    raptor = client.post("/api/v1/datasets/kb-1/run_raptor").json()

    assert graph["code"] == 0 and graph["data"] == {"graphrag_task_id": "t-1"}
    assert raptor["code"] == 0 and raptor["data"] == {"raptor_task_id": "t-2"}
    _assert_sync_facade(records, off_loop=True)


# ---------------------------------------------------------------------------
# delete_knowledge_graph（service 原位 async：DB run_sync + doc-store to_thread）
# ---------------------------------------------------------------------------


async def test_delete_knowledge_graph_offloads_doc_store_delete(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kb_id: SimpleNamespace(tenant_id="tenant-unit", name="kb")))
    deletes: list[dict] = []

    def _delete(condition, index_name, kb_id):
        deletes.append({"condition": condition, "index_name": index_name, "kb_id": kb_id, "off_loop": threading.current_thread() is not threading.main_thread()})

    monkeypatch.setattr(settings, "docStoreConn", SimpleNamespace(delete=_delete))

    assert await dataset_api_service.delete_knowledge_graph(async_db, "tenant-unit", "kb1") == (True, True)
    assert deletes[0]["condition"] == {"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}
    assert deletes[0]["kb_id"] == "kb1"
    assert deletes[0]["off_loop"] is True  # doc-store 同步 HTTP 必须在工作线程执行


async def test_delete_knowledge_graph_denies_without_access(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: False))

    assert await dataset_api_service.delete_knowledge_graph(async_db, "tenant-unit", "kb1") == (False, "No authorization.")


def test_delete_knowledge_graph_route_denied_shape(client, monkeypatch):
    async def _fake(db, tenant_id, dataset_id):
        return False, "No authorization."

    monkeypatch.setattr(dataset_api_service, "delete_knowledge_graph", _fake)

    body = client.delete("/api/v1/datasets/kb-1/knowledge_graph").json()

    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."


# ---------------------------------------------------------------------------
# 依赖树（11 条路由全部纯异步轨）
# ---------------------------------------------------------------------------


def test_dataset_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("POST", "/api/v1/datasets"),
        ("DELETE", "/api/v1/datasets"),
        ("GET", "/api/v1/datasets"),
        ("PUT", "/api/v1/datasets/{dataset_id}"),
        ("GET", "/api/v1/datasets/{dataset_id}/auto_metadata"),
        ("PUT", "/api/v1/datasets/{dataset_id}/auto_metadata"),
        ("GET", "/api/v1/datasets/{dataset_id}/knowledge_graph"),
        ("DELETE", "/api/v1/datasets/{dataset_id}/knowledge_graph"),
        ("POST", "/api/v1/datasets/{dataset_id}/run_graphrag"),
        ("GET", "/api/v1/datasets/{dataset_id}/trace_graphrag"),
        ("POST", "/api/v1/datasets/{dataset_id}/run_raptor"),
        ("GET", "/api/v1/datasets/{dataset_id}/trace_raptor"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert current_tenant_id not in calls, f"{method} {path} 依赖树含同步 current_tenant_id"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_tenant_id in calls, f"{method} {path} 缺异步鉴权依赖"
        assert get_async_db in calls, f"{method} {path} 缺 AsyncSession 依赖"
