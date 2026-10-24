"""Nine public SDK bot routes reject malformed beta auth before business IO."""

import sys
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db.services.api_service import APITokenService
from api.utils.api_utils import async_beta_token_required, beta_token_required


@pytest.mark.parametrize("authorization", [None, "", "Bearer", "Basic test-beta", "Bearer test-beta extra"])
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/chatbots/dialog/completions", {"question": "test", "stream": False}),
        ("GET", "/chatbots/dialog/info", None),
        ("POST", "/agentbots/agent/completions", {"question": "test", "stream": False}),
        ("GET", "/agentbots/agent/inputs", None),
        ("POST", "/searchbots/ask", {"question": "test", "kb_ids": ["dataset"], "search_id": "search"}),
        ("POST", "/searchbots/retrieval_test", {"question": "test", "kb_id": ["dataset"]}),
        ("POST", "/searchbots/related_questions", {"question": "test", "search_id": "search"}),
        ("GET", "/searchbots/detail?search_id=search", None),
        ("POST", "/searchbots/mindmap", {"question": "test", "kb_ids": ["dataset"], "search_id": "search"}),
    ],
)
def test_malformed_beta_authorization_has_no_business_side_effects(
    client: Any, monkeypatch: pytest.MonkeyPatch, authorization: str | None, method: str, path: str, body: dict[str, Any] | None
) -> None:
    module = sys.modules["api.apps.sdk.session"]
    assert beta_token_required not in client.app.dependency_overrides
    assert async_beta_token_required not in client.app.dependency_overrides
    forbidden = Mock(side_effect=AssertionError("unauthorized request reached business IO"))
    monkeypatch.setattr(APITokenService, "query", forbidden)
    for name in ["execute", "scalar", "get", "query", "commit", "flush", "add", "delete"]:
        monkeypatch.setattr(Session, name, forbidden)
    for name in [
        "validate_iframe_access",
        "async_iframe_completion",
        "agent_completion",
        "async_ask",
        "gen_mindmap",
        "LLMBundle",
        "get_model_config_by_type_and_name",
        "get_tenant_default_model_by_type",
    ]:
        monkeypatch.setattr(module, name, forbidden)
    for service, names in [
        (module.SearchService, ["query", "get_detail"]),
        (module.UserCanvasService, ["get_agent_dsl_with_release"]),
        (module.UserTenantService, ["query"]),
    ]:
        for name in names:
            monkeypatch.setattr(service, name, forbidden)
    monkeypatch.setattr(module.settings, "STORAGE_IMPL", forbidden)
    monkeypatch.setattr(module.settings, "retriever", forbidden)
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.request(method, "/api/v1" + path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"retcode": 109, "retmsg": "Authorization is not valid!", "data": False}
    forbidden.assert_not_called()
