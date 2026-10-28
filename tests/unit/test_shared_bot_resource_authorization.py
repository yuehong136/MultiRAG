"""Beta identity stays scoped before shared Redis, model and retrieval work."""

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.utils.api_utils import async_beta_token_required, beta_token_required


@pytest.fixture
def shared_client(client: TestClient) -> TestClient:
    client.app.dependency_overrides[beta_token_required] = lambda: "owner"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "owner"
    return client


@pytest.mark.parametrize("canvas_owner", [None, "foreign"])
def test_persondata_rejects_other_agent_before_redis(shared_client: TestClient, monkeypatch: pytest.MonkeyPatch, canvas_owner: str | None) -> None:
    from api.apps import datav_app

    module = sys.modules["api.apps.restful_apis.bot"]
    monkeypatch.setattr(module.UserCanvasService, "get_by_id", lambda *_: None if canvas_owner is None else SimpleNamespace(user_id=canvas_owner))
    fetch = Mock(side_effect=AssertionError("Denied persondata request reached Redis"))
    monkeypatch.setattr(datav_app, "fetch_persondata_list", fetch)
    response = shared_client.get("/api/v1/agentbots/agent/persondataList")
    assert response.status_code == 200 and response.json()["code"] == 102
    fetch.assert_not_called()


def test_persondata_owned_agent_still_reads_candidates(shared_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps import datav_app

    module = sys.modules["api.apps.restful_apis.bot"]
    monkeypatch.setattr(module.UserCanvasService, "get_by_id", lambda *_: SimpleNamespace(user_id="owner"))
    fetch = Mock(return_value=[{"title": "Owned candidate"}])
    monkeypatch.setattr(datav_app, "fetch_persondata_list", fetch)
    response = shared_client.get("/api/v1/agentbots/agent/persondataList")
    assert response.json()["data"] == [{"title": "Owned candidate"}]
    fetch.assert_called_once_with("agent")


@pytest.mark.parametrize("membership_role", [None, "invite", "unknown"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/searchbots/ask", {"question": "q", "kb_ids": ["kb"], "search_id": "search"}),
        ("POST", "/searchbots/related_questions", {"question": "q", "search_id": "search"}),
        ("GET", "/searchbots/detail?search_id=search", None),
        ("POST", "/searchbots/mindmap", {"question": "q", "kb_ids": ["kb"], "search_id": "search"}),
    ],
)
def test_foreign_saved_search_denies_before_provider(
    shared_client: TestClient, monkeypatch: pytest.MonkeyPatch, membership_role: str | None, method: str, path: str, body: dict[str, Any] | None
) -> None:
    module = sys.modules["api.apps.restful_apis.bot"]
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: {"tenant_id": "foreign", "search_config": {"chat_id": "secret"}})
    membership = None if membership_role is None else SimpleNamespace(role=membership_role)
    monkeypatch.setattr(module.UserTenantService, "get_membership", lambda *_a, **_kw: membership)
    forbidden = Mock(side_effect=AssertionError("Denied search reached model, index or execution"))
    for name in ["async_ask", "gen_mindmap", "get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "LLMBundle"]:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", forbidden)
    response = shared_client.request(method, "/api/v1" + path, json=body)
    assert response.status_code == 200 and response.json()["code"] == 109
    assert response.json()["message"] == "No authorization for the search app."
    forbidden.assert_not_called()


@pytest.mark.parametrize("role", ["owner", "admin", "normal"])
def test_search_detail_allows_live_joined_membership(db: Session, monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    module = sys.modules["api.apps.restful_apis.bot"]
    detail = {"tenant_id": "team", "search_config": {}}
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: detail)
    membership = Mock(return_value=SimpleNamespace(role=role))
    monkeypatch.setattr(module.UserTenantService, "get_membership", membership)
    assert module._shared_search_detail(db, "search", "owner") is detail
    membership.assert_called_once_with(db, tenant_id="team", user_id="owner")


@pytest.mark.parametrize("path", ["/searchbots/ask", "/searchbots/mindmap"])
@pytest.mark.parametrize("datasets", [[], ["foreign"], ["owned", "foreign"]])
def test_shared_dataset_denial_precedes_stream_and_execution(shared_client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, datasets: list[str]) -> None:
    module = sys.modules["api.apps.restful_apis.bot"]
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", lambda _db, kb_id, _user: kb_id == "owned")
    ask, mindmap = Mock(), AsyncMock()
    monkeypatch.setattr(module, "async_ask", ask)
    monkeypatch.setattr(module, "gen_mindmap", mindmap)
    response = shared_client.post("/api/v1" + path, json={"question": "q", "kb_ids": datasets})
    assert response.status_code == 200 and response.json()["code"] == 109
    assert "text/event-stream" not in response.headers["content-type"]
    ask.assert_not_called()
    mindmap.assert_not_awaited()


def test_saved_search_effective_dataset_does_not_fall_back_to_request(shared_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.bot"]
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: {"tenant_id": "owner", "search_config": {"kb_ids": []}})
    monkeypatch.setattr(module.UserTenantService, "get_membership", lambda *_a, **_kw: SimpleNamespace(role="owner"))
    forbidden = Mock(side_effect=AssertionError("Empty effective selection reached provider"))
    monkeypatch.setattr(module, "async_ask", forbidden)
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", forbidden)
    response = shared_client.post("/api/v1/searchbots/ask", json={"question": "q", "kb_ids": ["owned"], "search_id": "search"})
    assert response.json()["code"] == 109
    forbidden.assert_not_called()


def test_shared_retrieval_uses_dataset_visibility_before_models(shared_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.bot"]
    visible = Mock(return_value=False)
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", visible)
    forbidden = Mock(side_effect=AssertionError("Denied retrieval reached data or providers"))
    for service, method in [(module.KnowledgebaseService, "get_by_ids"), (module.SearchService, "get_detail")]:
        monkeypatch.setattr(service, method, forbidden)
    monkeypatch.setattr(module, "build_named_bundle_async", forbidden)
    response = shared_client.post("/api/v1/searchbots/retrieval_test", json={"question": "q", "kb_id": ["foreign"]})
    assert response.status_code == 200 and response.json()["retcode"] == 103
    assert visible.call_args.args[1:] == ("foreign", "owner")
    forbidden.assert_not_called()
