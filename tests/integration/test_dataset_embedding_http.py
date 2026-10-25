"""Real HTTP/auth, PostgreSQL and Milvus with a controlled embedding provider."""

import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import sqlalchemy as sa
from pymilvus import DataType, MilvusClient
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service, dataset_embedding_service
from api.db.db_models import Knowledgebase, LLMFactories, TenantLLM, UserTenant
from common import settings
from common.config_utils import CONFIGS
from core.nlp import search
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api, sql_state
from tests.support.web_consumer import tsx_runner


@pytest.fixture
def embedding_api(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = management_api
    dataset, owner = env["datasets"][0], env["users"][0]
    name = "embedding_" + uuid4().hex
    collection = search.index_name_one(owner, name)
    models: list[int] = []
    with Session(env["engine"]) as db:
        db.merge(LLMFactories(name="Ollama", tags="EMBEDDING", status="1"))
        for tenant, model in [(owner, "old"), (owner, "candidate"), (env["users"][1], "foreign")]:
            row = TenantLLM(tenant_id=tenant, llm_factory="Ollama", llm_name=model, mdl_type="embedding", api_key="controlled", api_base="http://controlled.invalid")
            db.add(row)
            db.flush()
            models.append(row.id)
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == dataset).values(name=name, embd_id="old@Ollama", tenant_embd_id=models[0], chunk_num=3))
        db.commit()

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db
            db.commit()

    monkeypatch.setattr(dataset_api_service, "db_connection", scratch_db)
    state: dict[str, Any] = {"vectors": [[1, 0], [1, 0]], "calls": [], "model_tenants": []}

    class Provider:
        def __init__(self, db: Session, tenant: str, config: dict[str, Any]) -> None:
            self.db = db
            assert config["tenant_id"] == tenant == owner
            assert config["id"] == models[1]
            state["model_tenants"].append(tenant)

        def encode(self, texts: list[str]) -> tuple[Any, int]:
            assert self.db is None
            state["calls"].append(texts)
            return np.asarray(state["vectors"]), 0

    monkeypatch.setattr(dataset_embedding_service, "LLMBundle", Provider)
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    chunk_ids = [uuid4().hex for _ in range(3)]
    # Keep a separate record: the parent fixture rewrites its own listener/SQL
    # record during teardown. Register the collection before a setup RPC can fail.
    record_path = env["record_path"].with_suffix(".embedding.json")
    record = {"database": env["engine"].url.database, "embedding_collection": collection, "embedding_model_ids": models, "embedding_chunk_ids": chunk_ids}
    record_path.write_text(json.dumps(record))
    try:
        schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
        schema.add_field("q_2_vec", DataType.FLOAT_VECTOR, dim=2)
        for field in ("kb_id", "doc_id", "docnm_kwd", "content_with_weight"):
            schema.add_field(field, DataType.VARCHAR, max_length=65535)
        schema.add_field("available_int", DataType.INT64)
        indexes = reader.prepare_index_params()
        indexes.add_index("q_2_vec", index_type="AUTOINDEX", metric_type="COSINE")
        reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)
        reader.insert(
            collection,
            [
                {
                    "pk": identifier,
                    "q_2_vec": [1, 0],
                    "kb_id": dataset,
                    "doc_id": env["documents"][0],
                    "docnm_kwd": "title",
                    "content_with_weight": "source content",
                    "available_int": 1 if i < 2 else 0,
                }
                for i, identifier in enumerate(chunk_ids)
            ],
        )
        reader.flush(collection, timeout=60)
        assert settings.docStoreConn.db_type() == "milvus"
        env.update(embedding_state=state, model_ids=models, collection=collection, reader=reader, chunk_ids=chunk_ids)
        yield env
    finally:
        if reader.has_collection(collection):
            reader.drop_collection(collection)
        assert not reader.has_collection(collection)
        reader.close()
        with env["engine"].begin() as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(models)))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.id.in_(models))) == 0
        record.update(embedding_collection_removed=True, embedding_models_removed=True)
        record_path.write_text(json.dumps(record))


