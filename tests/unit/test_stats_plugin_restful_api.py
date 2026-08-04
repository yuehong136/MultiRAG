from types import SimpleNamespace

from fastapi.routing import APIRoute, iter_route_contexts

from api.db.db_models import get_async_db, get_db
from api.db.services.api_service import API4ConversationService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import async_current_user


def _iter_routes(app):
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            yield SimpleNamespace(path=context.path, methods=context.methods)


def test_plugin_tools_uses_canonical_route(client, monkeypatch):
    from agent.plugin import GlobalPluginManager

    tool = SimpleNamespace(get_metadata=lambda: {"name": "unit-tool"})
    monkeypatch.setattr(GlobalPluginManager, "get_llm_tools", lambda: [tool])

    response = client.get("/api/v1/plugin/tools")

    assert response.status_code == 200
    assert response.json()["data"] == [{"name": "unit-tool"}]


def test_stats_uses_canonical_route(client, monkeypatch):
    monkeypatch.setattr(
        UserTenantService,
        "query",
        classmethod(lambda cls, db, **kwargs: [SimpleNamespace(tenant_id="tenant-unit")]),
    )
    monkeypatch.setattr(
        API4ConversationService,
        "stats",
        classmethod(lambda cls, db, tenant_id, start, end, source: [{"dt": "2026-08-02", "pv": 3, "uv": 2, "tokens": 1000, "duration": 1, "round": 4, "thumb_up": 1}]),
    )

    response = client.get("/api/v1/system/stats")

    assert response.status_code == 200
    assert response.json()["data"]["pv"] == [["2026-08-02", 3]]


def test_stats_and_plugin_legacy_routes_are_removed(client):
    routes = {(method, route.path) for route in _iter_routes(client.app) for method in route.methods}

    assert ("GET", "/api/v1/plugin/tools") in routes
    assert ("GET", "/api/v1/system/stats") in routes
    assert ("GET", "/v1/plugin/llm_tools") not in routes
    assert ("GET", "/v1/api/stats") not in routes


def test_stats_and_plugin_routes_use_async_dependencies(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("GET", "/api/v1/plugin/tools"),
        ("GET", "/api/v1/system/stats"),
        ("POST", "/api/v1/plugin/run-plugin-script"),
        ("POST", "/api/v1/plugin/install-dep"),
        ("POST", "/api/v1/plugin/uninstall-dep"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls
        assert api_apps.manager not in calls
        assert async_current_user in calls
        assert get_async_db in calls
