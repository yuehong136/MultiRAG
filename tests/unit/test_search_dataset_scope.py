"""Selected datasets retain paired index identities across retrieval backends."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from common.doc_store.doc_store_base import MatchTextExpr
from core.nlp.search import Dealer, index_name


def test_ambiguous_names_never_expand_as_cartesian_product() -> None:
    with pytest.raises(ValueError, match="binding"):
        index_name(["owner-a", "owner-b"], ["one", "two", "three"])


async def test_retrieval_uses_dataset_bindings_instead_of_tenant_name_cross_product(monkeypatch: pytest.MonkeyPatch) -> None:
    dealer = Dealer.__new__(Dealer)
    dealer.dataStore = SimpleNamespace(db_type=lambda: "milvus")
    bindings = [("multirag_a_one", "one-id"), ("multirag_a_two", "two-id"), ("multirag_b_three", "three-id")]
    fetch = AsyncMock(return_value=Dealer.SearchResult(total=0, ids=[], field={}))
    monkeypatch.setattr(dealer, "search", fetch)
    await dealer.retrieval("query", "", object(), ["a", "a", "b"], ["one", "two", "three"], 1, 10, kb_ids=["one-id", "two-id", "three-id"])
    assert fetch.call_args.args[1] == [item[0] for item in bindings]
    assert fetch.call_args.args[0]["kb_ids"] == [item[1] for item in bindings]
    assert fetch.call_args.args[0]["dataset_indices"] == bindings


async def test_infinity_multi_dataset_queries_only_bound_tables() -> None:
    calls: list[tuple[Any, ...]] = []
    dealer = Dealer.__new__(Dealer)
    dealer.qryr = SimpleNamespace(question=lambda *_a, **_k: (MatchTextExpr(["content_ltks"], "query", 10), []))

    def fetch(*args: Any, **_: Any) -> dict[str, Any]:
        calls.append(args)
        identifier = args[7][0]
        return {identifier: {"doc_id": identifier, "kb_id": args[8][0], "_score": 0.9 if identifier == "index-b" else 0.5}}

    dealer.dataStore = SimpleNamespace(
        db_type=lambda: "infinity", search=fetch, get_total=len, get_doc_ids=list, get_fields=lambda result, _: result, get_highlight=lambda *_: {}, get_aggregation=lambda *_: []
    )
    result = await dealer.search({"question": "query", "kb_ids": ["kb-a", "kb-b"], "search_mode": {"sparse": {}}, "page": 2, "size": 1}, ["index-a", "index-b"], ["kb-a", "kb-b"])
    assert [(args[7], args[8], args[2]["kb_id"]) for args in calls] == [(["index-a"], ["kb-a"], ["kb-a"]), (["index-b"], ["kb-b"], ["kb-b"])]
    assert result.ids == ["index-a"] and result.total == 2


@pytest.mark.parametrize("dataset_ids", [[], ["one-id", "two-id"]])
async def test_explicit_dataset_selection_never_broadens_when_binding_is_missing(monkeypatch: pytest.MonkeyPatch, dataset_ids: list[str]) -> None:
    dealer = Dealer.__new__(Dealer)
    fetch = AsyncMock()
    monkeypatch.setattr(dealer, "search", fetch)
    if dataset_ids:
        with pytest.raises(ValueError, match="one tenant/name"):
            await dealer.retrieval("query", "", object(), ["owner"], ["one"], 1, 10, kb_ids=dataset_ids)
    else:
        result = await dealer.retrieval("query", "", object(), ["owner"], ["one"], 1, 10, kb_ids=dataset_ids)
        assert result["total"] == 0 and result["chunks"] == []
    fetch.assert_not_awaited()
