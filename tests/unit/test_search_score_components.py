"""Retrieval reports only the score components a ranking path computes.

Clients show a null component as unavailable and 0.0 as a real zero match, so
engines that return one store score leave lexical/vector components null,
while application reranking and rerank models keep their computed values.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from api.db.services.chunk_feedback_service import _retrieval_signal
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import PAGERANK_FLD, TAG_FLD
from common.doc_store.doc_store_base import EXACT_SCORE_NO_VECTOR
from core.graphrag import search as graph_search
from core.nlp import query, search
from core.nlp.search import Dealer, _component_score
from core.prompts import generator
from core.prompts.generator import chunks_format
from core.utils.tavily_conn import Tavily

DENSE = {"dense": {}}
SPARSE = {"sparse": {}}
HYBRID = {"hybrid": {"weight_sparse": 0.3, "weight_dense": 0.7}}
FUSION = {"fusion": {"weights": "0.5,0.5"}}
TOKEN_SCORES = {"aligned": 0.25, "orthogonal": 0.75, "missing": 0.5}


def chunk(score: float, **fields: Any) -> dict[str, Any]:
    return {"doc_id": "doc", "kb_id": "kb", "docnm_kwd": "doc.txt", "content_with_weight": "text", "content_ltks": "text", "_score": score, **fields}


def dealer_for(backend: str, fields: dict[str, dict[str, Any]], query_vector: list[float] | None = None) -> Dealer:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(db_type=lambda: backend)
    candidates = Dealer.SearchResult(total=len(fields), ids=list(fields), query_vector=[1.0, 0.0] if query_vector is None else query_vector, field=fields)
    dealer.search = AsyncMock(return_value=candidates)
    dealer._existing_doc_ids = AsyncMock(return_value={"doc"})
    # Token similarity is controlled per chunk; vector similarity uses the real cosine path.
    qryr = SimpleNamespace(question=lambda text, **_: (None, [text]), token_similarity=lambda _keywords, docs: [TOKEN_SCORES[tokens[0]] for tokens in docs])
    qryr.hybrid_similarity = MethodType(query.FulltextQueryer.hybrid_similarity, qryr)
    dealer.qryr = qryr
    return dealer


async def scores(dealer: Dealer, mode: dict[str, Any], **options: Any) -> dict[str, tuple[Any, Any, Any]]:
    ranks = await dealer.retrieval("query", "", object(), "tenant", ["kb"], 1, 10, similarity_threshold=0, search_mode=mode, **{"rank_feature": {}, **options})
    return {row["chunk_id"]: (row["similarity"], row["vector_similarity"], row["term_similarity"]) for row in ranks["chunks"]}


@contextmanager
def no_sql() -> Iterator[None]:
    yield None


@contextmanager
def live_document_sql() -> Iterator[SimpleNamespace]:
    # retrieval_by_children reads the live (kb_id, doc_id) pairs in one query.
    yield SimpleNamespace(execute=lambda _statement: SimpleNamespace(tuples=lambda: [("kb", "doc")]))


async def test_milvus_dense_reports_the_store_cosine_as_vector_component() -> None:
    # Without a full-text branch Milvus ranks by the stored COSINE similarity itself.
    dealer = dealer_for("milvus", {"near": chunk(0.9), "orthogonal": chunk(0.0)})
    expected = {"near": (0.9, 0.9, None), "orthogonal": (0.0, 0.0, None)}
    assert await scores(dealer, DENSE) == expected
    # A hybrid mode without lexical weight issues the same dense-only query.
    assert await scores(dealer, {"hybrid": {"weight_sparse": 0, "weight_dense": 1}}) == expected


@pytest.mark.parametrize(
    ("rank_feature", "fields", "boost"),
    [
        pytest.param(None, {PAGERANK_FLD: 30}, 30.0, id="default-pagerank"),
        pytest.param({"finance": 1.0}, {TAG_FLD: {"finance": 1}}, 10.0, id="matching-tag"),
    ],
)
async def test_milvus_rank_features_raise_similarity_above_one_but_not_the_component(rank_feature: dict[str, float] | None, fields: dict[str, Any], boost: float) -> None:
    dealer = dealer_for("milvus", {"boosted": chunk(0.6, **fields)})
    assert await scores(dealer, DENSE, rank_feature=rank_feature) == {"boosted": (pytest.approx(0.6 + boost), 0.6, None)}


@pytest.mark.parametrize(("mode", "score"), [(SPARSE, 12.5), (HYBRID, 0.8), (FUSION, 0.8)], ids=["sparse-bm25", "hybrid", "fusion"])
async def test_milvus_full_text_and_fused_scores_have_no_components(mode: dict[str, Any], score: float) -> None:
    # Raw BM25 is unbounded and fused scores cannot be split into their parts.
    dealer = dealer_for("milvus", {"hit": chunk(score)})
    assert await scores(dealer, mode) == {"hit": (score, None, None)}


@pytest.mark.parametrize("backend", ["infinity", "oceanbase", "vastbase"])
@pytest.mark.parametrize("mode", [DENSE, SPARSE, HYBRID, FUSION], ids=["dense", "sparse", "hybrid", "fusion"])
async def test_single_score_engines_have_no_components(backend: str, mode: dict[str, Any]) -> None:
    # Infinity adds pagerank_fea to its store score, so even a dense similarity can exceed 1.
    dealer = dealer_for(backend, {"hit": chunk(20.8)})
    assert await scores(dealer, mode) == {"hit": (20.8, None, None)}


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
async def test_app_rerank_keeps_computed_components_and_nulls_missing_vectors(backend: str) -> None:
    fields = {
        "aligned": chunk(0.1, content_ltks="aligned"),
        "orthogonal": chunk(0.1, content_ltks="orthogonal"),
        "missing": chunk(0.1, content_ltks="missing"),
    }
    dealer = dealer_for(backend, fields)
    # Candidates carry no vectors; the exact server pass scores cosine + 1, or the
    # no-vector sentinel for a chunk without a stored vector.
    exact = {"aligned": 2.0, "orthogonal": 1.0, "missing": EXACT_SCORE_NO_VECTOR}
    dealer.dataStore.search = lambda _fields, _highlight, condition, *_args: {key: exact[key] for key in condition["id"]}
    dealer.dataStore.get_scores = lambda result: result
    result = await scores(dealer, {"hybrid": {"weight_sparse": 0.5, "weight_dense": 0.5}})
    assert result == {
        "aligned": (pytest.approx(0.625), pytest.approx(1.0), 0.25),
        "orthogonal": (pytest.approx(0.375), pytest.approx(0.0), 0.75),
        # It ranks with a zero placeholder vector but has no vector similarity to report.
        "missing": (pytest.approx(0.25), None, 0.5),
    }


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
async def test_app_rerank_without_query_vector_has_no_components(backend: str) -> None:
    # Sparse-only search has no query vector; the engine's full-text score ranks the chunks.
    dealer = dealer_for(backend, {"hit": chunk(12.5, content_ltks="aligned")}, query_vector=[])
    assert await scores(dealer, SPARSE) == {"hit": (12.5, None, None)}


async def test_rerank_model_components_are_kept_including_zero() -> None:
    fields = {"first": chunk(0.1, content_ltks="aligned"), "second": chunk(0.1, content_ltks="orthogonal")}
    model = SimpleNamespace(similarity=lambda _query, _texts: (np.array([0.9, 0.0]), 0))
    result = await scores(dealer_for("milvus", fields), DENSE, rerank_mdl=model, vector_similarity_weight=0.5)
    assert result == {"first": (pytest.approx(0.575), 0.9, 0.25), "second": (pytest.approx(0.375), 0.0, 0.75)}


def test_component_score_reports_only_finite_values() -> None:
    values = [0.0, np.float32(0.5), None, float("nan"), float("inf")]
    assert [_component_score(values, index) for index in range(len(values))] == [0.0, 0.5, None, None, None]
    assert _component_score(np.array([0.25]), 0) == 0.25
    assert _component_score(None, 0) is None


def test_merged_parent_averages_a_component_only_when_every_child_has_it(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    parent = {"content_with_weight": "parent text", "doc_id": "doc", "kb_id": "kb", "docnm_kwd": "doc.txt"}
    dealer.dataStore = SimpleNamespace(get=lambda *_: dict(parent), db_type=lambda: "milvus")
    monkeypatch.setattr(search, "db_connection", live_document_sql)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda *_args, **_kwargs: [SimpleNamespace(id="kb", tenant_id="tenant", name="kb-name", status="1")])
    child = {"doc_id": "doc", "kb_id": "kb", "content_ltks": "child", "important_kwd": [], "mom_id": "parent"}
    chunks = [
        {**child, "chunk_id": "first", "similarity": 0.2, "vector_similarity": 0.2, "term_similarity": 0.6},
        {**child, "chunk_id": "second", "similarity": 0.4, "vector_similarity": 0.4, "term_similarity": None},
    ]
    [merged] = dealer.retrieval_by_children(chunks, ["tenant"])
    assert merged["chunk_id"] == "parent"
    assert (merged["similarity"], merged["vector_similarity"]) == (pytest.approx(0.3), pytest.approx(0.3))
    # One child without a term score leaves the parent's unavailable instead of averaging the rest.
    assert merged["term_similarity"] is None


async def test_toc_relevance_raises_similarity_and_added_chunks_have_no_components(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    toc = [{"level": 1, "title": "Section", "ids": ["hit", "toc-only"]}]
    added = {"content_ltks": "toc", "content_with_weight": "toc text", "kb_id": "kb", "docnm_kwd": "doc.txt"}
    dealer.dataStore = SimpleNamespace(search=lambda *_args, **_kwargs: object(), get_fields=lambda *_: {"toc": {"content_with_weight": json.dumps(toc)}}, get=lambda *_: dict(added))
    monkeypatch.setattr(search, "db_connection", no_sql)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda *_args, **_kwargs: [SimpleNamespace(tenant_id="tenant", name="kb-name")])
    monkeypatch.setattr(generator, "relevant_chunks_with_toc", AsyncMock(return_value=[("hit", 0.8), ("toc-only", 0.6)]))
    hit = {"chunk_id": "hit", "doc_id": "doc", "kb_id": "kb", "similarity": 0.5, "vector_similarity": 0.5, "term_similarity": 0.0}
    ranked = {row["chunk_id"]: row for row in await dealer.retrieval_by_toc("question", [hit], ["tenant"], ["kb-name"], object())}
    # TOC relevance is added to the ranking score, which can therefore exceed 1.
    assert (ranked["hit"]["similarity"], ranked["hit"]["vector_similarity"], ranked["hit"]["term_similarity"]) == (pytest.approx(1.3), 0.5, 0.0)
    assert (ranked["toc-only"]["similarity"], ranked["toc-only"]["vector_similarity"], ranked["toc-only"]["term_similarity"]) == (0.6, None, None)


async def test_knowledge_graph_summary_has_no_components(monkeypatch: pytest.MonkeyPatch) -> None:
    kg = graph_search.KGSearch.__new__(graph_search.KGSearch)
    monkeypatch.setattr(graph_search, "db_connection", no_sql)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda *_args, **_kwargs: [SimpleNamespace(name="kb-name")])
    kg.query_rewrite = AsyncMock(return_value=([], ["entity"]))
    entity = {"sim": 0.9, "pagerank": 1.0, "description": json.dumps({"description": "known entity"}), "n_hop_ents": []}
    kg.get_relevant_ents_by_keywords = AsyncMock(return_value={"entity": entity})
    kg.get_relevant_ents_by_types = lambda *_: {}
    kg.get_relevant_relations_by_txt = AsyncMock(return_value={})
    kg._community_retrieval_ = lambda *_: ""
    summary = await kg.retrieval("question", "tenant", ["kb"], object(), object())
    assert "known entity" in summary["content_with_weight"]
    assert (summary["similarity"], summary["vector_similarity"], summary["term_similarity"]) == (1.0, None, None)


def test_web_results_have_no_components() -> None:
    tavily = Tavily.__new__(Tavily)
    tavily.tavily_client = SimpleNamespace(search=lambda **_: {"results": [{"url": "https://example.com", "title": "Example", "content": "web text", "score": 0.83}]})
    [row] = tavily.retrieve_chunks("question")["chunks"]
    assert (row["similarity"], row["vector_similarity"], row["term_similarity"]) == (0.83, None, None)


def test_references_keep_a_computed_zero_and_serialize_unavailable_as_null() -> None:
    reference = {"chunks": [{"chunk_id": "computed", "similarity": 0.4, "vector_similarity": 0.0, "term_similarity": None}, {"chunk_id": "web", "similarity": 0.8}]}
    payload = json.loads(json.dumps(chunks_format(reference)))
    assert [(row["similarity"], row["vector_similarity"], row["term_similarity"]) for row in payload] == [(0.4, 0.0, None), (0.8, None, None)]
    # Feedback weighting skips unavailable components instead of failing on them.
    assert _retrieval_signal({"similarity": 0.4, "vector_similarity": None, "term_similarity": None}) == 0.4
