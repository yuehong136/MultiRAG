"""The retained SDK route applies metadata inside its validated document scope."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.sdk import doc as sdk
from api.db.db_models import Knowledgebase
from api.utils.api_utils import token_required
from common import settings


@pytest.fixture
def sdk_retrieval(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, AsyncMock]:
    client.app.dependency_overrides[token_required] = lambda: "owner"
    kb = Knowledgebase(id="dataset", tenant_id="owner", name="dataset", tenant_embd_id=None, embd_id="embedding")
    monkeypatch.setattr(sdk.KnowledgebaseService, "query", lambda *_a, **_k: [kb])
    monkeypatch.setattr(sdk.KnowledgebaseService, "get_by_ids", lambda *_: [kb])
    monkeypatch.setattr(sdk.KnowledgebaseService, "get_by_id", lambda *_: kb)
    monkeypatch.setattr(sdk.KnowledgebaseService, "list_documents_by_ids", lambda *_: ["old", "new", "without-metadata"])
    monkeypatch.setattr(sdk.DocMetadataService, "get_flatted_meta_by_kbs", lambda *_: {"version": {"v1": ["old"], "v2": ["new", "outside-selection"]}})
    monkeypatch.setattr(sdk, "get_model_config_by_type_and_name", lambda *_: {})
    monkeypatch.setattr(sdk, "LLMBundle", lambda *_: None)
    monkeypatch.setattr(sdk, "label_question", lambda *_: {})
    retrieval = AsyncMock(return_value={"total": 0, "chunks": [], "doc_aggs": []})
    monkeypatch.setattr(settings, "retriever", SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, *_: chunks))
    return client, retrieval


@pytest.mark.parametrize(
    ("documents", "conditions", "logic", "expected"),
    [
        (["old", "new"], [{"name": "version", "comparison_operator": "is", "value": "v2"}], "and", ["new"]),
        (["old"], [{"name": "version", "comparison_operator": "is", "value": "v2"}], "and", []),
        (["without-metadata"], [{"name": "version", "comparison_operator": "not is", "value": "v1"}], "and", []),
        ([], [{"name": "version", "comparison_operator": "is", "value": "absent"}, {"name": "version", "comparison_operator": "is", "value": "v2"}], "and", []),
        (["old", "new"], [{"name": "missing", "comparison_operator": "is", "value": "x"}, {"name": "version", "comparison_operator": "is", "value": "v2"}], "or", ["new"]),
    ],
)
def test_legacy_http_intersects_metadata(sdk_retrieval: tuple[TestClient, AsyncMock], documents: list[str], conditions: list[dict[str, Any]], logic: str, expected: list[str]) -> None:
    client, retrieval = sdk_retrieval
    response = client.post("/api/v1/retrieval", json={"dataset_ids": ["dataset"], "question": "q", "document_ids": documents, "metadata_condition": {"conditions": conditions, "logic": logic}})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    if expected:
        assert retrieval.call_args.args[10] == expected
    else:
        assert response.json()["data"] == {"total": 0, "chunks": [], "doc_aggs": {}}
        retrieval.assert_not_called()


@pytest.mark.parametrize("documents", [[], ["old", "new"]])
def test_legacy_http_no_filter_retains_document_selection(sdk_retrieval: tuple[TestClient, AsyncMock], documents: list[str]) -> None:
    client, retrieval = sdk_retrieval
    response = client.post("/api/v1/retrieval", json={"dataset_ids": ["dataset"], "question": "q", "document_ids": documents})
    assert response.json()["code"] == 0, response.text
    assert retrieval.call_args.args[10] == (documents or None)


def test_legacy_http_validates_documents_before_filtering(sdk_retrieval: tuple[TestClient, AsyncMock]) -> None:
    client, retrieval = sdk_retrieval
    response = client.post(
        "/api/v1/retrieval",
        json={"dataset_ids": ["dataset"], "question": "q", "document_ids": ["unauthorized"], "metadata_condition": {"conditions": [{"name": "version", "comparison_operator": "is", "value": "v2"}]}},
    )
    assert response.json()["code"] != 0 and "don't own" in response.json()["message"]
    retrieval.assert_not_called()


@pytest.mark.parametrize(
    "constraint", [{"document_ids": ["old"]}, {"metadata_condition": {"conditions": []}}, {"metadata_condition": {"conditions": [{"name": "version", "comparison_operator": "is", "value": "absent"}]}}]
)
@pytest.mark.parametrize("question", ["q", " "])
def test_legacy_http_rejects_kg_with_constraints(sdk_retrieval: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch, constraint: dict[str, Any], question: str) -> None:
    client, retrieval = sdk_retrieval
    graph = AsyncMock()
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=graph))
    response = client.post("/api/v1/retrieval", json={"dataset_ids": ["dataset"], "question": question, "use_kg": True, **constraint})
    assert response.status_code == 200 and response.json()["code"] == 400, response.text
    assert "cannot be combined" in response.json()["message"]
    retrieval.assert_not_called()
    graph.assert_not_called()


def test_legacy_http_preserves_unrestricted_kg(sdk_retrieval: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, retrieval = sdk_retrieval
    monkeypatch.setattr(sdk, "get_tenant_default_model_by_type", lambda *_: {})
    graph = AsyncMock(return_value={"doc_id": "", "content_with_weight": "graph"})
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=graph))
    response = client.post("/api/v1/retrieval", json={"dataset_ids": ["dataset"], "question": "q", "use_kg": True, "document_ids": [], "metadata_condition": {}})
    assert response.json()["code"] == 0 and response.json()["data"]["chunks"][0]["content"] == "graph", response.text
    assert retrieval.call_args.args[10] is None
    graph.assert_awaited_once()


def test_sdk_multi_dataset_passes_ordered_tenant_name_id_bindings(sdk_retrieval: tuple[TestClient, AsyncMock], monkeypatch: pytest.MonkeyPatch) -> None:
    client, retrieval = sdk_retrieval
    rows = [
        Knowledgebase(id=identifier, tenant_id=owner, name="name-" + identifier, tenant_embd_id=None, embd_id="embedding") for identifier, owner in (("a", "owner"), ("b", "owner"), ("c", "other"))
    ]
    monkeypatch.setattr(sdk.KnowledgebaseService, "query", lambda *_a, **_k: rows)
    monkeypatch.setattr(sdk.KnowledgebaseService, "get_by_ids", lambda *_: rows)
    monkeypatch.setattr(sdk.KnowledgebaseService, "get_by_id", lambda *_: rows[0])
    response = client.post("/api/v1/retrieval", json={"dataset_ids": ["a", "b", "c"], "question": "q"})
    assert response.json()["code"] == 0, response.text
    assert retrieval.call_args.args[3:5] == (["owner", "owner", "other"], ["name-a", "name-b", "name-c"])
    assert retrieval.call_args.kwargs["kb_ids"] == ["a", "b", "c"]


async def test_embedded_retrieval_pairs_loaded_rows_when_sql_order_differs(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.sdk import session

    rows = [Knowledgebase(id=identifier, tenant_id=owner, name="name-" + identifier, tenant_embd_id=None, embd_id="embedding") for identifier, owner in (("b", "other"), ("a", "owner"))]
    monkeypatch.setattr(session.UserTenantService, "query", lambda *_a, **_k: [SimpleNamespace(tenant_id="owner"), SimpleNamespace(tenant_id="other")])
    monkeypatch.setattr(session.KnowledgebaseService, "query", lambda *_a, **kwargs: [row for row in rows if row.id == kwargs["id"] and row.tenant_id == kwargs["tenant_id"]])
    monkeypatch.setattr(session.KnowledgebaseService, "get_by_ids", lambda *_: rows)
    monkeypatch.setattr(session, "build_named_bundle_async", AsyncMock(return_value=object()))
    monkeypatch.setattr(session, "_label_question_with_conn", lambda *_: {})
    fetch = AsyncMock(return_value={"total": 0, "chunks": [], "doc_aggs": []})
    monkeypatch.setattr(session.settings, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, *_: chunks))
    await session.retrieval_test_embedded(session.SearchBotRetrievalTestRequest(question="q", kb_id=["a", "b"]), async_db, "owner")
    assert fetch.call_args.args[3:5] == (["other", "owner"], ["name-b", "name-a"])
    assert fetch.call_args.kwargs["kb_ids"] == ["b", "a"]
