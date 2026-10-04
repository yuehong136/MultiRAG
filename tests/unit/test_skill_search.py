"""Search boundaries: immutable generations, real score fusion, failures and limits."""

from typing import Any

import pytest

from api.skills.model_runtime import rerank_scores
from api.skills.search import SkillSearchRuntime, checked_vectors, chunks
from api.skills.search_types import IndexMatch, SkillIndexDocument, SkillSearchError


class Models:
    def __init__(self) -> None:
        self.resolved: list[tuple[str, str, str]] = []
        self.reranked = False
        self.vectors: Any = None

    async def models(self, tenant_id: str) -> list[dict[str, Any]]:
        return []

    async def resolve(self, tenant: str, identifier: str, kind: str) -> dict[str, Any]:
        self.resolved.append((tenant, identifier, kind))
        return {"id": identifier, "max_tokens": 16}

    async def encode(self, tenant: str, model: dict[str, Any], texts: list[str], *, query: bool = False) -> Any:
        return self.vectors if self.vectors is not None else [[1.0, 0.25] for _ in texts]

    async def rerank(self, tenant: str, model: dict[str, Any], query: str, texts: list[str]) -> Any:
        self.reranked = True
        return list(range(len(texts)))


class Store:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.matches: dict[tuple[str, str], list[IndexMatch]] = {}

    def create(self, name: str, dimension: int) -> None:
        self.calls.append("create")
        assert dimension == 2

    def insert(self, name: str, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)

    def seal(self, name: str, expected: list[dict[str, Any]]) -> None:
        self.calls.append("seal")
        assert self.rows == expected

    def search(self, name: str, field: str, query: str | list[float], mode: str, limit: int) -> list[IndexMatch]:
        return self.matches.get((field, mode), [])[:limit]

    def delete(self, name: str, version_ids: list[str] | None = None) -> None:
        raise RuntimeError("provider secret must not leak")

    def count(self, name: str) -> int:
        return len(self.rows)


@pytest.fixture
def config() -> dict[str, Any]:
    return {
        "embedding_model_id": "9007199254740993",
        "rerank_model_id": None,
        "vector_weight": 0.25,
        "similarity_threshold": 0,
        "fields": {"name": {"enabled": True, "weight": 3}, "content": {"enabled": True, "weight": 1}},
    }


def test_utf8_chunks_preserve_all_characters_and_boundaries() -> None:
    text = "技能\n中文\t😀" * 1000
    result = chunks(text, 17)
    assert "".join(result) == text
    assert max(len(item.encode()) for item in result) <= 17
    with pytest.raises(SkillSearchError, match="limit"):
        chunks("😀", 3)


@pytest.mark.parametrize("value", [[], [[0, 0]], [[float("nan"), 1]], [[float("inf"), 1]], [[1e100, 1]], [[1e-100, 1e-100]], [[1]], [[1, 2], [3, 4]], ["no"]])
def test_invalid_embedding_never_becomes_ready(value: Any) -> None:
    with pytest.raises(SkillSearchError, match="invalid"):
        checked_vectors(value, 1, 2)


async def test_build_indexes_every_enabled_chunk_and_keeps_model_identity(config: dict[str, Any]) -> None:
    store, models = Store(), Models()
    runtime = SkillSearchRuntime(store, models)
    document = SkillIndexDocument("a" * 32, "b" * 32, "first", "unused", [], "1.0.0", "nested 中文\n" * 100, "c" * 64)
    assert await runtime.build("skill_" + "d" * 32, "tenant", config, [document]) == 2
    assert store.calls == ["create", "seal"]
    assert "".join(row["text"] for row in store.rows if row["field"] == "content") == document.content
    assert len({row["id"] for row in store.rows}) == len(store.rows)
    assert models.resolved == [("tenant", "9007199254740993", "embedding")]


async def test_invalid_provider_response_does_not_create_generation(config: dict[str, Any]) -> None:
    store, models = Store(), Models()
    models.vectors = [[0, 0]]
    with pytest.raises(SkillSearchError):
        await SkillSearchRuntime(store, models).build("skill_" + "d" * 32, "tenant", config, [])
    assert store.calls == []


async def test_field_weight_hybrid_fusion_and_real_rerank(config: dict[str, Any]) -> None:
    store, models = Store(), Models()
    store.matches = {
        ("name", "keyword"): [IndexMatch("v1", "s1", 3.0, "first"), IndexMatch("v1", "s1", 2.0, "duplicate chunk")],
        ("name", "vector"): [IndexMatch("v2", "s2", 1.0, "second")],
        ("content", "keyword"): [IndexMatch("v2", "s2", 1.0, "body")],
    }
    runtime = SkillSearchRuntime(store, models)
    hits = await runtime.query("skill_" + "d" * 32, "tenant", config, "term", "hybrid", 20)
    assert [hit.version_id for hit in hits] == ["v1", "v2"]
    assert hits[0].score == pytest.approx(0.75 * 0.75 * 0.75)
    assert hits[1].score == pytest.approx(0.25 * 0.75 + 0.75 * 0.25 * 0.5)
    config["rerank_model_id"] = "8"
    reranked = await runtime.query("skill_" + "d" * 32, "tenant", config, "term", "hybrid", 20)
    assert models.reranked and reranked[0].version_id == "v2"


async def test_delete_failure_is_safe_retryable_error(config: dict[str, Any]) -> None:
    with pytest.raises(SkillSearchError) as error:
        await SkillSearchRuntime(Store(), Models()).delete("skill_" + "d" * 32)
    assert error.value.error_code == "INDEX_UNAVAILABLE"
    assert "secret" not in str(error.value)


async def test_candidate_truncation_is_exposed(config: dict[str, Any]) -> None:
    store = Store()
    store.matches[("name", "keyword")] = [IndexMatch(str(i), str(i), 1.0, "text") for i in range(100)]
    hits = await SkillSearchRuntime(store, Models()).query("skill_" + "d" * 32, "tenant", config, "text", "keyword", 1)
    assert len(hits) == 1 and hits.truncated


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "failure"},
        {"results": []},
        {"results": [{"index": 0, "relevance_score": float("nan")}]},
        {"results": [{"index": True, "relevance_score": 0.4}]},
        {"results": [{"index": 1, "relevance_score": 0.4}]},
    ],
)
def test_rerank_rejects_malformed_success(payload: Any) -> None:
    with pytest.raises(SkillSearchError):
        rerank_scores(payload, 1)


def test_rerank_keeps_single_candidate_score_and_rejects_duplicate_indices() -> None:
    assert rerank_scores({"results": [{"index": 0, "relevance_score": 0.95}]}, 1) == [0.95]
    with pytest.raises(SkillSearchError):
        rerank_scores({"results": [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.7}]}, 2)
