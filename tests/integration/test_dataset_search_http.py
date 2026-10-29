"""Real JWT/API-key, SQL metadata and Milvus readback for dataset search/graph.

Model/filter generation and KG output are controlled; document ranking,
predicates, HTTP/auth and SQL metadata are real.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from pymilvus import DataType
from sqlalchemy.orm import Session

from api.apps.services import dataset_search_service
from api.apps.services.dataset_search_service import _bundle as build_search_bundle
from api.db.db_models import APIToken, Document, DocumentMetadata, Knowledgebase, Search, TenantLLM, UserTenant
from common import settings
from common.constants import RetCode
from core.nlp import search
from tests.support.dataset_search_http import request
from tests.support.dataset_search_http import search_api as search_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.web_consumer import tsx_runner


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid", "fusion"])
def test_http_search_filters_disabled_chunks_and_metadata(search_api: dict[str, Any], mode: str) -> None:
    env = search_api
    payload = {
        "question": "availability",
        "search_mode": {"type": mode},
        "highlight": True,
        "similarity_threshold": 0,
        "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "match"}]},
    }
    response = request(env, "POST", "/search", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0, body
    assert {chunk["chunk_id"] for chunk in body["data"]["chunks"]} == {env["rows"]["dataset"][1]["id"]}, body
    assert body["data"]["total"] == 1
    assert all("vector" not in chunk for chunk in body["data"]["chunks"])
    payload["meta_data_filter"]["manual"][0]["value"] = "absent"
    absent = request(env, "POST", "/search", json=payload).json()
    assert absent["code"] == 0 and absent["data"]["total"] == 0, absent
    assert absent["data"]["chunks"] == []
    # Independent SDK readback confirms both enabled/disabled source rows exist.
    raw = env["reader"].query(
        env["collections"]["dataset"], filter="pk in " + json.dumps([row["id"] for row in env["rows"]["dataset"]]), output_fields=["pk", "available_int"], consistency_level="Strong"
    )
    assert {row["available_int"] for row in raw} == {0, 1}


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid", "fusion"])
def test_http_metadata_and_document_scope_intersect(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = search_api
    with Session(env["engine"]) as db:
        for doc, version in (("doc", "v1"), ("second_doc", "v2")):
            metadata = db.get(DocumentMetadata, env[doc])
            assert metadata is not None
            metadata.meta_fields = {"category": "match", "version": version}
        db.commit()
    # A new connection proves the filter reads persisted metadata.
    with Session(env["engine"]) as db:
        assert db.get(DocumentMetadata, env["doc"]).meta_fields["version"] == "v1"
        assert db.get(DocumentMetadata, env["second_doc"]).meta_fields["version"] == "v2"

    both = [env["doc"], env["second_doc"]]
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "search_mode": {"type": mode}, "similarity_threshold": 0}

    def check(documents: list[str] | None, conditions: list[dict[str, Any]], logic: str, expected: set[str], semi_expected: set[str] | None = None) -> None:
        for method in ("manual", "auto", "semi_auto"):
            monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": conditions, "logic": logic}))
            config = {"method": method, "manual": conditions, "logic": logic, "semi_auto": ["category", "version"]}
            body = request(env, "POST", "/search", json={**payload, "doc_ids": documents, "meta_data_filter": config}).json()
            assert body["code"] == 0, body
            mode_expected = set() if documents == [] else (semi_expected if method == "semi_auto" and semi_expected is not None else expected)
            assert {row["doc_id"] for row in body["data"]["chunks"]} == mode_expected, (method, body)
            assert {row["doc_id"] for row in body["data"]["doc_aggs"]} == mode_expected, body
            assert body["data"]["total"] == len(mode_expected), body

        legacy = {"conditions": [{"name": c["key"], "comparison_operator": c["op"], "value": c["value"]} for c in conditions], "logic": logic}
        response = requests.post(
            env["base"] + "/api/v1/retrieval", headers={"Authorization": "Bearer " + env["sdk_token"]}, json={**payload, "document_ids": documents or [], "metadata_condition": legacy}, timeout=30
        )
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        assert {row["document_id"] for row in body["data"]["chunks"]} == expected, body
        assert body["data"]["total"] == len(expected), body

    current = {"key": "version", "op": "is", "value": "v2"}
    ready = {"key": "category", "op": "is", "value": "match"}
    missing = {"key": "missing", "op": "is", "value": "x"}
    absent = {"key": "version", "op": "is", "value": "absent"}
    check(both, [current], "and", {env["second_doc"]})
    check([env["doc"]], [current], "and", set())
    check(both, [absent], "and", set())
    check(both, [missing, ready], "and", set())
    check(both, [missing, current], "or", {env["second_doc"]}, semi_expected=set())
    check(None, [current], "and", {env["second_doc"]})
    check([], [current], "and", {env["second_doc"]})

    for documents, expected in ((None, set(both)), ([], set()), ([env["doc"]], {env["doc"]})):
        body = request(env, "POST", "/search", json={**payload, "doc_ids": documents}).json()
        assert body["code"] == 0 and {row["doc_id"] for row in body["data"]["chunks"]} == expected, body
    raw = env["reader"].query(env["collections"]["dataset"], filter="knowledge_graph_kwd == ''", output_fields=["doc_id", "available_int"], consistency_level="Strong")
    assert {row["doc_id"] for row in raw} == {env["doc"]}
    assert {row["available_int"] for row in raw} == {0, 1}


def test_http_kg_constraint_contract(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.restful_apis import chunk as sdk

    env = search_api
    graph = AsyncMock(return_value={"doc_id": "", "content_with_weight": "unrestricted graph"})
    metadata = Mock(side_effect=AssertionError("KG constraints must be rejected before reading metadata"))
    models = AsyncMock(wraps=dataset_search_service._bundle)
    sdk_models = Mock(wraps=sdk.LLMBundle)
    monkeypatch.setattr(settings, "kg_retriever", SimpleNamespace(retrieval=graph))
    monkeypatch.setattr(dataset_search_service.DocMetadataService, "get_flatted_meta_by_kbs", metadata)
    monkeypatch.setattr(dataset_search_service, "_bundle", models)
    monkeypatch.setattr(sdk, "LLMBundle", sdk_models)
    monkeypatch.setattr(sdk, "get_tenant_default_model_by_type", lambda *_: {})
    for constraint in (
        {"doc_ids": [env["doc"]]},
        {"meta_data_filter": {"method": "manual", "manual": [{"key": "missing", "op": "is", "value": "absent"}]}},
        {"meta_data_filter": {"method": "auto"}},
        {"search_id": env["saved_search"]},
    ):
        response = request(env, "POST", "/search", json={"question": "availability", "use_kg": True, **constraint})
        expected_code = 109 if "search_id" in constraint else 400
        assert response.status_code == 200 and response.json()["code"] == expected_code, response.text
        if expected_code == 400:
            assert "cannot be combined" in response.json()["message"]
    for constraint in (
        {"document_ids": [env["doc"]]},
        {"metadata_condition": {"conditions": []}},
        {"metadata_condition": {"conditions": [{"name": "category", "comparison_operator": "is", "value": "absent"}]}},
    ):
        response = requests.post(
            env["base"] + "/api/v1/retrieval",
            headers={"Authorization": "Bearer " + env["sdk_token"]},
            json={"question": "availability", "dataset_ids": [env["dataset"]], "use_kg": True, **constraint},
            timeout=30,
        )
        assert response.status_code == 200 and response.json()["code"] == 400, response.text
        assert "cannot be combined" in response.json()["message"]
    metadata.assert_not_called()
    models.assert_not_called()
    sdk_models.assert_not_called()
    graph.assert_not_called()

    empty = request(env, "POST", "/search", json={"question": "availability", "use_kg": True, "doc_ids": []}).json()
    assert empty["code"] == 400
    graph.assert_not_called()
    payload = {"question": "availability", "use_kg": True, "meta_data_filter": {}}
    unrestricted = request(env, "POST", "/search", json=payload).json()
    assert unrestricted["code"] == 0 and unrestricted["data"]["chunks"][0]["content_with_weight"] == "unrestricted graph", unrestricted
    legacy = requests.post(
        env["base"] + "/api/v1/retrieval",
        headers={"Authorization": "Bearer " + env["sdk_token"]},
        json={"question": "availability", "dataset_ids": [env["dataset"]], "use_kg": True, "document_ids": [], "metadata_condition": {}},
        timeout=30,
    ).json()
    assert legacy["code"] == 0 and legacy["data"]["chunks"][0]["content"] == "unrestricted graph", legacy
    assert graph.await_count == 2


@pytest.mark.parametrize("collection", [False, True])
def test_http_search_multi_dataset_and_auth_matrix(search_api: dict[str, Any], collection: bool) -> None:
    env = search_api
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "highlight": True}
    body = request(env, "POST", "/search", collection=collection, json=payload).json()
    assert body["code"] == 0, body
    assert {chunk["kb_id"] for chunk in body["data"]["chunks"]} == {env["dataset"], env["second"]}
    assert body["data"]["total"] == 2
    assert request(env, "POST", "/search", collection=collection, token=env["api_key"], json=payload).json()["code"] == 109
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][1], tenant_id=env["owners"][0], invited_by=env["owners"][0], role="normal"))
        db.commit()
    try:
        # Membership alone cannot expose another creator's private datasets.
        assert request(env, "POST", "/search", collection=collection, token=env["api_key"], json=payload).json()["code"] == 109
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id.in_([env["dataset"], env["second"]])).values(permission="team"))
            db.commit()
        sdk = request(env, "POST", "/search", collection=collection, token=env["api_key"], json=payload).json()
        assert sdk["code"] == 0 and sdk["data"]["total"] == 2, sdk
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id.in_([env["dataset"], env["second"]])).values(permission="me"))
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id == env["owners"][1], UserTenant.tenant_id == env["owners"][0]))
            db.commit()
    payload["dataset_ids"].append(env["foreign"])
    assert request(env, "POST", "/search", collection=collection, json=payload).json()["code"] == 109
    payload["dataset_ids"] = [env["dataset"]]
    payload["search_id"] = env["saved_search"]
    assert request(env, "POST", "/search", collection=collection, json=payload).json()["code"] == 109
    assert request(env, "POST", "/search", collection=collection, token="invalid", json=payload).status_code == 401


@pytest.mark.parametrize("mode", ["dense", "sparse", "hybrid", "fusion"])
def test_http_search_prunes_deleted_documents_before_rerank(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = search_api
    collections = [env["collections"][key] for key in ("dataset", "second")]

    def indexed_rows() -> list[list[dict[str, Any]]]:
        return [sorted(env["reader"].query(collection, filter="pk != ''", output_fields=["*"], consistency_level="Strong"), key=lambda row: row["pk"]) for collection in collections]

    before = indexed_rows()
    seen: list[list[str]] = []

    def rerank(_self: search.Dealer, _model: Any, result: search.Dealer.SearchResult, *_args: Any, **_kwargs: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        docs = [result.field[key]["doc_id"] for key in result.ids]
        assert docs == [env["second_doc"]], docs
        seen.append(docs)
        scores = np.ones(len(docs))
        return scores, scores, scores

    monkeypatch.setattr(search.Dealer, "rerank_by_model", rerank)
    payload = {
        "question": "availability",
        "dataset_ids": [env["dataset"], env["second"]],
        "search_mode": {"type": mode},
        "rerank_id": "controlled",
        "similarity_threshold": 0,
        "highlight": True,
        "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "match"}]},
    }
    # Prime the lookup, then remove only SQL. Index and metadata intentionally
    # remain stale so both metadata narrowing and repeated requests exercise it.
    initial = request(env, "POST", "/search", json={**payload, "rerank_id": None})
    assert initial.status_code == 200 and initial.json()["code"] == 0, initial.text
    assert initial.json()["data"]["total"] == 2
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Document).where(Document.id == env["doc"]))
        db.commit()
    for _ in range(2):
        response = request(env, "POST", "/search", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == 0 and body["data"]["total"] == 1, body
        assert {row["doc_id"] for row in body["data"]["chunks"]} == {env["second_doc"]}, body
        assert {row["doc_id"] for row in body["data"]["doc_aggs"]} == {env["second_doc"]}, body
    assert len(seen) == 2
    # Stale metadata must not reopen an explicitly selected deleted document.
    payload["doc_ids"] = [env["doc"]]
    deleted_only = request(env, "POST", "/search", json=payload)
    assert deleted_only.status_code == 200, deleted_only.text
    assert deleted_only.json()["code"] == 0 and deleted_only.json()["data"]["chunks"] == [], deleted_only.text
    assert deleted_only.json()["data"]["total"] == 0
    assert len(seen) == 2
    payload.pop("meta_data_filter")
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Document).where(Document.id == env["second_doc"]))
        db.commit()
    payload.pop("doc_ids")
    all_deleted = request(env, "POST", "/search", json=payload)
    assert all_deleted.status_code == 200, all_deleted.text
    assert all_deleted.json()["code"] == 0 and all_deleted.json()["data"]["total"] == 0, all_deleted.text
    assert all_deleted.json()["data"]["chunks"] == [] and all_deleted.json()["data"]["doc_aggs"] == []
    assert len(seen) == 2
    with Session(env["engine"]) as db:
        assert db.get(Document, env["doc"]) is None
        assert db.get(Document, env["second_doc"]) is None
        assert db.get(DocumentMetadata, env["doc"]) is not None
    assert indexed_rows() == before


def test_dataset_raptor_requires_live_dataset_but_file_summary_requires_document(search_api: dict[str, Any]) -> None:
    env = search_api
    collection = env["collections"]["dataset"]
    summaries = []
    for doc_id, marker in (("graph_raptor_x", "raptor"), (env["doc"], "raptor"), ("graph_raptor_x", "")):
        summaries.append({**env["rows"]["dataset"][1], "id": uuid4().hex, "doc_id": doc_id, "raptor_kwd": marker, "content_with_weight": "availability summary"})
    assert settings.docStoreConn.insert(summaries, collection, env["dataset"]) == []
    env["reader"].flush(collection)
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Document).where(Document.id == env["doc"]))
        db.commit()
    response = request(env, "POST", "/search", json={"question": "availability", "similarity_threshold": 0})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] == 0 and body["data"]["total"] == 1, body
    assert [row["chunk_id"] for row in body["data"]["chunks"]] == [summaries[0]["id"]], body
    assert body["data"]["chunks"][0]["doc_id"] == "graph_raptor_x"
    indexed = env["reader"].query(collection, filter="pk in " + json.dumps([row["id"] for row in summaries]), output_fields=["pk"], consistency_level="Strong")
    assert {row["pk"] for row in indexed} == {row["id"] for row in summaries}


def test_http_graph_document_scope_and_hidden_artifacts(search_api: dict[str, Any]) -> None:
    env = search_api
    graph = request(env, "GET", "/graph", params={"doc_id": env["doc"]}).json()
    assert graph["code"] == 0, graph
    assert graph["data"]["graph"]["nodes"] == [{"id": "node", "pagerank": 1}]
    assert graph["data"]["mind_map"]["children"][0]["id"] == "node(1)"
    aggregate = request(env, "GET", "/graph").json()
    assert aggregate["code"] == 0 and aggregate["data"]["graph"]["nodes"][0]["id"] == "node", aggregate
    assert request(env, "GET", "/graph", params={"doc_id": env["foreign_doc"]}).json()["code"] == 102
    assert request(env, "GET", "/graph", token=env["api_key"], params={"doc_id": env["doc"]}).json()["code"] == 109


@pytest.mark.external_consumer
def test_actual_web_client_against_scratch_http(search_api: dict[str, Any]) -> None:
    import os
    import subprocess
    from pathlib import Path

    root = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    runner = tsx_runner(root)
    script = root / "scripts/verify-dataset-retrieval.ts"
    if not runner.exists() or not script.exists():
        pytest.skip("Independent Web checkout unavailable; select WEB_DATASET_CHECKOUT for live consumer acceptance")
    env = search_api
    execution = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        env={
            **os.environ,
            "DATASET_ACCEPTANCE_BASE": env["base"],
            "DATASET_ACCEPTANCE_TOKEN": env["jwt"],
            "DATASET_ACCEPTANCE_IDS": json.dumps([env["dataset"], env["second"]]),
            "DATASET_ACCEPTANCE_CHUNKS": json.dumps([env["rows"][key][1]["id"] for key in ("dataset", "second")]),
            "DATASET_ACCEPTANCE_DOC": env["doc"],
        },
    )
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert "dataset Web acceptance:" in execution.stdout

    # Retirement follows consumer acceptance; independent store snapshots prove
    # the unregistered paths cannot execute retrieval/graph storage operations.
    def snapshot() -> list[dict[str, Any]]:
        return sorted(
            env["reader"].query(env["collections"]["dataset"], filter="", limit=100, output_fields=["pk", "content_with_weight", "available_int"], consistency_level="Strong"),
            key=lambda row: row["pk"],
        )

    before = snapshot()
    for method, path, options in (
        ("POST", "/v1/chunk/retrieval_test", {"json": {"kb_ids": [env["dataset"]], "question": "availability"}}),
        ("GET", "/v1/chunk/knowledge_graph", {"params": {"doc_id": env["doc"]}}),
    ):
        response = requests.request(method, env["base"] + path, headers={"Authorization": "Bearer " + env["jwt"]}, timeout=30, **options)
        assert response.status_code == 404, response.text
    assert snapshot() == before
    assert len(before) == 6


def test_saved_search_status_and_metadata_contract(search_api: dict[str, Any]) -> None:
    env = search_api
    with Session(env["engine"]) as db:
        saved = db.get(Search, env["saved_search"])
        assert saved is not None
        saved.tenant_id = env["owners"][0]
        saved.search_config = {"meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "absent"}]}}
        db.commit()
    payload = {"question": "availability", "search_id": env["saved_search"], "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "match"}]}}
    response = request(env, "POST", "/search", json=payload).json()
    assert response["code"] == 0 and response["data"]["chunks"] == [] and response["data"]["total"] == 0, response
    with Session(env["engine"]) as db:
        saved = db.get(Search, env["saved_search"])
        assert saved is not None
        saved.status = "0"
        db.commit()
    retired = request(env, "POST", "/search", json=payload).json()
    assert retired["code"] == 102 and retired["message"] == "Search app not found!", retired


def test_http_modes_have_distinct_candidates_weights_and_pages(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api
    query_vector = [1.0] + [0.0] * 767
    lexical_vector = [0.0, 1.0] + [0.0] * 766

    class Embedding:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array(query_vector), 0

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return Embedding()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    lexical = {**env["rows"]["dataset"][1], "content_with_weight": "rivet rivet rivet", "content_ltks": "rivet rivet rivet", "vector": lexical_vector, "q_768_vec": lexical_vector}
    semantic = {**lexical, "id": uuid4().hex, "content_with_weight": "unrelated wording", "content_ltks": "unrelated wording", "vector": query_vector, "q_768_vec": query_vector}
    store = settings.docStoreConn
    assert store.delete({"id": lexical["id"]}, env["collections"]["dataset"], env["dataset"]) == 1
    assert store.insert([lexical, semantic], env["collections"]["dataset"], env["dataset"]) == []
    env["reader"].flush(env["collections"]["dataset"], timeout=60)
    raw = env["reader"].query(
        env["collections"]["dataset"], filter="pk in " + json.dumps([lexical["id"], semantic["id"]]), output_fields=["pk", "doc_id", "content_with_weight", "q_768_vec"], consistency_level="Strong"
    )
    assert {row["pk"]: row["content_with_weight"] for row in raw} == {lexical["id"]: "rivet rivet rivet", semantic["id"]: "unrelated wording"}
    assert {row["doc_id"] for row in raw} == {env["doc"]}

    def run(mode: dict[str, Any] | None, threshold: float = 0.0, page: int = 1, size: int = 10, documents: list[str] | None = None) -> dict[str, Any]:
        payload = {"question": "rivet", "similarity_threshold": threshold, "page": page, "size": size, "doc_ids": documents}
        if mode is not None:
            payload["search_mode"] = mode
        response = request(env, "POST", "/search", json=payload)
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        return body["data"]

    for mode in (None, {"type": "dense"}):
        body = run(mode, threshold=0.2)
        assert [row["chunk_id"] for row in body["chunks"]] == [semantic["id"]], body
    sparse = run({"type": "sparse"})
    assert [row["chunk_id"] for row in sparse["chunks"]] == [lexical["id"]], sparse
    for mode, expected in (
        ({"type": "fusion", "weights": "0.9,0.1"}, [lexical["id"], semantic["id"]]),
        ({"type": "fusion", "weights": "0.1,0.9"}, [semantic["id"], lexical["id"]]),
        ({"type": "hybrid", "weight_dense": 0.8, "weight_sparse": 0.2}, [semantic["id"], lexical["id"]]),
    ):
        body = run(mode)
        assert [row["chunk_id"] for row in body["chunks"]] == expected, body
        pages = [run(mode, page=page, size=1) for page in (1, 2)]
        assert [chunk["chunk_id"] for body in pages for chunk in body["chunks"]] == expected, pages
        assert all(body["total"] == 2 for body in pages), pages
        missing = run(mode, documents=[env["second_doc"]])
        assert missing["total"] == 0 and missing["chunks"] == [] and missing["doc_aggs"] == [], missing
    for weights in ("nan,1", "-1,2", "0,0"):
        response = request(env, "POST", "/search", json={"question": "rivet", "search_mode": {"type": "fusion", "weights": weights}})
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("collection", [False, True])
def test_http_rerank_threshold_survives_document_and_metadata_scope(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, collection: bool) -> None:
    env = search_api

    class ControlledModel:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array([0.2] * 768), 0

        def similarity(self, query: str, texts: list[str]) -> tuple[np.ndarray, int]:
            return np.array([0.9 if "high" in text else 0.1 for text in texts]), 0

    async def bundle(*args: Any, **kwargs: Any) -> ControlledModel:
        return ControlledModel()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    low = {**env["rows"]["dataset"][1], "content_with_weight": "availability low", "content_ltks": "availability low"}
    high = {**low, "id": uuid4().hex, "content_with_weight": "availability high", "content_ltks": "availability high"}
    store = settings.docStoreConn
    assert store.delete({"id": low["id"]}, env["collections"]["dataset"], env["dataset"]) == 1
    assert store.insert([low, high], env["collections"]["dataset"], env["dataset"]) == []
    env["reader"].flush(env["collections"]["dataset"], timeout=60)

    def snapshot() -> list[dict[str, Any]]:
        return sorted(
            env["reader"].query(env["collections"]["dataset"], filter="", limit=100, output_fields=["pk", "doc_id", "content_with_weight", "available_int"], consistency_level="Strong"),
            key=lambda row: row["pk"],
        )

    before = snapshot()
    assert {row["pk"] for row in before} >= {low["id"], high["id"]}
    for scope in ({}, {"doc_ids": [env["doc"], env["second_doc"]]}, {"meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "match"}]}}):
        payload = {
            "dataset_ids": [env["dataset"], env["second"]],
            "question": "availability",
            "similarity_threshold": 0.5,
            "vector_similarity_weight": 1.0,
            "rerank_id": "controlled",
            "size": 1,
            **scope,
        }
        for page in (1, 2):
            response = request(env, "POST", "/search", collection=collection, json={**payload, "page": page})
            body = response.json()
            assert response.status_code == 200 and body["code"] == 0, body
            result = body["data"]
            assert result["total"] == 1, result
            assert [row["chunk_id"] for row in result["chunks"]] == ([high["id"]] if page == 1 else []), result
            assert result["doc_aggs"] == [{"doc_name": "doc.txt", "doc_id": env["doc"], "count": 1}], result
    assert snapshot() == before
    with Session(env["engine"]) as db:
        metadata = db.get(DocumentMetadata, env["doc"])
        assert metadata is not None and metadata.meta_fields == {"category": "match"}


def test_http_search_reports_only_computed_score_components(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Real Milvus scores: computed components (including 0.0) are kept, the rest are null."""
    from api.apps.restful_apis import chunk as sdk

    env = search_api
    query_vector = [1.0] + [0.0] * 767
    lexical_vector = [0.0, 1.0] + [0.0] * 766

    class Embedding:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array(query_vector), 0

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return Embedding()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    monkeypatch.setattr(sdk, "LLMBundle", lambda *_: Embedding())
    lexical = {**env["rows"]["dataset"][1], "content_with_weight": "rivet rivet rivet", "content_ltks": "rivet rivet rivet", "vector": lexical_vector, "q_768_vec": lexical_vector}
    semantic = {**lexical, "id": uuid4().hex, "content_with_weight": "unrelated wording", "content_ltks": "unrelated wording", "vector": query_vector, "q_768_vec": query_vector}
    store = settings.docStoreConn
    assert store.delete({"id": lexical["id"]}, env["collections"]["dataset"], env["dataset"]) == 1
    assert store.insert([lexical, semantic], env["collections"]["dataset"], env["dataset"]) == []
    env["reader"].flush(env["collections"]["dataset"], timeout=60)
    raw = env["reader"].query(env["collections"]["dataset"], filter="pk in " + json.dumps([lexical["id"], semantic["id"]]), output_fields=["pk", "q_768_vec"], consistency_level="Strong")
    assert {row["pk"]: [float(value) for value in row["q_768_vec"][:2]] for row in raw} == {lexical["id"]: [0.0, 1.0], semantic["id"]: [1.0, 0.0]}

    def scores(mode: str, *, sdk_route: bool = False, rerank: bool = False) -> dict[str, tuple[Any, Any, Any]]:
        payload: dict[str, Any] = {"question": "rivet", "search_mode": {"type": mode}, "similarity_threshold": 0}
        if rerank:
            payload["rerank_id"] = "controlled"
        if sdk_route:
            response = requests.post(env["base"] + "/api/v1/retrieval", headers={"Authorization": "Bearer " + env["sdk_token"]}, json={**payload, "dataset_ids": [env["dataset"]]}, timeout=30)
        else:
            response = request(env, "POST", "/search", json=payload)
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        chunks = body["data"]["chunks"]
        # Unavailable components are explicit JSON null, never omitted.
        assert chunks and all({"similarity", "vector_similarity", "term_similarity"} <= chunk.keys() for chunk in chunks), body
        return {chunk.get("chunk_id", chunk.get("id")): (chunk["similarity"], chunk["vector_similarity"], chunk["term_similarity"]) for chunk in chunks}

    for sdk_route in (False, True):
        # Dense-only Milvus ranks by the stored COSINE similarity, so an orthogonal vector keeps a real 0.0.
        dense = scores("dense", sdk_route=sdk_route)
        assert dense == {semantic["id"]: (pytest.approx(1.0), pytest.approx(1.0), None), lexical["id"]: (pytest.approx(0.0), pytest.approx(0.0), None)}, dense
        for mode in ("sparse", "hybrid", "fusion"):
            # Raw BM25 and fused scores cannot be split into lexical and vector parts.
            single_score = scores(mode, sdk_route=sdk_route)
            assert lexical["id"] in single_score and all(vector is None and term is None for _, vector, term in single_score.values()), (mode, single_score)

    def model_rank(_self: search.Dealer, _model: Any, result: search.Dealer.SearchResult, *_args: Any, **_kwargs: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        term = np.array([0.25 if key == lexical["id"] else 0.0 for key in result.ids])
        vector = np.array([0.75 if key == semantic["id"] else 0.5 for key in result.ids])
        return term + vector, term, vector

    monkeypatch.setattr(search.Dealer, "rerank_by_model", model_rank)
    for mode in ("dense", "hybrid"):
        # A reranker computes both components for every chunk, including a 0.0 term match.
        reranked = scores(mode, rerank=True)
        assert reranked == {semantic["id"]: (0.75, 0.75, 0.0), lexical["id"]: (0.75, 0.5, 0.25)}, (mode, reranked)


def test_http_vector_schema_failure_is_business_error(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api

    embedded: list[str] = []

    class WrongDimensionEmbedding:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            embedded.append(text)
            return np.array([1.0, 0.0]), 0

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return WrongDimensionEmbedding()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    response = request(env, "POST", "/search", json={"question": "availability", "search_mode": {"type": "dense"}})
    body = response.json()
    assert response.status_code == 200 and body["code"] == RetCode.DATA_ERROR, body
    assert "data" not in body, body
    assert embedded == ["availability"]
    raw = env["reader"].query(env["collections"]["dataset"], filter="pk in " + json.dumps([row["id"] for row in env["rows"]["dataset"]]), output_fields=["pk", "doc_id"], consistency_level="Strong")
    assert {row["pk"] for row in raw} == {row["id"] for row in env["rows"]["dataset"]}


def test_http_semi_auto_field_selection_and_deletion_races(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api
    documents = [env["doc"], env["second_doc"]]
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "similarity_threshold": 0, "search_mode": {"type": "dense"}}
    conditions = {"conditions": [{"key": "category", "op": "is", "value": "match"}]}
    generate = AsyncMock(return_value=conditions)
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)

    def run(config: dict[str, Any], scope: list[str] | None = None) -> dict[str, Any]:
        response = request(env, "POST", "/search", json={**payload, "meta_data_filter": config, "doc_ids": scope})
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        return body["data"]

    def zero(body: dict[str, Any]) -> None:
        assert body["total"] == 0 and body["chunks"] == [] and body["doc_aggs"] == [], body

    for scope in (None, [env["doc"]]):
        for selection in ([], ["retired_key"], ["category", "retired_key"], [{"key": "retired_key", "op": "is"}]):
            zero(run({"method": "semi_auto", "semi_auto": selection}, scope))
    generate.assert_not_called()
    assert {row["doc_id"] for row in run({"method": "semi_auto", "semi_auto": ["category"]})["chunks"]} == set(documents)
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": []}))
    zero(run({"method": "semi_auto", "semi_auto": ["category"]}))
    for no_filter in ({}, {"method": "manual", "manual": []}, {"method": "auto"}):
        assert {row["doc_id"] for row in run(no_filter)["chunks"]} == set(documents)

    # An independent HTTP directory read succeeds; a subsequent committed delete
    # makes that selection stale before the retrieval POST, like the UI race.
    response = requests.get(
        env["base"] + "/api/v1/datasets/metadata/keys", params={"dataset_ids": ",".join([env["dataset"], env["second"]])}, headers={"Authorization": "Bearer " + env["jwt"]}, timeout=30
    )
    assert response.status_code == 200 and response.json()["code"] == 0 and "category" in response.json()["data"], response.text

    def replace_metadata(fields: dict[str, Any]) -> None:
        with Session(env["engine"]) as db:
            for doc in documents:
                metadata = db.get(DocumentMetadata, doc)
                assert metadata is not None
                metadata.meta_fields = fields.copy()
            db.commit()
        with Session(env["engine"]) as reader:
            assert all(reader.get(DocumentMetadata, doc).meta_fields == fields for doc in documents)

    replace_metadata({"stable": "match"})
    generate.reset_mock()
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", generate)
    zero(run({"method": "semi_auto", "semi_auto": ["category"]}))
    zero(run({"method": "semi_auto", "semi_auto": ["stable", "category"]}))
    generate.assert_not_called()

    # Also commit a deletion while the request awaits model inference. The
    # production service must reload metadata after that await before matching.
    replace_metadata({"stable": "match", "category": "match"})

    async def delete_during_generation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert "category" in args[1]
        await asyncio.to_thread(replace_metadata, {"stable": "match"})
        return conditions

    raced = AsyncMock(side_effect=delete_during_generation)
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", raced)
    zero(run({"method": "semi_auto", "semi_auto": ["category"]}))
    raced.assert_awaited_once()
    raw = env["reader"].query(env["collections"]["dataset"], filter="available_int == 1", output_fields=["doc_id"], consistency_level="Strong")
    assert {row["doc_id"] for row in raw} == {env["doc"]}


def test_http_invalid_manual_membership_is_not_unrestricted(search_api: dict[str, Any]) -> None:
    env = search_api
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "similarity_threshold": 0}
    operands: list[Any] = [[None], [{}], [["match"]], [float("inf")], [float("-inf")], [float("nan")], ["match", None], {"match": True}]

    def post(path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
        # 1e400 is a JSON number that overflows to infinity in the body parser;
        # NaN also tests the parser's permissive non-finite input handling.
        encoded = json.dumps(body).replace("Infinity", "1e400")
        response = requests.post(env["base"] + path, data=encoded, headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, timeout=30)
        result = response.json()
        assert response.status_code == 200 and result["code"] == 0, result
        return result["data"]

    for operator in ("in", "not in"):
        for logic in ("and", "or"):
            for value in operands:
                conditions = [{"key": "category", "op": "is", "value": "match"}, {"key": "category", "op": operator, "value": value}]
                body = post("/api/v1/datasets/" + env["dataset"] + "/search", {**payload, "meta_data_filter": {"method": "manual", "logic": logic, "manual": conditions}}, env["jwt"])
                assert body["total"] == 0 and body["chunks"] == [] and body["doc_aggs"] == [], (operator, logic, value, body)
                legacy = [{"name": c["key"], "comparison_operator": c["op"], "value": c["value"]} for c in conditions]
                body = post("/api/v1/retrieval", {**payload, "metadata_condition": {"logic": logic, "conditions": legacy}}, env["sdk_token"])
                assert body["total"] == 0 and body["chunks"] == [], (operator, logic, value, body)
    for operator, values in (("in", ["MATCH", True, 1.5]), ("not in", ["other", False, 1.5])):
        body = post(
            "/api/v1/datasets/" + env["dataset"] + "/search", {**payload, "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": operator, "value": values}]}}, env["jwt"]
        )
        assert {row["doc_id"] for row in body["chunks"]} == {env["doc"], env["second_doc"]}, body
    # Independent SQL/Milvus reads prove metadata/chunks stayed present; the
    # empty response came from operand validation rather than missing sources.
    with Session(env["engine"]) as db:
        assert all(db.get(DocumentMetadata, env[doc]).meta_fields == {"category": "match"} for doc in ("doc", "second_doc"))
    for dataset, doc in (("dataset", "doc"), ("second", "second_doc")):
        raw = env["reader"].query(env["collections"][dataset], filter="available_int == 1", output_fields=["doc_id"], consistency_level="Strong")
        assert {row["doc_id"] for row in raw} == {env[doc]}


