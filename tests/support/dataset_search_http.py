"""Shared authenticated dataset-search scratch fixture and HTTP helper."""

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
from api.db.db_models import APIToken, Document, DocumentMetadata, Knowledgebase, Search, get_db
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


def request(env: dict[str, Any], method: str, suffix: str, *, token: str | None = None, collection: bool = False, **kwargs: Any) -> requests.Response:
    path = "/api/v1/datasets/search" if collection else "/api/v1/datasets/" + env["dataset"] + suffix
    return requests.request(method, env["base"] + path, headers={"Authorization": "Bearer " + (token or env["jwt"])}, timeout=30, **kwargs)
