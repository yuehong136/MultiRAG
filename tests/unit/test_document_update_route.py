"""Real route registration, trusted dependencies and PATCH-local error material."""

import sys
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db, get_db
from api.utils.api_utils import async_current_tenant_id, async_current_user, current_tenant_id
from api.utils.document_update_contract import DocumentUpdateError, DocumentUpdatePatch

_PATH = "/api/v1/datasets/kb1/documents/doc1"


def _writer(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any] | BaseException, calls: list[Any]) -> None:
    async def save(db: AsyncSession, dataset: str, document: str, actor: str, patch: DocumentUpdatePatch) -> dict[str, Any]:
        assert isinstance(db, AsyncSession)
        calls.append((dataset, document, actor, patch.model_dump(exclude_unset=True)))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setitem(vars(sys.modules["api.apps.restful_apis.document"]), "update_document_parser", save)


def _assert_error(response: Any, status: int, numeric: int, code: str, outcome: str = "unchanged") -> None:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["code"] == code and body["retcode"] == numeric
    assert body["details"] == body["data"] == {"outcome": outcome}
    assert body["detail"] == body["retmsg"] == body["message"]
    assert len(body["request_id"]) == 32 and body["request_id"] == response.headers["X-Request-ID"]
    assert "private" not in response.text and "client-id" not in response.text