def test_http_typed_membership_preserves_scalar_types_and_scope(search_api: dict[str, Any]) -> None:
    env = search_api
    first, second = env["doc"], env["second_doc"]
    fields = {
        first: {"mixed_number": 0, "mixed_boolean": False, "revision": 0, "positive": 5, "ratio": 1.5, "published": True, "product": "F2", "products": ["F2", "f11"], "mixed_items": ["F2", 0, False]},
        second: {
            "mixed_number": "0",
            "mixed_boolean": "False",
            "revision": 1,
            "positive": 6,
            "ratio": 2.5,
            "published": False,
            "product": "G1",
            "products": ["G1"],
            "mixed_items": ["G1", "0", "False"],
        },
        env["foreign_doc"]: {
            "mixed_number": 0,
            "mixed_boolean": False,
            "revision": 0,
            "positive": 5,
            "ratio": 1.5,
            "published": True,
            "product": "F2",
            "products": ["F2", "f11"],
            "mixed_items": ["F2", 0, False],
        },
    }
    with Session(env["engine"]) as db:
        for doc_id, values in fields.items():
            row = db.get(DocumentMetadata, doc_id)
            assert row is not None
            row.meta_fields = values
        db.commit()

    def metadata_snapshot() -> dict[str, Any]:
        with Session(env["engine"]) as db:
            return {row.id: row.meta_fields for row in db.scalars(sa.select(DocumentMetadata).where(DocumentMetadata.id.in_(fields)))}

    def chunk_snapshot() -> dict[str, Any]:
        return {
            key: sorted(
                env["reader"].query(collection, filter="", limit=100, output_fields=["pk", "doc_id", "available_int", "content_with_weight"], consistency_level="Strong"), key=lambda row: row["pk"]
            )
            for key, collection in env["collections"].items()
        }

    sql_before, milvus_before = metadata_snapshot(), chunk_snapshot()
    assert sql_before == fields
    assert type(sql_before[first]["mixed_number"]) is int and type(sql_before[second]["mixed_number"]) is str
    assert type(sql_before[first]["mixed_boolean"]) is bool and type(sql_before[second]["mixed_boolean"]) is str
    assert all({row["available_int"] for row in rows} == {0, 1} for rows in milvus_before.values())
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "similarity_threshold": 0, "search_mode": {"type": "dense"}}

    def check(conditions: list[dict[str, Any]], expected: set[str], scope: list[str] | None = None) -> None:
        for token in (env["jwt"], env["sdk_token"]):
            response = request(env, "POST", "/search", token=token, json={**payload, "doc_ids": scope, "meta_data_filter": {"method": "manual", "manual": conditions}})
            body = response.json()
            assert response.status_code == 200 and body["code"] == 0, body
            assert body["data"]["total"] == len(expected), (conditions, scope, body)
            assert {row["doc_id"] for row in body["data"]["chunks"]} == expected, (conditions, scope, body)
            assert {row["doc_id"] for row in body["data"]["doc_aggs"]} == expected, body
        selector = {"logic": "and", "conditions": [{"name": condition["key"], "comparison_operator": condition["op"], "value": condition["value"]} for condition in conditions]}
        sdk_payload = {**payload, "document_ids": scope or []}
        if conditions:
            sdk_payload["metadata_condition"] = selector
        response = requests.post(env["base"] + "/api/v1/retrieval", headers={"Authorization": "Bearer " + env["sdk_token"]}, json=sdk_payload, timeout=30)
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        assert body["data"]["total"] == len(expected), (conditions, scope, body)
        assert {row["document_id"] for row in body["data"]["chunks"]} == expected, (conditions, scope, body)

    both = {first, second}
    check([], both)
    cases: list[tuple[str, list[Any], set[str]]] = [
        ("mixed_number", [0], {first}),
        ("mixed_number", ["0"], {second}),
        ("mixed_boolean", [False], {first}),
        ("mixed_boolean", ["FALSE"], {second}),
        ("published", [True], {first}),
        ("published", [False], {second}),
        ("revision", [1], {second}),
        ("revision", [False], set()),
        ("revision", [True], set()),
        ("published", [1], set()),
        ("positive", [5], {first}),
        ("ratio", [1.5], {first}),
        ("product", ["f2"], {first}),
        ("product", ["f"], set()),
        ("product", ["f2", 0, False], {first}),
        ("mixed_number", [0, "0"], both),
        ("mixed_boolean", [False, "false"], both),
    ]
    for key, values, members in cases:
        for operator in ("in", "not in"):
            check([{"key": key, "op": operator, "value": values}], members if operator == "in" else both - members)
    # Production aggregation expands arrays: a document can have both a matching
    # and a nonmatching item, so its in/not-in result sets need not be complements.
    list_cases: list[tuple[str, list[Any], set[str], set[str]]] = [
        ("products", ["f2", "F11"], {first}, {second}),
        ("products", ["F11"], {first}, both),
        ("products", [], set(), both),
        ("mixed_items", [0], {first}, both),
        ("mixed_items", [False], {first}, both),
        ("mixed_items", ["0", "FALSE"], {second}, both),
        ("mixed_items", ["f2", 0, False], {first}, {second}),
    ]
    for key, values, members, nonmembers in list_cases:
        check([{"key": key, "op": "in", "value": values}], members)
        check([{"key": key, "op": "not in", "value": values}], nonmembers)
    check([{"key": "mixed_number", "op": "in", "value": ["0"]}], set(), [first])
    check([{"key": "mixed_boolean", "op": "not in", "value": [False]}], set(), [first])
    check([{"key": "products", "op": "in", "value": ["F2", "F11"]}], set(), [second])
    check([{"key": "products", "op": "not in", "value": ["F2", "F11"]}], set(), [first])
    check([{"key": "products", "op": "contains", "value": "F"}], {first})
    check([{"key": "products", "op": "not contains", "value": "F"}], {second})
    check([{"key": "mixed_number", "op": "in", "value": [0]}, {"key": "mixed_boolean", "op": "not in", "value": ["False"]}], {first})
    assert metadata_snapshot() == sql_before
    assert chunk_snapshot() == milvus_before


