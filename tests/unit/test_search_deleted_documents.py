"""Deleted document candidates never reach ranking or pagination."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services.document_service import DocumentService
from common import settings
from core.nlp import search
from core.nlp.search import Dealer


def candidates() -> Dealer.SearchResult:
    fields = {
        key: {"doc_id": doc, "kb_id": kb, "docnm_kwd": doc, "content_with_weight": key, "content_ltks": key, "_score": score}
        for key, doc, kb, score in [("stale", "gone", "a", 1.0), ("one", "live", "a", 0.9), ("two", "other", "b", 0.8), ("low", "live", "a", 0.1)]
    }
    return Dealer.SearchResult(total=1000, ids=list(fields), query_vector=[0.2, 0.2], field=fields, highlight={key: key for key in fields}, keywords=["query"], aggregation=[("gone", 900)])


async def test_prune_aligns_fields_highlights_and_preserves_backend_total(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    result = candidates()
    result.ids += ["missing", "empty", "no-doc"]
    result.field.update({"empty": {}, "no-doc": {"content_with_weight": "unowned"}})
    lookup = AsyncMock(return_value={"live", "other"})
    monkeypatch.setattr(dealer, "_existing_doc_ids", lookup)
    kept = await dealer._prune_deleted_chunks(result)
    assert kept.ids == ["one", "two", "low"]
    assert list(kept.field) == kept.ids == list(kept.highlight)
    assert kept.total == 1000
    assert kept.query_vector is result.query_vector and kept.keywords is result.keywords
    assert result.ids[0] == "stale"  # Source result is not mutated.
    assert lookup.call_args.args[0] == ["gone", "live", "other", "live"]


async def test_empty_candidates_do_not_open_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    connection = AsyncMock(side_effect=AssertionError("no SQL expected"))
    monkeypatch.setattr(search, "async_db_connection", connection)
    assert (await dealer._prune_deleted_chunks(Dealer.SearchResult(total=10, ids=[], field=None))).ids == []
    connection.assert_not_called()


async def test_only_dataset_raptor_with_live_dataset_is_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID

    dealer = Dealer.__new__(Dealer)
    fields = {
        "dataset-summary": {"doc_id": GRAPH_RAPTOR_FAKE_DOC_ID, "kb_id": "a", "raptor_kwd": "raptor"},
        "deleted-dataset": {"doc_id": GRAPH_RAPTOR_FAKE_DOC_ID, "kb_id": "gone-kb", "raptor_kwd": "raptor"},
        "unmarked": {"doc_id": GRAPH_RAPTOR_FAKE_DOC_ID, "kb_id": "a"},
        "file-summary": {"doc_id": "gone", "kb_id": "a", "raptor_kwd": "raptor"},
        "missing-parent": {"kb_id": "a", "raptor_kwd": "raptor"},
        "outside-selection": {"doc_id": GRAPH_RAPTOR_FAKE_DOC_ID, "kb_id": "b", "raptor_kwd": "raptor"},
        "ordinary": {"doc_id": ["live"], "kb_id": "a"},
    }
    monkeypatch.setattr(dealer, "_existing_doc_ids", AsyncMock(return_value={"live"}))
    monkeypatch.setattr(dealer, "_existing_kb_ids", AsyncMock(return_value={"a", "b"}))
    result = await dealer._prune_deleted_chunks(Dealer.SearchResult(total=999, ids=list(fields), field=fields, query_vector=[]), ["a"])
    assert result.ids == ["dataset-summary", "ordinary"]
    assert result.total == 999
    assert dealer._existing_kb_ids.call_args.args[0] == ["a", "gone-kb", "b"]


async def test_document_lookup_has_no_positive_or_negative_cache(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    dealer = Dealer.__new__(Dealer)

    @asynccontextmanager
    async def connection() -> AsyncIterator[AsyncSession]:
        yield async_db

    lookup = AsyncMock(side_effect=[{"live"}, set(), set()])
    monkeypatch.setattr(search, "async_db_connection", connection)
    monkeypatch.setattr(DocumentService, "get_existing_ids_async", lookup)
    assert await dealer._existing_doc_ids(["live"]) == {"live"}
    assert await dealer._existing_doc_ids(["live"]) == set()
    assert await dealer._existing_doc_ids(["live"]) == set()
    assert lookup.await_count == 3


@pytest.mark.parametrize("backend", ["milvus", "infinity", "elasticsearch", "opensearch"])
@pytest.mark.parametrize("external", [False, True])
async def test_retrieval_prunes_before_all_ranking_paths(monkeypatch: pytest.MonkeyPatch, backend: str, external: bool) -> None:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(db_type=lambda: backend)
    lookup = AsyncMock(return_value={"live", "other"})
    fetch = AsyncMock(return_value=candidates())
    monkeypatch.setattr(dealer, "_existing_doc_ids", lookup)
    monkeypatch.setattr(dealer, "search", fetch)
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", backend == "infinity")

    def rank(result: Dealer.SearchResult, *_args: Any, **_kwargs: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        assert result.ids == ["one", "two", "low"]
        assert "stale" not in result.field
        values = np.array([0.9, 0.8, 0.1])
        return values, values, values

    def model_rank(_model: Any, result: Dealer.SearchResult, *args: Any, **kwargs: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return rank(result, *args, **kwargs)

    monkeypatch.setattr(dealer, "rerank", rank)
    monkeypatch.setattr(dealer, "rerank_by_model", model_rank)
    options = {"rerank_mdl": object() if external else None, "kb_ids": ["a", "b"], "rank_feature": {}, "highlight": True}
    first = await dealer.retrieval("query", "permission predicate", None, ["tenant"], ["name-a", "name-b"], 1, 1, **options)
    second = await dealer.retrieval("query", "permission predicate", None, ["tenant"], ["name-a", "name-b"], 2, 1, **options)
    assert first["total"] == second["total"] == 2
    assert [row["chunk_id"] for row in first["chunks"] + second["chunks"]] == ["one", "two"]
    assert {row["doc_id"] for row in first["doc_aggs"]} == {"live", "other"}
    req = fetch.call_args.args[0]
    assert req["available_int"] == 1 and req["filter_exp"] == "permission predicate"
    assert fetch.call_args.args[2] == ["a", "b"]


async def test_all_deleted_returns_empty_without_reranking(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    monkeypatch.setattr(dealer, "search", AsyncMock(return_value=candidates()))
    monkeypatch.setattr(dealer, "_existing_doc_ids", AsyncMock(return_value=set()))
    rerank = AsyncMock(side_effect=AssertionError("deleted text reached reranker"))
    monkeypatch.setattr(dealer, "rerank_by_model", rerank)
    assert await dealer.retrieval("query", "", None, "tenant", ["name"], 1, 10, doc_ids=["gone"], rerank_mdl=object()) == {"total": 0, "chunks": [], "doc_aggs": []}
    rerank.assert_not_called()


async def test_pruning_keeps_deep_page_window_and_candidate_count(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(db_type=lambda: "milvus")
    fetch = AsyncMock(return_value=candidates())
    monkeypatch.setattr(dealer, "search", fetch)
    monkeypatch.setattr(dealer, "_existing_doc_ids", AsyncMock(return_value={"live", "other"}))
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    ranks = await dealer.retrieval("query", "", None, "tenant", ["name"], 8, 10, rank_feature={}, aggs=False)
    assert fetch.call_args.args[0]["page"] == 2
    assert fetch.call_args.args[0]["size"] == 70
    assert ranks["total"] == 2 and len(ranks["chunks"]) == 2
    assert ranks["doc_aggs"] == []


async def test_sql_failure_propagates_instead_of_returning_stale_text(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    monkeypatch.setattr(dealer, "search", AsyncMock(return_value=candidates()))
    monkeypatch.setattr(dealer, "_existing_doc_ids", AsyncMock(side_effect=RuntimeError("SQL unavailable")))
    with pytest.raises(RuntimeError, match="SQL unavailable"):
        await dealer.retrieval("query", "", None, "tenant", ["name"], 1, 10)
