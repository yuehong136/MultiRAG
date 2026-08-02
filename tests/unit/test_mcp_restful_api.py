"""MCP RESTful API contract tests for the ``/api/v1/mcp/servers`` surface."""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.services.mcp_server_service import MCPServerService
from api.db.services.user_service import TenantService
from api.utils.api_utils import async_current_user
from common.constants import RetCode


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _route_module():
    return sys.modules["api.apps.restful_apis.mcp"]


def _server(**overrides):
    payload = {
        "id": "mcp-1",
        "tenant_id": "user-unit",
        "name": "server-one",
        "url": "https://example.test/mcp",
        "server_type": "sse",
        "description": "demo",
        "variables": {"authorization_token": "secret", "tools": {"old": {"name": "old"}}},
        "headers": {"Authorization": "Bearer token"},
    }
    payload.update(overrides)
    return Obj(**payload)


def _assert_sync_facades(sessions):
    assert sessions
    assert all(isinstance(session, Session) for session in sessions)


def test_mcp_list_uses_rest_query_contract_and_sync_facade(client, monkeypatch):
    sessions = []

    def _get_servers(cls, session, tenant_id, ids, page, page_size, orderby, desc, keywords):
        sessions.append(session)
        assert tenant_id == "user-unit"
        assert ids == ["mcp-1", "mcp-2", "mcp-3"]
        assert (page, page_size, orderby, desc, keywords) == (0, 0, "name", False, "demo")
        return [{"id": "mcp-1"}, {"id": "mcp-2"}, {"id": "mcp-3"}]

    monkeypatch.setattr(MCPServerService, "get_servers", classmethod(_get_servers))

    response = client.get(
        "/api/v1/mcp/servers",
        params=[
            ("mcp_ids", "mcp-1,mcp-2"),
            ("mcp_ids", "mcp-3"),
            ("keywords", "demo"),
            ("page", "2"),
            ("page_size", "1"),
            ("orderby", "name"),
            ("desc", "false"),
        ],
    )

    assert response.status_code == 200
    assert response.json() == {
        "retcode": 0,
        "retmsg": "success",
        "data": {"mcp_servers": [{"id": "mcp-2"}], "total": 3},
    }
    _assert_sync_facades(sessions)


def test_mcp_detail_and_download_are_tenant_scoped(client, monkeypatch):
    sessions = []

    def _get_or_none(cls, session, **filters):
        sessions.append(session)
        assert filters == {"id": "mcp-1", "tenant_id": "user-unit"}
        return _server()

    def _get_by_id(cls, session, mcp_id):
        sessions.append(session)
        assert mcp_id == "mcp-1"
        return _server()

    monkeypatch.setattr(MCPServerService, "get_or_none", classmethod(_get_or_none))
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(_get_by_id))

    detail = client.get("/api/v1/mcp/servers/mcp-1?mode=preview").json()
    download = client.get("/api/v1/mcp/servers/mcp-1?mode=download").json()

    assert detail["retcode"] == 0
    assert detail["data"]["id"] == "mcp-1"
    assert download["retcode"] == 0
    assert download["data"] == {
        "mcpServers": {
            "server-one": {
                "type": "sse",
                "url": "https://example.test/mcp",
                "name": "server-one",
                "authorization_token": "secret",
                "tools": {"old": {"name": "old"}},
            }
        }
    }
    _assert_sync_facades(sessions)

    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda cls, session, mcp_id: _server(tenant_id="other")))
    missing = client.get("/api/v1/mcp/servers/mcp-1?mode=download").json()
    assert missing["retcode"] == int(RetCode.DATA_ERROR)
    assert missing["retmsg"] == "Cannot find MCP server mcp-1 for user user-unit"


