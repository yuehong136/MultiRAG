"""Mode, weights, pagination and failures reach the shared storage contract."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from api.utils.dataset_search import SearchDatasetRequest, normalize_search_mode, search_mode_response
from common.doc_store.doc_store_base import FusionExpr, MatchDenseExpr, MatchTextExpr
from core.nlp.search import Dealer, _mode_weights


@pytest.fixture
def mode_dealer() -> Dealer:
    dealer = Dealer.__new__(Dealer)
    dealer.qryr = SimpleNamespace(question=lambda text, **_: (MatchTextExpr(["content_ltks"], text, 10), [text]))
    dealer.get_vector = AsyncMock(return_value=MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10, {"similarity": 0.2}))
    dealer.dataStore = SimpleNamespace(
        db_type=lambda: "milvus",
        search=lambda *_a, **_k: {"hit": {"_score": 0.9, "doc_id": "doc"}},
        get_total=len,
        get_doc_ids=list,
        get_highlight=lambda *_: {},
        get_aggregation=lambda *_: [],
        get_fields=lambda result, _: result,
    )
    return dealer


@pytest.mark.parametrize("backend", ["milvus", "infinity", "elasticsearch", "opensearch"])
@pytest.mark.parametrize(
    ("mode", "types", "weights"),
    [
        ({"dense": {}}, [MatchDenseExpr], None),
        ({"sparse": {}}, [MatchTextExpr], None),
        ({"hybrid": {"weight_dense": 0.8, "weight_sparse": 0.2}}, [MatchTextExpr, MatchDenseExpr, FusionExpr], "0.2,0.8"),
        ({"fusion": {"weights": "0.9,0.1"}}, [MatchTextExpr, MatchDenseExpr, FusionExpr], "0.9,0.1"),
    ],
)
async def test_modes_select_distinct_candidates_and_preserve_pagination(mode_dealer: Dealer, backend: str, mode: dict[str, Any], types: list[type], weights: str | None) -> None:
    calls: list[tuple[Any, ...]] = []

    def search(*args: Any, **_: Any) -> dict[str, Any]:
        calls.append(deepcopy(args))
        return {"hit": {"_score": 0.9, "doc_id": "doc"}}

    mode_dealer.dataStore.db_type = lambda: backend
    mode_dealer.dataStore.search = search
    result = await mode_dealer.search({"question": "keywords", "search_mode": mode, "doc_ids": ["doc"], "page": 3, "size": 2, "topk": 10, "available_int": 1}, ["scratch"], ["kb"], object())
    assert result.ids == ["hit"]
    assert [type(expr) for expr in calls[0][3]] == types
    assert calls[0][2] == {"doc_id": ["doc"], "available_int": 1}
    assert calls[0][5:9] == (4, 2, ["scratch"], ["kb"])
    if weights is not None:
        assert calls[0][3][-1].fusion_params["weights"] == weights
    if mode == {"sparse": {}}:
        mode_dealer.get_vector.assert_not_awaited()


@pytest.mark.parametrize("weights", ["nan,1", "inf,1", "-1,2", "0,0", "1", "1,2,3", "word,1"])
def test_fusion_rejects_unusable_weights(weights: str) -> None:
    with pytest.raises(ValidationError):
        SearchDatasetRequest(question="q", search_mode={"type": "fusion", "weights": weights})


def test_fusion_normalizes_sparse_then_dense_weights() -> None:
    request = SearchDatasetRequest(question="q", search_mode={"type": "fusion", "weights": "1,3"})
    assert request.get_search_mode_dict() == {"fusion": {"weights": "0.25,0.75"}}


@pytest.mark.parametrize(
    ("value", "keyed", "weights"),
    [
        pytest.param(None, None, (0.0, 1.0), id="default"),
        pytest.param({}, None, (0.0, 1.0), id="empty"),
        pytest.param({"type": "dense"}, {"dense": {}}, (0.0, 1.0), id="documented-dense"),
        pytest.param({"type": "dense", "weight_dense": 0.7}, {"dense": {}}, (0.0, 1.0), id="stale-weights"),
        pytest.param({"type": "sparse"}, {"sparse": {}}, (1.0, 0.0), id="documented-sparse"),
        pytest.param({"type": "hybrid", "weight_dense": 0.6, "weight_sparse": 0.4}, {"hybrid": {"weight_dense": 0.6, "weight_sparse": 0.4}}, (0.4, 0.6), id="documented-hybrid"),
        pytest.param({"type": "fusion", "weights": "1,3"}, {"fusion": {"weights": "0.25,0.75"}}, (0.25, 0.75), id="documented-fusion"),
        pytest.param({"hybrid": {"weight_dense": 0.6, "weight_sparse": 0.4}}, {"hybrid": {"weight_dense": 0.6, "weight_sparse": 0.4}}, (0.4, 0.6), id="stored-hybrid"),
        pytest.param({"sparse": {}}, {"sparse": {}}, (1.0, 0.0), id="stored-sparse"),
    ],
)
def test_normalized_search_modes_satisfy_retrieval(value: object, keyed: dict[str, Any] | None, weights: tuple[float, float]) -> None:
    normalized = normalize_search_mode(value)
    assert normalized == keyed
    _, sparse, dense = _mode_weights(normalized)
    assert (sparse, dense) == pytest.approx(weights)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("dense", id="string"),
        pytest.param(["dense"], id="list"),
        pytest.param({"type": "keyword"}, id="unknown-type"),
        pytest.param({"type": "hybrid", "weight_dense": 0, "weight_sparse": 0}, id="zero-weights"),
        pytest.param({"type": "fusion", "weights": "nan,1"}, id="non-finite-fusion"),
        pytest.param({"dense": {}, "sparse": {}}, id="two-modes"),
        pytest.param({"hybrid": 0.7}, id="scalar-params"),
    ],
)
def test_normalize_search_mode_rejects_unusable_values(value: object) -> None:
    with pytest.raises(ValueError):
        normalize_search_mode(value)


def test_search_mode_response_uses_documented_shape() -> None:
    assert search_mode_response({"hybrid": {"weight_dense": 0.6, "weight_sparse": 0.4}}) == {"type": "hybrid", "weight_dense": 0.6, "weight_sparse": 0.4}
    assert search_mode_response({"type": "dense"}) == {"type": "dense"}
    assert search_mode_response(None) is None
    assert search_mode_response({"type": "keyword"}) == {"type": "keyword"}


async def test_search_failure_reaches_rest_error_boundary(mode_dealer: Dealer) -> None:
    def fail(*_: Any, **__: Any) -> Any:
        raise RuntimeError("storage unavailable")

    mode_dealer.dataStore.search = fail
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await mode_dealer.search({"question": "q", "search_mode": {"dense": {}}}, ["scratch"], ["kb"], object())


async def test_explicit_dense_requires_embedding(mode_dealer: Dealer) -> None:
    with pytest.raises(ValueError, match="embedding"):
        await mode_dealer.search({"question": "q", "search_mode": {"dense": {}}}, ["scratch"], ["kb"])


@pytest.mark.parametrize(("weights", "has_text"), [(None, False), (None, True), ("0.9,0.1", True)])
def test_es_query_keeps_independent_vector_candidates_and_boosts(weights: str | None, has_text: bool) -> None:
    from unittest.mock import MagicMock

    from common.doc_store.doc_store_base import OrderByExpr
    from core.utils.es_conn import ESConnection

    cls = next(cell.cell_contents for cell in ESConnection.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.logger = MagicMock()
    conn.es = MagicMock()
    conn.es.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}}
    expressions = []
    if has_text:
        expressions.append(MatchTextExpr(["content_ltks"], "keywords", 10))
    if not has_text or weights:
        expressions.append(MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10, {"similarity": 0.4}))
    if weights:
        expressions.append(FusionExpr("weighted_sum", 10, {"weights": weights}))
    conn.search(["id"], [], {"doc_id": ["selected"], "available_int": 1}, expressions, OrderByExpr(), 4, 2, ["scratch"], ["kb"])
    body = conn.es.search.call_args.kwargs["body"]
    assert body["from"] == 4 and body["size"] == 2
    if has_text:
        assert body["query"]["bool"]["boost"] == (0.9 if weights else 1.0)
    else:
        assert "query" not in body
    if "knn" in body:
        assert body["knn"]["boost"] == (0.1 if weights else 1.0)
        assert "must" not in body["knn"]["filter"]["bool"]
        assert {"terms": {"doc_id": ["selected"]}} in body["knn"]["filter"]["bool"]["filter"]
        assert {"terms": {"kb_id": ["kb"]}} in body["knn"]["filter"]["bool"]["filter"]


@pytest.mark.parametrize(
    ("text_scores", "vector_scores", "expected"), [({"a": 100, "b": 1}, {"a": 0.1, "b": 0.9}, ["b", "a"]), ({"a": 100, "b": 1}, {}, ["a", "b"]), ({}, {"a": 0.1, "b": 0.9}, ["b", "a"])]
)
def test_milvus_fusion_applies_weights_to_available_branches(text_scores: dict[str, float], vector_scores: dict[str, float], expected: list[str]) -> None:
    from common.doc_store.milvus_conn_base import MilvusConnectionBase

    def result(scores: dict[str, float]) -> tuple[list[dict[str, Any]], dict[str, float], int]:
        return [{"id": key, "distance": score} for key, score in scores.items()], scores, len(scores)

    # Call the real fusion function with controlled per-branch candidate scores.
    store = SimpleNamespace(
        _execute_text_queries=lambda *_: result(text_scores),
        _execute_dense_query=lambda *_: result(vector_scores),
        _fusion_weights=MilvusConnectionBase._fusion_weights,
        _normalize_scores=MilvusConnectionBase._normalize_scores,
    )
    rows, total = MilvusConnectionBase._search_with_text_and_dense(
        store, [], "doc_id == 'selected'", [], MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10), FusionExpr("weighted_sum", 10, {"weights": "0.2,0.8"}), ["scratch"], 10, 0, {}
    )
    assert [row["id"] for row in rows] == expected
    assert total == 2
    assert rows[0]["_score"] == (0.2 if not vector_scores else 0.8)


@pytest.mark.parametrize(
    ("mode", "expected_weights", "expected_first"),
    [({"dense": {}}, (0.0, 1.0), "semantic"), ({"hybrid": {"weight_sparse": 0.9, "weight_dense": 0.1}}, (0.9, 0.1), "lexical"), ({"fusion": {"weights": "0.1,0.9"}}, (0.1, 0.9), "semantic")],
)
async def test_es_postranking_preserves_mode_weights(mode_dealer: Dealer, mode: dict[str, Any], expected_weights: tuple[float, float], expected_first: str) -> None:
    from unittest.mock import Mock

    import numpy as np

    mode_dealer.dataStore.db_type = lambda: "elasticsearch"
    fields = {identifier: {"doc_id": "doc", "kb_id": "kb", "content_with_weight": identifier} for identifier in ("lexical", "semantic")}
    mode_dealer.search = AsyncMock(return_value=Dealer.SearchResult(total=2, ids=list(fields), query_vector=[1.0, 0.0], field=fields))
    mode_dealer._existing_doc_ids = AsyncMock(return_value={"doc"})

    def rank(_result: Dealer.SearchResult, _question: str, sparse_weight: float, dense_weight: float, **_: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        # Independent lexical and vector signals prefer opposite documents.
        lexical, dense = np.array([1.0, 0.0]), np.array([0.0, 1.0])
        return sparse_weight * lexical + dense_weight * dense, lexical, dense

    mode_dealer.rerank = Mock(side_effect=rank)
    result = await mode_dealer.retrieval("keywords", "", object(), "tenant", ["kb"], 1, 10, similarity_threshold=0, vector_similarity_weight=0.3, rank_feature={}, search_mode=mode)
    assert mode_dealer.rerank.call_args.args[2:] == expected_weights
    assert result["chunks"][0]["chunk_id"] == expected_first
    assert result["total"] == 2


@pytest.mark.parametrize("dense_only", [False, True])
def test_milvus_candidate_failure_is_not_empty_success(dense_only: bool) -> None:
    from unittest.mock import MagicMock

    from core.utils.milvus_conn import MilvusConnection

    cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store.logger = MagicMock()
    rpc = MagicMock()
    rpc.describe_collection.return_value = {"fields": [{"name": "sparse_vector"}]}
    rpc.search.side_effect = RuntimeError("candidate RPC failed")
    store._get_connection = lambda: rpc
    store.search_field_configs = {"content_ltks": {"sparse_field": "sparse_vector"}}
    with pytest.raises(RuntimeError, match="candidate RPC failed"):
        if dense_only:
            store._execute_dense_query(["scratch"], MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10), "doc_id == 'selected'", [], 10, 0)
        else:
            store._execute_text_queries(["scratch"], [MatchTextExpr(["content_ltks"], "keywords", 10)], "doc_id == 'selected'", [], 10, 0)
