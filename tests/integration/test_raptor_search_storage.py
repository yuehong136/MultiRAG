"""RAPTOR replacement through the real ES connector and independent client."""

import copy
from typing import Any

import pytest

from common.doc_store.document_history import document_history
from core.utils.raptor_migration import replace_raptor_summaries, summary_rows
from tests.support.search_metadata import search_store as search_store


@pytest.mark.parametrize("search_store", ["elasticsearch"], indirect=True)
def test_raptor_es_wait_for_and_canonical_identity(search_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_store
    index = env["index_name"]("raptor_g_es_")
    env["writer"].indices.create(
        index=index,
        body={
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {"doc_id": {"type": "keyword"}, "kb_id": {"type": "keyword"}, "raptor_kwd": {"type": "keyword"}, "q_2_vec": {"type": "dense_vector", "dims": 2, "index": False}}
            },
        },
    )
    store = env["store"]
    ordinary = {"id": "ordinary", "doc_id": "doc", "kb_id": "kb", "content_with_weight": "ordinary", "q_2_vec": [0.1, 0.2]}
    legacy = {**ordinary, "id": "legacy", "raptor_kwd": "raptor", "content_with_weight": "legacy"}
    assert store.insert([ordinary, legacy], index, "kb") == []
    assert store.insert([{**ordinary, "id": "foreign"}], index, "foreign-kb") == []
    previous = {"doc": summary_rows(document_history(store, index, "kb", "doc"))}
    fresh = {**legacy, "id": "fresh", "kb_id": ["kb"], "content_with_weight": "Psi summary", "extra": {"raptor_method": "psi"}, "raptor_layer_int": 1}
    replace_raptor_summaries(store, index, "kb", [fresh], previous, lambda: False, 1)
    # Immediate independent search verifies the connector's visibility contract.
    rows = env["reader"].search(index=index, body={"query": {"term": {"kb_id": "kb"}}})["hits"]["hits"]
    assert {row["_id"] for row in rows} == {"ordinary", "fresh"}
    assert env["reader"].get(index=index, id="ordinary")["_source"] == ordinary
    assert env["reader"].get(index=index, id="fresh")["_source"]["kb_id"] == "kb"
    assert env["reader"].get(index=index, id="foreign")["_source"]["kb_id"] == "foreign-kb"
    before = document_history(store, index, "kb", "doc")
    original = store.insert
    injected = False

    def missing_receipt(*args: Any, **kwargs: Any) -> Any:
        nonlocal injected
        result = original(*args, **kwargs)
        if not injected:
            injected = True
            return None
        return result

    monkeypatch.setattr(store, "insert", missing_receipt)
    with pytest.raises(RuntimeError, match="acknowledged"):
        replace_raptor_summaries(store, index, "kb", [{**copy.deepcopy(fresh), "id": "failed"}], {"doc": summary_rows(before)}, lambda: False, 1)
    assert document_history(store, index, "kb", "doc") == before
