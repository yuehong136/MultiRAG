"""search RESTful API 契约测试（Phase 2.5 批次 1：AsyncSession 收口）。

五条路由走真实 ``api.apps.app`` 的 HTTP 契约式测试。service 桩保留真实类型契约
——记录并断言同步 service 收到 run_sync 的同步 facade（``sqlalchemy.orm.Session``），
"AsyncSession 直递同步 service" 的变异必红。
响应形状锁 REST 风格 envelope：成功 ``{"code": 0, "data": ...}``，
失败 ``{"code": <RetCode>, "message": ...}``（get_result 非 SUCCESS 时丢弃 data）。
"""

from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.services.search_service import SearchService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _assert_sync_facade(sessions):
    """同步 service 必须运行在 run_sync 的同步 facade 上（AsyncSession 直递必红）。"""
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def _stub_tenant_exists(monkeypatch, sessions):
    def _get_by_id(cls, s, tenant_id):
        sessions.append(s)
        return SimpleNamespace(id=tenant_id)

    monkeypatch.setattr(TenantService, "get_by_id", classmethod(_get_by_id))


def test_search_create_persists_owner_fields(client, monkeypatch):
    sessions: list[object] = []
    _stub_tenant_exists(monkeypatch, sessions)
    monkeypatch.setattr(SearchService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or []))
    saved: list[dict] = []

    def _save(cls, s, **kwargs):
        sessions.append(s)
        saved.append(kwargs)
        return SimpleNamespace(id=kwargs["id"])

    monkeypatch.setattr(SearchService, "save", classmethod(_save))

    resp = client.post("/api/v1/searches", json={"name": " demo ", "description": "d"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"search_id": saved[0]["id"]}
    assert saved[0]["name"] == "demo"  # 首尾空白修剪
    assert saved[0]["tenant_id"] == "tenant-unit"
    assert saved[0]["created_by"] == "tenant-unit"
    _assert_sync_facade(sessions)


def test_search_create_rejects_empty_name(client):
    resp = client.post("/api/v1/searches", json={"name": "   "})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Search name can't be empty."


def test_search_list_returns_apps_and_total(client, monkeypatch):
    sessions: list[object] = []
    rows = [{"id": "s1", "tenant_id": "tenant-unit", "name": "demo"}]

    def _get_by_tenant_ids(cls, s, joined_ids, user_id, page, page_size, orderby, desc, keywords):
        sessions.append(s)
        assert (joined_ids, user_id) == ([], "tenant-unit")
        return rows, 1

    monkeypatch.setattr(SearchService, "get_by_tenant_ids", classmethod(_get_by_tenant_ids))

    resp = client.get("/api/v1/searches")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"search_apps": rows, "total": 1}
    _assert_sync_facade(sessions)


def test_search_get_returns_detail_for_member(client, monkeypatch):
    sessions: list[object] = []
    detail = {"id": "s1", "name": "demo", "search_config": {}}
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or [SimpleNamespace(tenant_id="tenant-unit")]))
    monkeypatch.setattr(SearchService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or [SimpleNamespace(id="s1")]))
    monkeypatch.setattr(SearchService, "get_detail", classmethod(lambda cls, s, sid: sessions.append(s) or dict(detail)))

    resp = client.get("/api/v1/searches/s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == detail
    _assert_sync_facade(sessions)


def test_search_get_denies_non_member(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or []))

    resp = client.get("/api/v1/searches/s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.OPERATING_ERROR)
    assert body["message"] == "Has no permission for this operation."
    _assert_sync_facade(sessions)


def test_search_update_merges_config_and_returns_row(client, monkeypatch):
    sessions: list[object] = []
    _stub_tenant_exists(monkeypatch, sessions)
    monkeypatch.setattr(SearchService, "accessible4deletion", classmethod(lambda cls, s, sid, tid: sessions.append(s) or True))

    def _query(cls, s, **kwargs):
        sessions.append(s)
        if "id" in kwargs:
            return [SimpleNamespace(id="s1", name="old", search_config={"a": 1})]
        return []  # 重名探测

    monkeypatch.setattr(SearchService, "query", classmethod(_query))
    updates: list[tuple[str, dict]] = []
    monkeypatch.setattr(SearchService, "update_by_id", classmethod(lambda cls, s, sid, values: sessions.append(s) or updates.append((sid, values)) or True))
    monkeypatch.setattr(SearchService, "get_by_id", classmethod(lambda cls, s, sid: sessions.append(s) or Obj(id="s1", name="new", search_config={"a": 1, "b": 2})))

    resp = client.put("/api/v1/searches/s1", json={"name": " new ", "search_config": {"b": 2}})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "s1", "name": "new", "search_config": {"a": 1, "b": 2}}
    sid, values = updates[0]
    assert sid == "s1"
    assert values["name"] == "new"
    assert values["search_config"] == {"a": 1, "b": 2}  # 旧配置浅合并新补丁
    _assert_sync_facade(sessions)


def test_search_delete_requires_ownership(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(SearchService, "accessible4deletion", classmethod(lambda cls, s, sid, tid: sessions.append(s) or False))

    resp = client.delete("/api/v1/searches/s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."
    _assert_sync_facade(sessions)


def test_search_delete_success(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(SearchService, "accessible4deletion", classmethod(lambda cls, s, sid, tid: sessions.append(s) or True))
    monkeypatch.setattr(SearchService, "delete_by_id", classmethod(lambda cls, s, sid: sessions.append(s) or True))

    resp = client.delete("/api/v1/searches/s1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] is True
    _assert_sync_facade(sessions)


def test_search_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("POST", "/api/v1/searches"),
        ("GET", "/api/v1/searches"),
        ("GET", "/api/v1/searches/{search_id}"),
        ("PUT", "/api/v1/searches/{search_id}"),
        ("DELETE", "/api/v1/searches/{search_id}"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert current_tenant_id not in calls, f"{method} {path} 依赖树含同步 current_tenant_id"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_tenant_id in calls, f"{method} {path} 缺异步鉴权依赖"
        assert get_async_db in calls, f"{method} {path} 缺 AsyncSession 依赖"
