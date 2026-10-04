"""Generation construction and weighted retrieval over immutable skill versions."""

import asyncio
import hashlib
import math
import os
import struct
from collections.abc import Callable
from typing import Any, TypeVar

from api.skills.milvus_store import MilvusSkillStore
from api.skills.model_runtime import ModelRuntime
from api.skills.search_types import FIELDS, IndexStore, SearchModels, SkillIndexDocument, SkillSearchError, SkillSearchHit

T = TypeVar("T")


class SkillSearchHits(list[SkillSearchHit]):
    truncated: bool = False


def chunks(text: str, budget: int) -> list[str]:
    """Split on complete Unicode characters without discarding whitespace."""
    result: list[str] = []
    pending: list[str] = []
    size = 0
    for character in text:
        length = len(character.encode("utf-8"))
        if length > budget:
            raise SkillSearchError("MODEL_LIMIT_INVALID", "Model token limit is too small", False)
        if size + length > budget:
            result.append("".join(pending))
            pending, size = [], 0
        pending.append(character)
        size += length
    if pending:
        result.append("".join(pending))
    return result


def checked_vectors(value: Any, count: int, dimension: int | None = None) -> list[list[float]]:
    try:
        vectors = [[struct.unpack("!f", struct.pack("!f", float(item)))[0] for item in row] for row in value]
        dim = dimension or len(vectors[0])
        valid = len(vectors) == count and 0 < dim <= 32768 and all(len(row) == dim and all(math.isfinite(item) for item in row) and any(item != 0 for item in row) for row in vectors)
    except (TypeError, ValueError, IndexError, OverflowError):
        valid = False
    if not valid:
        raise SkillSearchError("EMBEDDING_INVALID", "Provider returned invalid embedding vectors")
    return vectors


def enabled_fields(config: dict[str, Any]) -> list[tuple[str, float]]:
    fields = config.get("fields", {})
    result = [(field, float(fields[field]["weight"])) for field in FIELDS if fields.get(field, {}).get("enabled") and float(fields[field].get("weight", 0)) > 0]
    if not result or any(not math.isfinite(weight) or weight > 10 for _, weight in result):
        raise SkillSearchError("INVALID_SEARCH_CONFIG", "Search requires an enabled positive-weight field", False)
    return result


