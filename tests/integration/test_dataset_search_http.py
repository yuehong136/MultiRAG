"""Real JWT/API-key, SQL metadata and Milvus readback for dataset search/graph.

Only embedding output is controlled; ranking, predicates, HTTP and SQL are real.
"""

import asyncio
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any
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
    from unittest.mock import AsyncMock

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

    def check(documents: list[str] | None, conditions: list[dict[str, Any]], logic: str, expected: set[str]) -> None:
        for method in ("manual", "auto", "semi_auto"):
            monkeypatch.setattr("core.prompts.generator.gen_meta_filter", AsyncMock(return_value={"conditions": conditions, "logic": logic}))
            config = {"method": method, "manual": conditions, "logic": logic, "semi_auto": ["category", "version"]}
            body = request(env, "POST", "/search", json={**payload, "doc_ids": documents, "meta_data_filter": config}).json()
            assert body["code"] == 0, body
            assert {row["doc_id"] for row in body["data"]["chunks"]} == expected, (method, body)
            assert {row["doc_id"] for row in body["data"]["doc_aggs"]} == expected, body
            assert body["data"]["total"] == len(expected), body

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
    check(both, [missing, current], "or", {env["second_doc"]})
    check(None, [current], "and", {env["second_doc"]})
    check([], [current], "and", {env["second_doc"]})

    for documents, expected in ((None, set(both)), ([], set(both)), ([env["doc"]], {env["doc"]})):
        body = request(env, "POST", "/search", json={**payload, "doc_ids": documents}).json()
        assert body["code"] == 0 and {row["doc_id"] for row in body["data"]["chunks"]} == expected, body
    raw = env["reader"].query(env["collections"]["dataset"], filter="knowledge_graph_kwd == ''", output_fields=["doc_id", "available_int"], consistency_level="Strong")
    assert {row["doc_id"] for row in raw} == {env["doc"]}
    assert {row["available_int"] for row in raw} == {0, 1}


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