def test_update_returns_full_fresh_document_and_actual_disabled_state(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    _writer(
        monkeypatch,
        {"id": "doc1", "kb_id": "kb1", "parser_id": "general", "pipeline_id": None, "name": "stored.pdf", "status": "0", "run": "3", "chunk_num": 7, "token_num": 11, "parser_config": {"old": None}},
        calls,
    )
    response = client.patch(_PATH, json={"name": "requested.pdf"})
    assert response.status_code == 200 and response.json()["code"] == 0
    assert set(response.json()) == {"code", "message", "data"} and response.json()["message"] == "success"
    document = response.json()["data"]
    assert document == {
        "id": "doc1",
        "dataset_id": "kb1",
        "chunk_method": "general",
        "pipeline_id": None,
        "name": "stored.pdf",
        "status": "0",
        "run": "DONE",
        "state": "DONE",
        "enabled": False,
        "chunk_count": 7,
        "token_count": 11,
        "parser_config": {"old": None},
    }
    assert calls == [("kb1", "doc1", "user-unit", {"name": "requested.pdf"})]


@pytest.mark.parametrize(
    "payload",
    [
        {"pipeline_id": None},
        {"chunk_method": None},
        {"parser_config": None},
        {"name": None},
        {"enabled": None},
        {"pipeline_id": 1},
        {"chunk_method": True},
        {"parser_config": []},
        {"unknown": 1},
        {"metadata": {}},
        {"enabled": 2},
        {"enabled": "true"},
        {"meta_fields": {"nested": {"private": "secret"}}},
        {"parser_config": {"ext": {}}},
        {"parser_config": {"operator:x": {}}},
        {"parser_config": {"chunk_token_num": "512"}},
        {"parser_config": {"pages": None}},
        {"parser_config": {"raptor": {"use_raptor": None}}},
    ],
)
def test_strict_shape_errors_precede_writer(client: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    calls: list[Any] = []
    _writer(monkeypatch, AssertionError("Writer reached invalid body"), calls)
    response = client.patch(_PATH, json=payload, headers={"X-Request-ID": "client-id-private"})
    _assert_error(response, 422, 101, "DOCUMENT_UPDATE_VALIDATION")
    assert calls == []


@pytest.mark.parametrize("payload", [{"chunk_token_num": 8193}, {"pages": [[5, 1]]}, {"mineru_lang": "en"}])
def test_config_semantic_errors_use_fixed_400_contract(client: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    calls: list[Any] = []
    _writer(monkeypatch, AssertionError("Writer reached invalid config"), calls)
    _assert_error(client.patch(_PATH, json={"parser_config": payload}), 400, 102, "DOCUMENT_UPDATE_INVALID")
    assert calls == []


@pytest.mark.parametrize(
    "status,numeric,code,outcome",
    [
        (400, 102, "DOCUMENT_UPDATE_INVALID", "unchanged"),
        (403, 109, "DOCUMENT_UPDATE_FORBIDDEN", "unchanged"),
        (404, 102, "DOCUMENT_UPDATE_UNAVAILABLE", "unchanged"),
        (409, 102, "DOCUMENT_UPDATE_CONFLICT", "unchanged"),
        (500, 500, "DOCUMENT_UPDATE_FAILED", "unchanged"),
        (500, 500, "DOCUMENT_UPDATE_OUTCOME_UNKNOWN", "unknown"),
    ],
)
def test_typed_failure_never_returns_document(client: Any, monkeypatch: pytest.MonkeyPatch, status: int, numeric: int, code: str, outcome: Any) -> None:
    calls: list[Any] = []
    _writer(monkeypatch, DocumentUpdateError("Safe update error.", status=status, numeric_code=numeric, code=code, outcome=outcome), calls)
    _assert_error(client.patch(_PATH, json={"name": "new.pdf"}), status, numeric, code, outcome)
    assert len(calls) == 1


def test_dependency_authentication_uses_same_bridge_before_handler(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    _writer(monkeypatch, AssertionError("Authentication reached writer"), calls)

    async def denied() -> None:
        raise HTTPException(401, "private credentials", headers={"WWW-Authenticate": "Bearer"})

    client.app.dependency_overrides[async_current_user] = denied
    response = client.patch(_PATH, json={})
    _assert_error(response, 401, 401, "DOCUMENT_UPDATE_UNAUTHORIZED")
    assert response.headers["WWW-Authenticate"] == "Bearer" and not calls


def test_unexpected_failure_is_safe_unknown(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    _writer(monkeypatch, RuntimeError("private SQL and token"), [])
    _assert_error(client.patch(_PATH, json={}), 500, 500, "DOCUMENT_UPDATE_OUTCOME_UNKNOWN", "unknown")


def test_presence_is_not_expanded_with_defaults(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    _writer(monkeypatch, {"id": "doc1", "kb_id": "kb1", "parser_id": "naive", "run": "0", "status": "1"}, calls)
    for payload in [{}, {"pipeline_id": ""}, {"parser_config": {}}, {"parser_config": {"raptor": {"use_raptor": False}}}]:
        assert client.patch(_PATH, json=payload).status_code == 200
        assert calls[-1][-1] == payload


def test_put_alias_and_legacy_parser_route_are_removed(client: Any) -> None:
    assert client.put(_PATH, json={}).status_code == 405
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/document/change_parser" not in paths
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "ChangeParserRequest" not in schemas and "LegacyDocumentParserPatch" not in schemas
    reference = paths["/api/v1/datasets/{dataset_id}/documents/{document_id}"]["patch"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    assert schemas[reference]["title"] == "UpdateDocumentRequest"
    assert {"chunk_method", "pipeline_id", "parser_config"} <= set(schemas[reference]["properties"])


def test_update_route_has_trusted_async_dependency_tree(client: Any, route_dependency_calls: Any) -> None:
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "PATCH", "/api/v1/datasets/{dataset_id}/documents/{document_id}")
    assert get_db not in calls and current_tenant_id not in calls and api_apps.manager not in calls
    assert async_current_tenant_id not in calls and async_current_user in calls and get_async_db in calls


@pytest.mark.parametrize("method", ["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@pytest.mark.parametrize("payload", [{}, {"doc_id": "doc1", "parser_id": "paper"}, {"pipeline_id": None}, {"parser_config": []}])
def test_old_parser_is_routing_absence_without_private_calls(client: Any, monkeypatch: pytest.MonkeyPatch, method: str, payload: dict[str, Any]) -> None:
    from types import SimpleNamespace

    from fastapi.routing import iter_route_contexts

    from api.db.services import document_parser_service
    from common import settings

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("retired parser reached a private dependency")

    for module in [document_parser_service, sys.modules["api.apps.restful_apis.document"]]:
        monkeypatch.setattr(module, "update_document_parser", forbid)
    monkeypatch.setattr(settings, "STORAGE_IMPL", SimpleNamespace(get=forbid, get_bytes=forbid, put=forbid, rm=forbid))
    client.app.dependency_overrides[get_async_db] = forbid
    client.app.dependency_overrides[get_db] = forbid
    client.app.dependency_overrides[async_current_user] = forbid
    assert not any(context.path == "/v1/document/change_parser" for context in iter_route_contexts(client.app.routes))
    response = client.request(method, "/v1/document/change_parser", json=payload, headers={"Authorization": "Bearer INVALID_"}, follow_redirects=False)
    assert response.status_code == 404 and "location" not in response.headers and response.headers["content-type"] == "application/json"
    if method == "HEAD":
        assert response.content == b""
    else:
        assert response.json() == {"code": 404, "message": "Not Found: /v1/document/change_parser", "data": None, "error": "Not Found"}