def test_mcp_create_validates_probes_and_persists_tools(client, monkeypatch):
    sessions = []
    inserted = []

    def _duplicate(cls, session, **filters):
        sessions.append(session)
        assert filters == {"name": "server-one", "tenant_id": "user-unit"}
        return False, None

    def _tenant(cls, session, tenant_id):
        sessions.append(session)
        assert tenant_id == "user-unit"
        return Obj(id=tenant_id)

    def _insert(cls, session, **payload):
        sessions.append(session)
        inserted.append(payload)
        return Obj(**payload)

    monkeypatch.setattr(MCPServerService, "get_by_name_and_tenant", classmethod(_duplicate))
    monkeypatch.setattr(MCPServerService, "insert", classmethod(_insert))
    monkeypatch.setattr(TenantService, "get_by_id", classmethod(_tenant))
    monkeypatch.setattr(_route_module(), "get_uuid", lambda: "new-id")
    monkeypatch.setattr(
        _route_module(),
        "get_mcp_tools",
        lambda servers, timeout: ({servers[0].id: [{"name": "search"}, {"invalid": True}]}, ""),
    )

    response = client.post(
        "/api/v1/mcp/servers",
        json={
            "name": "server-one",
            "url": "https://example.test/mcp",
            "server_type": "sse",
            "description": "demo",
            "variables": {"tools": {"stale": {}}, "token": "abc"},
            "headers": {"Authorization": "Bearer token"},
            "timeout": 2.5,
        },
    )

    body = response.json()
    assert response.status_code == 200
    assert body["retcode"] == 0
    assert body["data"]["id"] == "new-id"
    assert inserted[0]["tenant_id"] == "user-unit"
    assert inserted[0]["variables"] == {"token": "abc", "tools": {"search": {"name": "search"}}}
    _assert_sync_facades(sessions)


def test_mcp_update_uses_path_id_and_preserves_omitted_fields(client, monkeypatch):
    sessions = []
    updates = []
    updated = _server(name="renamed", variables={"tools": {"search": {"name": "search"}}})
    get_calls = 0

    def _get_by_id(cls, session, mcp_id):
        nonlocal get_calls
        sessions.append(session)
        assert mcp_id == "mcp-1"
        get_calls += 1
        return _server() if get_calls == 1 else updated

    def _filter_update(cls, session, filters, payload):
        sessions.append(session)
        updates.append(dict(payload))
        tenant_clause = filters[1]
        assert tenant_clause.right.value == "user-unit"
        return True

    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(_get_by_id))
    monkeypatch.setattr(MCPServerService, "filter_update", classmethod(_filter_update))
    monkeypatch.setattr(
        _route_module(),
        "get_mcp_tools",
        lambda servers, timeout: ({servers[0].id: [{"name": "search"}]}, ""),
    )

    response = client.put("/api/v1/mcp/servers/mcp-1", json={"name": "renamed"})

    body = response.json()
    assert body["retcode"] == 0
    assert body["data"]["name"] == "renamed"
    assert body["data"]["description"] == "demo"
    assert updates[0]["id"] == "mcp-1"
    assert updates[0]["url"] == "https://example.test/mcp"
    assert updates[0]["server_type"] == "sse"
    assert "description" not in updates[0]
    _assert_sync_facades(sessions)


def test_mcp_delete_rejects_cross_tenant_server(client, monkeypatch):
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda cls, session, mcp_id: _server(tenant_id="other")))

    def _must_not_delete(*args, **kwargs):
        raise AssertionError("cross-tenant server must not be deleted")

    monkeypatch.setattr(MCPServerService, "delete_by_ids", classmethod(_must_not_delete))

    body = client.delete("/api/v1/mcp/servers/mcp-1").json()

    assert body["retcode"] == int(RetCode.DATA_ERROR)
    assert body["retmsg"] == "Cannot find MCP server mcp-1 for user user-unit"


def test_mcp_import_accepts_standard_config_and_renames_duplicates(client, monkeypatch):
    inserted = []

    def _duplicate(cls, session, *, name, tenant_id):
        assert isinstance(session, Session)
        assert tenant_id == "user-unit"
        return name == "server-one", None

    def _insert(cls, session, **payload):
        assert isinstance(session, Session)
        inserted.append(payload)
        return Obj(**payload)

    monkeypatch.setattr(MCPServerService, "get_by_name_and_tenant", classmethod(_duplicate))
    monkeypatch.setattr(MCPServerService, "insert", classmethod(_insert))
    monkeypatch.setattr(_route_module(), "get_uuid", lambda: "imported-id")
    monkeypatch.setattr(
        _route_module(),
        "get_mcp_tools",
        lambda servers, timeout: ({servers[0].id: [{"name": "search"}]}, ""),
    )

    body = client.post(
        "/api/v1/mcp/servers/import",
        json={
            "mcpServers": {
                "server-one": {
                    "type": "streamable-http",
                    "url": "https://example.test/mcp",
                    "authorization_token": "secret",
                }
            }
        },
    ).json()

    assert body["retcode"] == 0
    assert body["data"]["results"][0]["new_name"] == "server-one_0"
    assert inserted[0]["id"] == "imported-id"
    assert inserted[0]["variables"] == {
        "authorization_token": "secret",
        "tools": {"search": {"name": "search"}},
    }


