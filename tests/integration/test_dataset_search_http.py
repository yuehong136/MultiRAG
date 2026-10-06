"""Real JWT/API-key, SQL metadata and Milvus readback for dataset search/graph.

Model/filter generation and KG output are controlled; document ranking,
predicates, HTTP/auth and SQL metadata are real.
"""

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from pymilvus import DataType, Function, FunctionType, MilvusClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.apps.services import dataset_search_service
from api.db.db_models import APIToken, Document, DocumentMetadata, Knowledgebase, Search, UserTenant, get_db
from common import settings
from common.config_utils import CONFIGS
from common.constants import RetCode
from core.nlp import search
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def search_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = runtime_upload_api
    ids = {key: uuid4().hex for key in ("dataset", "second", "foreign", "doc", "second_doc", "foreign_doc", "saved_search")}
    ids["sdk_token"] = "search-" + uuid4().hex
    collections = {key: search.index_name_one(env["owners"][0 if key != "foreign" else 1], "search_" + ids[key]) for key in ("dataset", "second", "foreign")}
    env["record"].update(search_ids=ids, search_collections=collections)
    env["record_path"].write_text(json.dumps(env["record"]))
    with Session(env["engine"]) as db:
        for key, doc in (("dataset", "doc"), ("second", "second_doc"), ("foreign", "foreign_doc")):
            owner = env["owners"][0 if key != "foreign" else 1]
            db.add(Knowledgebase(id=ids[key], tenant_id=owner, created_by=owner, name="search_" + ids[key], embd_id="controlled", parser_id="naive", parser_config={}))
            db.add(Document(id=ids[doc], kb_id=ids[key], created_by=owner, name=doc + ".txt", type="txt", parser_id="naive", parser_config={}))
            db.add(DocumentMetadata(id=ids[doc], tenant_id=owner, kb_id=ids[key], meta_fields={"category": "match"}))
        db.add(Search(id=ids["saved_search"], tenant_id=env["owners"][1], created_by=env["owners"][1], name="private", search_config={"meta_data_filter": {"method": "manual"}}))
        db.add(APIToken(tenant_id=env["owners"][0], token=ids["sdk_token"], name="search-scope"))
        db.commit()

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(search, "db_connection", scratch_db)
    from api.apps import app

    def sdk_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, sdk_db)
    from api.db import db_models

    # Legacy APIToken.query opens SessionLocal itself; keep that real SQL lookup
    # inside the same scratch database as the request dependency.
    monkeypatch.setattr(db_models, "SessionLocal", sa.orm.sessionmaker(env["engine"], expire_on_commit=False))
    async_engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr(db_models, "async_session_factory", sessions)

    class Embedding:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array([0.2] * 768), 0

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return Embedding()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    from api.apps.sdk import doc as sdk

    monkeypatch.setattr(sdk, "LLMBundle", lambda *_: Embedding())
    monkeypatch.setattr(sdk, "get_model_config_by_type_and_name", lambda *_: {})
    monkeypatch.setattr(sdk, "label_question", lambda *_: {})
    store = settings.docStoreConn
    assert store.db_type() == "milvus", "This regression targets the supported local Milvus environment"
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    rows: dict[str, list[dict[str, Any]]] = {}
    text_fields = ["content_with_weight", "title_tks", "title_sm_tks", "important_kwd", "important_tks", "question_tks", "content_ltks", "content_sm_ltks"]

    def create_collection(collection: str) -> None:
        # Seed the supported retrieval schema directly. Generic create_idx
        # currently creates ingestion-only fields (no q_768_vec/BM25 fields).
        schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
        for field in ("kb_id", "doc_id", "docnm_kwd", "knowledge_graph_kwd", "removed_kwd", "mom_id"):
            schema.add_field(field, DataType.VARCHAR, max_length=512)
        schema.add_field("available_int", DataType.INT64)
        schema.add_field("source_id", DataType.VARCHAR, max_length=65535)
        for field in ("vector", "q_768_vec"):
            schema.add_field(field, DataType.FLOAT_VECTOR, dim=768)
        for field in text_fields:
            schema.add_field(field, DataType.VARCHAR, max_length=65535, enable_analyzer=True, analyzer_params={"tokenizer": "standard"})
            sparse = "sparse_vector" if field == "content_with_weight" else field + "_sparse"
            schema.add_field(sparse, DataType.SPARSE_FLOAT_VECTOR)
            schema.add_function(Function(name="bm25_" + field, function_type=FunctionType.BM25, input_field_names=[field], output_field_names=[sparse]))
        indexes = reader.prepare_index_params()
        for field in ("vector", "q_768_vec"):
            indexes.add_index(field, index_type="AUTOINDEX", metric_type="COSINE")
        for field in text_fields:
            indexes.add_index("sparse_vector" if field == "content_with_weight" else field + "_sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
        reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)

    try:
        # Each case retains three unique collections and all real indexes.
        # Overlap their independent creation/loading RPCs, then seed and verify.
        with ThreadPoolExecutor(max_workers=len(collections)) as pool:
            list(pool.map(create_collection, collections.values()))
        for key, doc in (("dataset", "doc"), ("second", "second_doc"), ("foreign", "foreign_doc")):
            rows[key] = []
            for available in (0, 1):
                row = {
                    **dict.fromkeys(text_fields, ""),
                    "knowledge_graph_kwd": "",
                    "removed_kwd": "N",
                    "mom_id": "",
                    "source_id": [],
                    "id": uuid4().hex,
                    "kb_id": ids[key],
                    "doc_id": ids[doc],
                    "docnm_kwd": doc + ".txt",
                    "content_with_weight": "availability dataset regression",
                    "content_ltks": "availability dataset regression",
                    "available_int": available,
                    "vector": [0.2] * 768,
                    "q_768_vec": [0.2] * 768,
                }
                rows[key].append(row)
            assert store.insert(rows[key], collections[key], ids[key]) == []
        graph = {"nodes": [{"id": "node", "pagerank": 1}], "edges": []}
        mind_map = {"id": "node", "children": [{"id": "node"}]}
        artifacts = []
        for kind, content, removed in (("graph", graph, "N"), ("subgraph", graph, "N"), ("subgraph", {"nodes": [{"id": "removed"}]}, "Y"), ("mind_map", mind_map, "N")):
            artifacts.append({**rows["dataset"][0], "id": uuid4().hex, "source_id": [ids["doc"]], "knowledge_graph_kwd": kind, "removed_kwd": removed, "content_with_weight": json.dumps(content)})
        assert store.insert(artifacts, collections["dataset"], ids["dataset"]) == []
        for collection in collections.values():
            reader.flush(collection, timeout=60)
        yield {**env, **ids, "collections": collections, "rows": rows, "reader": reader}
    finally:
        asyncio.run(async_engine.dispose())
        for key, collection in collections.items():
            store.delete_idx(collection, ids[key])
            assert not reader.has_collection(collection)
        reader.close()
        with Session(env["engine"]) as db:
            for model, clause in (
                (APIToken, APIToken.token == ids["sdk_token"]),
                (DocumentMetadata, DocumentMetadata.id.in_([ids["doc"], ids["second_doc"], ids["foreign_doc"]])),
                (Document, Document.kb_id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
                (Knowledgebase, Knowledgebase.id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
                (Search, Search.id == ids["saved_search"]),
            ):
                db.execute(sa.delete(model).where(clause))
            db.commit()
        env["record"]["search_collections_removed"] = True
        env["record_path"].write_text(json.dumps(env["record"]))


def request(env: dict[str, Any], method: str, suffix: str, *, token: str | None = None, **kwargs: Any) -> requests.Response:
    return requests.request(method, env["base"] + "/api/v1/datasets/" + env["dataset"] + suffix, headers={"Authorization": "Bearer " + (token or env["jwt"])}, timeout=30, **kwargs)


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
            mode_expected = semi_expected if method == "semi_auto" and semi_expected is not None else expected
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

    for documents, expected in ((None, set(both)), ([], set(both)), ([env["doc"]], {env["doc"]})):
        body = request(env, "POST", "/search", json={**payload, "doc_ids": documents}).json()
        assert body["code"] == 0 and {row["doc_id"] for row in body["data"]["chunks"]} == expected, body
    raw = env["reader"].query(env["collections"]["dataset"], filter="knowledge_graph_kwd == ''", output_fields=["doc_id", "available_int"], consistency_level="Strong")
    assert {row["doc_id"] for row in raw} == {env["doc"]}
    assert {row["available_int"] for row in raw} == {0, 1}


def test_http_kg_constraint_contract(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.sdk import doc as sdk

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
        assert response.status_code == 200 and response.json()["code"] == 400, response.text
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

    payload = {"question": "availability", "use_kg": True, "doc_ids": [], "meta_data_filter": {}}
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


def test_http_search_multi_dataset_and_auth_matrix(search_api: dict[str, Any]) -> None:
    env = search_api
    payload = {"question": "availability", "dataset_ids": [env["dataset"], env["second"]], "highlight": True}
    body = request(env, "POST", "/search", json=payload).json()
    assert body["code"] == 0, body
    assert {chunk["kb_id"] for chunk in body["data"]["chunks"]} == {env["dataset"], env["second"]}
    assert body["data"]["total"] == 2
    assert request(env, "POST", "/search", token=env["api_key"], json=payload).json()["code"] == 109
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][1], tenant_id=env["owners"][0], invited_by=env["owners"][0], role="normal"))
        db.commit()
    try:
        sdk = request(env, "POST", "/search", token=env["api_key"], json=payload).json()
        assert sdk["code"] == 0 and sdk["data"]["total"] == 2, sdk
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id == env["owners"][1], UserTenant.tenant_id == env["owners"][0]))
            db.commit()
    payload["dataset_ids"].append(env["foreign"])
    assert request(env, "POST", "/search", json=payload).json()["code"] == 109
    payload.pop("dataset_ids")
    payload["search_id"] = env["saved_search"]
    assert request(env, "POST", "/search", json=payload).json()["code"] == 109
    assert request(env, "POST", "/search", token="invalid", json=payload).status_code == 401


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
    runner = root / "node_modules/.bin/tsx"
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
        first: {"mixed_number": 0, "mixed_boolean": False, "revision": 0, "positive": 5, "ratio": 1.5, "published": True, "product": "F2"},
        second: {"mixed_number": "0", "mixed_boolean": "False", "revision": 1, "positive": 6, "ratio": 2.5, "published": False, "product": "G1"},
        env["foreign_doc"]: {"mixed_number": 0, "mixed_boolean": False, "revision": 0, "positive": 5, "ratio": 1.5, "published": True, "product": "F2"},
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
    check([{"key": "mixed_number", "op": "in", "value": ["0"]}], set(), [first])
    check([{"key": "mixed_boolean", "op": "not in", "value": [False]}], set(), [first])
    check([{"key": "mixed_number", "op": "in", "value": [0]}, {"key": "mixed_boolean", "op": "not in", "value": ["False"]}], {first})
    assert metadata_snapshot() == sql_before
    assert chunk_snapshot() == milvus_before


def test_http_and_sdk_joint_search_keep_only_selected_index_bindings(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Two owners and three datasets must never query the three cross-pairs."""
    from api.apps.sdk import session

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
            db.execute(sa.delete(UserTenant).where(UserTenant.id == membership_id))
            db.commit()
            assert db.get(UserTenant, membership_id) is None
