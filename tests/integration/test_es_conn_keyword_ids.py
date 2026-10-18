"""Real chunk/memory queries on owned indices in a disposable ES listener.

The existing service manager prepares METADATA_TEST_ES_URL before collection.
No application endpoint or existing index is used or migrated.
"""

import copy
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch

from common.doc_store.doc_store_base import MatchTextExpr, OrderByExpr
from core.utils import es_conn as core_es
from memory.utils import es_conn as memory_es


@pytest.fixture(params=["core", "memory"])
def es_store(request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    url = os.environ.get("METADATA_TEST_ES_URL")
    assert url, "A disposable ES endpoint must be prepared; missing ES is not a skip"
    with ExitStack() as resources:
        writer = resources.enter_context(Elasticsearch(url, request_timeout=30))
        reader = resources.enter_context(Elasticsearch(url, request_timeout=30))
        assert reader.info()["version"]["number"].startswith("8.")
        backend = request.param
        factory = core_es.ESConnection if backend == "core" else memory_es.ESConnection
        cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
        # Bind the real transport without initializing the application singleton
        # pool. The inherited create_idx and both query implementations are real.
        conn = object.__new__(cls)
        conn.es = writer
        conn.logger = logging.getLogger(__name__)
        conn.mapping = json.loads(Path("configs/es_mapping.json").read_text())

        def remove_index(index: str) -> None:
            if reader.indices.exists(index=index):
                reader.indices.delete(index=index)
            assert not reader.indices.exists(index=index)

        def create_index(mapping: dict[str, Any] | None = None) -> str:
            index = f"multirag_test_keyword_ids_{uuid4().hex}"
            resources.callback(remove_index, index)
            original_mapping = conn.mapping
            try:
                if mapping is not None:
                    conn.mapping = mapping
                result = conn.create_idx(index, "scope", 0)
                assert result and result["acknowledged"]
            finally:
                conn.mapping = original_mapping
            assert reader.indices.exists(index=index)
            return index

        yield {"backend": backend, "conn": conn, "writer": writer, "reader": reader, "create_index": create_index}


def _write(env: dict[str, Any], index: str, documents: list[dict[str, Any]]) -> None:
    operations: list[dict[str, Any]] = []
    for document in documents:
        operations.extend([{"index": {"_index": index, "_id": document["id"]}}, {"kb_id": "scope", "memory_id": "scope", **document}])
    result = env["writer"].bulk(operations=operations, refresh=True)
    assert result["errors"] is False
    assert all(item["index"]["status"] in {200, 201} for item in result["items"])
    # Independent client readback, rather than trusting a successful bulk call.
    for document in documents:
        source = env["reader"].get(index=index, id=document["id"])["_source"]
        assert all(source[key] == value for key, value in document.items())


def _search(
    env: dict[str, Any],
    indexes: list[str],
    order: OrderByExpr | None = None,
    condition: dict[str, Any] | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    expressions = [MatchTextExpr(["content_ltks"], text, 10)] if text else []
    result = env["conn"].search(["id", "docnm_kwd", "create_timestamp_flt"], [], condition or {}, expressions, order or OrderByExpr(), 0, 10, indexes, ["scope"])
    if env["backend"] == "memory":
        result, count = result
        assert result["hits"]["total"]["value"] == count
    assert result["timed_out"] is False
    assert result["_shards"]["failed"] == 0
    return result


def _ids(result: dict[str, Any]) -> list[str]:
    return [hit["_id"] for hit in result["hits"]["hits"]]


def test_new_index_preserves_exact_ids_for_filtering_and_raw_sort(es_store: dict[str, Any]) -> None:
    index = es_store["create_index"]()
    ids = ["Chunk-ID_01", "chunk-id_01", "Chunk-ID_010"]
    documents = [{"id": value, "content_ltks": "needle", "docnm_kwd": value} for value in ids]
    documents.append({"id": "foreign", "kb_id": "other", "memory_id": "other", "content_ltks": "needle"})
    _write(es_store, index, documents)
    reader = es_store["reader"]
    mapping = reader.indices.get_mapping(index=index)[index]["mappings"]["properties"]
    assert mapping["id"]["type"] == "keyword"
    assert mapping["docnm_kwd"]["type"] == "keyword"
    assert mapping["kb_id"]["type"] == mapping["memory_id"]["type"] == "keyword"
    for value in ids:
        assert _ids(_search(es_store, [index], condition={"id": value})) == [value]
    assert set(_ids(_search(es_store, [index], condition={"id": ids[:2]}))) == set(ids[:2])
    assert _ids(_search(es_store, [index], condition={"id": "Chunk"})) == []
    assert set(_ids(_search(es_store, [index], text="needle"))) == set(ids)
    # Application queries intentionally continue skipping id sorting for legacy
    # indices; a direct read proves that a new index's id is actually sortable.
    sorted_result = reader.search(index=index, query={"term": {"kb_id": "scope"}}, sort=[{"id": "asc"}])
    assert _ids(sorted_result) == sorted(ids)
    assert reader.count(index=index)["count"] == len(documents)


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_keyword_and_numeric_sort_across_unmapped_indices(es_store: dict[str, Any], direction: str) -> None:
    create_index: Callable[[], str] = es_store["create_index"]
    mapped, unmapped = create_index(), create_index()
    _write(es_store, mapped, [{"id": "b", "docnm_kwd": "Bravo", "create_timestamp_flt": 2}, {"id": "a", "docnm_kwd": "Alpha", "create_timestamp_flt": 1}])
    _write(es_store, unmapped, [{"id": "missing", "create_timestamp_flt": 3}])
    properties = es_store["reader"].indices.get_mapping(index=unmapped)[unmapped]["mappings"]["properties"]
    assert "docnm_kwd" not in properties
    order = getattr(OrderByExpr(), direction)("docnm_kwd").asc("create_timestamp_flt")
    result = _search(es_store, [mapped, unmapped], order)
    expected = ["a", "b", "missing"] if direction == "asc" else ["b", "a", "missing"]
    assert _ids(result) == expected
    # A wholly absent string field must also be sortable; numeric order remains
    # numeric (and id is still omitted from mixed sorting).
    order = OrderByExpr().asc("id").asc("absent_kwd").desc("create_timestamp_flt")
    assert _ids(_search(es_store, [mapped, unmapped], order)) == ["missing", "b", "a"]
    assert es_store["reader"].count(index=[mapped, unmapped])["count"] == 3


def test_existing_text_id_index_is_not_sorted_or_migrated(es_store: dict[str, Any]) -> None:
    legacy = copy.deepcopy(es_store["conn"].mapping)
    legacy["mappings"]["properties"]["id"] = {"type": "text"}
    index = es_store["create_index"](legacy)
    _write(es_store, index, [{"id": "Old-ID_B", "docnm_kwd": "Bravo"}, {"id": "Old-ID_A", "docnm_kwd": "Alpha"}])
    before = es_store["reader"].indices.get_mapping(index=index)[index]["mappings"]
    assert before["properties"]["id"]["type"] == "text"
    assert es_store["conn"].create_idx(index, "scope", 0) is True
    assert set(_ids(_search(es_store, [index], OrderByExpr().asc("id")))) == {"Old-ID_A", "Old-ID_B"}
    assert _ids(_search(es_store, [index], OrderByExpr().desc("id").asc("docnm_kwd"))) == ["Old-ID_A", "Old-ID_B"]
    assert es_store["reader"].indices.get_mapping(index=index)[index]["mappings"] == before
    assert es_store["reader"].count(index=index)["count"] == 2


@pytest.mark.parametrize("es_store", ["memory"], indirect=True)
@pytest.mark.parametrize("method", ["get_forgotten_messages", "get_missing_field_message"])
def test_memory_maintenance_handles_unmapped_dates(es_store: dict[str, Any], method: str) -> None:
    index = es_store["create_index"]()
    _write(es_store, index, [{"id": "message-without-dates"}])
    args = (["id"], index, "scope")
    if method == "get_missing_field_message":
        args += ("content_ltks",)
    result = getattr(es_store["conn"], method)(*args)
    assert result["_shards"]["failed"] == 0
    assert _ids(result) == ([] if method == "get_forgotten_messages" else ["message-without-dates"])
    # Once mapped, the real date type still controls chronological ordering.
    _write(
        es_store,
        index,
        [
            {"id": "late", "valid_at": "2026-10-18 10:00:00", "forget_at": "2026-10-18 10:00:00"},
            {"id": "early", "valid_at": "2026-10-17 10:00:00", "forget_at": "2026-10-17 10:00:00"},
        ],
    )
    result = getattr(es_store["conn"], method)(*args)
    assert result["_shards"]["failed"] == 0
    expected = ["early", "late"]
    if method == "get_missing_field_message":
        expected.append("message-without-dates")
    assert _ids(result) == expected
