"""memory RESTful API 契约测试（Phase 2.5 批次 4：AsyncSession 收口）。

路由按 IO 面分形态：
- 纯 DB（create/list/config，MemoryService 全 DB 面）：路由层 run_sync——桩断言
  收到同步 facade（``sqlalchemy.orm.Session``）；
- 混轨 8 条（消息面 MessageService=msg_store、update 的 Redis size cache、search 的
  query_message 内嵌 embedding）：service ``*_async`` 包装——桩断言在工作线程收到
  ``db_connection`` 自开的同步 Session。
add_message 已于 11.11 转换（tests 见 test_memory_add_message_route.py）。
"""

import sys
import threading

from sqlalchemy.orm import Session

from api.apps.services import memory_api_service
from api.db.db_models import get_db
from api.utils.api_utils import async_current_user
from common.constants import RetCode
from common.exceptions import ArgumentException, NotFoundException


def _record(records, s):
    records.append({"session": s, "off_loop": threading.current_thread() is not threading.main_thread()})


def _assert_sync_facade(records, *, off_loop=None):
    assert records
    for r in records:
        assert isinstance(r["session"], Session), f"同步 service 收到 {type(r['session']).__name__}，应为 sqlalchemy.orm.Session"
        if off_loop is not None:
            assert r["off_loop"] is off_loop


def _route_module():
    return sys.modules["api.apps.restful_apis.memory"]


_CREATE_BODY = {"name": "mem-1", "memory_type": ["raw"], "embd_id": "e@f", "llm_id": "l@f"}


# ---------------------------------------------------------------------------
# 纯 DB 形态（路由层 run_sync）
# ---------------------------------------------------------------------------


def test_memory_create_uses_sync_facade(client, monkeypatch):
    records: list[dict] = []

    def _ensure(s, tenant_id, params, *, strict=False):
        _record(records, s)
        assert tenant_id == "user-unit"
        assert strict is True
        return dict(params, tenant_llm_id=7)

    monkeypatch.setattr(_route_module(), "ensure_tenant_model_id_for_params", _ensure)

    def _create(s, tenant_id, memory_info):
        _record(records, s)
        assert memory_info["tenant_llm_id"] == 7  # 路由层 ensure 的产物要进 service
        return True, {"memory_id": "m-1", "name": "mem-1"}

    monkeypatch.setattr(memory_api_service, "create_memory", _create)

    resp = client.post("/api/v1/memories", json=_CREATE_BODY)

    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"memory_id": "m-1", "name": "mem-1"}
    _assert_sync_facade(records)


def test_memory_create_argument_error_envelope(client, monkeypatch):
    monkeypatch.setattr(_route_module(), "ensure_tenant_model_id_for_params", lambda s, t, p, **kwargs: p)

    def _create(s, tenant_id, memory_info):
        raise ArgumentException("Memory name cannot be empty or whitespace.")

    monkeypatch.setattr(memory_api_service, "create_memory", _create)

    body = client.post("/api/v1/memories", json=_CREATE_BODY).json()

    assert body["retcode"] == int(RetCode.ARGUMENT_ERROR)
    assert body["retmsg"] == "Memory name cannot be empty or whitespace."
    assert body["data"] is False


def test_memory_create_rejects_unresolved_chat_model(client, monkeypatch):
    def _ensure(_session, _tenant_id, _params, *, strict=False):
        assert strict is True
        raise ArgumentException("Tenant Model with name gemini and type chat not found")

    def _create(*_args):
        raise AssertionError("create_memory must not run")

    monkeypatch.setattr(_route_module(), "ensure_tenant_model_id_for_params", _ensure)
    monkeypatch.setattr(memory_api_service, "create_memory", _create)

    body = client.post("/api/v1/memories", json=_CREATE_BODY).json()

    assert body["retcode"] == int(RetCode.ARGUMENT_ERROR)
    assert body["retmsg"] == "Tenant Model with name gemini and type chat not found"
    assert body["data"] is False


def test_memory_list_envelope(client, monkeypatch):
    records: list[dict] = []
    payload = {"memory_list": [{"id": "m-1"}], "total_count": 1}

    def _list(s, user_id, filter_params, keywords, page, page_size):
        _record(records, s)
        assert user_id == "user-unit"
        assert (filter_params, keywords, page, page_size) == ({"storage_type": "es"}, "kw", 2, 10)
        return payload

    monkeypatch.setattr(memory_api_service, "list_memory", _list)

    body = client.get("/api/v1/memories?storage_type=es&keywords=kw&page=2&page_size=10").json()

    assert body["retcode"] == 0
    assert body["data"] == payload
    _assert_sync_facade(records)


