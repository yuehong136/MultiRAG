"""Repeated paths retain their strongest rank and all similarity contributions."""

import csv
import io
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.services.knowledgebase_service import KnowledgebaseService
from core.graphrag import search


@pytest.mark.parametrize("weights", [(0.2, 0.8), (0.8, 0.2), (0.0, 0.0), (-0.8, -0.2), (-0.2, -0.8)])
async def test_nhop_max_rank_and_similarity_sum(monkeypatch: pytest.MonkeyPatch, weights: tuple[float, float]) -> None:
    engine = search.KGSearch.__new__(search.KGSearch)
    entities = {str(i): {"sim": 0.6, "pagerank": 1, "description": "", "n_hop_ents": [{"path": ["A", "B"], "weights": [weight]}]} for i, weight in enumerate(weights)}
    monkeypatch.setattr(search, "db_connection", lambda: nullcontext(None))
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda *a, **k: [SimpleNamespace(name="kb")])
    monkeypatch.setattr(search, "index_name", lambda *a: "index")
    monkeypatch.setattr(search, "num_tokens_from_string", lambda _: 0)
    monkeypatch.setattr(search, "get_relation", AsyncMock(return_value={"description": "relation"}))
    monkeypatch.setattr(engine, "query_rewrite", AsyncMock(return_value=([], ["A"])))
    monkeypatch.setattr(engine, "get_relevant_ents_by_keywords", AsyncMock(return_value=entities))
    monkeypatch.setattr(engine, "get_relevant_ents_by_types", lambda *a: {})
    monkeypatch.setattr(engine, "get_relevant_relations_by_txt", AsyncMock(return_value={}))
    monkeypatch.setattr(engine, "_community_retrieval_", lambda *a: "")
    result = await engine.retrieval("q", "tenant", ["kb"], None, None)
    rows = list(csv.DictReader(io.StringIO(result["content_with_weight"].split("---- Relations ----\n")[1])))
    assert len(rows) == 1
    assert rows[0]["From Entity"] == "A" and rows[0]["To Entity"] == "B"
    assert float(rows[0]["Score"]) == pytest.approx(0.6 * max(weights))
