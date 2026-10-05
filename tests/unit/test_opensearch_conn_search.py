"""Capture production OpenSearch DSL; candidate scores below are controlled, not live search."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from common.constants import PAGERANK_FLD
from common.doc_store.doc_store_base import FusionExpr, MatchDenseExpr, MatchExpr, MatchTextExpr, OrderByExpr
from core.nlp.search import Dealer
from core.utils.opensearch_conn import OSConnection


def _connection() -> Any:
    cls = next(cell.cell_contents for cell in OSConnection.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.os = MagicMock()
    conn.os.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}, "timed_out": False}
    return conn


def _expressions(weights: str | None = "0.9,0.1", similarity: float = 0.2) -> list[MatchExpr]:
    expressions: list[MatchExpr] = [
        MatchTextExpr(["content_ltks"], "rivet", 1, {"minimum_should_match": 0.3}),
        MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 1, {"similarity": similarity}),
    ]
    if weights is not None:
        expressions.append(FusionExpr("weighted_sum", 1, {"weights": weights}))
    return expressions


def _search(expressions: list[MatchExpr], rank_feature: dict[str, float] | None = None) -> dict[str, Any]:
    conn = _connection()
    conn.search(["id"], [], {"doc_id": ["selected"], "available_int": 1, "category_kwd": "allowed"}, expressions, OrderByExpr(), 4, 2, ["scratch"], ["kb"], rank_feature=rank_feature)
    return deepcopy(conn.os.search.call_args.kwargs["body"])


def _clauses(query: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    found = [query[kind]] if kind in query else []
    for value in query.values():
        if isinstance(value, dict):
            found.extend(_clauses(value, kind))
        elif isinstance(value, list):
            for child in value:
                if isinstance(child, dict):
                    found.extend(_clauses(child, kind))
    return found


def _score(query: dict[str, Any], row: dict[str, Any]) -> float | None:
    """Evaluate boolean membership with fixed lexical/kNN scores to expose AND vs OR."""
    if "bool" in query:
        spec = query["bool"]
        score = 0.0
        for name in ("filter", "must", "must_not"):
            clauses = spec.get(name, [])
            if isinstance(clauses, dict):
                clauses = [clauses]
            for clause in clauses:
                result = _score(clause, row)
                if (result is None) != (name == "must_not"):
                    return None
                if name == "must":
                    score += result or 0.0
        should = spec.get("should", [])
        if isinstance(should, dict):
            should = [should]
        matches = [result for clause in should if (result := _score(clause, row)) is not None]
        required = spec.get("minimum_should_match", int(bool(should) and not spec.get("must") and not spec.get("filter")))
        if len(matches) < required:
            return None
        return (score + sum(matches)) * spec.get("boost", 1.0)
    if "terms" in query:
        return 0.0 if all(row.get(field) in values for field, values in query["terms"].items()) else None
    if "term" in query:
        return 0.0 if all(row.get(field) == value for field, value in query["term"].items()) else None
    if "range" in query:
        return 0.0 if all(row.get(field, 1) < bounds["lt"] for field, bounds in query["range"].items()) else None
    if "query_string" in query:
        spec = query["query_string"]
        return spec.get("boost", 1.0) if spec["query"] in row.get("content_ltks", "").split() else None
    if "knn" in query:
        spec = next(iter(query["knn"].values()))
        if row.get("vector_score") is None or _score(spec["filter"], row) is None:
            return None
        return row["vector_score"] * spec.get("boost", 1.0)
    if "rank_feature" in query:
        spec = query["rank_feature"]
        return row.get(spec["field"], 0.0) * spec.get("boost", 1.0)
    raise AssertionError(f"Unexpected DSL clause: {query}")


@pytest.mark.parametrize("weights", ["0.9,0.1", "0.1,0.9"])
def test_independent_candidates_and_weight_reversal(weights: str) -> None:
    body = _search(_expressions(weights))
    common = {"kb_id": "kb", "doc_id": "selected", "available_int": 1, "category_kwd": "allowed"}
    # lexical is outside vector top-k; semantic is the vector candidate and has no query token.
    rows = {
        "lexical": {**common, "content_ltks": "rivet", "vector_score": None},
        "semantic": {**common, "content_ltks": "fastener", "vector_score": 1.0},
        "unrelated": {**common, "content_ltks": "other", "vector_score": None},
    }
    scores = {key: score for key, row in rows.items() if (score := _score(body["query"], row)) is not None}
    assert set(scores) == {"lexical", "semantic"}
    assert max(scores, key=scores.__getitem__) == ("lexical" if weights == "0.9,0.1" else "semantic")
    assert _score(body["query"], {**common, "content_ltks": "rivet", "vector_score": 1.0}) == pytest.approx(1.0)


@pytest.mark.parametrize("branch", ["lexical", "semantic"])
@pytest.mark.parametrize("outside", [{"kb_id": "foreign"}, {"doc_id": "other"}, {"category_kwd": "denied"}, {"available_int": 0}])
def test_scope_is_required_for_both_candidate_branches(branch: str, outside: dict[str, Any]) -> None:
    row = {
        "kb_id": "kb",
        "doc_id": "selected",
        "category_kwd": "allowed",
        "available_int": 1,
        "content_ltks": "rivet" if branch == "lexical" else "fastener",
        "vector_score": None if branch == "lexical" else 1.0,
    }
    query = _search(_expressions())["query"]
    assert _score(query, row) is not None
    assert _score(query, {**row, **outside}) is None


@pytest.mark.parametrize("similarity", [0.0, 0.2, 0.8])
def test_similarity_does_not_replace_branch_weights(similarity: float) -> None:
    body = _search(_expressions("0.1,0.9", similarity))
    lexical = _clauses(body["query"], "query_string")
    dense = _clauses(body["query"], "knn")
    assert len(lexical) == len(dense) == 1
    assert lexical[0]["boost"] == 0.1
    assert lexical[0]["minimum_should_match"] == "30%"
    vector = dense[0]["q_2_vec"]
    assert vector["boost"] == 0.9
    assert vector["k"] == 1 and vector["vector"] == [1.0, 0.0]
    assert not _clauses(vector["filter"], "query_string")
    assert {"doc_id": ["selected"]} in _clauses(vector["filter"], "terms")
    assert {"kb_id": ["kb"]} in _clauses(vector["filter"], "terms")
    assert {"category_kwd": "allowed"} in _clauses(vector["filter"], "term")
    assert {"available_int": {"lt": 1}} in _clauses(vector["filter"], "range")
    assert body["from"] == 4 and body["size"] == 2


def test_unfused_branches_have_unit_boost() -> None:
    query = _search(_expressions(None))["query"]
    assert _clauses(query, "query_string")[0]["boost"] == 1.0
    assert _clauses(query, "knn")[0]["q_2_vec"]["boost"] == 1.0


def test_rank_feature_cannot_admit_a_non_candidate() -> None:
    query = _search(_expressions(), {PAGERANK_FLD: 10.0})["query"]
    row = {"kb_id": "kb", "doc_id": "selected", "category_kwd": "allowed", "available_int": 1, "content_ltks": "unrelated", "vector_score": None, PAGERANK_FLD: 100.0}
    assert _score(query, row) is None
    assert _score(query, {**row, "content_ltks": "rivet"}) is not None
    vector_filter = _clauses(query, "knn")[0]["q_2_vec"]["filter"]
    assert not _clauses(vector_filter, "rank_feature")


@pytest.mark.parametrize("available", [0, 1])
def test_availability_filter_is_identical_in_vector_and_outer_scope(available: int) -> None:
    conn = _connection()
    conn.search(["id"], [], {"available_int": available}, _expressions(), None, 0, 10, "scratch", ["kb"])
    query = conn.os.search.call_args.kwargs["body"]["query"]
    row = {"kb_id": "kb", "content_ltks": "rivet", "vector_score": 1.0}
    assert _score(query, {**row, "available_int": available}) is not None
    assert _score(query, {**row, "available_int": 1 - available}) is None
    vector_filter = _clauses(query, "knn")[0]["q_2_vec"]["filter"]
    assert query["bool"]["filter"] == vector_filter["bool"]["filter"]


def test_request_features_and_caller_inputs_are_preserved() -> None:
    conn = _connection()
    condition = {"doc_id": ["metadata-selected"], "available_int": 1, "knowledge_graph_kwd": "entity"}
    expressions = _expressions()
    before = deepcopy(condition)
    order = OrderByExpr().asc("page_num_int").desc("create_timestamp_flt")
    conn.search(["id"], ["content_ltks"], condition, expressions, order, 4, 2, "scratch-a,scratch-b", ["kb"], ["docnm_kwd"])
    request = conn.os.search.call_args.kwargs
    body = request["body"]
    assert request["index"] == ["scratch-a", "scratch-b"]
    assert request["track_total_hits"] is True
    assert body["from"] == 4 and body["size"] == 2
    assert body["highlight"]["fields"]["content_ltks"]["require_field_match"] is False
    assert body["aggs"]["aggs_docnm_kwd"]["terms"]["field"] == "docnm_kwd"
    assert body["sort"] == [
        {"page_num_int": {"order": "asc", "unmapped_type": "float", "mode": "avg", "numeric_type": "double"}},
        {"create_timestamp_flt": {"order": "desc", "unmapped_type": "float"}},
    ]
    assert {"knowledge_graph_kwd": "entity"} in _clauses(body["query"], "term")
    assert condition == before
    assert expressions[1].extra_options == {"similarity": 0.2}


@pytest.mark.parametrize(
    ("mode", "text_weight", "dense_weight"),
    [
        ({"dense": {}}, None, 1.0),
        ({"sparse": {}}, 1.0, None),
        ({"hybrid": {"weight_sparse": 0.9, "weight_dense": 0.1}}, 0.9, 0.1),
        ({"fusion": {"weights": "0.1,0.9"}}, 0.1, 0.9),
        ({"hybrid": {"weight_sparse": 0.0, "weight_dense": 1.0}}, None, 1.0),
        ({"fusion": {"weights": "1,0"}}, 1.0, None),
    ],
)
async def test_dealer_modes_reach_real_adapter(mode: dict[str, Any], text_weight: float | None, dense_weight: float | None) -> None:
    conn = _connection()
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = conn
    dealer.qryr = SimpleNamespace(question=lambda text, **_: (MatchTextExpr(["content_ltks"], text, 10), []))
    dealer.get_vector = AsyncMock(return_value=MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10, {"similarity": 0.2}))
    await dealer.search({"question": "rivet", "search_mode": mode, "doc_ids": ["metadata-selected"], "available_int": 1, "page": 3, "size": 2}, ["scratch"], ["kb"], object())
    body = conn.os.search.call_args.kwargs["body"]
    text = _clauses(body["query"], "query_string")
    dense = _clauses(body["query"], "knn")
    assert [clause["boost"] for clause in text] == ([] if text_weight is None else [text_weight])
    assert [clause["q_2_vec"]["boost"] for clause in dense] == ([] if dense_weight is None else [dense_weight])
    assert {"doc_id": ["metadata-selected"]} in _clauses(body["query"], "terms")
    assert body["from"] == 4 and body["size"] == 2
    if dense_weight is None:
        dealer.get_vector.assert_not_awaited()
