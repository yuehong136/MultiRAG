"""Authentication and retirement contracts across the production REST families."""

from __future__ import annotations

import sys
from typing import Any, NoReturn

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

PROTECTED_ROUTES = [
    ("GET", "/agents"),
    ("POST", "/agents/chat/completions"),
    ("GET", "/chats"),
    ("GET", "/chats/chat/sessions"),
    ("POST", "/chat/completions"),
    ("GET", "/datasets"),
    ("GET", "/datasets/kb/documents"),
    ("GET", "/datasets/kb/documents/doc/chunks/chunk"),
    ("GET", "/connectors"),
    ("GET", "/files"),
    ("GET", "/langfuse/api-key"),
    ("GET", "/mcp/servers"),
    ("GET", "/memories"),
    ("GET", "/messages"),
    ("POST", "/openai/chat/chat/completions"),
    ("GET", "/plugin/tools"),
    ("POST", "/retrieval"),
    ("GET", "/searches"),
    ("GET", "/system/tokens"),
    ("POST", "/tasks/task/cancel"),
    ("GET", "/tenants"),
    ("GET", "/users/me"),
]


def unexpected_io(*args: Any, **kwargs: Any) -> NoReturn:
    raise AssertionError("Unauthenticated or retired request reached database/agent execution")


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES, ids=[f"{method} {path}" for method, path in PROTECTED_ROUTES])
def test_rest_families_refuse_missing_identity_before_database_io(client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str) -> None:
    from api.apps import manager
    from api.utils import api_utils

    for dependency in (manager, api_utils.async_current_user, api_utils.current_tenant_id, api_utils.async_current_tenant_id):
        client.app.dependency_overrides.pop(dependency, None)
    monkeypatch.setattr(Session, "execute", unexpected_io)
    monkeypatch.setattr(AsyncSession, "execute", unexpected_io)
    monkeypatch.setattr(AsyncSession, "run_sync", unexpected_io)
    response = client.request(method, "/api/v1" + path, json={})
    assert response.status_code in {200, 401, 403}, response.text
    if response.status_code == 200:
        payload = response.json()
        assert payload.get("retcode", payload.get("code")) not in {None, 0}, payload


def test_retired_agent_openai_path_does_not_enter_completion(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.agent"]
    for name in ("prepare_agent_run", "completion_openai", "agent_completion"):
        monkeypatch.setattr(module, name, unexpected_io)
    monkeypatch.setattr(AsyncSession, "execute", unexpected_io)
    monkeypatch.setattr(AsyncSession, "run_sync", unexpected_io)
    path = "/api/v1/agents_openai/agent/chat/completions"
    response = client.post(path, json={"messages": [{"role": "user", "content": "hello"}], "stream": True})
    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": f"Not Found: {path}", "data": None, "error": "Not Found"}
    assert "/api/v1/agents_openai/{agent_id}/chat/completions" not in client.app.openapi()["paths"]


def test_missing_route_uses_json_404_without_database_io(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(AsyncSession, "execute", unexpected_io)
    monkeypatch.setattr(AsyncSession, "run_sync", unexpected_io)
    path = "/api/v1/__missing_route__"
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": f"Not Found: {path}", "data": None, "error": "Not Found"}
