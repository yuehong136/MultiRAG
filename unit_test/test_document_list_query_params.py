import json
from types import SimpleNamespace

from api.apps import document as document_app


def _call_list_docs(**overrides):
    params = {
        "filter_params": document_app.DocumentFilter(),
        "dataset_id": None,
        "legacy_kb_id": None,
        "keywords": "",
        "page": 0,
        "page_size": 0,
        "orderby": "create_time",
        "desc": True,
        "create_time_from": 0,
        "create_time_to": 0,
        "db": "db",
        "user": SimpleNamespace(id="user-1"),
    }
    params.update(overrides)
    response = document_app.list_docs(**params)
    return json.loads(response.body)


def _stub_list_docs_dependencies(monkeypatch, seen):
    monkeypatch.setattr(
        document_app.UserTenantService,
        "query",
        lambda *_args, **_kwargs: [SimpleNamespace(tenant_id="tenant-1")],
    )

    def fake_kb_query(*_args, **kwargs):
        seen["authorized_kb_id"] = kwargs.get("id")
        return True

    def fake_get_by_kb_id(*args, **_kwargs):
        seen["listed_kb_id"] = args[1]
        return ([{"id": "doc-1", "thumbnail": "", "parser_config": {}}], 1)

    monkeypatch.setattr(document_app.KnowledgebaseService, "query", fake_kb_query)
    monkeypatch.setattr(document_app.DocumentService, "get_by_kb_id", fake_get_by_kb_id)


def test_document_list_prefers_dataset_id_query_param(monkeypatch):
    seen = {}
    _stub_list_docs_dependencies(monkeypatch, seen)

    body = _call_list_docs(dataset_id="kb-1")

    assert body["code"] == 0
    assert seen["authorized_kb_id"] == "kb-1"
    assert seen["listed_kb_id"] == "kb-1"


def test_document_list_accepts_legacy_kb_id_query_param(monkeypatch):
    seen = {}
    _stub_list_docs_dependencies(monkeypatch, seen)

    body = _call_list_docs(legacy_kb_id="kb-legacy")

    assert body["code"] == 0
    assert seen["authorized_kb_id"] == "kb-legacy"
    assert seen["listed_kb_id"] == "kb-legacy"


def test_document_list_rejects_conflicting_dataset_id_params(monkeypatch):
    service_calls = []
    monkeypatch.setattr(document_app.UserTenantService, "query", lambda *_args, **_kwargs: service_calls.append("tenant"))

    body = _call_list_docs(dataset_id="kb-1", legacy_kb_id="kb-2")

    assert body["code"] == 101
    assert body["message"] == 'Query parameters "id" and "kb_id" must match.'
    assert service_calls == []
