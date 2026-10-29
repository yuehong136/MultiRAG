"""Dify errors use its own envelope and preserve strict retrieval limits."""

import logging
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from api.db.db_models import get_db
from api.utils.api_utils import current_user


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
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    run = AsyncMock(side_effect=AssertionError("Invalid arguments reached retrieval"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="owner")
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
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    dependency = Mock(side_effect=AssertionError("Health check resolved retrieval dependencies"))
    run = AsyncMock(side_effect=AssertionError("Health check executed retrieval"))
    bundle = Mock(side_effect=AssertionError("Health check initialized model"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    client.app.dependency_overrides[get_db] = dependency
    client.app.dependency_overrides[current_user] = dependency
    response = client.get("/api/v1/dify/retrieval/health", params=params, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"retcode": 0, "retmsg": "success", "data": True}
    dependency.assert_not_called()
    run.assert_not_called()
    bundle.assert_not_called()


def test_get_health_check_has_one_route_and_no_query_parameters(client: TestClient) -> None:
    from fastapi.openapi.utils import get_openapi
    from fastapi.routing import iter_route_contexts

    routes = [route for route in iter_route_contexts(client.app.routes) if route.path == "/api/v1/dify/retrieval/health" and "GET" in route.methods]
    assert len(routes) == 1
    assert routes[0].original_route.dependant.dependencies == []
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    operation = get_openapi(title="Dify", version="1", routes=module.router.routes)["paths"]["/dify/retrieval/health"]["get"]
    assert operation.get("parameters", []) == []


def test_malformed_json_has_dify_envelope_without_echo(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    run = AsyncMock()
    monkeypatch.setattr(module, "_run_retrieval", run)
    response = client.post("/api/v1/dify/retrieval", content='{"secret-query":', headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error_code"] == 101
    assert "invalid or malformed arguments:" in response.json()["error_msg"]
    assert "secret-query" not in response.text
    run.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer invalid-fixture-key"},
        {"Authorization": "Basic invalid-fixture-key"},
        {"Authorization": "Bearer invalid-fixture-key extra"},
        [("Authorization", "Bearer first"), ("Authorization", "Bearer second")],
    ],
)
def test_post_invalid_api_key_has_dify_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch, headers: Any) -> None:
    from api.utils import api_utils

    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    run = AsyncMock(side_effect=AssertionError("Unauthenticated request reached retrieval"))
    monkeypatch.setattr(module, "_run_retrieval", run)
    monkeypatch.setattr(api_utils.APITokenService, "query", lambda *_args, **_kwargs: [])
    response = client.post("/api/v1/dify/retrieval", headers=headers, json={"knowledge_id": "kb", "query": "text"})
    assert response.status_code == 403 and response.json()["error_code"] == 403
    run.assert_not_called()


def test_normalized_post_request_uses_strict_settings(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    run = AsyncMock(return_value=module.RetrievalResponse())
    monkeypatch.setattr(module, "_run_retrieval", run)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="owner")
    settings = {"top_k": "12", "score_threshold": "0.66"}
    data = {"knowledge_id": "kb", "query": "text", "use_kg": "true"}
    response = client.post("/api/v1/dify/retrieval", json={**data, "retrieval_setting": settings})
    assert response.status_code == 200 and response.json() == {"records": []}
    request = run.call_args.args[0]
    assert request.use_kg is True
    assert request.retrieval_setting.top_k == 12 and request.retrieval_setting.score_threshold == 0.66


def test_denial_precedes_metadata_model_and_index(client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda *_: SimpleNamespace(tenant_id="owner"))
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", lambda *_: False)
    metadata = Mock(side_effect=AssertionError("Denied request accessed metadata"))
    bundle = Mock(side_effect=AssertionError("Denied request initialized model"))
    retriever = SimpleNamespace(retrieval=AsyncMock(), retrieval_by_children=Mock())
    kg_retriever = SimpleNamespace(retrieval=AsyncMock())
    monkeypatch.setattr(module.DocMetadataService, "get_flatted_meta_by_kbs", metadata)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    monkeypatch.setattr(module.settings, "retriever", retriever)
    monkeypatch.setattr(module.settings, "kg_retriever", kg_retriever)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="attacker")
    with caplog.at_level(logging.WARNING):
        response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "SECRET_REQUEST_CONTENT", "use_kg": True})
    assert response.status_code == 403 and response.json()["error_code"] == 109
    metadata.assert_not_called()
    bundle.assert_not_called()
    retriever.retrieval.assert_not_called()
    retriever.retrieval_by_children.assert_not_called()
    kg_retriever.retrieval.assert_not_called()
    assert "tenant_id=attacker knowledge_id=kb" in caplog.text
    assert "SECRET_REQUEST_CONTENT" not in caplog.text


def test_post_lookup_failure_does_not_fall_back_to_health(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    lookup = Mock(side_effect=RuntimeError("controlled lookup failure"))
    bundle = Mock(side_effect=AssertionError("Failed lookup initialized model"))
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lookup)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="owner")
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "text"})
    assert response.status_code == 500 and response.json()["error_code"] == 500
    assert response.json()["error_msg"] == "controlled lookup failure"
    assert "data" not in response.json()
    bundle.assert_not_called()


