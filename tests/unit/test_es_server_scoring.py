"""Candidate scoring and late citation reads must retain authorization and errors."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from api.db.services.dialog_service import _hydrate_chunk_vectors
from common import settings
from common.doc_store.doc_store_base import EXACT_SCORE_NO_VECTOR
from common.doc_store.es_conn_base import ESConnectionBase
from core.nlp.search import Dealer
from core.utils.opensearch_conn import OSConnection


def _os_class() -> type:
    return next(cell.cell_contents for cell in OSConnection.__closure__ if isinstance(cell.cell_contents, type))


@pytest.mark.parametrize("reader", [ESConnectionBase.get_scores, _os_class().get_scores])
@pytest.mark.parametrize("raw", [None, True, "0.5", float("nan"), float("inf")])
def test_missing_or_invalid_scores_are_failures(reader: Any, raw: Any) -> None:
    with pytest.raises(ValueError):
        reader({"hits": {"hits": [{"_id": "candidate", "_score": raw}]}})


@pytest.mark.parametrize("reader", [ESConnectionBase.get_scores, _os_class().get_scores])
def test_zero_and_empty_scores_are_valid_but_duplicate_hits_fail(reader: Any) -> None:
    assert reader({"hits": {"hits": []}}) == {}
    hit = {"_id": "candidate", "_score": 0.0}
    assert reader({"hits": {"hits": [hit]}}) == {"candidate": 0.0}
    with pytest.raises(ValueError):
        reader({"hits": {"hits": [hit, hit]}})


async def test_score_pass_keeps_paired_indexes_candidates_and_scalar_predicates() -> None:
    dealer = Dealer.__new__(Dealer)
    fetch = Mock(side_effect=[{"one": 1.25}, {"two": 0.5}])
    dealer.dataStore = SimpleNamespace(search=fetch, get_scores=lambda result: result)
    result = Dealer.SearchResult(total=2, ids=["one", "two"], query_vector=[1.0, 0.0], field={"one": {"kb_id": "a"}, "two": {"kb_id": "b"}})
    scores = await dealer._knn_scores(result, ["index-a", "index-b"], ["a", "b"], {"doc_id": ["doc"], "available_int": 1, "auth": "scope"})
    assert scores == {"one": 0.25, "two": -0.5}
    for call, index, kb, identifier in zip(fetch.call_args_list, ["index-a", "index-b"], ["a", "b"], ["one", "two"], strict=True):
        assert call.args[0] == []
        assert call.args[2] == {"doc_id": ["doc"], "available_int": 1, "auth": "scope", "id": [identifier]}
        assert call.args[7:9] == ([index], [kb])


@pytest.mark.parametrize("raw", [-0.5, 2.5, 3.5])
async def test_score_pass_reports_only_vector_cosines_and_the_no_vector_sentinel(raw: float) -> None:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(search=Mock(return_value={"orthogonal": 1.0, "missing": EXACT_SCORE_NO_VECTOR}), get_scores=lambda result: result)
    result = Dealer.SearchResult(total=2, ids=["orthogonal", "missing"], query_vector=[1.0, 0.0], field={"orthogonal": {"kb_id": "a"}, "missing": {"kb_id": "a"}})
    # An orthogonal vector is a computed 0.0; a chunk without a vector has no score to report.
    assert await dealer._knn_scores(result, ["index-a"], ["a"], {}) == {"orthogonal": 0.0, "missing": None}
    # Only the sentinel itself widens the cosine + 1 range.
    dealer.dataStore.search = Mock(return_value={"orthogonal": raw})
    with pytest.raises(ValueError, match="invalid cosine"):
        await dealer._knn_scores(result, ["index-a"], ["a"], {})


async def test_empty_scope_does_not_issue_a_score_or_vector_read() -> None:
    dealer = Dealer.__new__(Dealer)
    fetch = Mock(side_effect=AssertionError("empty selection must not broaden"))
    dealer.dataStore = SimpleNamespace(search=fetch)
    assert await dealer._knn_scores(Dealer.SearchResult(1, ["one"], [1.0], {"one": {"kb_id": "a"}}), [], [], {}) == {}
    assert await dealer.fetch_chunk_vectors(["one"], "tenant", [], 1, kb_names=[], doc_ids=["doc"]) == {}
    fetch.assert_not_called()


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
async def test_citations_hydrate_only_authorized_candidates_and_parent_child_vector(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setattr(settings, "DOC_ENGINE", backend)
    fetch = AsyncMock(return_value={"child": [1.0, 0.0]})
    retriever = SimpleNamespace(fetch_chunk_vectors=fetch)
    chunks = [
        {"chunk_id": "parent", "_citation_vector_chunk_id": "child", "doc_id": "doc", "kb_id": "kb", "vector": [0.0, 0.0]},
        {"chunk_id": "inline", "doc_id": "doc", "kb_id": "kb", "vector": [0.5, 0.5]},
        {"chunk_id": "web", "doc_id": "url", "vector": [0.0, 0.0]},
        {"chunk_id": "foreign", "doc_id": "private", "kb_id": "foreign", "vector": [0.0, 0.0]},
    ]
    await _hydrate_chunk_vectors(retriever, chunks, [SimpleNamespace(id="kb", name="name", tenant_id="owner")])
    fetch.assert_awaited_once_with(["child"], "owner", ["kb"], 2, kb_names=["name"], doc_ids=["doc"])
    assert chunks[0]["vector"] == [1.0, 0.0] and chunks[-1]["vector"] == [0.0, 0.0]


@pytest.mark.parametrize("outcome", [RuntimeError("unavailable"), {}])
async def test_citation_read_failure_or_disappeared_chunk_is_not_fake_success(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> None:
    monkeypatch.setattr(settings, "DOC_ENGINE", "elasticsearch")
    fetch = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
    chunks = [{"chunk_id": "one", "doc_id": "doc", "kb_id": "kb", "vector": [0.0]}]
    with pytest.raises((RuntimeError, LookupError)):
        await _hydrate_chunk_vectors(SimpleNamespace(fetch_chunk_vectors=fetch), chunks, [SimpleNamespace(id="kb", name="name", tenant_id="owner")])


@pytest.mark.parametrize("backend", ["milvus", "infinity", "oceanbase"])
async def test_other_engines_do_not_fetch_citation_vectors(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setattr(settings, "DOC_ENGINE", backend)
    fetch = AsyncMock(side_effect=AssertionError("unexpected vector fetch"))
    await _hydrate_chunk_vectors(SimpleNamespace(fetch_chunk_vectors=fetch), [{"chunk_id": "one", "vector": [0.0]}], [])
    fetch.assert_not_awaited()
