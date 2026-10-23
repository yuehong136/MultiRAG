"""Full generic reads retain caps, offsets, scope and operational failures."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from core.nlp import search
from core.nlp.search import Dealer


@pytest.fixture
def chunk_dealer(monkeypatch: pytest.MonkeyPatch) -> Dealer:
    @contextmanager
    def connection() -> Iterator[object]:
        yield object()

    monkeypatch.setattr(search, "db_connection", connection)
    from api.db.services.knowledgebase_service import KnowledgebaseService

    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", lambda *_: [SimpleNamespace(name="name", tenant_id="owner")])
    dealer = Dealer.__new__(Dealer)
    data = {str(number): {"content_with_weight": str(number)} for number in range(257)}

    def fetch(*args: Any) -> dict[str, Any]:
        offset, limit = args[5:7]
        assert args[2] == {"doc_id": "doc"} and args[7:9] == (["multirag_owner_name"], ["kb"])
        return dict(list(data.items())[offset : offset + limit])

    def fields(result: Any, _fields: list[str]) -> dict[str, Any]:
        return {row["pk"]: {"content_with_weight": row["content_with_weight"]} for row in result} if isinstance(result, list) else result

    dealer.dataStore = SimpleNamespace(
        db_type=lambda: "milvus",
        search=Mock(side_effect=fetch),
        get_fields=fields,
        get_total=lambda _: len(data),
        graph_source_rows=Mock(return_value=[{"pk": key, **row} for key, row in data.items()]),
    )
    return dealer


@pytest.mark.parametrize("backend", ["milvus", "infinity"])
def test_default_cap_full_read_and_offset(chunk_dealer: Dealer, backend: str) -> None:
    chunk_dealer.dataStore.db_type = lambda: backend
    assert len(chunk_dealer.chunk_list("doc", "owner", ["kb"], max_count=130)) == 130
    complete = chunk_dealer.chunk_list("doc", "owner", ["kb"], max_count=1, retrieve_all=True)
    assert [row["id"] for row in complete] == [str(number) for number in range(257)]
    tail = chunk_dealer.chunk_list("doc", "owner", ["kb"], offset=128, retrieve_all=True)
    assert [row["id"] for row in tail] == [str(number) for number in range(128, 257)]


@pytest.mark.parametrize("failure", ["repeated", "incomplete", "storage"])
def test_full_read_never_silently_accepts_partial_or_repeated_pages(chunk_dealer: Dealer, failure: str) -> None:
    chunk_dealer.dataStore.db_type = lambda: "infinity"
    original = chunk_dealer.dataStore.search
    first = {str(number): {"content_with_weight": str(number)} for number in range(128)}
    if failure == "repeated":
        original.side_effect = lambda *_: first.copy()
    elif failure == "incomplete":
        original.side_effect = [first.copy(), {}]
    else:
        original.side_effect = RuntimeError("storage failed")
    with pytest.raises(RuntimeError):
        chunk_dealer.chunk_list("doc", "owner", ["kb"], retrieve_all=True)


def test_empty_and_foreign_binding_do_not_issue_storage_queries(chunk_dealer: Dealer) -> None:
    assert chunk_dealer.chunk_list("doc", "owner", [], retrieve_all=True) == []
    with pytest.raises(PermissionError):
        chunk_dealer.chunk_list("doc", "foreign", ["kb"], retrieve_all=True)
    with pytest.raises(ValueError, match="one dataset"):
        chunk_dealer.chunk_list("doc", "owner", ["a", "b"], retrieve_all=True)
    chunk_dealer.dataStore.search.assert_not_called()