def test_memory_config_success_and_not_found(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(memory_api_service, "get_memory_config", lambda s, mid: _record(records, s) or {"id": mid, "name": "mem"})

    ok = client.get("/api/v1/memories/m-1/config").json()
    assert ok["retcode"] == 0 and ok["data"] == {"id": "m-1", "name": "mem"}
    _assert_sync_facade(records)

    def _missing(s, mid):
        raise NotFoundException(f"Memory '{mid}' not found.")

    monkeypatch.setattr(memory_api_service, "get_memory_config", _missing)
    missing = client.get("/api/v1/memories/m-x/config").json()
    assert missing["retcode"] == int(RetCode.NOT_FOUND)
    assert missing["retmsg"] == "Memory 'm-x' not found."


# ---------------------------------------------------------------------------
# 混轨形态（service *_async 包装：工作线程 + db_connection 自开短会话）
# ---------------------------------------------------------------------------


def test_memory_update_runs_off_loop(client, monkeypatch):
    records: list[dict] = []

    def _update(s, memory_id, new_settings):
        _record(records, s)
        assert (memory_id, new_settings) == ("m-1", {"name": "renamed"})
        return True, {"id": "m-1", "name": "renamed"}

    monkeypatch.setattr(memory_api_service, "update_memory", _update)

    body = client.put("/api/v1/memories/m-1", json={"name": "renamed"}).json()

    assert body["retcode"] == 0
    assert body["data"] == {"id": "m-1", "name": "renamed"}
    _assert_sync_facade(records, off_loop=True)


def test_memory_delete_runs_off_loop(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(memory_api_service, "delete_memory", lambda s, mid: _record(records, s) or True)

    body = client.delete("/api/v1/memories/m-1").json()

    assert body["retcode"] == 0
    assert body["data"] is True
    _assert_sync_facade(records, off_loop=True)


def test_memory_detail_messages_runs_off_loop(client, monkeypatch):
    records: list[dict] = []
    payload = {"messages": {"message_list": []}, "storage_type": "es"}

    def _detail(s, memory_id, agent_ids, keywords, page, page_size):
        _record(records, s)
        assert (memory_id, agent_ids, keywords) == ("m-1", ["a-1", "a-2"], "kw")
        return payload

    monkeypatch.setattr(memory_api_service, "get_memory_messages", _detail)

    body = client.get("/api/v1/memories/m-1?agent_id=a-1,a-2&keywords= kw ").json()

    assert body["retcode"] == 0
    assert body["data"] == payload
    _assert_sync_facade(records, off_loop=True)


def test_message_ops_run_off_loop(client, monkeypatch):
    records: list[dict] = []
    forgotten: list[tuple] = []
    monkeypatch.setattr(memory_api_service, "forget_message", lambda s, mid, msg_id: _record(records, s) or forgotten.append((mid, msg_id)) or True)
    monkeypatch.setattr(memory_api_service, "update_message_status", lambda s, mid, msg_id, status: _record(records, s) or status)
    monkeypatch.setattr(memory_api_service, "get_message_content", lambda s, mid, msg_id: _record(records, s) or {"message_id": msg_id})

    forget = client.delete("/api/v1/messages/m-1:42").json()
    status = client.put("/api/v1/messages/m-1:42", json={"status": True}).json()
    content = client.get("/api/v1/messages/m-1:42/content").json()

    assert forget["retcode"] == 0 and forget["data"] is True
    assert forgotten == [("m-1", 42)]  # path 复合参数 memory_id:message_id 解析
    assert status["retcode"] == 0 and status["data"] is True
    assert content["retcode"] == 0 and content["data"] == {"message_id": 42}
    _assert_sync_facade(records, off_loop=True)


def test_message_search_and_recent_run_off_loop(client, monkeypatch):
    records: list[dict] = []

    def _search(s, filter_dict, params):
        _record(records, s)
        assert filter_dict["memory_id"] == ["m-1", "m-2"]  # 逗号形态拆分
        assert params["top_n"] == 3
        return [{"message_id": 1}]

    monkeypatch.setattr(memory_api_service, "search_message", _search)

    def _recent(s, memory_ids, agent_id, session_id, limit):
        _record(records, s)
        assert (memory_ids, limit) == (["m-1"], 5)
        return [{"message_id": 2}]

    monkeypatch.setattr(memory_api_service, "get_messages", _recent)

    search = client.get("/api/v1/messages/search?memory_id=m-1,m-2&query=q&top_n=3").json()
    recent = client.get("/api/v1/messages?memory_id=m-1&limit=5").json()

    assert search["retcode"] == 0 and search["data"] == [{"message_id": 1}]
    assert recent["retcode"] == 0 and recent["data"] == [{"message_id": 2}]
    _assert_sync_facade(records, off_loop=True)


# ---------------------------------------------------------------------------
# 依赖树（12 条路由全部纯异步轨）
# ---------------------------------------------------------------------------


def test_memory_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("POST", "/api/v1/memories"),
        ("PUT", "/api/v1/memories/{memory_id}"),
        ("DELETE", "/api/v1/memories/{memory_id}"),
        ("GET", "/api/v1/memories"),
        ("GET", "/api/v1/memories/{memory_id}/config"),
        ("GET", "/api/v1/memories/{memory_id}"),
        ("POST", "/api/v1/messages"),
        ("DELETE", "/api/v1/messages/{memory_id}:{message_id}"),
        ("PUT", "/api/v1/messages/{memory_id}:{message_id}"),
        ("GET", "/api/v1/messages/search"),
        ("GET", "/api/v1/messages"),
        ("GET", "/api/v1/messages/{memory_id}:{message_id}/content"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_user in calls, f"{method} {path} 缺异步鉴权依赖"
