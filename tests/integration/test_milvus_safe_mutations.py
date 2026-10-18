"""Safe replacement against an owned real Milvus schema, with independent reads."""

import copy
import json
import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from pymilvus import DataType, Function, FunctionType, MilvusClient, connections
from pymilvus.client.search_result import Hit

from common.config_utils import CONFIGS
from common.doc_store.document_history import document_history
from core.utils.milvus_conn import MilvusConnection


def _json_default(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(f"Unexpected Milvus readback type: {type(value).__name__}")


@pytest.fixture(scope="module")
def mutation_collection(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    config = CONFIGS["milvus"]
    options = {
        "uri": config["hosts"],
        "user": config.get("username", ""),
        "password": config.get("password", ""),
        "db_name": config.get("db_name") or "default",
        "token": config.get("token", ""),
        "timeout": 30,
    }
    identifier = uuid4().hex
    name, alias = "safe_mutation_" + identifier, "safe_mutation_writer_" + identifier
    configured_report_dir = request.config.getoption("--test-report-dir")
    report_dir = Path(configured_report_dir) if configured_report_dir else tmp_path_factory.mktemp("milvus-safe-mutations")
    record_path = report_dir / (name + ".json")
    record: dict[str, Any] = {"collection": name, "collection_absent_after_cleanup": False, "readbacks": []}

    def save_record() -> None:
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=_json_default))
        record_path.chmod(0o600)

    save_record()
    with ExitStack() as cleanup:
        reader = MilvusClient(**options)
        cleanup.callback(reader.close)

        def drop_owned_collection() -> None:
            if reader.has_collection(name):
                reader.drop_collection(name)
            assert reader.has_collection(name) is False
            record["collection_absent_after_cleanup"] = True
            save_record()

        cleanup.callback(drop_owned_collection)
        cleanup.callback(connections.disconnect, alias)
        connections.connect(alias=alias, **options)
        cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
        store = cls.__new__(cls)
        store.logger = logging.getLogger("test.milvus.safe_mutations.integration")
        store._using = alias

        schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
        for field in ("id", "doc_id", "kb_id", "img_id", "tag_kwd", "tag_feas", "source_id", "position_int", "page_num_int", "top_int", "create_time"):
            schema.add_field(field, DataType.VARCHAR, max_length=65535)
        schema.add_field("important_kwd", DataType.ARRAY, element_type=DataType.VARCHAR, max_capacity=16, max_length=512)
        schema.add_field("available_int", DataType.INT64)
        schema.add_field("pagerank_fea", DataType.INT64)
        schema.add_field("create_timestamp_flt", DataType.DOUBLE)
        schema.add_field("q_2_vec", DataType.FLOAT_VECTOR, dim=2)
        schema.add_field("content_with_weight", DataType.VARCHAR, max_length=65535, enable_analyzer=True, analyzer_params={"tokenizer": "standard"})
        schema.add_field("content_sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(name="content_bm25", function_type=FunctionType.BM25, input_field_names=["content_with_weight"], output_field_names=["content_sparse"]))
        indexes = reader.prepare_index_params()
        indexes.add_index("q_2_vec", index_type="AUTOINDEX", metric_type="COSINE")
        indexes.add_index("content_sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
        reader.create_collection(name, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)
        record["schema_fields"] = [
            {"name": field["name"], "type": str(field["type"]), "is_function_output": bool(field.get("is_function_output"))} for field in reader.describe_collection(name)["fields"]
        ]
        record["server_version"] = reader.get_server_version()
        save_record()
        yield {"store": store, "reader": reader, "collection": name, "record": record, "save_record": save_record}


@pytest.fixture
def mutation_row(mutation_collection: dict[str, Any], request: pytest.FixtureRequest) -> dict[str, Any]:
    env = mutation_collection
    env["case"] = request.node.nodeid
    row = {
        "pk": "owned-chunk",
        "id": "owned-chunk",
        "doc_id": "owned-document",
        "kb_id": "owned-dataset",
        "img_id": "owned-dataset/original.png",
        "tag_kwd": '["原标签"]',
        "tag_feas": '{"原标签": 0.75}',
        "source_id": "source-a\nsource-b",
        "position_int": "00000001_0000000a_00000014_0000001e_00000028",
        "page_num_int": "00000001",
        "top_int": "0000001e",
        "important_kwd": ["original"],
        "available_int": 1,
        "pagerank_fea": 99,
        "create_time": "2026-10-06 12:00:00",
        "create_timestamp_flt": 1791259200.0,
        "q_2_vec": [0.25, -0.5],
        "content_with_weight": "Original content and image caption.",
        "extra_metadata": {"source": "retained", "labels": ["财务"]},
    }
    assert env["reader"].upsert(env["collection"], [row])["upsert_count"] == 1
    before = _read_row(env)
    assert before["q_2_vec"] == [0.25, -0.5]
    return {**env, "before": before, "source": row}


def _read_row(env: dict[str, Any]) -> dict[str, Any]:
    # This client has a connection independent from the production write handler.
    rows = env["reader"].query(env["collection"], filter='pk == "owned-chunk"', output_fields=["*", "q_2_vec"], consistency_level="Strong", timeout=30)
    assert len(rows) == 1, rows
    env["record"]["readbacks"].append({"case": env["case"], "row": rows[0]})
    env["save_record"]()
    return rows[0]


def test_raw_varchar_schema_rejection_preserves_existing_row(mutation_row: dict[str, Any]) -> None:
    env = mutation_row
    replacement = {**env["source"], "img_id": "replacement.png", "tag_kwd": []}
    with pytest.raises(Exception, match=r"varchar|VarChar"):
        env["reader"].upsert(env["collection"], [replacement])
    assert _read_row(env) == env["before"]


@pytest.mark.parametrize("tags", [[], ["财务", "重要🚀", '含"引号']])
def test_image_update_retains_vector_metadata_and_creation_time(mutation_row: dict[str, Any], tags: list[str]) -> None:
    env = mutation_row
    condition = {"id": "owned-chunk", "doc_id": "owned-document", "kb_id": "owned-dataset"}
    payload = {"img_id": "owned-dataset/replacement.png", "tag_kwd": tags, "tag_feas": {"财务": 0.8}}
    inputs = copy.deepcopy((condition, payload))
    assert env["store"].update(condition, payload, env["collection"], "owned-dataset") is True
    expected = {**env["before"], "img_id": payload["img_id"], "tag_kwd": json.dumps(tags, ensure_ascii=False), "tag_feas": json.dumps(payload["tag_feas"], ensure_ascii=False)}
    assert _read_row(env) == expected
    assert (condition, payload) == inputs
    decoded = env["store"].get("owned-chunk", env["collection"], ["owned-dataset"])
    assert decoded is not None and decoded["tag_kwd"] == tags and decoded["tag_feas"] == payload["tag_feas"]
    fields = env["store"].get_fields([_read_row(env)], ["tag_kwd", "tag_feas"])
    assert fields["owned-chunk"] == {"pk": "owned-chunk", "tag_kwd": tags, "tag_feas": payload["tag_feas"]}


@pytest.mark.parametrize("payload", [{"content_with_weight": ["invalid varchar"]}, {"q_2_vec": [0.25]}])
def test_failed_update_preserves_complete_source(mutation_row: dict[str, Any], payload: dict[str, Any]) -> None:
    env = mutation_row
    assert env["store"].update({"id": "owned-chunk"}, payload, env["collection"], "owned-dataset") is False
    assert _read_row(env) == env["before"]


def test_failed_insert_replacement_keeps_existing_row(mutation_row: dict[str, Any]) -> None:
    env = mutation_row
    replacement = {**env["source"], "img_id": "replacement.png", "q_2_vec": [0.25]}
    before_input = copy.deepcopy(replacement)
    assert env["store"].insert([replacement], env["collection"], "owned-dataset")
    assert _read_row(env) == env["before"]
    assert replacement == before_input


@pytest.mark.parametrize("scope", [{"doc_id": "foreign-document"}, {"kb_id": "foreign-dataset"}])
def test_update_rejects_foreign_document_or_dataset(mutation_row: dict[str, Any], scope: dict[str, str]) -> None:
    env = mutation_row
    assert env["store"].update({"id": "owned-chunk", **scope}, {"img_id": "replacement.png"}, env["collection"], "owned-dataset") is False
    assert _read_row(env) == env["before"]


@pytest.mark.parametrize("delta, expected", [(5, 100), (-200, 0)])
def test_feedback_clamps_weight_and_preserves_complete_row(mutation_row: dict[str, Any], delta: int, expected: int) -> None:
    env = mutation_row
    assert env["store"].adjust_chunk_pagerank_fea("owned-chunk", env["collection"], "owned-dataset", delta) is True
    assert _read_row(env) == {**env["before"], "pagerank_fea": expected}


def test_feedback_native_schema_rejection_keeps_source(mutation_row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = mutation_row
    rpc = env["store"]._get_connection()
    calls: list[list[dict[str, Any]]] = []

    def reject_feedback_rows(collection_name: str, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
        # Inject an invalid type only at the write boundary; the real SDK/schema
        # rejects it. The read, row merge, storage call and Strong readback stay real.
        invalid_rows = copy.deepcopy(rows)
        for row in invalid_rows:
            row["pagerank_fea"] = [100]
        calls.append(invalid_rows)
        return rpc.upsert_rows(collection_name, invalid_rows, **kwargs)

    class RejectedFeedbackWriter:
        def __getattr__(self, name: str) -> Any:
            return getattr(rpc, name)

        def upsert_rows(self, collection_name: str, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
            return reject_feedback_rows(collection_name, rows, **kwargs)

    monkeypatch.setattr(env["store"], "_get_connection", lambda: RejectedFeedbackWriter())
    assert env["store"].adjust_chunk_pagerank_fea("owned-chunk", env["collection"], "owned-dataset", 5) is False
    assert len(calls) == 1
    assert _read_row(env) == env["before"]


def test_ann_sdk_hit_decodes_tags_and_preserves_payload(mutation_row: dict[str, Any]) -> None:
    env = mutation_row
    selected_fields = ["tag_kwd", "tag_feas", "content_with_weight", "q_2_vec"]
    results = env["reader"].search(
        env["collection"],
        data=[[0.25, -0.5]],
        anns_field="q_2_vec",
        filter='pk == "owned-chunk"',
        limit=1,
        output_fields=selected_fields,
        consistency_level="Strong",
        timeout=30,
    )
    assert len(results) == 1 and len(results[0]) == 1
    hit = results[0][0]
    assert isinstance(hit, Hit)
    before = copy.deepcopy(hit.data)
    fields = env["store"].get_fields([hit], [*selected_fields, "_score"])
    decoded = fields["owned-chunk"]
    assert decoded["tag_kwd"] == ["原标签"]
    assert decoded["tag_feas"] == {"原标签": 0.75}
    assert decoded["content_with_weight"] == env["before"]["content_with_weight"]
    assert decoded["q_2_vec"] == [0.25, -0.5]
    assert decoded["_score"] == pytest.approx(float(hit.distance))
    assert fields["distance"] == [hit.distance]
    assert hit.data == before
    assert _read_row(env) == env["before"]


def test_first_manual_chunk_creates_searchable_dense_and_sparse_schema(mutation_collection: dict[str, Any]) -> None:
    env = mutation_collection
    name = "manual_bootstrap_" + uuid4().hex
    reader = env["reader"]
    vector = [1.0] + [0.0] * 1023
    row = {
        "id": "first-manual-chunk",
        "doc_id": "unparsed-document",
        "kb_id": "manual-dataset",
        "content_with_weight": "quasar telescope observation",
        "content_ltks": "quasar telescope observation",
        "content_sm_ltks": "quasar telescope observation",
        "q_1024_vec": vector,
    }
    record: dict[str, Any] = {"collection": name, "collection_absent_after_cleanup": False}
    env["record"]["manual_bootstrap"] = record
    env["save_record"]()
    try:
        assert reader.has_collection(name) is False
        assert env["store"].insert([row], name, "manual-dataset") == []
        fields = {field["name"]: field for field in reader.describe_collection(name)["fields"]}
        record["schema_fields"] = {key: {"type": str(value["type"]), "params": value.get("params", {})} for key, value in fields.items()}
        env["save_record"]()
        assert fields["q_1024_vec"]["type"] == DataType.FLOAT_VECTOR
        assert int(fields["q_1024_vec"]["params"]["dim"]) == 1024
        assert int(fields["vector"]["params"]["dim"]) == 1024
        assert fields["content_ltks_sparse"]["type"] == DataType.SPARSE_FLOAT_VECTOR
        rows = reader.query(name, filter='pk == "first-manual-chunk"', output_fields=["*", "q_1024_vec"], consistency_level="Strong", timeout=30)
        assert len(rows) == 1 and rows[0]["q_1024_vec"] == vector
        for field, query in [("q_1024_vec", vector), ("content_ltks_sparse", "quasar telescope")]:
            hits = reader.search(name, data=[query], anns_field=field, limit=1, consistency_level="Strong", timeout=30)
            assert len(hits) == 1 and [hit.id for hit in hits[0]] == ["first-manual-chunk"]
            record[field + "_hits"] = [hit.id for hit in hits[0]]
        assert env["store"].create_idx(name, "manual-dataset", 1024) is True
        assert reader.query(name, filter='pk == "first-manual-chunk"', output_fields=["q_1024_vec"], consistency_level="Strong", timeout=30)[0]["q_1024_vec"] == vector
    finally:
        if reader.has_collection(name):
            reader.drop_collection(name)
        record["collection_absent_after_cleanup"] = not reader.has_collection(name)
        env["save_record"]()
        assert record["collection_absent_after_cleanup"] is True


def test_insert_rejects_dynamic_only_embedding_without_mutating_source(mutation_row: dict[str, Any]) -> None:
    env = mutation_row
    replacement = {**env["source"], "content_with_weight": "must not acknowledge an unsearchable embedding", "q_1024_vec": [1.0] + [0.0] * 1023}
    errors = env["store"].insert([replacement], env["collection"], "owned-dataset")
    assert errors and "embedding field is not indexed" in errors[0]
    assert _read_row(env) == env["before"]


def test_history_waits_for_concurrent_collection_initialization(mutation_collection: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = mutation_collection
    name = "history_bootstrap_" + uuid4().hex
    store, reader = env["store"], env["reader"]
    created, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original_create_index, original_ready = store.create_index, store.ensure_collection_loaded
    record: dict[str, Any] = {"collection": name, "collection_absent_after_cleanup": False}
    env["record"]["history_bootstrap"] = record
    env["save_record"]()

    def pause_indexing(collection_name: str, *args: Any, **kwargs: Any) -> None:
        if not created.is_set():
            created.set()
            assert release.wait(15), "Test did not release owned collection initialization"
        original_create_index(collection_name, *args, **kwargs)

    def observe_wait(collection_name: str, timeout: float = 60.0) -> None:
        waiting.set()
        original_ready(collection_name, timeout)

    monkeypatch.setattr(store, "create_index", pause_indexing)
    monkeypatch.setattr(store, "ensure_collection_loaded", observe_wait)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            creation = pool.submit(store.create_idx, name, "owned-dataset", 1024)
            try:
                assert created.wait(15), "Owned schema was not created"
                assert reader.has_collection(name) is True
                history = pool.submit(document_history, store, name, "owned-dataset", "unparsed-document")
                assert waiting.wait(5), "History bypassed collection readiness"
                assert history.done() is False
            finally:
                release.set()
            assert creation.result(timeout=30) is True
            assert history.result(timeout=30) == []
            record["complete_history"] = []
            record["concurrent_reader_waited"] = True
            # An already released collection must be loaded before an exact
            # history read, rather than being mistaken for empty history.
            row = {"id": "retained", "doc_id": "unparsed-document", "kb_id": "owned-dataset", "content_with_weight": "retained quasar", "q_1024_vec": [1.0] + [0.0] * 1023}
            assert store.insert([row], name, "owned-dataset") == []
            reader.release_collection(name)
            retained = document_history(store, name, "owned-dataset", "unparsed-document")
            assert [item["id"] for item in retained] == ["retained"]
            assert retained[0]["q_1024_vec"] == row["q_1024_vec"]
            record["released_collection_history_ids"] = [item["id"] for item in retained]
    finally:
        release.set()
        if reader.has_collection(name):
            reader.drop_collection(name)
        record["collection_absent_after_cleanup"] = not reader.has_collection(name)
        env["save_record"]()
        assert record["collection_absent_after_cleanup"] is True
