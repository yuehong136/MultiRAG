"""Strict destructive history paths against a real, owned Infinity database."""

import inspect
import logging
import os
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import infinity
import pytest
from infinity.common import ConflictType, NetworkAddress

from common.config_utils import CONFIGS
from common.doc_store.document_history import delete_document_history, document_history, document_index_exists, restore_document_history
from core.utils.infinity_conn import InfinityConnection
from tests.support.resource_journal import ResourceJournal


class OwnedPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.acquired = 0
        self.released = 0

    def get_conn(self) -> Any:
        self.acquired += 1
        return self.connection

    def release_conn(self, connection: Any) -> None:
        assert connection is self.connection
        self.released += 1


@pytest.fixture
def native_history() -> Iterator[tuple[Any, Any, ResourceJournal]]:
    host, port = os.environ.get("INFINITY_TEST_URI", CONFIGS["infinity"]["uri"]).rsplit(":", 1)
    name = "after325_history_" + uuid4().hex
    journal = ResourceJournal("after325-native-infinity-history")
    journal.event("reserve-infinity-database", endpoint={"host": host, "port": int(port)}, database=name, tables=["chunks_kb", "chunks_foreign", "missing_kb"])
    writer = infinity.connect(NetworkAddress(host, int(port)))
    reader = None
    pool = OwnedPool(writer)
    try:
        reader = infinity.connect(NetworkAddress(host, int(port)))
        writer.create_database(name, ConflictType.Error)
        cls = inspect.getclosurevars(InfinityConnection).nonlocals["cls"]
        store = object.__new__(cls)
        store.logger = logging.getLogger(__name__)
        store.dbName, store.connPool = name, pool
        database = writer.get_database(name)
        for table_name in ["chunks_kb", "chunks_foreign"]:
            table = database.create_table(
                table_name, {"id": {"type": "varchar"}, "doc_id": {"type": "varchar"}, "kb_id": {"type": "varchar"}, "content_with_weight": {"type": "varchar"}, "q_2_vec": {"type": "vector,2,float"}}
            )
            table.insert(
                [
                    {"id": "selected", "doc_id": "doc", "kb_id": "kb", "content_with_weight": table_name, "q_2_vec": [0.2, 0.4]},
                    {"id": "sibling", "doc_id": "other", "kb_id": "kb", "content_with_weight": "keep", "q_2_vec": [0.6, 0.8]},
                ]
            )
        yield store, reader.get_database(name), journal
    finally:
        try:
            assert pool.acquired == pool.released
            writer.get_database("default_db")
            if reader is not None:
                reader.get_database("default_db")
            writer.drop_database(name, ConflictType.Ignore)
            if reader is not None:
                assert name not in reader.list_databases().db_names
            journal.event("infinity-database-independent-cleanup-readback", database=name, absent=True, acquired=pool.acquired, released=pool.released)
        finally:
            if reader is not None:
                reader.disconnect()
            writer.disconnect()


def _native_rows(database: Any, table_name: str) -> list[dict[str, Any]]:
    frame, _ = database.get_table(table_name).output(["*"]).to_df()
    return sorted([{key: value.tolist() if hasattr(value, "tolist") else value for key, value in row.items()} for row in frame.to_dict(orient="records")], key=lambda row: row["id"])


def test_infinity_missing_table_and_existing_delete_restore_read_back_natively(native_history: tuple[Any, Any, ResourceJournal]) -> None:
    store, reader, journal = native_history
    before = _native_rows(reader, "chunks_kb")
    foreign = _native_rows(reader, "chunks_foreign")
    assert document_index_exists(store, "missing", "kb") is False
    delete_document_history(store, "missing", "kb", "doc")
    assert document_history(store, "missing", "kb", "doc") == []
    assert document_index_exists(store, "chunks", "kb") is True
    saved = document_history(store, "chunks", "kb", "doc")
    assert [row["id"] for row in saved] == ["selected"]
    delete_document_history(store, "chunks", "kb", "doc", ids=["selected"])
    assert _native_rows(reader, "chunks_kb") == [row for row in before if row["id"] == "sibling"]
    assert _native_rows(reader, "chunks_foreign") == foreign
    restore_document_history(store, "chunks", "kb", "doc", saved)
    assert _native_rows(reader, "chunks_kb") == before
    delete_document_history(store, "chunks", "kb", "doc")
    delete_document_history(store, "chunks", "kb", "doc")
    assert _native_rows(reader, "chunks_kb") == [row for row in before if row["id"] == "sibling"]
    assert _native_rows(reader, "chunks_foreign") == foreign
    journal.event(
        "infinity-native-history-independent-readback", missing_table_skipped=True, selected_rows_removed=True, sibling_and_foreign_unchanged=True, vectors_restored=True, repeated_delete=True
    )


