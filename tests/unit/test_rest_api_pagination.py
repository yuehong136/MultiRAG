"""HTTP pagination rejects ambiguous/coerced inputs before service execution."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.utils.pagination_utils import validate_rest_api_page_size
from api.utils.validation_utils import ListDatasetReq
from tests.support.resource_journal import ResourceJournal

# Route defaults and zero semantics stay local; every nonzero page is <=100.
ROUTES = [
    ("/datasets", 30, False),
    ("/datasets/kb/ingestions", 0, True),
    ("/datasets/kb/documents", 30, False),
    ("/datasets/kb/documents/doc/chunks", 30, False),
    ("/agents", 0, True),
    ("/agents/agent/sessions", 30, True),
    ("/chats", 0, True),
    ("/chats/chat/sessions", 30, True),
    ("/connectors/connector/logs", 15, False),
    ("/mcp/servers", 0, True),
    ("/memories", 50, False),
    ("/memories/memory", 50, False),
    ("/searches", 0, True),
]


@pytest.fixture(autouse=True)
def case_journal(request: pytest.FixtureRequest) -> Iterator[ResourceJournal]:
    journal = ResourceJournal(request.node.nodeid)
    journal.event("boundaries", external_services=False)
    yield journal
    journal.event("case-complete", external_resources_created=False)


@pytest.mark.parametrize("value", [101, 1000, -1, True, False, None, 1.0, 1.2, float("nan"), float("inf"), "1.0", "", "true", [], {}])
def test_page_size_does_not_coerce_or_truncate(value: Any) -> None:
    with pytest.raises(ValueError):
        validate_rest_api_page_size(value)
    with pytest.raises(ValidationError):
        ListDatasetReq(page_size=value)


@pytest.mark.parametrize("value", [0, 1, 100, "0", "1", "100"])
def test_page_size_preserves_legal_boundaries(value: Any) -> None:
    assert validate_rest_api_page_size(value) == int(value)


@pytest.mark.parametrize(("path", "default", "zero_allowed"), ROUTES)
def test_page_size_openapi_preserves_defaults_and_zero_contract(client: TestClient, path: str, default: int, zero_allowed: bool) -> None:
    # Resolve templated paths from the public OpenAPI schema rather than a route fake.
    templates = {
        "/datasets/kb/ingestions": "/datasets/{dataset_id}/ingestions",
        "/datasets/kb/documents": "/datasets/{dataset_id}/documents",
        "/datasets/kb/documents/doc/chunks": "/datasets/{dataset_id}/documents/{document_id}/chunks",
        "/agents/agent/sessions": "/agents/{canvas_id}/sessions",
        "/chats/chat/sessions": "/chats/{chat_id}/sessions",
        "/connectors/connector/logs": "/connectors/{connector_id}/logs",
        "/memories/memory": "/memories/{memory_id}",
    }
    schema = client.app.openapi()["paths"]["/api/v1" + templates.get(path, path)]["get"]
    size = next(item["schema"] for item in schema["parameters"] if item["name"] == "page_size")
    assert size["maximum"] == 100 and size["default"] == default
    assert size["minimum"] == (0 if zero_allowed else 1)


@pytest.mark.parametrize(("path", "default", "zero_allowed"), ROUTES)
def test_invalid_query_stops_before_handler(client: TestClient, monkeypatch: pytest.MonkeyPatch, path: str, default: int, zero_allowed: bool) -> None:
    # Avoid the independent agent ownership dependency querying a real DB; the
    # endpoint body is never allowed to run for invalid input.
    import sys
    from types import SimpleNamespace

    agent_api = sys.modules["api.apps.restful_apis.agent"]
    client.app.dependency_overrides[agent_api._require_canvas_access] = lambda: SimpleNamespace(id="agent")
    invalid = ["-1", "101", "1000", "", "1.0", "1.5", "nan", "inf", "true", "false", "null"]
    if not zero_allowed:
        invalid.append("0")
    for value in invalid:
        response = client.get("/api/v1" + path, params={"page_size": value})
        assert response.status_code == 422, (path, value, response.text)
    for params in [[("page_size", "1"), ("page_size", "100")], [("page_size", "100"), ("page_size", "100")], [("page", "1"), ("page", "2")]]:
        response = client.get("/api/v1" + path, params=params)
        assert response.status_code == 422, (path, params, response.text)


def test_retrieval_body_page_size_is_strict(client: TestClient) -> None:
    from api.utils.api_utils import token_required

    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    for value in [0, 101, True, 1.0, "1", None, [], {}]:
        response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": ["kb"], "page_size": value})
        assert response.status_code == 422, (value, response.text)
