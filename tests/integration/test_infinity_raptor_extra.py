"""Infinity RAPTOR writes, old-table migration and compensation with native readback."""

import copy
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import infinity
import pytest
from infinity.common import ConflictType, NetworkAddress
from infinity.index import IndexInfo, IndexType

from common.config_utils import CONFIGS
from common.doc_store.document_history import document_history
from core.utils.infinity_conn import InfinityConnection
from core.utils.raptor_migration import replace_raptor_summaries, summary_rows
from core.utils.raptor_utils import collect_raptor_methods


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
def extra_store() -> Iterator[tuple[Any, Any]]:
    host, port = os.environ.get("INFINITY_TEST_URI", CONFIGS["infinity"]["uri"]).rsplit(":", 1)
    name = "raptor_extra_" + uuid4().hex
    journal = os.environ.get("INFINITY_SCRATCH_JOURNAL")
    if journal:
        with Path(journal).open("a") as stream:
            stream.write(json.dumps({"database": name, "tables": ["multirag_chunks_kb", "multirag_foreign_kb"], "state": "registered-before-creation"}) + "\n")
    writer = infinity.connect(NetworkAddress(host, int(port)))
    reader = None
    pool = OwnedPool(writer)
    try:
        reader = infinity.connect(NetworkAddress(host, int(port)))
        writer.create_database(name, ConflictType.Error)
        cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store.dbName = name
        store.mapping_file_name = "infinity_mapping.json"
        store.table_name_prefix = "multirag_"
        store.logger = logging.getLogger(__name__)
        store.connPool = pool
        yield store, reader.get_database(name)
    finally:
        try:
            assert pool.acquired == pool.released
            if journal and reader is not None:
                database = reader.get_database(name)
                snapshot = {table: native_rows(database, table) for table in database.list_tables().table_names}
                with Path(journal).open("a") as stream:
                    stream.write(json.dumps({"database": name, "state": "independent-pre-cleanup-readback", "rows": snapshot}) + "\n")
            writer.get_database("default_db")
            if reader is not None:
                reader.get_database("default_db")
            writer.drop_database(name, ConflictType.Ignore)
            if reader is not None:
                assert name not in reader.list_databases().db_names
            if journal:
                with Path(journal).open("a") as stream:
                    stream.write(json.dumps({"database": name, "state": "absent-after-cleanup"}) + "\n")
        finally:
            if reader is not None:
                reader.disconnect()
            writer.disconnect()


def chunk(identifier: str = "summary", extra: Any = None) -> dict[str, Any]:
    return {
        "id": identifier,
        "pk": identifier,
        "doc_id": "doc",
        "kb_id": ["kb"],
        "docnm_kwd": "source.txt",
        "title_tks": "source.txt",
        "content_with_weight": "summary text",
        "content_ltks": "summary text",
        "content_sm_ltks": "summary text",
        "raptor_kwd": "raptor",
        "raptor_layer_int": 1,
        "create_timestamp_flt": 1792800000.123,
        "q_2_vec": [0.1, 0.2],
        "extra": extra if extra is not None else {"raptor_method": "psi", "raptor_clustering_method": "gmm", "labels": ["中文", 0, False]},
    }


def native_rows(database: Any, table: str = "multirag_chunks_kb") -> dict[str, dict[str, Any]]:
    rows = database.get_table(table).output(["*"]).to_df()[0].to_dict(orient="records")
    return {row["id"]: {key: value.tolist() if hasattr(value, "tolist") else value for key, value in row.items()} for row in rows}


def test_new_table_extra_insert_update_and_raptor_readback(extra_store: tuple[Any, Any]) -> None:
    store, reader = extra_store
    data = [chunk(), chunk("unrelated", {"raptor_method": "raptor"})]
    original = copy.deepcopy(data)
    assert store.insert(data, "multirag_chunks", "kb") == []
    assert data == original
    table = reader.get_table("multirag_chunks_kb")
    assert "extra" in set(table.show_columns()["name"])
    rows = native_rows(reader)
    assert json.loads(rows["summary"]["extra"]) == original[0]["extra"]
    assert rows["summary"]["raptor_kwd"] == "raptor"
    assert rows["summary"]["raptor_layer_int"] == 1
    assert collect_raptor_methods(rows) == {"psi", "raptor"}
    sibling = rows["unrelated"]
    update = {"extra": {"raptor_method": "raptor", "raptor_clustering_method": "ahc", "flags": [False, 0]}}
    before = copy.deepcopy(update)
    assert store.update({"id": "summary"}, update, "multirag_chunks", "kb")
    assert update == before
    rows = native_rows(reader)
    assert json.loads(rows["summary"]["extra"]) == update["extra"]
    assert rows["unrelated"] == sibling
    assert collect_raptor_methods(rows) == {"raptor"}


