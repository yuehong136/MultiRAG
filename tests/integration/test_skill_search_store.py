"""Real Milvus build/search/delete readbacks; only the external model is deterministic."""

import hashlib
from typing import Any
from uuid import uuid4

from pymilvus import MilvusClient

from api.skills.milvus_store import MilvusSkillStore
from api.skills.search import SkillSearchRuntime
from api.skills.search_types import SkillIndexDocument
from common.config_utils import CONFIGS


class DeterministicModel:
    async def models(self, tenant_id: str) -> list[dict[str, Any]]:
        return []

    async def resolve(self, tenant: str, identifier: str, kind: str) -> dict[str, Any]:
        return {"id": identifier, "max_tokens": 8192}

    async def encode(self, tenant: str, config: dict[str, Any], texts: list[str], *, query: bool = False) -> list[list[float]]:
        return [[1.0, 0.1] if "orange" in text else [0.1, 1.0] for text in texts]

    async def rerank(self, tenant: str, config: dict[str, Any], query: str, texts: list[str]) -> list[float]:
        return [0.9 if "orange" in text else 0.1 for text in texts]


async def test_skill_generation_real_search_and_scoped_delete() -> None:
    cfg = CONFIGS["milvus"]
    options = {"uri": cfg["hosts"], "user": cfg.get("username", ""), "password": cfg.get("password", ""), "db_name": cfg.get("db_name") or "default"}
    writer, reader = MilvusClient(**options), MilvusClient(**options)
    names = ["skill_" + uuid4().hex for _ in range(2)]
    documents = [
        SkillIndexDocument(uuid4().hex, uuid4().hex, name, name + " description", ["fruit"], "1.0.0", name * 10000, hashlib.sha256(name.encode()).hexdigest()) for name in ("orange", "banana")
    ]
    config = {
        "embedding_model_id": "9007199254740993",
        "rerank_model_id": "9007199254740994",
        "vector_weight": 0.5,
        "similarity_threshold": 0.0,
        "fields": {field: {"enabled": True, "weight": weight} for field, weight in (("name", 3), ("description", 1), ("content", 0.5))},
    }
    runtime = SkillSearchRuntime(MilvusSkillStore(writer), DeterministicModel())
    try:
        for name in names:
            assert await runtime.build(name, "tenant", config, documents) == 2
            rows = reader.query(name, filter='id != ""', output_fields=["id", "version_id", "content_digest", "text", "field", "vector"], consistency_level="Strong")
            assert len(rows) == 24
            for document in documents:
                selected = [row for row in rows if row["version_id"] == document.version_id]
                assert {row["content_digest"] for row in selected} == {document.content_digest}
                assert sum(len(row["text"]) for row in selected if row["field"] == "content") == len(document.content)
            for mode in ("keyword", "vector", "hybrid"):
                hits = await runtime.query(name, "tenant", config, "orange", mode, 20)
                assert hits and hits[0].version_id == documents[0].version_id
                assert hits[0].score == 0.9
        await runtime.delete(names[0], [documents[0].version_id])
        rows = reader.query(names[0], filter='id != ""', output_fields=["version_id"], consistency_level="Strong")
        assert {row["version_id"] for row in rows} == {documents[1].version_id}
        assert reader.query(names[1], filter='version_id == "' + documents[0].version_id + '"', output_fields=["id"], consistency_level="Strong")
        await runtime.delete(names[0])
        assert not reader.has_collection(names[0])
        await runtime.delete(names[0])  # Missing physical collection is a verified retry.
    finally:
        for name in names:
            if reader.has_collection(name):
                reader.drop_collection(name)
            assert not reader.has_collection(name)
        writer.close()
        reader.close()
