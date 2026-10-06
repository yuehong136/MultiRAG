"""Safe replacement against an owned real Milvus schema, with independent reads."""

import copy
import json
import logging
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from pymilvus import DataType, Function, FunctionType, MilvusClient, connections
from pymilvus.client.search_result import Hit

from common.config_utils import CONFIGS
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
