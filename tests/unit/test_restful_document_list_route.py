"""RESTful 文档列表路由契约测试。

GET /api/v1/datasets/{dataset_id}/documents 是 web 会话与 API token 的统一入口；
旧 POST /v1/document/list 仅保留为已弃用的兼容入口，sdk/doc.py 中的重复 GET 路由已移除。
"""

import json
import sys
from typing import Any

from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents"
_RAW_DOC = {
    "id": "doc1",
    "name": "a.pdf",
    "kb_id": "kb1",
    "chunk_num": 3,
    "token_num": 12,
    "parser_id": "naive",
    "run": "3",
    "thumbnail": "thumb.png",
    "source_type": "local/file",
    "parser_config": {},
    "create_time": 200,
}


def _route_module():
    return sys.modules["api.apps.restful_apis.document"]


def _stub_list(monkeypatch, sessions: list[object], calls: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(
        KnowledgebaseService,
        "accessible",
        classmethod(lambda cls, db, kb_id, user_id: sessions.append(db) or True),
    )

    def _get_by_kb_id(
        cls,
        db,
        kb_id,
        page_number,
        items_per_page,
        orderby,
        desc,
        keywords,
        run_status=None,
        types=None,
        suffix=None,
        name=None,
        doc_ids=None,
        return_empty_metadata=False,
    ):
        sessions.append(db)
        calls.append(
            {
                "kb_id": kb_id,
                "page": page_number,
                "page_size": items_per_page,
                "orderby": orderby,
                "desc": desc,
                "keywords": keywords,
                "run_status": run_status,
                "types": types,
                "suffix": suffix,
                "name": name,
                "doc_ids": doc_ids,
                "return_empty_metadata": return_empty_metadata,
            }
        )
        return ([] if doc_ids == [] else [dict(_RAW_DOC)], 0 if doc_ids == [] else 1)

    monkeypatch.setattr(DocumentService, "get_by_kb_id", classmethod(_get_by_kb_id))


def _assert_sync_facade(sessions: list[object]) -> None:
    assert sessions
    assert all(isinstance(session, Session) for session in sessions)


def test_list_documents_maps_fields_and_forwards_filters(client, monkeypatch):
    sessions: list[object] = []
    calls: list[dict[str, Any]] = []
    _stub_list(monkeypatch, sessions, calls)

    response = client.get(
        _PATH,
        params=[
            ("page", "2"),
            ("page_size", "20"),
            ("keywords", "report"),
            ("run_status", "DONE"),
            ("run", "1"),
            ("types", "pdf"),
            ("suffix", "pdf"),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {
        "total": 1,
        "docs": [
            {
                "id": "doc1",
                "name": "a.pdf",
                "dataset_id": "kb1",
                "chunk_count": 3,
                "token_count": 12,
                "chunk_method": "naive",
                "run": "DONE",
                "thumbnail": "/v1/document/image/kb1-thumb.png",
                "source_type": "local",
                "parser_config": {},
                "create_time": 200,
            }
        ],
    }
    assert calls == [
        {
            "kb_id": "kb1",
            "page": 2,
            "page_size": 20,
            "orderby": "create_time",
            "desc": True,
            "keywords": "report",
            "run_status": ["1", "3"],
            "types": ["pdf"],
            "suffix": ["pdf"],
            "name": None,
            "doc_ids": None,
            "return_empty_metadata": False,
        }
    ]
    _assert_sync_facade(sessions)


def test_list_documents_preserves_empty_metadata_match_as_empty_filter(client, monkeypatch):
    sessions: list[object] = []
    calls: list[dict[str, Any]] = []
    _stub_list(monkeypatch, sessions, calls)
    monkeypatch.setattr(
        DocMetadataService,
        "get_flatted_meta_by_kbs",
        classmethod(lambda cls, db, kb_ids: sessions.append(db) or {}),
    )
    metadata_condition = {"logic": "and", "conditions": [{"name": "author", "comparison_operator": "is", "value": "nobody"}]}

    response = client.get(_PATH, params={"metadata_condition": json.dumps(metadata_condition)})

    assert response.status_code == 200
    assert response.json()["data"] == {"total": 0, "docs": []}
    assert calls[0]["doc_ids"] == []
    _assert_sync_facade(sessions)


def test_list_documents_rejects_invalid_filter_values_before_db(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, *args, **kwargs: calls.append("db") or True))

    invalid_status = client.get(_PATH, params={"run_status": "BOGUS"}).json()
    invalid_type = client.get(_PATH, params={"types": "exe"}).json()
    invalid_metadata = client.get(_PATH, params={"metadata": "[1]"}).json()

    assert invalid_status["code"] == int(RetCode.DATA_ERROR)
    assert invalid_status["message"] == "Invalid filter run status conditions: BOGUS"
    assert invalid_type["code"] == int(RetCode.DATA_ERROR)
    assert "Invalid filter conditions: exe type" == invalid_type["message"]
    assert invalid_metadata["code"] == int(RetCode.DATA_ERROR)
    assert invalid_metadata["message"] == "metadata must be an object."
    assert calls == []


def test_list_documents_denies_inaccessible_dataset(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(
        KnowledgebaseService,
        "accessible",
        classmethod(lambda cls, db, kb_id, user_id: sessions.append(db) or False),
    )

    response = client.get(_PATH)

    assert response.status_code == 200
    assert response.json() == {"code": int(RetCode.DATA_ERROR), "message": "You don't own the dataset kb1."}
    _assert_sync_facade(sessions)


def test_list_documents_type_filter_aggregates_via_sql(client, monkeypatch):
    """type=filter 短路进 get_filter_by_kb_id（SQL 全量聚合），不跑分页文档查询。"""
    sessions: list[object] = []
    filter_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        KnowledgebaseService,
        "accessible",
        classmethod(lambda cls, db, kb_id, user_id: sessions.append(db) or True),
    )

    def _fail_get_by_kb_id(cls, *args, **kwargs):
        raise AssertionError("filter mode must not run the paginated doc query")

    monkeypatch.setattr(DocumentService, "get_by_kb_id", classmethod(_fail_get_by_kb_id))

    def _get_filter_by_kb_id(cls, db, kb_id, keywords, run_status, types, suffix):
        sessions.append(db)
        filter_calls.append({"kb_id": kb_id, "keywords": keywords, "run_status": run_status, "types": types, "suffix": suffix})
        return {"suffix": {"pdf": 2}, "run_status": {"3": 2}, "metadata": {"empty_metadata": {"true": 0}}}, 2

    monkeypatch.setattr(DocumentService, "get_filter_by_kb_id", classmethod(_get_filter_by_kb_id))

    response = client.get(
        _PATH,
        params=[
            ("type", "filter"),
            ("page", "5"),
            ("page_size", "1"),
            ("keywords", "report"),
            ("run_status", "DONE"),
            ("types", "pdf"),
            ("suffix", "pdf"),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] == {
        "total": 2,
        "filter": {"suffix": {"pdf": 2}, "run_status": {"3": 2}, "metadata": {"empty_metadata": {"true": 0}}},
    }
    assert filter_calls == [{"kb_id": "kb1", "keywords": "report", "run_status": ["3"], "types": ["pdf"], "suffix": ["pdf"]}]
    _assert_sync_facade(sessions)


def test_list_documents_type_filter_rejects_invalid_status_before_db(client, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, *args, **kwargs: calls.append("db") or True))

    body = client.get(_PATH, params={"type": "filter", "run_status": "BOGUS"}).json()

    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Invalid filter run status conditions: BOGUS"
    assert calls == []


def test_legacy_document_filter_post_is_deprecated(client):
    legacy_routes = []
    for context in iter_route_contexts(client.app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path == "/v1/document/filter" and "POST" in context.methods:
            legacy_routes.append(route)

    assert len(legacy_routes) == 1
    assert legacy_routes[0].endpoint.__module__ == "api.apps.document"
    assert legacy_routes[0].deprecated is True
    assert client.app.openapi()["paths"]["/v1/document/filter"]["post"]["deprecated"] is True


def test_document_list_has_one_restful_route_and_deprecated_legacy_post(client):
    restful_routes = []
    legacy_routes = []
    for context in iter_route_contexts(client.app.routes):
        route = context.original_route
        if isinstance(route, APIRoute) and context.path == "/api/v1/datasets/{dataset_id}/documents" and "GET" in context.methods:
            restful_routes.append(route)
        if isinstance(route, APIRoute) and context.path == "/v1/document/list" and "POST" in context.methods:
            legacy_routes.append(route)

    assert len(restful_routes) == 1
    assert restful_routes[0].endpoint.__module__ == "api.apps.restful_apis.document"
    assert len(legacy_routes) == 1
    assert legacy_routes[0].endpoint.__module__ == "api.apps.document"
    assert legacy_routes[0].deprecated is True
    assert client.app.openapi()["paths"]["/v1/document/list"]["post"]["deprecated"] is True


def test_document_list_route_has_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "GET", "/api/v1/datasets/{dataset_id}/documents")

    assert get_db not in calls
    assert current_tenant_id not in calls
    assert api_apps.manager not in calls
    assert async_current_tenant_id in calls
    assert get_async_db in calls
    assert _route_module().list_documents in calls
