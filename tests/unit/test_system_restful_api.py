"""system / config RESTful API 契约测试。

system 的 version/token 四条与 config 的两条 authenticated 路由已切纯异步轨
（AsyncSession + async_current_user）：走真实 ``api.apps.app`` 的 HTTP 契约式测试。
service 桩保留真实类型契约——记录并断言同步 service 收到的是 run_sync 的同步
facade（``sqlalchemy.orm.Session``），"AsyncSession 直递同步 service" 的变异必红
（79b6007d 判例同型防线）。ping / healthz 不在转换范围，保持原有直调形态。
"""

import asyncio
from types import SimpleNamespace

from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.deps import get_doc_store, get_storage
from api.apps.restful_apis import system_api
from api.db.db_models import get_async_db, get_db
from api.db.services.api_service import APITokenService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import UserTenantService
from api.utils import health_utils
from api.utils.api_utils import async_current_user
from common.constants import RetCode
from common.versions import get_multirag_version
from tests.unit.conftest import iter_api_routes


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _stub_owner(monkeypatch, sessions, rows=None):
    """UserTenantService.query 桩：记录收到的 session（类型契约锚点）。"""
    if rows is None:
        rows = [SimpleNamespace(role="owner", tenant_id="tenant-1")]

    def _query(cls, s, **kwargs):
        sessions.append(s)
        assert kwargs == {"user_id": "user-unit"}  # 鉴权产物 Principal 的 id 必须传进查询
        return rows

    monkeypatch.setattr(UserTenantService, "query", classmethod(_query))


def _assert_sync_facade(sessions):
    """同步 service 必须运行在 run_sync 的同步 facade 上（AsyncSession 直递必红）。"""
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def test_system_restful_ping_returns_pong():
    response = asyncio.run(system_api.ping())

    assert response.status_code == 200
    assert response.body == b"pong"


def test_system_restful_healthz_sets_status(monkeypatch):
    async def _ok(_db):
        return {"status": "ok"}, True

    async def _degraded(_db):
        return {"status": "degraded"}, False

    monkeypatch.setattr(system_api, "run_health_checks_async", _ok)
    response = Response()

    assert asyncio.run(system_api.healthz(response, db=AsyncSession())) == {"status": "ok"}
    assert response.status_code == 200

    monkeypatch.setattr(system_api, "run_health_checks_async", _degraded)
    response = Response()

    assert asyncio.run(system_api.healthz(response, db=AsyncSession())) == {"status": "degraded"}
    assert response.status_code == 500


def test_system_restful_version_returns_version_envelope(client):
    resp = client.get("/api/v1/system/version")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == get_multirag_version()


def test_system_restful_status_reports_components(client, monkeypatch):
    sessions: list[object] = []

    class FakeDocStore:
        def health(self):
            return {"type": "milvus", "status": "green"}

    class FakeStorage:
        def health(self):
            return True

    def _get_kb(cls, db, kb_id):
        sessions.append(db)
        assert kb_id == "x"
        return None

    route_module = _system_route_module()
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(_get_kb))
    monkeypatch.setattr(
        route_module,
        "get_pool_status",
        lambda: {
            "pool_size": 10,
            "checked_out": 2,
            "checked_in": 8,
            "overflow": 0,
            "total_connections": 10,
            "usage_rate": 20,
        },
    )
    monkeypatch.setattr(route_module.settings, "STORAGE_IMPL_TYPE", "MINIO")
    monkeypatch.setattr(route_module.REDIS_CONN, "health", lambda: True)
    monkeypatch.setattr(route_module.REDIS_CONN, "smembers", lambda key: ["executor-1"] if key == "TASKEXE" else [])
    monkeypatch.setattr(
        route_module.REDIS_CONN,
        "zrangebyscore",
        lambda key, _minimum, _maximum: ['{"name":"worker-1","pending":0}'] if key == "executor-1" else [],
    )
    client.app.dependency_overrides[get_doc_store] = FakeDocStore
    client.app.dependency_overrides[get_storage] = FakeStorage

    resp = client.get("/api/v1/system/status")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"]["doc_engine"]["type"] == "milvus"
    assert body["data"]["storage"]["storage"] == "minio"
    assert body["data"]["storage"]["status"] == "green"
    assert body["data"]["database"]["status"] == "green"
    assert body["data"]["database_pool"]["usage_rate"] == "20%"
    assert body["data"]["redis"]["status"] == "green"
    assert body["data"]["task_executor_heartbeats"] == {"executor-1": [{"name": "worker-1", "pending": 0}]}
    _assert_sync_facade(sessions)


