"""Dify errors use its own envelope and preserve strict retrieval limits."""

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from api.db.db_models import get_db
from api.utils.api_utils import token_required


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, "required arguments are missing: knowledge_id,query"),
        ({"knowledge_id": "kb"}, "required arguments are missing: query"),
        ([], "invalid or malformed arguments:"),
        (None, "required arguments are missing:"),
        ({"knowledge_id": 123, "query": "text"}, "invalid or malformed arguments:"),
        ({"knowledge_id": " ", "query": "text"}, "invalid or malformed arguments:"),
        ({"knowledge_id": "kb", "query": ""}, "invalid or malformed arguments:"),
        ({"knowledge_id": "kb", "query": "   "}, "invalid or malformed arguments:"),
        ({"knowledge_id": "kb", "query": "text", "search_mode": {"type": "hybrid", "weight_dense": 0, "weight_sparse": 0}}, "invalid or malformed arguments:"),
        *[
            ({"knowledge_id": "kb", "query": "text", "retrieval_setting": setting}, "invalid or malformed arguments:")
            for setting in [
                "bad",
                [],
                None,
                {"top_k": "bad"},
                {"top_k": 0},
                {"top_k": -1},
                {"top_k": 10001},
                {"top_k": 1.5},
                {"score_threshold": "bad"},
                {"score_threshold": -0.1},
                {"score_threshold": 1.1},
                {"score_threshold": "nan"},
                {"score_threshold": "inf"},
            ]
        ],
    ],
)
def test_post_argument_error_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: Any, expected: str) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock(side_effect=AssertionError("Invalid arguments reached retrieval"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[token_required] = lambda: "owner"
    response = client.post("/api/v1/dify/retrieval", json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == 101
    assert response.json()["error_msg"].startswith(expected)
    run.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"knowledge_id": "kb"},
        {"knowledge_id": " ", "query": "text"},
        {"knowledge_id": "kb", "query": "   "},
        {"knowledge_id": "kb", "query": "text", "top_k": "bad"},
        {"knowledge_id": "kb", "query": "text", "top_k": "0"},
        {"knowledge_id": "kb", "query": "text", "score_threshold": "nan"},
        {"knowledge_id": "private", "query": "text", "use_kg": "true"},
    ],
)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-fixture-key"}])
def test_get_health_check_has_no_retrieval_dependencies(client: TestClient, monkeypatch: pytest.MonkeyPatch, params: dict[str, str], headers: dict[str, str]) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    dependency = Mock(side_effect=AssertionError("Health check resolved retrieval dependencies"))
    run = AsyncMock(side_effect=AssertionError("Health check executed retrieval"))
    bundle = Mock(side_effect=AssertionError("Health check initialized model"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    client.app.dependency_overrides[get_db] = dependency
    client.app.dependency_overrides[token_required] = dependency
    response = client.get("/api/v1/dify/retrieval", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"retcode": 0, "retmsg": "success", "data": True}
    dependency.assert_not_called()
    run.assert_not_called()
    bundle.assert_not_called()


def test_get_health_check_has_one_route_and_no_query_parameters(client: TestClient) -> None:
    from fastapi.openapi.utils import get_openapi
    from fastapi.routing import iter_route_contexts

    routes = [route for route in iter_route_contexts(client.app.routes) if route.path == "/api/v1/dify/retrieval" and "GET" in route.methods]
    assert len(routes) == 1
    assert routes[0].original_route.dependant.dependencies == []
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    operation = get_openapi(title="Dify", version="1", routes=module.router.routes)["paths"]["/dify/retrieval"]["get"]
    assert operation.get("parameters", []) == []


def test_malformed_json_has_dify_envelope_without_echo(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock()
    monkeypatch.setattr(module, "_run_retrieval", run)
    response = client.post("/api/v1/dify/retrieval", content='{"secret-query":', headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error_code"] == 101
    assert "invalid or malformed arguments:" in response.json()["error_msg"]
    assert "secret-query" not in response.text
    run.assert_not_called()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer"}, {"Authorization": "Bearer invalid-fixture-key"}])
def test_post_invalid_api_key_has_dify_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> None:
    from api.utils import api_utils

    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock(side_effect=AssertionError("Unauthenticated request reached retrieval"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    monkeypatch.setattr(api_utils.APIToken, "query", lambda *_args, **_kwargs: [])
    response = client.post("/api/v1/dify/retrieval", headers=headers, json={"knowledge_id": "kb", "query": "text"})
    assert response.status_code == 403 and response.json()["error_code"] == 403
    run.assert_not_called()


def test_normalized_post_request_uses_strict_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock(return_value=module.RetrievalResponse())
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[token_required] = lambda: "owner"
    settings = {"top_k": "12", "score_threshold": "0.66"}
    data = {"knowledge_id": "kb", "query": "text", "use_kg": "true"}
    response = client.post("/api/v1/dify/retrieval", json={**data, "retrieval_setting": settings})
    assert response.status_code == 200 and response.json() == {"records": []}
    request = run.call_args.args[0]
    assert request.use_kg is True
    assert request.retrieval_setting.top_k == 12 and request.retrieval_setting.score_threshold == 0.66


def test_denial_precedes_metadata_model_and_index(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda *_: SimpleNamespace(tenant_id="owner"))
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", lambda *_: False)
    metadata = Mock(side_effect=AssertionError("Denied request accessed metadata"))
    bundle = Mock(side_effect=AssertionError("Denied request initialized model"))
    monkeypatch.setattr(module.DocMetadataService, "get_flatted_meta_by_kbs", metadata)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    client.app.dependency_overrides[token_required] = lambda: "attacker"
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "private"})
    assert response.status_code == 403 and response.json()["error_code"] == 109
    metadata.assert_not_called()
    bundle.assert_not_called()


def test_post_lookup_failure_does_not_fall_back_to_health(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    lookup = Mock(side_effect=RuntimeError("controlled lookup failure"))
    bundle = Mock(side_effect=AssertionError("Failed lookup initialized model"))
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lookup)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    client.app.dependency_overrides[token_required] = lambda: "owner"
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "text"})
    assert response.status_code == 500 and response.json()["error_code"] == 500
    assert response.json()["error_msg"] == "controlled lookup failure"
    assert "data" not in response.json()
    bundle.assert_not_called()