@pytest.fixture
def allowed_retrieval(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    kb = SimpleNamespace(id="kb", name="Knowledge", tenant_id="owner", tenant_embd_id=17, embd_id="embedding")
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda *_: kb)
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", lambda *_: True)
    monkeypatch.setattr(module, "get_model_config_by_id", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "get_tenant_default_model_by_type", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "LLMBundle", lambda *_: object())
    monkeypatch.setattr(module, "label_question", lambda *_: {})
    monkeypatch.setattr(module.DocMetadataService, "get_flatted_meta_by_kbs", lambda *_: {})
    metadata = Mock(return_value={"doc": {"author": "alice"}})
    documents = Mock(return_value=[SimpleNamespace(id="doc")])
    monkeypatch.setattr(module.DocMetadataService, "get_metadata_for_documents", metadata)
    monkeypatch.setattr(module.DocumentService, "get_by_ids", documents)
    chunks = [{"doc_id": "doc", "content_with_weight": "text", "similarity": 0.8, "docnm_kwd": "Title", "vector": [0.1]}]
    retriever = SimpleNamespace(retrieval=AsyncMock(return_value={"chunks": chunks}), retrieval_by_children=Mock(side_effect=lambda rows, _: rows))
    kg = SimpleNamespace(retrieval=AsyncMock(return_value={"doc_id": "", "content_with_weight": "Graph aggregate", "similarity": 1.0, "docnm_kwd": "Related content in Knowledge Graph"}))
    monkeypatch.setattr(module.settings, "retriever", retriever)
    monkeypatch.setattr(module.settings, "kg_retriever", kg)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="member")
    return SimpleNamespace(module=module, chunks=chunks, retriever=retriever, kg=kg, documents=documents, metadata=metadata)


def test_metadata_and_real_kg_aggregate_keep_owner_scope(client: TestClient, monkeypatch: pytest.MonkeyPatch, allowed_retrieval: SimpleNamespace) -> None:
    ctx = allowed_retrieval
    monkeypatch.setattr(ctx.module, "meta_filter", lambda *_: ["doc"])
    response = client.post(
        "/api/v1/dify/retrieval",
        json={"knowledge_id": "kb", "query": "text", "use_kg": True, "metadata_condition": {"conditions": [{"name": "author", "comparison_operator": "is", "value": "alice"}]}},
    )
    assert response.status_code == 200, response.text
    records = response.json()["records"]
    assert [row["content"] for row in records] == ["Graph aggregate", "text"]
    assert records[0]["metadata"] == {"doc_id": "", "document_id": ""}
    assert records[1]["metadata"] == {"doc_id": "doc", "document_id": "doc", "author": "alice"}
    assert ctx.retriever.retrieval.call_args.kwargs["tenant_id"] == "owner"
    assert ctx.retriever.retrieval.call_args.kwargs["doc_ids"] == ["doc"]
    assert ctx.kg.retrieval.call_args.args[1:3] == (["owner"], ["kb"])
    assert ctx.documents.call_count == ctx.metadata.call_count == 1
    assert ctx.documents.call_args.args[1] == ["doc"]


def test_missing_sql_documents_are_filtered_without_losing_kg(client: TestClient, allowed_retrieval: SimpleNamespace) -> None:
    ctx = allowed_retrieval
    ctx.chunks.extend(
        [
            {"doc_id": "gone", "content_with_weight": "stale"},
            {"doc_id": "doc", "content_with_weight": "second"},
            {"doc_id": "", "content_with_weight": "unbound ordinary chunk"},
        ]
    )
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "text", "use_kg": True})
    assert response.status_code == 200, response.text
    assert [row["content"] for row in response.json()["records"]] == ["Graph aggregate", "text", "second"]
    assert ctx.documents.call_count == 1 and ctx.documents.call_args.args[1] == ["doc", "gone"]


def test_metadata_zero_matches_stays_restricted(client: TestClient, monkeypatch: pytest.MonkeyPatch, allowed_retrieval: SimpleNamespace) -> None:
    ctx = allowed_retrieval
    monkeypatch.setattr(ctx.module, "meta_filter", lambda *_: [])
    response = client.post(
        "/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "text", "metadata_condition": {"conditions": [{"name": "author", "comparison_operator": "is", "value": "nobody"}]}}
    )
    assert response.status_code == 200, response.text
    assert ctx.retriever.retrieval.call_args.kwargs["doc_ids"] == ["-999"]


@pytest.mark.parametrize("message,status", [("chunk_not_found_error", 404), ("controlled retrieval failure", 500)])
def test_retrieval_failure_uses_error_envelope(client: TestClient, allowed_retrieval: SimpleNamespace, message: str, status: int) -> None:
    allowed_retrieval.retriever.retrieval.side_effect = RuntimeError(message)
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "kb", "query": "text"})
    assert response.status_code == status, response.text
    assert response.json()["error_code"] == status and "records" not in response.json()
    allowed_retrieval.documents.assert_not_called()


def test_missing_kb_precedes_access_and_all_retrieval_work(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.dify_retrieval"]
    monkeypatch.setattr(module.KnowledgebaseService, "get_by_id", lambda *_: None)
    accessible = Mock(side_effect=AssertionError("Missing KB reached authorization"))
    monkeypatch.setattr(module.KnowledgebaseService, "accessible", accessible)
    client.app.dependency_overrides[current_user] = lambda: SimpleNamespace(tenant_id="owner")
    response = client.post("/api/v1/dify/retrieval", json={"knowledge_id": "missing", "query": "text"})
    assert response.status_code == 404 and response.json()["error_code"] == 404
    accessible.assert_not_called()


def test_old_get_is_method_not_allowed_and_post_is_registered(client: TestClient) -> None:
    assert client.get("/api/v1/dify/retrieval").status_code == 405
    schema = client.app.openapi()["paths"]
    assert "get" not in schema["/api/v1/dify/retrieval"]
    assert "post" in schema["/api/v1/dify/retrieval"]
