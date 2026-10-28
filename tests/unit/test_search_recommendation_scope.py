"""Saved-search recommendations authorize before binding an owner's model."""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def recommendation_module(client: TestClient) -> ModuleType:
    return sys.modules["api.apps.restful_apis.chat"]


@pytest.mark.parametrize("detail", [{}, {"tenant_id": "foreign", "search_config": {"chat_id": "secret"}}, {"tenant_id": "tenant-unit", "search_config": {}}])
@pytest.mark.parametrize("role", [None, "invite", "unknown"])
def test_denied_saved_search_never_resolves_or_calls_model(client: TestClient, recommendation_module: ModuleType, monkeypatch: pytest.MonkeyPatch, detail: dict[str, Any], role: str | None) -> None:
    module = recommendation_module
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: detail)
    membership = None if role is None else SimpleNamespace(role=role)
    monkeypatch.setattr(module.UserTenantService, "get_membership", lambda *_a, **_kw: membership)
    forbidden = Mock(side_effect=AssertionError("Denied search reached model work"))
    for name in ["get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "LLMBundle", "load_prompt"]:
        monkeypatch.setattr(module, name, forbidden)

    response = client.post("/api/v1/chat/recommendation", json={"question": "question", "search_id": "saved"})

    assert response.status_code == 200
    assert response.json()["code"] == 109
    assert response.json()["message"] == "No authorization for the search app."
    forbidden.assert_not_called()


@pytest.mark.parametrize("body", [{"question": " "}, {"question": "q", "search_id": None}, {"question": "q", "search_id": " saved "}])
def test_invalid_request_never_reads_search_or_model(client: TestClient, recommendation_module: ModuleType, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    module = recommendation_module
    forbidden = Mock(side_effect=AssertionError("Invalid input reached search or model work"))
    monkeypatch.setattr(module.SearchService, "get_detail", forbidden)
    for name in ["get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "LLMBundle"]:
        monkeypatch.setattr(module, name, forbidden)

    response = client.post("/api/v1/chat/recommendation", json=body)

    assert response.status_code == 200
    assert response.json()["code"] != 0
    forbidden.assert_not_called()


@pytest.mark.parametrize("role", ["owner", "admin", "normal"])
@pytest.mark.parametrize("chat_id", ["29", 29])
def test_joined_search_binds_owner_model_and_keeps_zero_settings(client: TestClient, recommendation_module: ModuleType, monkeypatch: pytest.MonkeyPatch, role: str, chat_id: str | int) -> None:
    module = recommendation_module
    settings = {"temperature": 0, "parameter": {"retained": True}}
    detail = {"tenant_id": "team", "search_config": {"chat_id": chat_id, "llm_setting": settings}}
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: detail)
    membership = Mock(return_value=SimpleNamespace(role=role))
    monkeypatch.setattr(module.UserTenantService, "get_membership", membership)
    lookup = Mock(return_value={"id": 29, "mdl_type": "chat"})
    monkeypatch.setattr(module, "get_model_config_by_type_and_name", lookup)
    monkeypatch.setattr(module, "load_prompt", lambda *_: "related prompt")
    bundles: list[Any] = []

    class Bundle:
        def __init__(self, db: Any, tenant_id: str, config: dict[str, Any]) -> None:
            self.db = db
            self.tenant_id = tenant_id
            self.config = config
            bundles.append(self)

        async def async_chat(self, prompt: str, messages: list[dict[str, str]], gen_conf: dict[str, Any]) -> str:
            assert self.db is None
            assert self.tenant_id == "team"
            assert self.config == {"id": 29, "mdl_type": "chat"}
            assert gen_conf == {"temperature": 0}
            gen_conf["temperature"] = 1
            return "1. First question\n 10. Tenth question\n11.   \nnon-numbered"

    monkeypatch.setattr(module, "LLMBundle", Bundle)

    response = client.post("/api/v1/chat/recommendation", json={"question": "question", "search_id": "saved"})

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["data"] == ["First question", "Tenth question"]
    assert lookup.call_args.args[1:] == ("team", "chat", "29")
    assert membership.call_args.kwargs == {"tenant_id": "team", "user_id": "tenant-unit"}
    assert settings == {"temperature": 0, "parameter": {"retained": True}}
    assert len(bundles) == 1


@pytest.mark.parametrize("config", [{"chat_id": True}, {"chat_id": 0}, {"chat_id": None}, {"llm_setting": []}, {"llm_setting": None}])
def test_invalid_saved_config_cannot_trigger_default_model_fallback(client: TestClient, recommendation_module: ModuleType, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]) -> None:
    module = recommendation_module
    monkeypatch.setattr(module.SearchService, "get_detail", lambda *_: {"tenant_id": "tenant-unit", "search_config": config})
    monkeypatch.setattr(module.UserTenantService, "get_membership", lambda *_a, **_kw: SimpleNamespace(role="owner"))
    forbidden = Mock(side_effect=AssertionError("Invalid saved config reached model work"))
    for name in ["get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "LLMBundle"]:
        monkeypatch.setattr(module, name, forbidden)

    response = client.post("/api/v1/chat/recommendation", json={"question": "question", "search_id": "saved"})

    assert response.json()["code"] != 0
    forbidden.assert_not_called()


async def test_recommendation_cancellation_propagates_without_success(recommendation_module: ModuleType, monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    module = recommendation_module
    monkeypatch.setattr(module, "get_tenant_default_model_by_type", lambda *_: {"id": 29})
    monkeypatch.setattr(module, "load_prompt", lambda *_: "prompt")

    class Bundle:
        def __init__(self, db: Any, tenant_id: str, config: dict[str, Any]) -> None:
            self.db = db

        async def async_chat(self, *_: Any) -> str:
            raise asyncio.CancelledError

    monkeypatch.setattr(module, "LLMBundle", Bundle)
    with pytest.raises(asyncio.CancelledError):
        await module.recommendation(module.RelatedQuestionsRequest(question="question"), db=async_db, tenant_id="tenant-unit")