def test_existing_table_migration_preserves_rows_and_adds_extra(extra_store: tuple[Any, Any]) -> None:
    store, reader = extra_store
    schema = json.loads((Path(__file__).parents[2] / "configs/infinity_mapping.json").read_text())
    schema.pop("extra")
    schema["q_2_vec"] = {"type": "vector,2,float"}
    database = store.connPool.connection.get_database(store.dbName)
    table = database.create_table("multirag_chunks_kb", schema)
    table.create_index("q_vec_idx", IndexInfo("q_2_vec", IndexType.Hnsw, {"M": "16", "ef_construction": "50", "metric": "cosine"}))
    table.insert([{"id": "old", "doc_id": "doc", "content": "retained", "q_2_vec": [1.0, 0.0]}])
    # Same prefix without the chunk index is deliberately outside migration ownership.
    foreign = database.create_table("multirag_foreign_kb", {"id": {"type": "varchar"}})
    foreign.insert([{"id": "foreign"}])
    before = native_rows(reader)
    store._migrate_db(store.connPool.connection)
    store._migrate_db(store.connPool.connection)
    after = native_rows(reader)
    assert {key: value for key, value in after["old"].items() if key != "extra"} == before["old"]
    assert after["old"]["extra"] == ""
    assert set(reader.get_table("multirag_foreign_kb").show_columns()["name"]) == {"id"}
    assert store.update({"id": "old"}, {"extra": {"raptor_method": "psi"}}, "multirag_chunks", "kb")
    assert json.loads(native_rows(reader)["old"]["extra"]) == {"raptor_method": "psi"}
    assert store.insert([chunk()], "multirag_chunks", "kb") == []
    assert set(native_rows(reader)) == {"old", "summary"}


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_unserializable_extra_has_no_row_mutation(extra_store: tuple[Any, Any], operation: str) -> None:
    store, reader = extra_store
    assert store.insert([chunk()], "multirag_chunks", "kb") == []
    before = native_rows(reader)
    with pytest.raises(TypeError):
        if operation == "insert":
            store.insert([chunk(extra={"invalid": {1, 2}})], "multirag_chunks", "kb")
        else:
            store.update({"id": "summary"}, {"extra": {"invalid": {1, 2}}}, "multirag_chunks", "kb")
    assert native_rows(reader) == before


