"""Shared authenticated dataset-search scratch fixture and HTTP helper."""

import asyncio
import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
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
    public_ids = {key: value for key, value in ids.items() if key != "sdk_token"}
    token_identity = {"owner": env["owners"][0], "name": "search-scope", "sha256": hashlib.sha256(ids["sdk_token"].encode()).hexdigest()}
    sql_url = env["engine"].url
    journal = env["journal"]
    journal.event(
        "reserve-search-resources",
        database=sql_url.database,
        host=sql_url.host,
        port=sql_url.port,
        role=sql_url.username,
        owners=env["owners"],
        ids=public_ids,
        api_token=token_identity,
        collections=collections,
        fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        identity_sha256=hashlib.sha256(json.dumps({"ids": public_ids, "api_token": token_identity, "collections": collections}, sort_keys=True).encode()).hexdigest(),
    )
    env["record"].update(search_ids=public_ids, search_api_token=token_identity, search_collections=collections)

    def save_record() -> None:
        env["record_path"].write_text(json.dumps(env["record"]))

    sql_rows = (
        (APIToken, APIToken.token == ids["sdk_token"]),
        (DocumentMetadata, DocumentMetadata.id.in_([ids["doc"], ids["second_doc"], ids["foreign_doc"]])),
        (Document, Document.kb_id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
        (Knowledgebase, Knowledgebase.id.in_([ids["dataset"], ids["second"], ids["foreign"]])),
        (Search, Search.id == ids["saved_search"]),
    )

    def remove_rows() -> None:
        with Session(env["engine"]) as db:
            for model, clause in sql_rows:
                db.execute(sa.delete(model).where(clause))
            db.commit()
        with env["engine"].connect() as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(clause)) for model, clause in sql_rows}
        env["record"]["search_remaining"] = remaining
        journal.event("search-sql-cleanup-readback", database=sql_url.database, remaining=remaining)
        assert not any(remaining.values()), remaining

    with ExitStack() as cleanup:
        cleanup.callback(save_record)
        save_record()
        with env["engine"].connect() as reader:
            identity = reader.execute(sa.text("SELECT current_database(), current_user, inet_server_addr()::text, inet_server_port()")).one()
            existing = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(clause)) for model, clause in sql_rows}
        journal.event("search-database-endpoint", database=identity[0], role=identity[1], address=identity[2], port=identity[3])
        journal.event("search-sql-before-create", database=identity[0], existing=existing)
        assert not any(existing.values()), "Scratch SQL identity is already borrowed: " + repr(existing)
        cleanup.callback(remove_rows)
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

        def dispose_engine() -> None:
            asyncio.run(async_engine.dispose())

        cleanup.callback(dispose_engine)
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        monkeypatch.setattr(db_models, "async_session_factory", sessions)

        class Embedding:
            def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
                return np.array([0.2] * 768), 0

        async def bundle(*args: Any, **kwargs: Any) -> Any:
            return Embedding()

        monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
        from api.apps.restful_apis import chunk as sdk

        monkeypatch.setattr(sdk, "LLMBundle", lambda *_: Embedding())
        monkeypatch.setattr(sdk, "get_model_config_by_type_and_name", lambda *_: {})
        monkeypatch.setattr(sdk, "label_question", lambda *_: {})
        store = settings.docStoreConn
        assert store.db_type() == "milvus", "This regression targets the supported local Milvus environment"
        cfg = CONFIGS["milvus"]
        endpoint = urlsplit(cfg["hosts"] if "://" in cfg["hosts"] else "//" + cfg["hosts"])
        journal.event("reserve-search-collections", scheme=endpoint.scheme, host=endpoint.hostname, port=endpoint.port, database=cfg.get("db_name") or "default", collections=collections)
        reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
        cleanup.callback(reader.close)
        collection_cleanup = dict.fromkeys(collections, False)
        env["record"]["search_collection_cleanup"] = collection_cleanup

        def remove_collection(key: str, collection: str) -> None:
            if reader.has_collection(collection):
                store.delete_idx(collection, ids[key])
            absent = not reader.has_collection(collection)
            collection_cleanup[key] = absent
            env["record"]["search_collections_removed"] = all(collection_cleanup.values())
            journal.event("search-collection-cleanup-readback", collection=collection, absent=absent)
            assert absent, collection

        for key, collection in collections.items():
            exists = reader.has_collection(collection)
            journal.event("search-collection-before-create", collection=collection, exists=exists)
            assert not exists, "Scratch collection identity is already borrowed: " + collection
            cleanup.callback(remove_collection, key, collection)
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
            journal.event(
                "reserve-search-rows", collection=collections[key], ids=[row["id"] for row in rows[key]], input_sha256=hashlib.sha256(json.dumps(rows[key], sort_keys=True).encode()).hexdigest()
            )
            assert store.insert(rows[key], collections[key], ids[key]) == []
        graph = {"nodes": [{"id": "node", "pagerank": 1}], "edges": []}
        mind_map = {"id": "node", "children": [{"id": "node"}]}
        artifacts = []
        for kind, content, removed in (("graph", graph, "N"), ("subgraph", graph, "N"), ("subgraph", {"nodes": [{"id": "removed"}]}, "Y"), ("mind_map", mind_map, "N")):
            artifacts.append({**rows["dataset"][0], "id": uuid4().hex, "source_id": [ids["doc"]], "knowledge_graph_kwd": kind, "removed_kwd": removed, "content_with_weight": json.dumps(content)})
        journal.event(
            "reserve-search-artifacts", collection=collections["dataset"], ids=[row["id"] for row in artifacts], input_sha256=hashlib.sha256(json.dumps(artifacts, sort_keys=True).encode()).hexdigest()
        )
        assert store.insert(artifacts, collections["dataset"], ids["dataset"]) == []
        for collection in collections.values():
            reader.flush(collection, timeout=60)
        yield {**env, **ids, "collections": collections, "rows": rows, "reader": reader}


def request(env: dict[str, Any], method: str, suffix: str, *, token: str | None = None, collection: bool = False, **kwargs: Any) -> requests.Response:
    path = "/api/v1/datasets/search" if collection else "/api/v1/datasets/" + env["dataset"] + suffix
    return requests.request(method, env["base"] + path, headers={"Authorization": "Bearer " + (token or env["jwt"])}, timeout=30, **kwargs)