def vectors(env: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(env["reader"].query(env["collection"], filter="pk != ''", output_fields=["*"], limit=100), key=lambda row: row["pk"])


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_check_and_switch_leave_stored_vectors_unchanged(embedding_api: dict[str, Any], kind: str) -> None:
    env = embedding_api
    dataset = env["datasets"][0]
    before_sql, before_vectors = sql_state(env), vectors(env)
    path = f"/datasets/{dataset}/embedding/check"
    response = request_api(env, "POST", path, credential=env[kind][0], payload={"embd_id": "candidate@Ollama", "check_num": 5})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    assert response.json()["data"]["summary"]["valid"] == 2
    assert response.json()["data"]["summary"]["avg_cos_sim"] == 1
    assert sql_state(env) == before_sql and vectors(env) == before_vectors
    response = request_api(env, "PUT", f"/datasets/{dataset}", credential=env[kind][0], payload={"embedding_model": "candidate@Ollama"})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    after = sql_state(env)
    kb = after[Knowledgebase.__tablename__][dataset]
    assert kb["embd_id"] == "candidate@Ollama" and kb["tenant_embd_id"] == env["model_ids"][1]
    assert kb["chunk_num"] == 3 and vectors(env) == before_vectors
    assert request_api(env, "GET", f"/datasets/{dataset}", credential=env[kind][0]).json()["data"]["embedding_model"] == "candidate@Ollama"


def test_check_rejects_incompatible_invalid_empty_and_foreign_samples(embedding_api: dict[str, Any]) -> None:
    env = embedding_api
    dataset = env["datasets"][0]
    state = env["embedding_state"]
    before_sql, before_vectors = sql_state(env), vectors(env)
    path = f"/datasets/{dataset}/embedding/check"
    for candidate, code in [([[0, 1], [0, 1]], 10), ([[1, 0, 0], [1, 0, 0]], 102), ([[float("nan"), 0], [1, 0]], 102)]:
        state["vectors"] = candidate
        result = request_api(env, "POST", path, credential=env["keys"][0], payload={"embd_id": "candidate@Ollama"})
        assert result.status_code == 200 and result.json()["code"] == code, result.text
    with env["engine"].begin() as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["users"][2], UserTenant.tenant_id == env["users"][0]).values(role="normal"))
    state["calls"].clear()
    for credential in [env["jwt"][1], env["keys"][1], env["jwt"][2], env["keys"][2]]:
        result = request_api(env, "POST", path, credential=credential, payload={"embd_id": "candidate@Ollama"})
        assert result.json()["code"] == 109, result.text
    assert not state["calls"]
    result = request_api(env, "POST", path, credential=env["keys"][0], payload={"embd_id": "foreign@Ollama"})
    assert result.json()["code"] == 102 and not state["calls"], result.text
    assert sql_state(env) == before_sql and vectors(env) == before_vectors
    # Missing collection belongs to an owned dataset and is an explicit empty-sample failure.
    result = request_api(env, "POST", f"/datasets/{env['datasets'][1]}/embedding/check", credential=env["keys"][0], payload={"embd_id": "candidate@Ollama"})
    assert result.json()["code"] == 102, result.text
    assert sql_state(env) == before_sql and vectors(env) == before_vectors


@pytest.mark.external_consumer
def test_actual_web_embedding_consumer(embedding_api: dict[str, Any]) -> None:
    env = embedding_api
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    assert tsx_runner(checkout).exists() and (checkout / "scripts/verify-embedding-check.ts").exists(), "Web checkout and embedding consumer are required"
    result = subprocess.run(
        [str(tsx_runner(checkout)), "--tsconfig", str(checkout / "tsconfig.app.json"), str(checkout / "scripts/verify-embedding-check.ts")],
        cwd=checkout,
        env={**os.environ, "EMBEDDING_BASE": env["base"], "EMBEDDING_TOKEN": env["jwt"][0], "EMBEDDING_DATASET": env["datasets"][0]},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "embedding Web acceptance passed" in result.stdout
