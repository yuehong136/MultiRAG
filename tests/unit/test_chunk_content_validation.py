"""Canonical chunk RESTful route and empty-content regression contracts."""

from contextlib import nullcontext
from types import SimpleNamespace

from fastapi.routing import APIRoute, iter_route_contexts

from api.db.db_models import get_async_db, get_db
from api.utils.api_utils import async_current_tenant_id
from common import settings
from common.constants import RetCode
from common.string_utils import is_content_empty

_CHUNKS = "/api/v1/datasets/kb1/documents/doc1/chunks"
_CHUNK_ROUTE = "/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks"


def _chunk_route_module():
    import sys

    return sys.modules["api.apps.restful_apis.chunk"]


def _iter_routes(app):
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            yield SimpleNamespace(path=context.path, methods=context.methods)


def test_is_content_empty_truth_table():
    assert is_content_empty(None)
    assert is_content_empty("")
    assert is_content_empty(" ")
    assert is_content_empty("\n\t ")
    assert not is_content_empty("text")
    assert not is_content_empty(" x ")


def _stub_write_context(monkeypatch, db):
    module = _chunk_route_module()
    monkeypatch.setattr(module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(
        module,
        "_write_context",
        lambda _db, _user_id, _dataset_id, _document_id: (
            SimpleNamespace(id="kb1", tenant_id="tenant-unit", name="kb", tenant_embd_id=None, embd_id="embed"),
            SimpleNamespace(id="doc1", kb_id="kb1", name="d.txt", parser_id="naive"),
        ),
    )


def test_restful_add_chunk_rejects_whitespace_content(client, monkeypatch, db):
    _stub_write_context(monkeypatch, db)

    response = client.post(_CHUNKS, json={"content": "  \n "})

    assert response.status_code == 200
    assert response.json() == {"code": int(RetCode.DATA_ERROR), "message": "`content` is required"}


class _FakeDocStore:
    def __init__(self):
        self.updates = []

    def get(self, chunk_id, index_name, dataset_ids):
        del index_name, dataset_ids
        if chunk_id != "c1":
            return None
        return {"id": "c1", "doc_id": "doc1", "content_with_weight": "old content", "available_int": 1}

    def update(self, condition, patch, index_name, dataset_id):
        self.updates.append((condition, patch, index_name, dataset_id))
        return True


def test_restful_update_chunk_rejects_whitespace_content(client, monkeypatch, db):
    _stub_write_context(monkeypatch, db)
    store = _FakeDocStore()
    monkeypatch.setattr(settings, "docStoreConn", store, raising=False)

    response = client.patch(f"{_CHUNKS}/c1", json={"content": " "})

    assert response.status_code == 200
    assert response.json() == {"code": int(RetCode.DATA_ERROR), "message": "`content` is required"}
    assert store.updates == []


def test_chunk_routes_are_canonical_and_web_legacy_routes_are_deprecated(client):
    routes = {(method, route.path) for route in _iter_routes(client.app) for method in route.methods}

    for method, path in (
        ("GET", _CHUNK_ROUTE),
        ("GET", f"{_CHUNK_ROUTE}/{{chunk_id}}"),
        ("POST", _CHUNK_ROUTE),
        ("DELETE", _CHUNK_ROUTE),
        ("PATCH", f"{_CHUNK_ROUTE}/{{chunk_id}}"),
        ("PATCH", _CHUNK_ROUTE),
    ):
        assert (method, path) in routes

    legacy_routes = (
        ("POST", "/v1/chunk/list"),
        ("GET", "/v1/chunk/get"),
        ("POST", "/v1/chunk/set"),
        ("POST", "/v1/chunk/switch"),
        ("POST", "/v1/chunk/rm"),
        ("POST", "/v1/chunk/create"),
    )
    for method, path in legacy_routes:
        assert (method, path) in routes

    schema = client.app.openapi()
    for method, path in legacy_routes:
        assert schema["paths"][path][method.lower()]["deprecated"] is True

    for method, path in (
        ("PUT", f"{_CHUNK_ROUTE}/{{chunk_id}}"),
        ("POST", f"{_CHUNK_ROUTE}/switch"),
    ):
        assert (method, path) not in routes


def test_chunk_routes_use_only_async_auth_and_db(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("GET", _CHUNK_ROUTE),
        ("GET", f"{_CHUNK_ROUTE}/{{chunk_id}}"),
        ("POST", _CHUNK_ROUTE),
        ("DELETE", _CHUNK_ROUTE),
        ("PATCH", f"{_CHUNK_ROUTE}/{{chunk_id}}"),
        ("PATCH", _CHUNK_ROUTE),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls
        assert api_apps.manager not in calls
        assert async_current_tenant_id in calls
        assert get_async_db in calls
