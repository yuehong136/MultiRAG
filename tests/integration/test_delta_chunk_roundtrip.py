"""Manual chunk bootstrap and nullable native fields through authenticated HTTP."""

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from pymilvus import MilvusClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

import core.llm as provider_models
from api.db.db_models import Document, Knowledgebase, TenantLLM
from common import settings
from common.config_utils import CONFIGS
from common.doc_store.document_history import document_history
from core.nlp.search import index_name_one
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api


def test_manual_insert_nullable_readback_and_search(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = management_api
    tenant, dataset, document = env["users"][0], env["datasets"][0], env["documents"][0]
    name = "delta_chunk_" + uuid4().hex
    collection = index_name_one(tenant, name)
    factory = "DeltaChunkEmbeddingFixture"
    record: dict[str, Any] = {"collection": collection, "database": env["engine"].url.database}
    evidence = env["record_path"].with_suffix(".chunk-roundtrip.json")

    class Embedding:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
            return np.array([[0.25, 0.75] for _ in texts]), 0

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.chunk"], "db_connection", scratch_db)
    monkeypatch.setattr(provider_models, "EmbeddingModel", {factory: Embedding})
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": factory}])
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    route = f"/datasets/{dataset}/documents/{document}/chunks"
    try:
        with Session(env["engine"]) as db:
            kb = db.get(Knowledgebase, dataset)
            assert kb is not None
            kb.name, kb.embd_id = name, "delta-embedding@" + factory
            db.add(TenantLLM(tenant_id=tenant, llm_factory=factory, llm_name="delta-embedding", mdl_type="embedding", api_key="fixture", api_base="https://unused.invalid"))
            db.commit()
        assert not reader.has_collection(collection)
        response = request_api(env, "POST", route, credential=env["jwt"][0], payload={"content": "quasar telescope observation"})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        chunk = response.json()["data"]["chunk"]
        assert chunk["available"] is True
        chunk_id = chunk["id"]
        with Session(env["engine"]) as db:
            assert db.scalar(select(Document.chunk_num).where(Document.id == document)) == 1
        native = reader.query(collection, filter="pk == " + json.dumps(chunk_id), output_fields=["*", "q_2_vec"], consistency_level="Strong")
        assert len(native) == 1 and native[0]["img_id"] is None
        for field, data in [("q_2_vec", [[0.25, 0.75]]), ("content_ltks_sparse", ["quasar telescope"])]:
            hits = reader.search(collection, data=data, anns_field=field, limit=1, consistency_level="Strong")
            assert [hit.id for hit in hits[0]] == [chunk_id]
        read = request_api(env, "GET", route, credential=env["keys"][0], params={"id": chunk_id})
        assert read.json()["code"] == 0 and read.json()["data"]["chunks"] == [chunk], read.text
        nullable = dict.fromkeys(["available_int", "img_id", "position_int", "important_kwd", "question_kwd", "tag_kwd", "tag_feas"])
        rows = [
            {**nullable, "id": label, "doc_id": document, "kb_id": dataset, "content_with_weight": label, "q_2_vec": [0.25, 0.75], "available_int": available}
            for label, available in [("nullable", None), ("disabled", 0)]
        ]
        assert settings.docStoreConn.insert(rows, collection, dataset) == []
        persisted = reader.query(collection, filter='pk in ["nullable", "disabled"]', output_fields=["*"], consistency_level="Strong")
        assert len(persisted) == 2 and all(row["img_id"] is None for row in persisted)
        for label, expected in [("nullable", True), ("disabled", False)]:
            # Ordinary get() uses the collection's bounded consistency. Wait
            # only for directly seeded rows to become visible, then check the
            # full nullable payload. Other errors fail immediately.
            deadline = time.monotonic() + 10
            while True:
                payload = request_api(env, "GET", route, credential=env["jwt"][0], params={"id": label}).json()
                if payload != {"code": 102, "message": f"Chunk not found: {dataset}/{label}"} or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            assert payload["code"] == 0, payload
            value = payload["data"]["chunks"][0]
            assert value["available"] is expected
            assert value["important_keywords"] == value["questions"] == value["positions"] == []
            assert value["image_id"] == ""
        reader.release_collection(collection)
        history = document_history(settings.docStoreConn, collection, dataset, document)
        assert {row["id"] for row in history} == {chunk_id, "nullable", "disabled"}
        record.update(
            manual_chunk_id=chunk_id,
            sql_chunk_count=1,
            dense_and_sparse_search=True,
            native_null_image=True,
            nullable_default=True,
            explicit_zero_disabled=True,
            history_ids=sorted(row["id"] for row in history),
        )
    finally:
        if reader.has_collection(collection):
            reader.drop_collection(collection)
        record["collection_absent"] = not reader.has_collection(collection)
        reader.close()
        with Session(env["engine"]) as db:
            db.execute(delete(TenantLLM).where(TenantLLM.tenant_id == tenant))
            db.commit()
        evidence.write_text(json.dumps(record, indent=2))
        assert record["collection_absent"]
