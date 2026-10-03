import sys
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from common.doc_store.infinity_conn_base import InfinityConnectionBase
from core.nlp.search import Dealer


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "available_int=0"), (1, "available_int=1"), (None, "1=1"), ("", "1=1"), ([], "1=1"), ({}, "1=1"), ([0, 1], "available_int IN (0, 1)"), ("0", "available_int='0'"), (2, "available_int=2")],
)
def test_availability_filter_preserves_zero_and_existing_value_semantics(value: Any, expected: str) -> None:
    connection = SimpleNamespace(field_keyword=lambda field: False)
    assert InfinityConnectionBase.equivalent_condition_to_str(connection, {"available_int": value}) == expected


def test_zero_availability_combines_with_existing_filters() -> None:
    connection = SimpleNamespace(field_keyword=lambda field: field == "source_id", convert_matching_field=lambda field: field)
    condition = {
        "available_int": 0,
        "doc_id": ["O'Reilly", "other"],
        "source_id": ["source'one", "source-two"],
        "entity_name": "投影直线L'",
        "status": 0,
        "timestamp": 0.0,
        "empty": [],
        123: "ignored",
    }
    assert InfinityConnectionBase.equivalent_condition_to_str(connection, condition) == (
        "available_int=0 AND doc_id IN ('O''Reilly', 'other') AND (filter_fulltext('source_id', 'source''one') or filter_fulltext('source_id', 'source-two')) AND entity_name='投影直线L'''"
    )


@pytest.mark.parametrize("availability", [0, 1, None])
def test_dealer_passes_availability_to_storage_filters(availability: int | None) -> None:
    condition = Dealer.get_filters(None, {"doc_ids": ["doc"], "available_int": availability})
    assert condition == ({"doc_id": ["doc"]} if availability is None else {"doc_id": ["doc"], "available_int": availability})


@pytest.mark.parametrize("availability", [0, 1, None])
def test_chunk_list_passes_explicit_availability_to_retriever(availability: int | None, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    chunk_app = sys.modules["api.apps.chunk"]
    doc = SimpleNamespace(id="doc", kb_id="kb")
    monkeypatch.setattr(chunk_app.DocumentService, "get_tenant_id", lambda *args: "tenant")
    monkeypatch.setattr(chunk_app.DocumentService, "get_by_id", lambda *args: doc)
    monkeypatch.setattr(chunk_app.DocumentService, "serialize_document", lambda *args: {"id": "doc"})
    monkeypatch.setattr(chunk_app.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="kb"))
    monkeypatch.setattr(chunk_app.KnowledgebaseService, "get_kb_ids", lambda *args: ["kb"])
    queries: list[dict[str, Any]] = []

    async def search(query: dict[str, Any], *args: Any, **kwargs: Any) -> SimpleNamespace:
        queries.append(query)
        return SimpleNamespace(total=0, ids=[])

    monkeypatch.setattr(chunk_app.settings, "retriever", SimpleNamespace(search=search), raising=False)
    response = client.post("/v1/chunk/list", json={"doc_id": "doc", "available_int": availability})
    assert response.status_code == 200
    assert response.json()["retcode"] == 0
    assert len(queries) == 1
    assert queries[0]["doc_ids"] == ["doc"]
    if availability is None:
        assert "available_int" not in queries[0]
    else:
        assert queries[0]["available_int"] == availability
