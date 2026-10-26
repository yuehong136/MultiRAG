"""Empty boolean matches stay restricted through the real search coordinator."""

from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db.services.doc_metadata_service import DocMetadataService
from common.doc_store.doc_store_base import DocStoreConnection
from common.metadata_utils import apply_meta_data_filter
from core.nlp.search import Dealer


@pytest.mark.parametrize("empty_position", [0, 1, 2])
@pytest.mark.parametrize("empty_kind", ["missing-field", "absent-value", "disjoint"])
async def test_empty_and_selection_reaches_store_without_widening(db: Session, monkeypatch: pytest.MonkeyPatch, empty_position: int, empty_kind: str) -> None:
    rows = [("one", {"author": "Toby Jones", "pages": 40, "status": "ready"}), ("two", {"author": "Alice", "pages": 10, "status": "ready"})]
    metadata_store = Mock()
    metadata_store.list_by_kb_ids.return_value = deepcopy(rows)
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: metadata_store))
    monkeypatch.setattr(DocMetadataService, "_kb_tenant", classmethod(lambda *_: "owner"))
    monkeypatch.setattr("common.settings.DOC_ENGINE", "milvus")
    conditions = [{"key": "author", "op": "contains", "value": "Toby"}, {"key": "pages", "op": ">", "value": "30"}]
    empty = {
        "missing-field": {"key": "missing", "op": "contains", "value": "anything"},
        "absent-value": {"key": "author", "op": "contains", "value": "Nobody"},
        "disjoint": {"key": "author", "op": "contains", "value": "Alice"},
    }[empty_kind]
    conditions.insert(empty_position, empty)
    before = deepcopy(rows)

    async def load() -> dict[str, Any]:
        return DocMetadataService.get_flatted_meta_by_kbs(db, ["dataset"])

    selected = await apply_meta_data_filter({"method": "manual", "manual": conditions}, metas_loader=load)
    assert selected == ["-999"]

    # The controlled adapter independently evaluates the coordinator's actual
    # predicate against stored rows. Omitting doc_id would expose both documents.
    store = Mock(spec=DocStoreConnection)
    store.db_type.return_value = "milvus"

    def search(*args: Any, **kwargs: Any) -> list[str]:
        predicate = args[2]
        assert predicate == {"kb_id": ["dataset"], "doc_id": ["-999"]}
        return [doc_id for doc_id, _ in rows if doc_id in predicate["doc_id"]]

    store.search.side_effect = search
    store.get_total.side_effect = len
    store.get_doc_ids.side_effect = lambda result: result
    store.get_highlight.return_value = {}
    store.get_aggregation.return_value = []
    store.get_fields.return_value = {}
    dealer = object.__new__(Dealer)
    dealer.dataStore = store
    result = await dealer.search({"doc_ids": selected, "kb_ids": ["dataset"]}, ["owned-index"], ["dataset"])
    assert result.total == 0 and result.ids == []
    store.search.assert_called_once()
    assert rows == before
    metadata_store.list_by_kb_ids.assert_called_once_with(db, "owner", ["dataset"])


async def test_unrestricted_and_explicit_empty_selection_have_distinct_search_effects() -> None:
    store = Mock(spec=DocStoreConnection)
    dealer = object.__new__(Dealer)
    dealer.dataStore = store
    assert await apply_meta_data_filter(None) is None
    assert dealer.get_filters({"doc_ids": None}) == {}
    selected = await apply_meta_data_filter(None, base_doc_ids=[])
    assert selected == []
    assert dealer.get_filters({"doc_ids": selected}) == {"doc_id": []}
    result = await dealer.search({"doc_ids": selected}, ["owned-index"], ["dataset"])
    assert result.total == 0 and result.ids == []
    store.search.assert_not_called()
    store.db_type.assert_not_called()
