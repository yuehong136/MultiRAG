"""Strict Skills collections, independent from knowledge-base mappings."""

import json
import math
import re
from typing import Any

from api.skills.search_types import IndexMatch, SkillSearchError


class MilvusSkillStore:
    def __init__(self, client: Any = None) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from pymilvus import MilvusClient

            from common.app_config import get_app_config

            config = get_app_config().milvus
            self._client = MilvusClient(
                uri=config.hosts or "http://localhost:19530",
                user=config.username,
                password=config.password,
                db_name=config.db_name or "default",
                token=config.token,
                timeout=config.timeout or 60,
                **(config.kwargs or {}),
            )
        return self._client

    @staticmethod
    def _name(name: str) -> str:
        if not re.fullmatch(r"skill_[0-9a-f]{32}", name):
            raise SkillSearchError("INVALID_INDEX_NAME", "Invalid Skills index name", False)
        return name

    def create(self, name: str, dimension: int) -> None:
        from pymilvus import DataType, Function, FunctionType, MilvusClient

        name = self._name(name)
        if self.client.has_collection(name):
            raise SkillSearchError("INDEX_ALREADY_EXISTS", "A generation must use a new collection", False)
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, max_length=32, is_primary=True)
        for field, size in (("version_id", 32), ("skill_id", 32), ("field", 16), ("content_digest", 64)):
            schema.add_field(field, DataType.VARCHAR, max_length=size)
        schema.add_field("text", DataType.VARCHAR, max_length=32768, enable_analyzer=True, analyzer_params={"type": "standard"})
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dimension)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="skill_bm25", input_field_names=["text"], output_field_names=["sparse"], function_type=FunctionType.BM25))
        indexes = MilvusClient.prepare_index_params()
        indexes.add_index("vector", index_type="AUTOINDEX", metric_type="COSINE")
        indexes.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25", params={"bm25_k1": 1.2, "bm25_b": 0.75})
        self.client.create_collection(name, schema=schema, index_params=indexes, consistency_level="Strong")

    def insert(self, name: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            result = self.client.insert(self._name(name), rows)
            if int(result.get("insert_count", -1)) != len(rows):
                raise SkillSearchError("INDEX_WRITE_INCOMPLETE", "Index did not accept all records")

    def seal(self, name: str, expected: list[dict[str, Any]]) -> None:
        name = self._name(name)
        self.client.flush(name)
        self.client.load_collection(name)
        if self.count(name) != len(expected):
            raise SkillSearchError("INDEX_VERIFY_FAILED", "Index count verification failed")
        for offset in range(0, len(expected), 128):
            batch = expected[offset : offset + 128]
            records = self.client.get(name, ids=[row["id"] for row in batch], output_fields=["id", "version_id", "skill_id", "field", "text", "content_digest", "vector"], consistency_level="Strong")
            actual = {row["id"]: row for row in records}
            for row in batch:
                record = actual.get(row["id"])
                if (
                    record is None
                    or any(record[key] != row[key] for key in ("version_id", "skill_id", "field", "text", "content_digest"))
                    or len(record["vector"]) != len(row["vector"])
                    or any(not math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-7) for actual, expected in zip(record["vector"], row["vector"], strict=True))
                ):
                    raise SkillSearchError("INDEX_VERIFY_FAILED", "Index identity or dimension verification failed")
        if expected:
            first = expected[0]
            if not self.search(name, first["field"], first["vector"], "vector", 1):
                raise SkillSearchError("INDEX_VERIFY_FAILED", "Index search readback failed")

    def search(self, name: str, field: str, query: str | list[float], mode: str, limit: int) -> list[IndexMatch]:
        result = self.client.search(
            self._name(name),
            data=[query],
            anns_field="sparse" if mode == "keyword" else "vector",
            filter="field == " + json.dumps(field),
            limit=limit,
            output_fields=["version_id", "skill_id", "text"],
            search_params={"metric_type": "BM25" if mode == "keyword" else "COSINE"},
            consistency_level="Strong",
        )
        return [IndexMatch(hit["entity"]["version_id"], hit["entity"]["skill_id"], float(hit["distance"]), hit["entity"]["text"]) for hit in result[0]]

    def delete(self, name: str, version_ids: list[str] | None = None) -> None:
        name = self._name(name)
        if not self.client.has_collection(name):
            return
        if version_ids is None:
            self.client.drop_collection(name)
            if self.client.has_collection(name):
                raise SkillSearchError("INDEX_DELETE_INCOMPLETE", "Index collection remains after deletion")
        elif version_ids:
            expression = "version_id in " + json.dumps(version_ids)
            self.client.delete(name, filter=expression)
            self.client.flush(name)
            if self.client.query(name, filter=expression, limit=1, output_fields=["id"], consistency_level="Strong"):
                raise SkillSearchError("INDEX_DELETE_INCOMPLETE", "Index records remain after deletion")

    def count(self, name: str) -> int:
        rows = self.client.query(self._name(name), filter="", output_fields=["count(*)"], consistency_level="Strong")
        return int(rows[0]["count(*)"])
