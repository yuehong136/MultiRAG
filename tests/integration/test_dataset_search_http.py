"""Real JWT/API-key, SQL metadata and Milvus readback for dataset search/graph.

Only embedding output is controlled; ranking, predicates, HTTP and SQL are real.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from pymilvus import DataType, Function, FunctionType, MilvusClient
from sqlalchemy.orm import Session

from api.apps.services import dataset_search_service
from api.db.db_models import Document, DocumentMetadata, Knowledgebase, Search, UserTenant
from common import settings
from common.config_utils import CONFIGS
from core.nlp import search
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def search_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = runtime_upload_api
    ids = {key: uuid4().hex for key in ("dataset", "second", "foreign", "doc", "second_doc", "foreign_doc", "saved_search")}
    collections = {key: search.index_name_one(env["owners"][0 if key != "foreign" else 1], "search_" + ids[key]) for key in ("dataset", "second", "foreign")}
    with Session(env["engine"]) as db:
        for key, doc in (("dataset", "doc"), ("second", "second_doc"), ("foreign", "foreign_doc")):
            owner = env["owners"][0 if key != "foreign" else 1]
            db.add(Knowledgebase(id=ids[key], tenant_id=owner, created_by=owner, name="search_" + ids[key], embd_id="controlled", parser_id="naive", parser_config={}))
            db.add(Document(id=ids[doc], kb_id=ids[key], created_by=owner, name=doc + ".txt", type="txt", parser_id="naive", parser_config={}))
            db.add(DocumentMetadata(id=ids[doc], tenant_id=owner, kb_id=ids[key], meta_fields={"category": "match"}))
        db.add(Search(id=ids["saved_search"], tenant_id=env["owners"][1], created_by=env["owners"][1], name="private", search_config={"meta_data_filter": {"method": "manual"}}))
        db.commit()

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(search, "db_connection", scratch_db)

    class Embedding:
        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            return np.array([0.2] * 768), 0

    async def bundle(*args: Any, **kwargs: Any) -> Any:
        return Embedding()

    monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
    store = settings.docStoreConn
    assert store.db_type() == "milvus", "This regression targets the supported local Milvus environment"
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    rows: dict[str, list[dict[str, Any]]] = {}
    try:
        for key, doc in (("dataset", "doc"), ("second", "second_doc"), ("foreign", "foreign_doc")):
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
            text_fields = ["content_with_weight", "title_tks", "title_sm_tks", "important_kwd", "important_tks", "question_tks", "content_ltks", "content_sm_ltks"]
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
            reader.create_collection(collections[key], schema=schema, index_params=indexes, consistency_level="Strong")
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
            reader.flush(collection)
        yield {**env, **ids, "collections": collections, "rows": rows, "reader": reader}
    finally:
        for key, collection in collections.items():
            store.delete_idx(collection, ids[key])
            assert not reader.has_collection(collection)
        reader.close()
        with Session(env["engine"]) as db:
            for model, clause in (
                (DocumentMetadata, DocumentMetadata.id.in_([ids["doc"], ids["second_doc"], ids["foreign_doc"]])),
                (Document, Document.kb_id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
                (Knowledgebase, Knowledgebase.id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
                (Search, Search.id == ids["saved_search"]),
            ):
                db.execute(sa.delete(model).where(clause))
            db.commit()


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
