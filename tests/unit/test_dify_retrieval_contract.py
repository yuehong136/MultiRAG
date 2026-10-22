"""Dify errors use its own envelope and preserve strict retrieval limits."""

import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

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
    ],
)
def test_get_argument_errors(client: TestClient, monkeypatch: pytest.MonkeyPatch, params: dict[str, str]) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock()
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[token_required] = lambda: "owner"
    response = client.get("/api/v1/dify/retrieval", params=params)
    assert response.status_code == 400 and response.json()["error_code"] == 101
    run.assert_not_called()


def test_malformed_json_has_dify_envelope_without_echo(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock()
    monkeypatch.setattr(module, "_run_retrieval", run)
    response = client.post("/api/v1/dify/retrieval", content='{"secret-query":', headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error_code"] == 101
    assert "invalid or malformed arguments:" in response.json()["error_msg"]
    assert "secret-query" not in response.text
    run.assert_not_called()


@pytest.mark.parametrize("method", ["get", "post"])
def test_missing_api_key_has_dify_envelope(client: TestClient, method: str) -> None:
    response = getattr(client, method)("/api/v1/dify/retrieval", **({"json": {"knowledge_id": "kb", "query": "text"}} if method == "post" else {"params": {"knowledge_id": "kb", "query": "text"}}))
    assert response.status_code == 403 and response.json()["error_code"] == 403


@pytest.mark.parametrize("method", ["get", "post"])
def test_normalized_requests_use_same_strict_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    module = sys.modules["api.apps.sdk.dify_retrieval"]
    run = AsyncMock(return_value=module.RetrievalResponse())
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[token_required] = lambda: "owner"
    settings = {"top_k": "12", "score_threshold": "0.66"}
    data = {"knowledge_id": "kb", "query": "text", "use_kg": "true"}
    response = getattr(client, method)("/api/v1/dify/retrieval", **({"json": {**data, "retrieval_setting": settings}} if method == "post" else {"params": {**data, **settings}}))
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
