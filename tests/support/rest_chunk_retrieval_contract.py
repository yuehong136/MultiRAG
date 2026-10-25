"""REST chunk contract helpers extending the shared real search fixture."""

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase
from common import settings
from core.nlp.search import index_name_one
from tests.support.dataset_search_http import search_api as search_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@dataclass
class RestChunkContract:
    env: dict[str, Any]
    calls: list[str]

    @property
    def path(self) -> str:
        return f"/datasets/{self.env['dataset']}/documents/{self.env['doc']}/chunks"

    def request(self, method: str, path: str | None = None, *, credential: str | None = "jwt", **kwargs: Any) -> requests.Response:
        token = self.env.get(credential, credential) if credential is not None else None
        headers = {"Authorization": "Bearer " + token} if token else {}
        return requests.request(method, self.env["base"] + "/api/v1" + (path or self.path), headers=headers, timeout=60, **kwargs)

    def add(self, content: str, **fields: Any) -> dict[str, Any]:
        response = self.request("POST", json={"content": content, **fields})
        assert response.status_code == 200 and response.json().get("code") == 0, response.text
        return response.json()["data"]["chunk"]

    def native(self) -> list[dict[str, Any]]:
        return sorted(
            self.env["reader"].query(self.env["collections"]["dataset"], filter='pk != ""', output_fields=["*", "q_768_vec", "vector"], consistency_level="Strong"), key=lambda row: row["pk"]
        )

    def snapshot(self) -> dict[str, Any]:
        with Session(self.env["engine"]) as db:
            doc, kb = db.get(Document, self.env["doc"]), db.get(Knowledgebase, self.env["dataset"])
            return {"document": doc.to_dict() if doc else None, "dataset": kb.to_dict() if kb else None, "native": self.native(), "model_calls": list(self.calls)}

    def assert_count(self, expected: int) -> None:
        snapshot = self.snapshot()
        assert snapshot["document"]["chunk_num"] == snapshot["dataset"]["chunk_num"] == len(snapshot["native"]) == expected

    def wait_for_count(self, expected: int) -> None:
        # Production Milvus query reads are bounded-consistent, unlike the
        # independent Strong reader. Wait only for acknowledged index writes.
        deadline = time.monotonic() + 15
        while True:
            response = self.request("GET")
            body = response.json()
            assert response.status_code == 200 and body.get("code") == 0, response.text
            if body["data"]["total"] == expected:
                return
            assert time.monotonic() < deadline, body
            time.sleep(0.1)

    def retrieve(self, **fields: Any) -> requests.Response:
        return self.request("POST", "/retrieval", credential="sdk_token", json={"question": "contract", "dataset_ids": [self.env["dataset"]], "similarity_threshold": 0, **fields})


@pytest.fixture
def rest_chunk_contract(search_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[RestChunkContract]:
    env = search_api
    module = sys.modules["api.apps.restful_apis.chunk"]
    calls: list[str] = []

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    class Embedding:
        db = None
        max_length = 8192

        def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
            calls.append("encode")
            return np.full((len(texts), 768), 0.2), 0

        def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
            calls.append("query")
            return np.full(768, 0.2), 0

        async def async_chat(self, system: str, history: list[dict[str, str]], gen_conf: dict[str, Any]) -> str:
            calls.append("keyword chat")
            return "contract"

    def bundle(*args: Any, **kwargs: Any) -> Embedding:
        calls.append("bundle")
        return Embedding()

    monkeypatch.setattr(module, "db_connection", scratch_db)
    monkeypatch.setattr(module, "LLMBundle", bundle)
    monkeypatch.setattr(module, "get_model_config_by_type_and_name", lambda *_args, **_kwargs: {})
    sdk = sys.modules["api.apps.restful_apis.chunk"]
    monkeypatch.setattr(sdk, "LLMBundle", bundle)
    monkeypatch.setattr(sdk, "get_tenant_default_model_by_type", lambda *_args, **_kwargs: {})

    def model_config(db: Session, tenant_id: str, kind: str, name: str) -> dict[str, Any]:
        if kind == "rerank":
            raise LookupError("Controlled unknown rerank model")
        return {}

    monkeypatch.setattr(sdk, "get_model_config_by_type_and_name", model_config)

    def forbidden_storage(*args: Any, **kwargs: Any) -> None:
        calls.append("object mutation")
        raise AssertionError("Text contract must not mutate object storage")

    monkeypatch.setattr(settings.STORAGE_IMPL, "put", forbidden_storage)
    monkeypatch.setattr(settings.STORAGE_IMPL, "rm", forbidden_storage)
    # Reuse registered SQL/HTTP/MinIO/Milvus scopes. This fixture's search-only
    # seed schema uses scalar keyword text; actual manual writes need the
    # production schema's keyword arrays and dense/BM25 mappings.
    previous = env["collections"]["dataset"]
    settings.docStoreConn.delete_idx(previous, env["dataset"])
    assert not env["reader"].has_collection(previous)
    name = "rest_chunk_" + uuid4().hex
    collection = index_name_one(env["owners"][0], name)
    env["collections"]["dataset"] = collection
    env["record"]["replaced_search_collection"] = {"name": previous, "absent": True}
    env["record_path"].write_text(json.dumps(env["record"]))
    assert settings.docStoreConn.create_idx(collection, env["dataset"], 768)
    with env["engine"].begin() as db:
        for model in (Document, Knowledgebase):
            identifier = env["doc"] if model is Document else env["dataset"]
            db.execute(sa.update(model).where(model.id == identifier).values(chunk_num=0, token_num=0))
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["dataset"]).values(name=name))
    ctx = RestChunkContract(env, calls)
    ctx.assert_count(0)
    env["record"]["rest_chunk_contract"] = {"dataset": env["dataset"], "document": env["doc"], "collection": env["collections"]["dataset"]}
    env["record_path"].write_text(json.dumps(env["record"]))
    try:
        yield ctx
    finally:
        env["record"]["rest_chunk_contract"].update(final_native_count=len(ctx.native()), model_calls=len(calls), object_mutations=calls.count("object mutation"))
        env["record_path"].write_text(json.dumps(env["record"]))