class SkillSearchRuntime:
    def __init__(self, store: IndexStore | None = None, models: SearchModels | None = None) -> None:
        self.store = store or MilvusSkillStore()
        self.model_runtime = models or ModelRuntime()
        self._injected_store = store is not None

    def supported(self) -> bool:
        return self._injected_store or os.environ.get("DOC_ENGINE", "milvus").strip().lower() == "milvus"

    def _require_store(self) -> None:
        if not self.supported():
            raise SkillSearchError("SEARCH_BACKEND_UNSUPPORTED", "Skills search is unavailable on this index backend", False)

    async def _io(self, function: Callable[..., T], *args: Any) -> T:
        self._require_store()
        try:
            return await asyncio.to_thread(function, *args)
        except SkillSearchError:
            raise
        except Exception:
            raise SkillSearchError("INDEX_UNAVAILABLE", "Skills index operation failed") from None

    async def models(self, tenant_id: str) -> list[dict[str, Any]]:
        return await self.model_runtime.models(tenant_id)

    async def validate_models(self, tenant_id: str, config: dict[str, Any]) -> None:
        for key, kind in (("embedding_model_id", "embedding"), ("rerank_model_id", "rerank")):
            if config.get(key) is not None:
                await self.model_runtime.resolve(tenant_id, config[key], kind)

    async def build(self, index_name: str, tenant_id: str, config: dict[str, Any], documents: list[SkillIndexDocument]) -> int:
        self._require_store()
        model = await self.model_runtime.resolve(tenant_id, config.get("embedding_model_id"), "embedding")
        budget = min(8192, max(1, int(model["max_tokens"] * 0.8)))
        fields = enabled_fields(config)
        rows: list[dict[str, Any]] = []
        for document in documents:
            values = {"name": document.name, "tags": " ".join(document.tags), "description": document.description, "content": document.content}
            for field, _ in fields:
                for ordinal, text in enumerate(chunks(values[field], budget)):
                    row_id = hashlib.sha256(f"{document.version_id}\0{field}\0{ordinal}".encode()).hexdigest()[:32]
                    rows.append({"id": row_id, "skill_id": document.skill_id, "version_id": document.version_id, "field": field, "text": text, "content_digest": document.content_digest})
        # Even an empty generation obtains its dimension from the selected provider.
        probe_texts = [row["text"] for row in rows[:32]] or ["skills"]
        vectors = checked_vectors(await self.model_runtime.encode(tenant_id, model, probe_texts), len(probe_texts))
        dimension = len(vectors[0])
        await self._io(self.store.create, index_name, dimension)
        for offset in range(0, len(rows), 32):
            batch = rows[offset : offset + 32]
            if offset:
                vectors = checked_vectors(await self.model_runtime.encode(tenant_id, model, [row["text"] for row in batch]), len(batch), dimension)
            for row, vector in zip(batch, vectors, strict=True):
                row["vector"] = vector
            await self._io(self.store.insert, index_name, batch)
        await self._io(self.store.seal, index_name, rows)
        return dimension

    def keyword_part(self, raw_score: float, weight: float, total_weight: float) -> float:
        normalized = max(0.0, raw_score) / (1 + max(0.0, raw_score))
        return weight * normalized / total_weight

    def keyword_total(self, score: float) -> float:
        return score

    async def query(self, index_name: str, tenant_id: str, config: dict[str, Any], query: str, mode: str, limit: int) -> SkillSearchHits:
        self._require_store()
        if mode not in ("keyword", "vector", "hybrid") or not query.strip() or not 1 <= limit <= 10000:
            raise SkillSearchError("INVALID_SEARCH", "Invalid search mode, query or limit", False)
        vector: list[float] = []
        if mode != "keyword":
            model = await self.model_runtime.resolve(tenant_id, config.get("embedding_model_id"), "embedding")
            if len(query.encode()) > min(8192, max(1, int(model["max_tokens"] * 0.8))):
                raise SkillSearchError("QUERY_TOO_LONG", "Query exceeds the selected model limit", False)
            vector = checked_vectors(await self.model_runtime.encode(tenant_id, model, [query], query=True), 1)[0]
        fields = enabled_fields(config)
        total_weight = sum(weight for _, weight in fields)
        candidates: dict[str, dict[str, Any]] = {}
        raw_limit = min(16384, max(limit * 4, 100))
        truncated = False
        for field, weight in fields:
            modes = ("keyword", "vector") if mode == "hybrid" else (mode,)
            for search_mode in modes:
                matches = await self._io(self.store.search, index_name, field, query if search_mode == "keyword" else vector, search_mode, raw_limit)
                truncated = truncated or len(matches) == raw_limit
                best: dict[str, float] = {}
                for match in matches:
                    if not math.isfinite(match.score):
                        raise SkillSearchError("INDEX_RESULT_INVALID", "Index returned an invalid score")
                    normalized = max(0.0, match.score) / (1 + max(0.0, match.score)) if search_mode == "keyword" else max(0.0, min(1.0, match.score))
                    candidate = candidates.setdefault(match.version_id, {"skill_id": match.skill_id, "text": match.text, "text_score": -1.0, "keyword": 0.0, "vector": 0.0})
                    if normalized > candidate["text_score"]:
                        candidate.update(text=match.text, text_score=normalized)
                    best[match.version_id] = max(best.get(match.version_id, 0.0), match.score if search_mode == "keyword" else normalized)
                for version_id, score in best.items():
                    candidates[version_id][search_mode] += self.keyword_part(score, weight, total_weight) if search_mode == "keyword" else weight * score / total_weight
        hits: list[SkillSearchHit] = []
        for version_id, candidate in candidates.items():
            candidate["keyword"] = self.keyword_total(candidate["keyword"])
            score = candidate[mode] if mode != "hybrid" else (1 - config["vector_weight"]) * candidate["keyword"] + config["vector_weight"] * candidate["vector"]
            if score >= config["similarity_threshold"]:
                hits.append(SkillSearchHit(version_id, candidate["skill_id"], score, candidate["text"]))
        hits.sort(key=lambda hit: (-hit.score, hit.skill_id, hit.version_id))
        truncated = truncated or len(hits) > limit
        hits = hits[:limit]
        if config.get("rerank_model_id") is not None and hits:
            reranker = await self.model_runtime.resolve(tenant_id, config["rerank_model_id"], "rerank")
            scores = await self.model_runtime.rerank(tenant_id, reranker, query, [hit.text for hit in hits])
            try:
                values = [float(score) for score in scores]
                valid = len(values) == len(hits) and all(math.isfinite(score) for score in values)
            except (TypeError, ValueError, OverflowError):
                valid = False
            if not valid:
                raise SkillSearchError("RERANK_INVALID", "Reranker returned invalid scores")
            hits = [SkillSearchHit(hit.version_id, hit.skill_id, score, hit.text) for hit, score in zip(hits, values, strict=True)]
            hits.sort(key=lambda hit: (-hit.score, hit.skill_id, hit.version_id))
        result = SkillSearchHits(hits)
        result.truncated = truncated
        return result

    async def delete(self, index_name: str, version_ids: list[str] | None = None) -> None:
        await self._io(self.store.delete, index_name, version_ids)

    async def count(self, index_name: str) -> int:
        return await self._io(self.store.count, index_name)
