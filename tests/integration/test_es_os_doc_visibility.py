"""Owned real ES/OS writes, immediate independent search, and metadata CRUD."""

from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from tests.support.search_metadata import search_store as search_store


def test_bulk_success_is_immediately_retrievable(search_store: dict[str, Any]) -> None:
    env = search_store
    index = env["index_name"]()
    env["writer"].indices.create(index=index, body={"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "500ms"}})
    for iteration in range(4):
        identifier = f"chunk-{iteration}"
        assert env["store"].insert([{"id": identifier, "doc_id": "doc", "kb_id": "kb", "content_with_weight": "ready now"}], index, "kb") == []
        result = env["reader"].search(index=index, body={"query": {"term": {"doc_id.keyword": "doc"}}})
        assert result["hits"]["total"]["value"] == iteration + 1
        assert identifier in {hit["_id"] for hit in result["hits"]["hits"]}


def test_rejected_bulk_is_not_success(search_store: dict[str, Any]) -> None:
    env = search_store
    index = env["index_name"]()
    env["writer"].indices.create(index=index, body={"settings": {"number_of_replicas": 0, "blocks.write": True}})
    assert env["store"].insert([{"id": "rejected", "doc_id": "doc", "kb_id": "kb"}], index, "kb")
    assert env["reader"].count(index=index)["count"] == 0


def test_metadata_crud_has_complete_replacement_and_scoped_readback(search_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = search_store
    index = env["index_name"]("multirag_doc_meta_test_after058_d_")
    tenant = index.removeprefix("multirag_doc_meta_")
    monkeypatch.setitem(settings.__dict__, "docStoreConn", env["store"])
    monkeypatch.setattr(settings, "DOC_ENGINE", env["backend"])
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    monkeypatch.setattr(settings, "DOC_ENGINE_OCEANBASE", False)
    store = EngineMetadataStore()
    original = {"removed": "old", "keep": ["Ada", "Bob"], "enabled": False, "number": 0}
    replacement = {"keep": ["Ada"], "enabled": False, "number": 0}
    with Session() as db:
        assert store.upsert(db, "doc", tenant, "kb", original) is True
        mapping = env["reader"].indices.get_mapping(index=index)[index]["mappings"]
        assert mapping["dynamic"] == ("runtime" if env["backend"] == "elasticsearch" else "true")
        assert env["reader"].get(index=index, id="doc")["_source"] == {"id": "doc", "kb_id": "kb", "meta_fields": original}
        assert env["store"].count_idx(index) == 1
        assert store.upsert(db, "doc", tenant, "kb", replacement) is True
        assert env["reader"].get(index=index, id="doc")["_source"]["meta_fields"] == replacement
        assert env["reader"].search(index=index, body={"query": {"exists": {"field": "meta_fields.removed"}}})["hits"]["total"]["value"] == 0
        # The original metadata service calls search with these snake_case keywords.
        result = env["store"].search(
            select_fields=["*"],
            highlight_fields=[],
            condition={"id": ["doc"]},
            match_expressions=[],
            order_by=OrderByExpr().asc("id"),
            offset=0,
            limit=10,
            index_names=index,
            knowledgebase_ids=["kb"],
        )
        assert result["hits"]["total"]["value"] == 1
        assert store.list_by_kb_ids(db, tenant, ["kb"]) == [("doc", replacement)]
        assert store.list_by_kb_ids(db, tenant, ["foreign"]) == []
        assert env["store"].get_doc_metadata_rows(index, ["doc", "absent"], "kb") == [{"id": "doc", "kb_id": "kb", "meta_fields": replacement}]
        # A permanent mapping rejection must not fall back to delete+insert.
        assert store.upsert(db, "doc", tenant, "kb", {"number": {"invalid": "object"}}) is False
        assert env["reader"].get(index=index, id="doc")["_source"]["meta_fields"] == replacement
        assert store.upsert(db, "doc", tenant, "kb", {}) is True
        assert env["reader"].get(index=index, id="doc")["_source"]["meta_fields"] == {}
        assert store.delete(db, "doc", tenant, "kb") is True
        assert env["reader"].count(index=index)["count"] == 0
        store._drop_if_empty(index)
        assert not env["reader"].indices.exists(index=index)


def test_closed_index_refresh_and_count_fail_closed(search_store: dict[str, Any]) -> None:
    env = search_store
    index = env["index_name"]()
    assert env["store"].create_doc_meta_idx(index) is True
    env["writer"].indices.close(index=index)
    assert env["store"].refresh_idx(index) is False
    assert env["store"].count_idx(index) == -1
    assert env["reader"].indices.exists(index=index)
