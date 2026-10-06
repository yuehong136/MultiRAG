"""REST search keeps local ranking, metadata, graph and permission contracts."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.apps.services import dataset_search_service as service
from api.db.db_models import Knowledgebase
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils.dataset_search import SearchDatasetRequest
from common import settings
from common.constants import RetCode

type SearchEnv = tuple[AsyncSession, list[Knowledgebase], AsyncMock]


def kb(identifier: str, embedding: str = "same") -> Knowledgebase:
    return Knowledgebase(id=identifier, tenant_id="owner-" + identifier, name="name-" + identifier, embd_id=embedding, parser_config={})


@pytest.fixture
def search_env(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> SearchEnv:
    rows = [kb("a"), kb("b")]
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "scalars", AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))
    bundle = SimpleNamespace(db=None)
    monkeypatch.setattr(service, "_bundle", AsyncMock(return_value=bundle))
    monkeypatch.setattr(service, "label_question", lambda *_: {"tag": 2})
    retrieval = AsyncMock(return_value={"total": 71, "chunks": [{"vector": [1], "text": "page result"}], "doc_aggs": []})
    monkeypatch.setattr(settings, "retriever", SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, tenants: chunks))
    return (async_db, rows, retrieval)


async def test_search_preserves_multi_dataset_controls_and_total(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    db, _, retrieval = search_env
    monkeypatch.setattr(service, "cross_languages", AsyncMock(return_value="translated"))
    monkeypatch.setattr(service, "keyword_extraction", AsyncMock(return_value="keywords"))
    request = SearchDatasetRequest(
        question="  original  ",
        dataset_ids=["a", "b", "a"],
        doc_ids=["doc"],
        page=3,
        size=2,
        top_k=4096,
        similarity_threshold=0.4,
        vector_similarity_weight=0.6,
        highlight=True,
        keyword=True,
        cross_languages=["English"],
        rerank_id="rerank",
        search_mode={"type": "hybrid", "weight_dense": 0.2, "weight_sparse": 0.3},
    )
    success, result, code = await service.search_dataset(db, "user", "a", request)
    assert success and code == RetCode.SUCCESS
    args, kwargs = retrieval.call_args
    assert args[0] == "translated,keywords"
    assert args[3:6] == (["owner-a", "owner-b"], ["name-a", "name-b"], 3)
    assert args[6:11] == (2, 0.4, 0.6, 2048, ["doc"])
    assert kwargs["kb_ids"] == ["a", "b"]
    assert kwargs["highlight"] is True
    assert kwargs["search_mode"] == {"hybrid": {"weight_dense": 0.4, "weight_sparse": 0.6}}
    assert result == {"total": 71, "chunks": [{"text": "page result"}], "doc_aggs": [], "labels": {"tag": 2}}


async def test_search_denies_any_unauthorized_selection_before_metadata(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    db, _, retrieval = search_env
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(side_effect=[True, False]))
    metadata = AsyncMock()
    monkeypatch.setattr(service, "apply_meta_data_filter", metadata)
    assert (await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", dataset_ids=["a", "b"], meta_data_filter={"method": "auto"})))[2] == RetCode.AUTHENTICATION_ERROR
    metadata.assert_not_called()
    retrieval.assert_not_called()


async def test_search_rejects_anchor_outside_selection(search_env: SearchEnv) -> None:
    db, _, retrieval = search_env
    assert (await service.search_dataset(db, "user", "outside", SearchDatasetRequest(question="q", dataset_ids=["a", "b"])))[2] == RetCode.ARGUMENT_ERROR
    retrieval.assert_not_called()


async def test_search_rejects_mixed_embedding_before_model_io(search_env: SearchEnv) -> None:
    db, rows, retrieval = search_env
    rows[1].embd_id = "different"
    result = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", dataset_ids=["a", "b"]))
    assert result[2] == RetCode.DATA_ERROR and "different embedding models" in result[1]
    retrieval.assert_not_called()


@pytest.mark.parametrize(
    ("value", "selected", "expected"),
    [("absent", None, ["-999"]), ("absent", ["selected"], ["-999"]), ("match", ["selected"], ["-999"]), ("match", ["selected", "matched"], ["matched"]), ("match", None, ["matched"])],
)
async def test_search_intersects_metadata_and_selected_docs(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch, value: str, selected: list[str] | None, expected: list[str]) -> None:
    db, rows, retrieval = search_env
    rows.pop()
    monkeypatch.setattr(service.DocMetadataService, "get_flatted_meta_by_kbs", lambda *_: {"category": {"match": ["matched"]}})
    await service.search_dataset(
        db, "user", "a", SearchDatasetRequest(question="q", doc_ids=selected, meta_data_filter={"method": "manual", "manual": [{"key": "category", "op": "is", "value": value}]})
    )
    assert retrieval.call_args.args[10] == expected


async def test_saved_search_permission_checked_before_config_consumption(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.user_service import UserTenantService

    db, rows, retrieval = search_env
    rows.pop()
    monkeypatch.setattr(db, "scalar", AsyncMock(return_value=SimpleNamespace(tenant_id="other", search_config={"meta_data_filter": {"method": "auto"}})))
    monkeypatch.setattr(UserTenantService, "get_membership", lambda *_a, **_k: None)
    result = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", search_id="private-search"))
    assert result[2] == RetCode.AUTHENTICATION_ERROR
    retrieval.assert_not_called()


async def test_saved_search_metadata_overrides_request_and_auto_chat(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.user_service import UserTenantService

    db, rows, retrieval = search_env
    rows.pop()
    filt = {"method": "semi_auto", "semi_auto": ["category"]}
    monkeypatch.setattr(db, "scalar", AsyncMock(return_value=SimpleNamespace(tenant_id="owner-a", search_config={"meta_data_filter": filt, "chat_id": "configured"})))
    monkeypatch.setattr(UserTenantService, "get_membership", lambda *_a, **_k: SimpleNamespace(role="owner"))
    monkeypatch.setattr(service.DocMetadataService, "get_flatted_meta_by_kbs", lambda *_: {"category": {"match": ["doc"]}})
    filtered = AsyncMock(return_value=None)
    monkeypatch.setattr(service, "apply_meta_data_filter", filtered)
    await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", search_id="search", meta_data_filter={"method": "manual"}))
    assert filtered.call_args.args[0] == filt
    assert retrieval.call_args.args[10] is None
    assert service._bundle.call_args_list[0].kwargs == {"name": "configured"}


async def test_kg_result_and_children_do_not_replace_total(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    db, rows, _ = search_env
    rows.pop()
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=AsyncMock(return_value={"content_with_weight": "graph", "vector": [2]})))
    success, result, _ = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", use_kg=True))
    assert success and result["total"] == 71
    assert len(result["chunks"]) == 2
    assert all("vector" not in chunk for chunk in result["chunks"])


@pytest.mark.parametrize("constraint", [{"doc_ids": ["doc"]}, {"meta_data_filter": {"method": "manual", "manual": []}}, {"meta_data_filter": {"method": "auto"}}, {"search_id": "saved"}])
async def test_kg_rejects_constraints_before_model_and_metadata_io(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch, constraint: dict[str, Any]) -> None:
    db, rows, retrieval = search_env
    rows.pop()
    metadata = AsyncMock()
    graph = AsyncMock()
    monkeypatch.setattr(service, "apply_meta_data_filter", metadata)
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=graph))
    result = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", use_kg=True, **constraint))
    assert result[0] is False and result[2] == RetCode.BAD_REQUEST
    assert "cannot be combined" in result[1]
    service._bundle.assert_not_called()
    metadata.assert_not_called()
    graph.assert_not_called()
    retrieval.assert_not_called()


@pytest.mark.parametrize("constraint", [{"doc_ids": ["doc"]}, {"meta_data_filter": {"method": "manual", "manual": [{"key": "missing", "op": "is", "value": "none"}]}}, {"search_id": "saved"}])
def test_kg_constraint_http_rejection(client: TestClient, search_env: SearchEnv, constraint: dict[str, Any]) -> None:
    from api.db.db_models import get_async_db

    db, rows, retrieval = search_env
    rows.pop()
    client.app.dependency_overrides[get_async_db] = lambda: db
    response = client.post("/api/v1/datasets/a/search", json={"question": "q", "use_kg": True, **constraint})
    assert response.status_code == 200 and response.json()["code"] == 400, response.text
    assert "cannot be combined" in response.json()["message"]
    retrieval.assert_not_called()


async def test_kg_accepts_empty_document_and_metadata_selection(search_env: SearchEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    db, rows, _ = search_env
    rows.pop()
    graph = AsyncMock(return_value={"content_with_weight": "graph"})
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=graph))
    success, result, code = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", use_kg=True, doc_ids=[], meta_data_filter={}))
    assert success and code == RetCode.SUCCESS and len(result["chunks"]) == 2
    graph.assert_awaited_once()


async def test_bundle_detaches_sync_facade(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:

    def build(self: LLMBundle, db: Session, *args: Any) -> None:
        self.db = db

    monkeypatch.setattr(service.LLMBundle, "__init__", build)
    monkeypatch.setattr(service, "get_tenant_default_model_by_type", lambda *_: {})
    assert (await service._bundle(async_db, "owner", service.LLMType.CHAT)).db is None


@pytest.mark.parametrize(
    "payload",
    [
        {"question": " "},
        {"question": "q", "page": 0},
        {"question": "q", "size": 0},
        {"question": "q", "similarity_threshold": 2},
        {"question": "q", "dataset_ids": []},
        {"question": "q", "search_mode": {"type": "hybrid", "weight_dense": 0, "weight_sparse": 0}},
    ],
)
def test_search_route_rejects_invalid_requests(client: TestClient, payload: dict[str, Any]) -> None:
    assert client.post("/api/v1/datasets/a/search", json=payload).status_code == 422


def test_search_route_rest_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = AsyncMock(return_value=(True, {"total": 2, "chunks": [], "labels": {}}, RetCode.SUCCESS))
    monkeypatch.setattr(service, "search_dataset", fake)
    response = client.post("/api/v1/datasets/a/search", json={"question": "question"})
    assert response.json() == {"code": 0, "data": {"total": 2, "chunks": [], "labels": {}}}
    assert fake.call_args.args[1:3] == ("tenant-unit", "a")


@pytest.mark.parametrize("code", [RetCode.AUTHENTICATION_ERROR, RetCode.DATA_ERROR, RetCode.ARGUMENT_ERROR])
def test_search_route_preserves_failure_business_code(client: TestClient, monkeypatch: pytest.MonkeyPatch, code: RetCode) -> None:
    monkeypatch.setattr(service, "search_dataset", AsyncMock(return_value=(False, "denied", code)))
    response = client.post("/api/v1/datasets/a/search", json={"question": "q"})
    assert response.json()["code"] == code
    assert response.json()["message"] == "denied"


async def test_doc_graph_keeps_artifact_conditions_and_mind_map_ids(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import threading

    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "get", AsyncMock(side_effect=[SimpleNamespace(kb_id="a"), kb("a")]))
    calls = []

    def search(*args: Any) -> str:
        assert threading.current_thread() is not threading.main_thread()
        calls.append(args)
        return args[2]["knowledge_graph_kwd"][0]

    def fields(result: Any, _: Any) -> dict[str, Any]:
        obj = {"nodes": ["kept"]} if result == "subgraph" else {"id": "node", "children": [{"id": "node", "children": None}, {"id": "node"}]}
        return {"artifact": {"content_with_weight": json.dumps(obj)}}

    monkeypatch.setattr(settings, "docStoreConn", SimpleNamespace(index_exist=lambda *_: True, search=search, get_fields=fields))
    success, result = await service.get_document_graph(async_db, "user", "a", "doc")
    assert success and result["graph"] == {"nodes": ["kept"]}
    assert [item["id"] for item in result["mind_map"]["children"]] == ["node(1)", "node(2)"]
    assert calls[0][2] == {"kb_id": "a", "knowledge_graph_kwd": ["subgraph"], "source_id": "doc", "removed_kwd": "N"}
    assert calls[1][2] == {"kb_id": "a", "knowledge_graph_kwd": ["mind_map"], "doc_id": "doc"}


async def test_doc_graph_rejects_cross_dataset_without_store_io(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(KnowledgebaseService, "accessible_async", AsyncMock(return_value=True))
    monkeypatch.setattr(async_db, "get", AsyncMock(return_value=SimpleNamespace(kb_id="foreign")))
    assert await service.get_document_graph(async_db, "user", "a", "doc") == (False, "Document not found in dataset.")


def test_graph_route_selects_document_or_aggregate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate = AsyncMock(return_value=(True, {"graph": {"aggregate": True}}))
    document = AsyncMock(return_value=(True, {"graph": {"document": True}, "mind_map": {}}))
    monkeypatch.setattr(dataset_api_service, "get_knowledge_graph", aggregate)
    monkeypatch.setattr(service, "get_document_graph", document)
    assert client.get("/api/v1/datasets/a/graph").json()["data"]["graph"] == {"aggregate": True}
    assert client.get("/api/v1/datasets/a/graph?doc_id=doc").json()["data"]["graph"] == {"document": True}
    assert document.call_args.args[1:] == ("tenant-unit", "a", "doc")


async def test_sparse_search_passes_availability_and_document_predicates(monkeypatch: pytest.MonkeyPatch) -> None:
    from common.doc_store.doc_store_base import MatchTextExpr
    from core.nlp.search import Dealer

    calls = []

    def query_store(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append(args)
        return []

    dealer = object.__new__(Dealer)
    dealer.qryr = SimpleNamespace(question=lambda *_a, **_k: (MatchTextExpr(["content_ltks"], "query", 10), []))
    dealer.dataStore = SimpleNamespace(
        db_type=lambda: "milvus", search=query_store, get_total=lambda _: 0, get_doc_ids=lambda _: [], get_highlight=lambda *_: {}, get_aggregation=lambda *_: {}, get_fields=lambda *_: {}
    )
    await dealer.search({"question": "query", "available_int": 1, "doc_ids": ["-999"], "search_mode": {"sparse": {}}}, ["scratch"], ["dataset"])
    assert calls[0][2] == {"doc_id": ["-999"], "available_int": 1}
    assert isinstance(calls[0][3][0], MatchTextExpr)


def test_retired_chunk_routes_absent_and_other_contracts_retained(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]
    assert "/v1/chunk/retrieval_test" not in paths
    assert "/v1/chunk/knowledge_graph" not in paths
    assert client.post("/v1/chunk/retrieval_test", json={"question": "q", "kb_ids": ["a"]}).status_code == 404
    assert client.get("/v1/chunk/knowledge_graph?doc_id=doc").status_code == 404
    for path in ("/v1/chunk/list", "/v1/chunk/set", "/v1/chunk/switch", "/api/v1/retrieval", "/api/v1/datasets/{dataset_id}/graph/search", "/api/v1/datasets/{dataset_id}/knowledge_graph"):
        assert path in paths


def test_search_route_exposes_storage_exception_as_business_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "search_dataset", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    response = client.post("/api/v1/datasets/a/search", json={"question": "q", "search_mode": {"type": "dense"}})
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == RetCode.DATA_ERROR
    assert "data" not in body
    assert body["message"] == "Internal server error"


async def test_multi_dataset_retrieval_keeps_each_tenant_paired_with_its_dataset(search_env: SearchEnv) -> None:
    db, rows, retrieval = search_env
    rows[1].tenant_id = rows[0].tenant_id
    rows.append(kb("c"))
    result = await service.search_dataset(db, "user", "a", SearchDatasetRequest(question="q", dataset_ids=["a", "b", "c"]))
    assert result[0] is True
    assert retrieval.call_args.args[3:5] == (["owner-a", "owner-a", "owner-c"], ["name-a", "name-b", "name-c"])
    assert retrieval.call_args.kwargs["kb_ids"] == ["a", "b", "c"]