def test_system_restful_oceanbase_status_and_config(client, monkeypatch):
    route_module = _system_route_module()
    monkeypatch.setattr(route_module, "get_oceanbase_status", lambda: {"status": "alive"})
    monkeypatch.setattr(route_module.settings, "REGISTER_ENABLED", 0)
    monkeypatch.setattr(route_module.settings, "DISABLE_PASSWORD_LOGIN", True)

    oceanbase = client.get("/api/v1/system/oceanbase/status")
    config = client.get("/api/v1/system/config")

    assert oceanbase.status_code == 200
    assert oceanbase.json()["data"] == {"status": "alive"}
    assert config.status_code == 200
    assert config.json()["data"] == {"registerEnabled": 0, "disablePasswordLogin": True}


def test_system_restful_token_list_backfills_missing_beta(client, monkeypatch):
    sessions: list[object] = []
    _stub_owner(monkeypatch, sessions)

    def _query_tokens(cls, s, **kwargs):
        sessions.append(s)
        assert kwargs == {"tenant_id": "tenant-1"}
        return [Obj(token="tok-1", beta="", name="old", description=None)]

    updates: list[dict] = []

    def _filter_update(cls, s, filters, payload):
        sessions.append(s)
        updates.append(dict(payload))
        return 1

    monkeypatch.setattr(APITokenService, "query", classmethod(_query_tokens))
    monkeypatch.setattr(APITokenService, "filter_update", classmethod(_filter_update))

    resp = client.get("/api/v1/system/tokens")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert len(body["data"]) == 1
    assert len(body["data"][0]["beta"]) == 32  # 缺失 beta 的旧记录回填
    assert updates[0]["token"] == "tok-1"
    assert updates[0]["beta"] == body["data"][0]["beta"]
    _assert_sync_facade(sessions)


def test_system_restful_token_create_allows_empty_body_default_name(client, monkeypatch):
    sessions: list[object] = []
    _stub_owner(monkeypatch, sessions)
    saved: list[dict] = []

    def _save(cls, s, **kwargs):
        sessions.append(s)
        saved.append(kwargs)
        return Obj(**kwargs)

    monkeypatch.setattr(APITokenService, "save", classmethod(_save))

    resp = client.post("/api/v1/system/tokens")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"]["name"] == "API Token"
    assert body["data"]["tenant_id"] == "tenant-1"
    assert body["data"]["token"].startswith("multirag-")
    assert len(body["data"]["beta"]) == 32
    assert saved[0]["name"] == "API Token"
    assert saved[0]["description"] is None
    _assert_sync_facade(sessions)


def test_system_restful_token_create_body_contract(client, monkeypatch):
    sessions: list[object] = []
    _stub_owner(monkeypatch, sessions)
    saved: list[dict] = []

    def _save(cls, s, **kwargs):
        sessions.append(s)
        saved.append(kwargs)
        return Obj(**kwargs)

    monkeypatch.setattr(APITokenService, "save", classmethod(_save))

    body_wins = client.post("/api/v1/system/tokens?name=query-name", json={"name": "body-name", "description": "   "})
    query_fallback = client.post("/api/v1/system/tokens?name=query-name", json={"description": " desc "})

    assert body_wins.json()["retcode"] == 0
    assert saved[0]["name"] == "body-name"  # body name 优先于 query name
    assert saved[0]["description"] is None  # 空白 description 归一为 None
    assert query_fallback.json()["retcode"] == 0
    assert saved[1]["name"] == "query-name"  # body 无 name 时回退 query name
    assert saved[1]["description"] == "desc"  # str_strip_whitespace 修剪
    _assert_sync_facade(sessions)


def test_system_restful_token_delete_uses_path_token(client, monkeypatch):
    sessions: list[object] = []
    _stub_owner(monkeypatch, sessions)
    deleted: list[list] = []

    def _filter_delete(cls, s, filters):
        sessions.append(s)
        deleted.append(filters)
        return 1

    monkeypatch.setattr(APITokenService, "filter_delete", classmethod(_filter_delete))

    resp = client.delete("/api/v1/system/tokens/tok-9")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] is True
    tenant_clause, token_clause = deleted[0]
    assert tenant_clause.right.value == "tenant-1"
    assert token_clause.right.value == "tok-9"  # 删除条件取自 path 中的 token
    _assert_sync_facade(sessions)


def test_system_restful_token_routes_report_missing_owner_tenant(client, monkeypatch):
    sessions: list[object] = []
    _stub_owner(monkeypatch, sessions, rows=[SimpleNamespace(role="normal", tenant_id="tenant-1")])

    responses = [
        client.get("/api/v1/system/tokens"),
        client.post("/api/v1/system/tokens"),
        client.delete("/api/v1/system/tokens/tok-1"),
    ]

    for resp in responses:
        assert resp.status_code == 200
        body = resp.json()
        assert body["retcode"] == int(RetCode.DATA_ERROR)
        assert body["retmsg"] == "Tenant not found!"
    _assert_sync_facade(sessions)


