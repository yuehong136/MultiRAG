"""Retained MCP retrieval authenticates strictly; retired operations stay absent."""

from typing import Any
from unittest.mock import Mock

import pytest

from api.db.services.api_service import APITokenService
from api.db.services.knowledgebase_service import KnowledgebaseService


@pytest.mark.parametrize("authorization", [None, "", "Bearer", "Basic credential", "Bearer credential extra"])
def test_retrieval_malformed_auth_never_reaches_sql_or_provider(client: Any, monkeypatch: pytest.MonkeyPatch, authorization: str | None) -> None:
    forbidden = Mock(side_effect=AssertionError("Malformed auth reached SQL"))
    monkeypatch.setattr(APITokenService, "query", forbidden)
    monkeypatch.setattr(KnowledgebaseService, "query", forbidden)
    response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": ["private"]}, headers={} if authorization is None else {"Authorization": authorization})
    assert response.status_code == 401 and response.json()["code"] == 401
    forbidden.assert_not_called()


def test_retired_parse_and_batch_session_operations_have_no_openapi_or_effects(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError("Retired operation reached SQL"))
    monkeypatch.setattr(KnowledgebaseService, "query", forbidden)
    for method in ["POST", "DELETE"]:
        response = client.request(method, "/api/v1/datasets/private/chunks", json={"document_ids": ["document"]})
        assert response.status_code == 404
    # The path still has its active GET/POST operations, hence Starlette's 405.
    response = client.request("DELETE", "/api/v1/agents/agent/sessions", json={"ids": ["session"]})
    assert response.status_code == 405
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/v1/datasets/{dataset_id}/chunks" not in paths
    assert "delete" not in paths["/api/v1/agents/{canvas_id}/sessions"]
    assert "delete" in paths["/api/v1/agents/{canvas_id}/sessions/{session_id}"]
    assert "post" in paths["/api/v1/datasets/{dataset_id}/documents/parse"]
    assert "post" in paths["/api/v1/datasets/{dataset_id}/documents/stop"]
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "path,body,key,expected_code",
    [("/retrieval", {"question": "q", "dataset_ids": ["private"]}, "code", 401), ("/searchbots/ask", {"question": "q", "kb_ids": ["private"], "search_id": "search"}, "retcode", 109)],
)
def test_duplicate_authorization_fields_do_not_choose_one_credential(client: Any, monkeypatch: pytest.MonkeyPatch, path: str, body: dict[str, Any], key: str, expected_code: int) -> None:
    forbidden = Mock(side_effect=AssertionError("Ambiguous Authorization reached SQL"))
    monkeypatch.setattr(APITokenService, "query", forbidden)
    response = client.post("/api/v1" + path, json=body, headers=[("Authorization", "Bearer first"), ("Authorization", "Bearer second")])
    assert response.status_code == 401 and response.json()[key] == expected_code
    forbidden.assert_not_called()