@pytest.mark.parametrize("failure", [False, True])
def test_raptor_replacement_and_failure_recovery(extra_store: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    store, reader = extra_store
    assert store.insert([chunk("old", {"raptor_method": "raptor"}), chunk("sibling", {"raptor_method": "raptor"})], "multirag_chunks", "kb") == []
    # A different document remains untouched through success and compensation.
    assert store.update({"id": "sibling"}, {"doc_id": "other"}, "multirag_chunks", "kb")
    before = native_rows(reader)
    previous = {"doc": summary_rows(document_history(store, "multirag_chunks", "kb", "doc"))}
    source = [chunk()]
    original = copy.deepcopy(source)
    if failure:
        insert = store.insert
        failed = False

        def fail_after_insert(*args: Any, **kwargs: Any) -> list[str]:
            nonlocal failed
            receipt = insert(*args, **kwargs)
            if not failed:
                failed = True
                raise RuntimeError("controlled insert lost reply")
            return receipt

        monkeypatch.setattr(store, "insert", fail_after_insert)
        with pytest.raises(RuntimeError, match="controlled insert lost reply"):
            replace_raptor_summaries(store, "multirag_chunks", "kb", source, previous, lambda: False, 10)
        assert native_rows(reader) == before
    else:
        written = replace_raptor_summaries(store, "multirag_chunks", "kb", source, previous, lambda: False, 10)
        rows = native_rows(reader)
        assert set(rows) == {"summary", "sibling"}
        assert rows["sibling"] == before["sibling"]
        assert written["doc"]["summary"] == rows["summary"]
        assert json.loads(rows["summary"]["extra"]) == source[0]["extra"]
    assert source == original


@pytest.mark.parametrize("field", ["extra", "content", "q_2_vec"])
def test_raptor_rejects_changed_native_payload_and_restores_history(extra_store: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    store, reader = extra_store
    assert store.insert([chunk("old", {"raptor_method": "raptor"})], "multirag_chunks", "kb") == []
    before = native_rows(reader)
    previous = {"doc": summary_rows(document_history(store, "multirag_chunks", "kb", "doc"))}
    insert = store.insert

    def corrupt_after_insert(*args: Any, **kwargs: Any) -> list[str]:
        receipt = insert(*args, **kwargs)
        changed = {"extra": '{"raptor_method":"wrong"}', "content": "wrong summary", "q_2_vec": [0.2, 0.1]}[field]
        store.connPool.connection.get_database(store.dbName).get_table("multirag_chunks_kb").update("id = 'summary'", {field: changed})
        return receipt

    monkeypatch.setattr(store, "insert", corrupt_after_insert)
    with pytest.raises(RuntimeError, match=r"RAPTOR inserted (content|vector) could not be confirmed"):
        replace_raptor_summaries(store, "multirag_chunks", "kb", [chunk()], previous, lambda: False, 10)
    assert native_rows(reader) == before


@pytest.mark.parametrize("dimension", [2, 768])
async def test_actual_raptor_generation_writes_native_history(extra_store: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, dimension: int) -> None:
    import numpy as np

    from common import resources

    # This consumer seam bypasses unrelated API bootstrap, SQL ownership and paid
    # models; all history reads and summary writes below use real Infinity RPCs.
    monkeypatch.setitem(resources._state, "secret_key", "infinity-raptor-test-secret")
    from core import raptor as raptor_module
    from core.svr import task_executor

    store, reader = extra_store
    vector_field = f"q_{dimension}_vec"
    source = [{"id": f"source-{i}", "doc_id": "doc", "kb_id": ["kb"], "content_with_weight": f"source {i}", vector_field: [0.1] * dimension} for i in range(2)]
    assert store.insert(source, "multirag_chunks", "kb") == []
    before = native_rows(reader)

    async def history(doc_id: str, *args: Any) -> list[dict[str, Any]]:
        return document_history(store, "multirag_chunks", "kb", doc_id)

    class ControlledRaptor:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __call__(self, inputs: list[tuple[str, np.ndarray]], *args: Any) -> tuple[list[tuple[str, np.ndarray]], list[tuple[int, int]]]:
            assert [text for text, _ in inputs] == ["source 0", "source 1"]
            return [*inputs, ("generated summary", np.full(dimension, 0.2))], [(0, 2), (2, 3)]

    monkeypatch.setattr(task_executor, "_raptor_history", history)
    monkeypatch.setitem(vars(task_executor.settings), "DOC_ENGINE_INFINITY", True)
    monkeypatch.setattr(raptor_module, "RecursiveAbstractiveProcessing4TreeOrganizedRetrieval", ControlledRaptor)
    config = {"raptor": {"scope": "file", "prompt": "{cluster_content}", "max_token": 32, "threshold": 0.1, "ext": {"tree_builder": "psi"}}}
    chunks, _, previous = await task_executor.run_raptor_for_kb({"id": "task", "tenant_id": "tenant", "kb_id": "kb", "name": "source.txt"}, config, None, None, dimension, doc_ids=["doc"])
    assert len(chunks) == 1
    prepared = task_executor._prepare_raptor_chunks(chunks, {"fields": []})
    replace_raptor_summaries(store, "multirag_chunks", "kb", prepared, previous, lambda: False, 10)
    rows = native_rows(reader)
    for identifier, row in before.items():
        assert rows[identifier] == row
    summary = rows[chunks[0]["id"]]
    assert summary["content"] == "generated summary" and summary["raptor_layer_int"] == 1
    assert json.loads(summary["extra"]) == {"raptor_method": "psi", "raptor_clustering_method": "gmm"}
    assert collect_raptor_methods(rows) == {"psi"}