def test_mcp_test_endpoint_returns_enabled_tools(client, monkeypatch):
    class Tool:
        def model_dump(self):
            return {"name": "search"}

    class ToolSession:
        def __init__(self, server, variables):
            assert server.id == "preview"
            assert variables == {"token": "abc"}

        def get_tools(self, timeout):
            assert timeout == 3
            return [Tool()]

    closed = []
    monkeypatch.setattr(_route_module(), "MCPToolCallSession", ToolSession)
    monkeypatch.setattr(_route_module(), "close_multiple_mcp_toolcall_sessions", lambda sessions: closed.extend(sessions))

    body = client.post(
        "/api/v1/mcp/servers/preview/test",
        json={"url": "https://example.test/mcp", "server_type": "sse", "variables": {"token": "abc"}, "timeout": 3},
    ).json()

    assert body["retcode"] == 0
    assert body["data"] == [{"name": "search", "enabled": True}]
    assert len(closed) == 1


def test_mcp_tools_list_refreshes_owned_server_tools(client, monkeypatch):
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda cls, session, mcp_id: _server()))
    monkeypatch.setattr(
        _route_module(),
        "get_mcp_tools",
        lambda servers, timeout: ({servers[0].id: [{"name": "search", "enabled": False}]}, ""),
    )

    body = client.get("/api/v1/mcp/servers/mcp-1/tools?timeout=3").json()

    assert body["retcode"] == 0
    assert body["data"] == [{"name": "search", "enabled": False}]


def test_mcp_tool_test_uses_resource_path_and_closes_session(client, monkeypatch):
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda cls, session, mcp_id: _server()))
    calls = []
    closed = []

    class ToolSession:
        def __init__(self, server, variables):
            assert server.id == "mcp-1"
            assert variables["authorization_token"] == "secret"

        def tool_call(self, name, arguments, timeout):
            calls.append((name, arguments, timeout))
            return {"content": [{"text": "ok"}], "isError": False}

    monkeypatch.setattr(_route_module(), "MCPToolCallSession", ToolSession)
    monkeypatch.setattr(_route_module(), "close_multiple_mcp_toolcall_sessions", lambda sessions: closed.extend(sessions))

    body = client.post(
        "/api/v1/mcp/servers/mcp-1/tools/search/test",
        json={"arguments": {"query": "q"}, "timeout": 4},
    ).json()

    assert body["retcode"] == 0
    assert body["data"] == {"content": [{"text": "ok"}], "isError": False}
    assert calls == [("search", {"query": "q"}, 4)]
    assert len(closed) == 1


def test_mcp_tools_cache_preserves_variables_and_tenant_scope(client, monkeypatch):
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda cls, session, mcp_id: _server()))
    updates = []

    def _update(cls, session, filters, payload):
        assert isinstance(session, Session)
        assert filters[1].right.value == "user-unit"
        updates.append(payload)
        return True

    monkeypatch.setattr(MCPServerService, "filter_update", classmethod(_update))

    body = client.put(
        "/api/v1/mcp/servers/mcp-1/tools",
        json={"tools": [{"name": "search", "enabled": False}]},
    ).json()

    assert body["retcode"] == 0
    assert body["data"] == {"search": {"name": "search", "enabled": False}}
    assert updates[0]["variables"] == {
        "authorization_token": "secret",
        "tools": {"search": {"name": "search", "enabled": False}},
    }


def test_mcp_restful_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("GET", "/api/v1/mcp/servers"),
        ("GET", "/api/v1/mcp/servers/{mcp_id}"),
        ("POST", "/api/v1/mcp/servers"),
        ("PUT", "/api/v1/mcp/servers/{mcp_id}"),
        ("DELETE", "/api/v1/mcp/servers/{mcp_id}"),
        ("POST", "/api/v1/mcp/servers/import"),
        ("POST", "/api/v1/mcp/servers/{mcp_id}/test"),
        ("GET", "/api/v1/mcp/servers/{mcp_id}/tools"),
        ("POST", "/api/v1/mcp/servers/{mcp_id}/tools/{tool_name}/test"),
        ("PUT", "/api/v1/mcp/servers/{mcp_id}/tools"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_user in calls, f"{method} {path} 缺异步鉴权依赖"
        assert get_async_db in calls, f"{method} {path} 缺 AsyncSession 依赖"