def _dependency_calls(app, method: str, path: str) -> set:
    """展开路由的完整 Depends 树，返回全部依赖 callable。

    ``Depends(manager)`` 的 LoginManager 实例同样以 ``dep.call`` 形态出现在树里
    （legacy ``/v1/system/version`` 阳性对照验证过），无需另走安全依赖结构。
    """
    for route in iter_api_routes(app):
        if route.path == path and method in route.methods:
            calls = set()
            stack = [route.dependant]
            while stack:
                dep = stack.pop()
                if dep.call is not None:
                    calls.add(dep.call)
                stack.extend(dep.dependencies)
            return calls
    raise AssertionError(f"route not found: {method} {path}")


def test_system_authenticated_routes_have_pure_async_dependency_tree(client):
    """需要数据库的已换轨路由不得再出现 get_db / manager 同步轨。"""
    import api.apps as api_apps

    for method, path in (
        ("GET", "/api/v1/system/version"),
        ("GET", "/api/v1/system/status"),
        ("GET", "/api/v1/system/tokens"),
        ("POST", "/api/v1/system/tokens"),
        ("DELETE", "/api/v1/system/tokens/{token}"),
    ):
        calls = _dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_user in calls, f"{method} {path} 缺异步鉴权依赖"
        assert get_async_db in calls, f"{method} {path} 缺 AsyncSession 依赖"

    oceanbase_calls = _dependency_calls(client.app, "GET", "/api/v1/system/oceanbase/status")
    assert get_db not in oceanbase_calls
    assert api_apps.manager not in oceanbase_calls
    assert async_current_user in oceanbase_calls


def test_legacy_system_routes_are_removed(client):
    routes = {(method, route.path) for route in iter_api_routes(client.app) for method in route.methods}

    for method, path in (
        ("GET", "/v1/system/status"),
        ("GET", "/v1/system/config"),
        ("GET", "/v1/system/oceanbase/status"),
        ("GET", "/v1/system/version"),
        ("GET", "/v1/system/healthz"),
        ("GET", "/v1/system/ping"),
        ("POST", "/v1/system/new_token"),
        ("GET", "/v1/system/token_list"),
        ("POST", "/v1/system/rm"),
        ("GET", "/v1/system/log_levels"),
        ("PUT", "/v1/system/log_levels"),
    ):
        assert (method, path) not in routes


def _system_route_module():
    """app 路由绑定的是 register_page 经 spec loader 加载的模块实例，
    与常规导入的 ``api.apps.restful_apis.system_api`` 不是同一对象——
    打桩模块级名字必须打在前者上（monkeypatch 真实 service 类则两边通用）。"""
    import sys

    return sys.modules["api.apps.restful_apis.system"]


def test_config_restful_log_level_validation(client, monkeypatch):
    monkeypatch.setattr(_system_route_module(), "set_log_level", lambda pkg_name, level: level == "INFO")

    ok = client.put("/api/v1/system/config/log", json={"pkg_name": "core", "level": "INFO"}).json()
    bad = client.put("/api/v1/system/config/log", json={"pkg_name": "core", "level": "NOPE"}).json()

    assert ok["retcode"] == 0
    assert ok["data"] == {"pkg_name": "core", "level": "INFO"}
    assert bad["retmsg"] == "Invalid log level: NOPE"


def test_config_restful_log_level_listing(client, monkeypatch):
    monkeypatch.setattr(_system_route_module(), "get_log_levels", lambda: {"core": "INFO"})

    resp = client.get("/api/v1/system/config/log")

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"core": "INFO"}


def test_config_routes_have_pure_async_dependency_tree(client):
    import api.apps as api_apps

    for method, path in (("GET", "/api/v1/system/config/log"), ("PUT", "/api/v1/system/config/log")):
        calls = _dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_user in calls, f"{method} {path} 缺异步鉴权依赖"
        assert get_async_db in calls, f"{method} {path} 缺 AsyncSession 依赖"


def test_multirag_server_alive_uses_restful_ping(monkeypatch):
    seen = {}

    class DummyResponse:
        status_code = 200

    def fake_get(url, *, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return DummyResponse()

    monkeypatch.setattr(health_utils.settings, "HOST_IP", "0.0.0.0")
    monkeypatch.setattr(health_utils.settings, "HOST_PORT", "9380")
    monkeypatch.setattr(health_utils.requests, "get", fake_get)

    assert health_utils.check_multirag_server_alive()["status"] == "alive"
    assert seen["url"] == "http://127.0.0.1:9380/api/v1/system/ping"
    assert seen["timeout"] == 10