def test_http_and_sdk_joint_search_keep_only_selected_index_bindings(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Two owners and three datasets must never query the three cross-pairs."""
    from api.apps.restful_apis import bot as session

    env = search_api
    monkeypatch.setattr(session, "build_named_bundle_async", dataset_search_service._bundle)
    monkeypatch.setattr(session, "_label_question_with_conn", lambda *_: {})
    beta_token = "binding-" + uuid4().hex
    reader = env["reader"]
    membership_id = uuid4().hex
    selected = [env[key] for key in ("dataset", "second", "foreign")]
    expected_chunks = {env["rows"][key][1]["id"] for key in ("dataset", "second", "foreign")}
    extras: list[tuple[str, str, str]] = []
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=membership_id, user_id=env["owners"][0], tenant_id=env["owners"][1], role="normal", invited_by=env["owners"][1]))
        db.query(APIToken).filter(APIToken.token == env["sdk_token"]).one().beta = beta_token
        db.commit()
        names = [db.get(Knowledgebase, identifier).name for identifier in selected]
    payload = {"question": "availability", "dataset_ids": selected, "similarity_threshold": 0, "size": 30, "top_k": 200}
    denied = request(env, "POST", "/search", json=payload).json()
    assert denied["code"] == 109, denied
    with Session(env["engine"]) as db:
        # Exercise authorized cross-owner bindings using an explicitly shared KB.
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["foreign"]).values(permission="team"))
        db.commit()

    def check(mode: str, sdk: bool = False, embedded: bool = False) -> None:
        keys = ("dataset", "second") if sdk and not embedded else ("dataset", "second", "foreign")
        selected_ids = {env[key] for key in keys}
        expected_ids = {env["rows"][key][1]["id"] for key in keys}
        expected_docs = {env["rows"][key][1]["doc_id"] for key in keys}
        data = {**payload, "dataset_ids": list(selected_ids), "search_mode": {"type": mode}}
        if embedded:
            response = requests.post(env["base"] + "/api/v1/searchbots/retrieval_test", headers={"Authorization": "Bearer " + beta_token}, json={**data, "kb_id": list(selected_ids)}, timeout=30)
        elif sdk:
            response = requests.post(env["base"] + "/api/v1/retrieval", headers={"Authorization": "Bearer " + env["sdk_token"]}, json=data, timeout=30)
        else:
            response = request(env, "POST", "/search", json=data)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["retcode" if embedded else "code"] == 0, body
        chunks = body["data"]["chunks"]
        assert {chunk["dataset_id" if sdk and not embedded else "kb_id"] for chunk in chunks} == selected_ids, body
        assert {chunk.get("chunk_id", chunk.get("id")) for chunk in chunks} == expected_ids, body
        assert body["data"]["total"] == len(keys), body
        assert {row["doc_id"] for row in body["data"]["doc_aggs"]} == expected_docs, body

    try:
        # Absent cross-pairs used to produce a Milvus RPC business failure.
        for mode in ("dense", "sparse", "hybrid", "fusion"):
            check(mode)
            check(mode, sdk=True)
        check("dense", embedded=True)
        for owner in env["owners"]:
            for name in names:
                collection = search.index_name_one(owner, name)
                if collection in env["collections"].values():
                    continue
                kb_id, doc_id, chunk_id = uuid4().hex, uuid4().hex, uuid4().hex
                extras.append((collection, kb_id, doc_id))
                with Session(env["engine"]) as db:
                    db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name=name, embd_id="controlled", parser_id="naive", parser_config={}))
                    db.add(Document(id=doc_id, kb_id=kb_id, created_by=owner, name="unselected.txt", type="txt", parser_id="naive", parser_config={}))
                    db.commit()
                schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
                schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
                for field in ("kb_id", "doc_id", "docnm_kwd", "content_with_weight"):
                    schema.add_field(field, DataType.VARCHAR, max_length=65535)
                schema.add_field("available_int", DataType.INT64)
                schema.add_field("q_768_vec", DataType.FLOAT_VECTOR, dim=768)
                indexes = reader.prepare_index_params()
                indexes.add_index("q_768_vec", index_type="FLAT", metric_type="COSINE")
                reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)
                reader.insert(
                    collection,
                    [{"pk": chunk_id, "kb_id": kb_id, "doc_id": doc_id, "docnm_kwd": "unselected.txt", "content_with_weight": "availability", "available_int": 1, "q_768_vec": [0.2] * 768}],
                )
                reader.flush(collection, timeout=60)
                assert reader.query(collection, filter="pk != ''", output_fields=["pk", "kb_id", "doc_id"], consistency_level="Strong") == [{"pk": chunk_id, "kb_id": kb_id, "doc_id": doc_id}]
        # Existing cross-pairs used to return live, unselected datasets.
        check("dense")
        check("dense", sdk=True)
        check("dense", embedded=True)
        for key in ("dataset", "second", "foreign"):
            raw = reader.query(env["collections"][key], filter="available_int == 1", output_fields=["pk", "kb_id", "doc_id"], consistency_level="Strong")
            assert len(raw) == 1 and raw[0]["pk"] in expected_chunks and raw[0]["kb_id"] == env[key]
        with Session(env["engine"]) as db:
            assert {db.get(Document, env[key]).kb_id for key in ("doc", "second_doc", "foreign_doc")} == set(selected)
    finally:
        for collection, kb_id, doc_id in extras:
            if reader.has_collection(collection):
                reader.drop_collection(collection)
            assert not reader.has_collection(collection)
            with Session(env["engine"]) as db:
                db.execute(sa.delete(Document).where(Document.id == doc_id))
                db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == kb_id))
                db.commit()
        with Session(env["engine"]) as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["foreign"]).values(permission="me"))
            db.execute(sa.delete(UserTenant).where(UserTenant.id == membership_id))
            db.commit()
            assert db.get(UserTenant, membership_id) is None


def test_http_metadata_filter_storage_failure_is_business_error(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.doc_metadata_service import DocMetadataService

    env = search_api
    failure = AsyncMock(side_effect=ConnectionError("controlled metadata storage failure"))
    monkeypatch.setattr(DocMetadataService, "filter_doc_ids_by_meta_pushdown_async", failure)
    payload = {"question": "availability", "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "in", "value": ["MATCH"]}]}}
    for token in (env["jwt"], env["sdk_token"]):
        response = request(env, "POST", "/search", token=token, json=payload)
        assert response.status_code == 200
        assert response.json()["code"] != 0, response.json()
    assert failure.await_count == 2
    with Session(env["engine"]) as db:
        assert db.get(DocumentMetadata, env["doc"]).meta_fields == {"category": "match"}
    raw = env["reader"].query(env["collections"]["dataset"], filter="available_int == 1", output_fields=["doc_id"], consistency_level="Strong")
    assert {row["doc_id"] for row in raw} == {env["doc"]}


def test_collection_search_filters_readback_and_model_scope(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api
    # SQL rows and model configuration are real; only provider execution is controlled.
    model_owners = [env["owners"][0], env["owners"][1]]
    with Session(env["engine"]) as db:
        models = [TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="controlled-rerank", mdl_type="rerank", api_key="controlled") for owner in model_owners]
        db.add_all(models)
        db.commit()
        model_ids = [model.id for model in models]
    payload = {"question": "availability", "dataset_ids": [env["second"], env["dataset"]], "meta_data_filter": {"method": "manual", "manual": [{"key": "category", "op": "=", "value": "absent"}]}}
    embedding_bundle = dataset_search_service._bundle

    class Rerank:
        def similarity(self, question: str, texts: list[str]) -> tuple[np.ndarray, int]:
            return np.ones(len(texts)), 0

    async def controlled_bundle(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("model_id") in model_ids:
            return Rerank()
        return await embedding_bundle(*args, **kwargs)

    bundle = AsyncMock(side_effect=controlled_bundle)
    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    try:
        for model_id in [model_ids[1], max(model_ids) + 10000]:
            denied = request(env, "POST", "/search", collection=True, json={**payload, "tenant_rerank_id": model_id})
            assert denied.status_code == 200 and denied.json()["code"] == 109, denied.text
        bundle.assert_not_called()
        empty = request(env, "POST", "/search", collection=True, json=payload).json()
        assert empty["code"] == 0 and empty["data"]["total"] == 0 and empty["data"]["chunks"] == [], empty
        payload["meta_data_filter"]["manual"][0]["value"] = "match"
        allowed = request(env, "POST", "/search", collection=True, json={**payload, "tenant_rerank_id": model_ids[0]}).json()
        assert allowed["code"] == 0 and allowed["data"]["total"] == 2, allowed
        assert {row["kb_id"] for row in allowed["data"]["chunks"]} == set(payload["dataset_ids"])
        # Independent SQL and Milvus reads prove failed search did not mutate sources.
        with Session(env["engine"]) as db:
            assert set(db.scalars(sa.select(TenantLLM.id).where(TenantLLM.id.in_(model_ids)))) == set(model_ids)
            assert set(db.scalars(sa.select(Document.id).where(Document.kb_id.in_(payload["dataset_ids"])))) == {env["doc"], env["second_doc"]}
        for key in ["dataset", "second"]:
            raw = env["reader"].query(env["collections"][key], filter="knowledge_graph_kwd == ''", output_fields=["pk", "available_int"], consistency_level="Strong")
            assert {row["pk"] for row in raw} == {row["id"] for row in env["rows"][key]}
            assert {row["available_int"] for row in raw} == {0, 1}
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(model_ids)))
            db.commit()
        with Session(env["engine"]) as db:
            assert not db.scalars(sa.select(TenantLLM.id).where(TenantLLM.id.in_(model_ids))).all()


def test_saved_search_weight_persistence_reaches_real_single_multi_ranking(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api
    created = requests.post(
        env["base"] + "/api/v1/searches",
        headers={"Authorization": "Bearer " + env["jwt"]},
        json={"name": "Weight scratch", "search_config": {"vector_similarity_weight": 0, "similarity_threshold": 0, "kb_ids": [env["dataset"]], "cross_languages": [], "keyword": False}},
        timeout=30,
    )
    assert created.status_code == 200 and created.json()["code"] == 0, created.text
    identifier = created.json()["data"]["search_id"]
    lexical = {
        **env["rows"]["dataset"][1],
        "id": uuid4().hex,
        "content_with_weight": "lexicalonly lexicalonly lexicalonly",
        "content_ltks": "lexicalonly lexicalonly lexicalonly",
        "vector": [-0.2] * 768,
        "q_768_vec": [-0.2] * 768,
    }
    assert settings.docStoreConn.insert([lexical], env["collections"]["dataset"], env["dataset"]) == []
    env["reader"].flush(env["collections"]["dataset"], timeout=60)
    from api.db.services import dialog_service

    class SearchBundle:
        max_length = 4096

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.db = None

        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array([0.2] * 768), len(text)

        def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
            return np.array([[0.2] * 768 for _ in texts]), len(texts)

        async def async_chat_streamly_delta(self, *args: Any, **kwargs: Any) -> Any:
            yield "lexicalonly answer"

    from api.db.joint_services import tenant_model_service

    monkeypatch.setattr(dialog_service, "LLMBundle", SearchBundle)
    # async_ask builds through the real detached_bundle helper, whose
    # constructor is resolved from tenant_model_service at call time.
    monkeypatch.setattr(tenant_model_service, "LLMBundle", SearchBundle)
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda *_a, **_kw: {})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda *_a, **_kw: {})
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", lambda *_a, **_kw: {})
    evidence: dict[str, Any] = {}
    try:
        for weight in [0, 1]:
            updated = requests.put(
                env["base"] + "/api/v1/searches/" + identifier,
                headers={"Authorization": "Bearer " + env["jwt"]},
                json={"name": "Weight scratch", "search_config": {"vector_similarity_weight": weight}},
                timeout=30,
            )
            assert updated.status_code == 200 and updated.json()["code"] == 0, updated.text
            with Session(env["engine"]) as db:
                saved = db.get(Search, identifier)
                assert saved.search_config["vector_similarity_weight"] == weight
                assert saved.search_config["cross_languages"] == [] and saved.search_config["keyword"] is False
            for datasets in [[env["dataset"]], [env["dataset"], env["second"]]]:
                response = request(
                    env, "POST", "/search", json={"question": "lexicalonly", "dataset_ids": datasets, "search_id": identifier, "vector_similarity_weight": 1 - weight, "similarity_threshold": 0.9}
                )
                body = response.json()
                assert response.status_code == 200 and body["code"] == 0, response.text
                assert body["data"]["chunks"], body
                first = body["data"]["chunks"][0]["chunk_id"]
                assert (first == lexical["id"]) is (weight == 0), body
                evidence[f"{weight}-{len(datasets)}"] = body["data"]
            completed = requests.post(env["base"] + "/api/v1/searches/" + identifier + "/completions", headers={"Authorization": "Bearer " + env["jwt"]}, json={"question": "lexicalonly"}, timeout=30)
            frames = [json.loads(line[5:]) for line in completed.text.splitlines() if line.startswith("data:")]
            assert completed.status_code == 200 and frames and all(frame["code"] == 0 for frame in frames), completed.text
            final = next(frame["data"] for frame in frames if isinstance(frame.get("data"), dict) and frame["data"].get("final"))
            assert final["reference"]["chunks"], final
            assert (final["reference"]["chunks"][0]["id"] == lexical["id"]) is (weight == 0), final
            evidence[f"{weight}-async-ask"] = final
        empty_selection = requests.put(
            env["base"] + "/api/v1/searches/" + identifier, headers={"Authorization": "Bearer " + env["jwt"]}, json={"name": "Weight scratch", "search_config": {"doc_ids": []}}, timeout=30
        )
        assert empty_selection.json()["code"] == 0, empty_selection.text
        completed = requests.post(env["base"] + "/api/v1/searches/" + identifier + "/completions", headers={"Authorization": "Bearer " + env["jwt"]}, json={"question": "lexicalonly"}, timeout=30)
        frames = [json.loads(line[5:]) for line in completed.text.splitlines() if line.startswith("data:")]
        assert completed.status_code == 200 and frames and all(frame["code"] == 0 for frame in frames), completed.text
        final = next(frame["data"] for frame in frames if isinstance(frame.get("data"), dict) and frame["data"].get("final"))
        assert final["reference"]["chunks"] == []
        evidence["explicit-empty-async-ask"] = final
        rejected = requests.put(
            env["base"] + "/api/v1/searches/" + identifier,
            headers={"Authorization": "Bearer " + env["jwt"]},
            json={"name": "Weight scratch", "search_config": {"vector_similarity_weight": 2}},
            timeout=30,
        )
        assert rejected.json()["code"] != 0
        with Session(env["engine"]) as db:
            assert db.get(Search, identifier).search_config["vector_similarity_weight"] == 1
        env["record_path"].with_suffix(".saved-weight-readback.json").write_text(json.dumps(evidence))
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Search).where(Search.id == identifier))
            db.commit()


def test_mcp_unsafe_urls_have_no_sql_object_or_probe_side_effects(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from api.db.db_models import MCPServer

    env = runtime_upload_api
    route = sys.modules["api.apps.restful_apis.mcp"]
    probe = Mock(side_effect=AssertionError("unsafe URL reached external MCP probe"))
    monkeypatch.setattr(route, "get_mcp_tools", probe)
    monkeypatch.setattr(route, "MCPToolCallSession", probe)
    identifier = uuid4().hex
    owner = env["owners"][0]
    with Session(env["engine"]) as db:
        db.add(MCPServer(id=identifier, name="Owned MCP", tenant_id=owner, url="https://public.example/mcp", server_type="sse", variables={}, headers={}))
        db.commit()
    before_objects = {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)}
    headers = {"Authorization": "Bearer " + env["jwt"]}
    base = env["base"] + "/api/v1/mcp/servers"
    try:
        for url in ["http://127.0.0.1/private", "http://[::1]/private", "file:///etc/passwd"]:
            payload = {"name": "Blocked MCP", "url": url, "server_type": "sse"}
            for method, path, body in [("POST", base, payload), ("PUT", base + "/" + identifier, payload), ("POST", base + "/" + identifier + "/test", {"url": url, "server_type": "sse"})]:
                result = requests.request(method, path, headers=headers, json=body, timeout=30)
                assert result.status_code == 200 and result.json()["retcode"] == int(RetCode.DATA_ERROR), result.text
            result = requests.post(
                base + "/import", headers=headers, json={"mcpServers": {"Blocked MCP": {"type": "sse", "url": url}, "Invalid type": {"type": [], "url": "https://public.example"}}}, timeout=30
            )
            assert result.status_code == 200 and result.json()["retcode"] == 0, result.text
            assert all(not entry["success"] for entry in result.json()["data"]["results"])
        with Session(env["engine"]) as db:
            stored = db.get(MCPServer, identifier)
            assert stored.name == "Owned MCP" and stored.url == "https://public.example/mcp"
            assert list(db.scalars(sa.select(MCPServer.id).where(MCPServer.tenant_id == owner))) == [identifier]
        assert {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)} == before_objects
        probe.assert_not_called()
        env["record_path"].with_suffix(".mcp-denial-readback.json").write_text(
            json.dumps({"server": identifier, "unchanged": True, "created_servers": [], "created_objects": [], "external_probe_calls": probe.call_count})
        )
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(MCPServer).where(MCPServer.id == identifier))
            db.commit()


def test_http_document_scope_presence_survives_all_metadata_modes(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_api
    condition = {"key": "category", "op": "is", "value": "match"}
    monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": [condition], "logic": "and"}))
    evidence: dict[str, Any] = {}
    for mode in ["dense", "sparse", "hybrid", "fusion"]:
        for method in ["manual", "auto", "semi_auto"]:
            for documents in [None, []]:
                response = request(
                    env,
                    "POST",
                    "/search",
                    json={
                        "question": "availability",
                        "dataset_ids": [env["dataset"], env["second"]],
                        "search_mode": {"type": mode},
                        "similarity_threshold": 0,
                        "doc_ids": documents,
                        "meta_data_filter": {"method": method, "manual": [condition], "semi_auto": ["category"]},
                    },
                )
                body = response.json()
                assert response.status_code == 200 and body["code"] == 0, response.text
                expected = {env["doc"], env["second_doc"]} if documents is None else set()
                assert {chunk["doc_id"] for chunk in body["data"]["chunks"]} == expected, (mode, method, documents, body)
                assert body["data"]["total"] == len(expected), body
                evidence[f"{mode}-{method}-{documents}"] = body["data"]
    env["record_path"].with_suffix(".empty-scope-readback.json").write_text(json.dumps(evidence))


def test_http_embedding_provider_errors_cross_real_bundle_without_writes(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Real HTTP/auth/SQL/model resolution/provider HTTP; only provider output is controlled."""
    import errno
    import hashlib
    import io
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from minio import Minio

    from api.db.services.llm_service import LLMBundle
    from common.config_utils import CONFIGS
    from core.llm.embedding import SILICONFLOWEmbed
    from tests.support.runtime_upload import read_object

    env = search_api
    journal = env["journal"]
    secret = "fixture-provider-private-response"
    provider_status = [429]
    calls: list[dict[str, Any]] = []
    bundles: list[LLMBundle] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body, "status": provider_status[0]})
            encoded = json.dumps({"error": {"message": secret, "api_key": "fixture-key"}}).encode()
            self.send_response(provider_status[0])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    async def real_bundle(*args: Any, **kwargs: Any) -> LLMBundle:
        bundle = await build_search_bundle(*args, **kwargs)
        assert isinstance(bundle, LLMBundle) and type(bundle.mdl) is SILICONFLOWEmbed
        bundles.append(bundle)
        return bundle

    def sql_snapshot() -> dict[str, list[Any]]:
        with Session(env["engine"]) as db:
            return {
                model.__tablename__: list(db.execute(sa.select(model.__table__).order_by(model.id))) for model in (TenantLLM, Knowledgebase, Document, DocumentMetadata, Search, APIToken, UserTenant)
            }

    def index_snapshot() -> dict[str, list[dict[str, Any]]]:
        return {
            key: sorted(
                env["reader"].query(collection, filter="pk != ''", output_fields=["pk", "doc_id", "available_int", "content_with_weight"], consistency_level="Strong"), key=lambda row: row["pk"]
            )
            for key, collection in env["collections"].items()
        }

    with ExitStack() as cleanup:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        for attempt in range(20):
            port = 20000 + int(uuid4().hex[:8], 16) % 30000
            journal.event("reserve-embedding-provider-listener", host="127.0.0.1", port=port, attempt=attempt)
            server.server_address = ("127.0.0.1", port)
            try:
                server.server_bind()
                break
            except OSError as exc:
                if exc.errno != errno.EADDRINUSE:
                    raise
                journal.event("borrowed-embedding-provider-port", host="127.0.0.1", port=port)
        else:
            pytest.fail("No owned embedding provider listener available")
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, daemon=True)

        def close_provider() -> None:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(("127.0.0.1", port)) != 0
            journal.event("embedding-provider-listener-cleanup", host="127.0.0.1", port=port, absent=True)

        cleanup.callback(close_provider)
        thread.start()
        cfg = CONFIGS["minio"]
        reader = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
        object_key = "embedding-error-" + uuid4().hex
        sentinel = b"unchanged embedding error fixture"
        journal.event("reserve-embedding-error-object", bucket=env["bucket"], key=object_key)

        def remove_object() -> None:
            reader.remove_object(env["bucket"], object_key)
            assert object_key not in {item.object_name for item in reader.list_objects(env["bucket"], recursive=True)}
            journal.event("embedding-error-object-cleanup", bucket=env["bucket"], key=object_key, absent=True)

        cleanup.callback(remove_object)
        reader.put_object(env["bucket"], object_key, io.BytesIO(sentinel), len(sentinel))
        with Session(env["engine"]) as db:
            kb = db.get(Knowledgebase, env["dataset"])
            assert kb is not None
            previous_model_id = kb.tenant_embd_id
        model_name = "error-fixture-" + uuid4().hex
        model_id: int | None = None
        journal.event("reserve-embedding-error-model", database=env["engine"].url.database, tenant_id=env["owners"][0], model_name=model_name, dataset=env["dataset"])

        def remove_model() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["dataset"]).values(tenant_embd_id=previous_model_id))
                db.execute(sa.delete(TenantLLM).where(TenantLLM.tenant_id == env["owners"][0], TenantLLM.llm_name == model_name))
                db.commit()
            with Session(env["engine"]) as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == env["owners"][0], TenantLLM.llm_name == model_name)) == 0
                assert db.get(Knowledgebase, env["dataset"]).tenant_embd_id == previous_model_id
            journal.event("embedding-error-model-cleanup", model_id=model_id, absent=True, dataset_restored=True)

        cleanup.callback(remove_model)
        with Session(env["engine"]) as db:
            model = TenantLLM(
                tenant_id=env["owners"][0],
                llm_factory="SILICONFLOW",
                llm_name=model_name,
                mdl_type="embedding",
                api_key="fixture-key",
                api_base=f"http://127.0.0.1:{port}/v1/embeddings",
                max_tokens=8192,
                used_tokens=17,
            )
            db.add(model)
            db.flush()
            model_id = model.id
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["dataset"]).values(tenant_embd_id=model_id))
            db.commit()
        monkeypatch.setattr(dataset_search_service, "_bundle", real_bundle)
        before_sql = sql_snapshot()
        before_indexes = index_snapshot()
        before_objects = [(item.object_name, item.etag, item.size) for item in reader.list_objects(env["bucket"], recursive=True)]
        assert read_object(reader, env["bucket"], object_key) == sentinel
        for status, http_status in ((429, 503), (401, 502)):
            provider_status[0] = status
            for collection in (False, True):
                prior_calls = len(calls)
                response = request(env, "POST", "/search", collection=collection, json={"question": "availability", "dataset_ids": [env["dataset"]], "search_mode": {"type": "dense"}})
                assert response.status_code == http_status, response.text
                assert response.json() == {"code": RetCode.EXCEPTION_ERROR, "message": f"Embedding provider request failed (HTTP {status})", "data": {"retryable": status == 429}}
                assert secret not in response.text and "fixture-key" not in response.text
                assert len(calls) == prior_calls + 1
                assert calls[-1] == {"path": "/v1/embeddings", "body": {"model": model_name, "input": "availability", "encoding_format": "float"}, "status": status}
                assert len(bundles) == len(calls) and bundles[-1].model_config["id"] == model_id
                assert sql_snapshot() == before_sql
                assert index_snapshot() == before_indexes
                assert [(item.object_name, item.etag, item.size) for item in reader.list_objects(env["bucket"], recursive=True)] == before_objects
                assert read_object(reader, env["bucket"], object_key) == sentinel
                journal.event(
                    "embedding-error-route-readback",
                    collection_route=collection,
                    provider_status=status,
                    http_status=http_status,
                    model_id=model_id,
                    provider_calls=len(calls),
                    sql_unchanged=True,
                    indexes_unchanged=True,
                    objects_unchanged=True,
                    object_sha256=hashlib.sha256(sentinel).hexdigest(),
                )