def test_elasticsearch_native_cleanup_keeps_sibling_and_foreign_dataset() -> None:
    from elasticsearch import Elasticsearch

    from common import settings
    from core.utils.es_conn import ESConnection

    endpoint = os.environ.get("METADATA_TEST_ES_URL")
    assert endpoint, "METADATA_TEST_ES_URL must identify the isolated ES test endpoint"
    cfg = settings.ES
    username = os.environ.get("METADATA_TEST_ES_USERNAME", cfg.get("username"))
    password = os.environ.get("METADATA_TEST_ES_PASSWORD", cfg.get("password"))
    auth = (username, password) if username and password else None
    index, missing = "after325-history-" + uuid4().hex, "after325-missing-" + uuid4().hex
    journal = ResourceJournal("after325-native-es-history")
    journal.event("reserve-elasticsearch-index", index=index, missing_probe=missing, endpoint_identity="METADATA_TEST_ES_URL", document_ids=["doc", "sibling"], dataset_ids=["kb", "foreign"])
    with Elasticsearch(endpoint, basic_auth=auth, request_timeout=5) as writer, Elasticsearch(endpoint, basic_auth=auth, request_timeout=5) as reader:
        cls = inspect.getclosurevars(ESConnection).nonlocals["cls"]
        store = object.__new__(cls)
        store.logger, store.es = logging.getLogger(__name__), writer
        writer.info()
        try:
            writer.indices.create(index=index, settings={"number_of_shards": 1, "number_of_replicas": 0}, mappings={"properties": {"doc_id": {"type": "keyword"}, "kb_id": {"type": "keyword"}}})
            rows = [
                {"id": "selected", "doc_id": "doc", "kb_id": "kb", "content_with_weight": "selected", "q_2_vec": [0.2, 0.4]},
                {"id": "sibling", "doc_id": "sibling", "kb_id": "kb", "content_with_weight": "sibling"},
                {"id": "foreign", "doc_id": "doc", "kb_id": "foreign", "content_with_weight": "foreign"},
            ]
            for row in rows:
                writer.index(index=index, id=row["id"], document=row)
            writer.indices.refresh(index=index)

            def native_rows() -> list[dict[str, Any]]:
                return sorted([row["_source"] for row in reader.search(index=index, query={"match_all": {}}, size=100)["hits"]["hits"]], key=lambda row: row["id"])

            before = native_rows()
            assert document_index_exists(store, missing, "kb") is False
            delete_document_history(store, missing, "kb", "doc")
            saved = document_history(store, index, "kb", "doc")
            assert [row["id"] for row in saved] == ["selected"]
            delete_document_history(store, index, "kb", "doc", ids=["selected"])
            assert native_rows() == [row for row in before if row["id"] != "selected"]
            restore_document_history(store, index, "kb", "doc", saved)
            assert native_rows() == before
            delete_document_history(store, index, "kb", "doc")
            delete_document_history(store, index, "kb", "doc")
            assert native_rows() == [row for row in before if row["id"] != "selected"]
            journal.event("elasticsearch-native-history-independent-readback", selected_removed=True, sibling_and_foreign_unchanged=True, restored=True, repeated_delete=True)
        finally:
            writer.indices.delete(index=index, ignore_unavailable=True)
            assert not reader.indices.exists(index=index)
            assert not reader.indices.exists(index=missing)
            journal.event("elasticsearch-index-independent-cleanup-readback", index=index, absent=True, missing_probe_absent=True)
