"""Document selection narrows candidates without overriding relevance."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from core.nlp.search import Dealer


@pytest.mark.parametrize("backend", ["milvus", "infinity", "elasticsearch", "opensearch"])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("mode", [{"dense": {}}, {"fusion": {"weights": "0.5,0.5"}}, {"hybrid": {"weight_dense": 0.5, "weight_sparse": 0.5}}, {"sparse": {}}])
async def test_threshold_survives_document_scope(monkeypatch: pytest.MonkeyPatch, backend: str, external: bool, selected: bool, mode: dict[str, Any]) -> None:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(db_type=lambda: backend)
    fields = {key: {"doc_id": key, "kb_id": "kb", "docnm_kwd": key, "content_with_weight": key, "_score": score} for key, score in [("low", 0.1), ("high", 0.9), ("boundary", 0.5)]}
    result = Dealer.SearchResult(total=3, ids=list(fields), field=fields, query_vector=[0.1, 0.2])
    fetch = AsyncMock(return_value=result)
    monkeypatch.setattr(dealer, "search", fetch)
    monkeypatch.setattr(dealer, "_existing_doc_ids", AsyncMock(return_value=set(fields)))

    def rank(*args: Any, **kwargs: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        scores = np.array([row["_score"] for row in fields.values()])
        return scores, scores, scores

    monkeypatch.setattr(dealer, "rerank", rank)
    monkeypatch.setattr(dealer, "rerank_by_model", rank)
    documents = list(fields) if selected else None
    options = {"similarity_threshold": 0.5, "doc_ids": documents, "search_mode": mode, "rerank_mdl": object() if external else None, "rank_feature": {}}
    first = await dealer.retrieval("query", "", None, "tenant", ["dataset"], 1, 1, **options)
    second = await dealer.retrieval("query", "", None, "tenant", ["dataset"], 2, 1, **options)
    expected = ["high", "boundary", "low"] if "sparse" in mode else ["high", "boundary"]
    assert first["total"] == second["total"] == len(expected)
    assert [chunk["chunk_id"] for page in (first, second) for chunk in page["chunks"]] == expected[:2]
    assert {doc["doc_id"] for doc in first["doc_aggs"]} == set(expected)
    assert fetch.call_args.args[0]["doc_ids"] == documents
