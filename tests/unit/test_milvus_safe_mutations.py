"""Milvus mutations preserve source rows when replacement cannot be written."""

import ast
import copy
import json
import logging
from typing import Any

import pytest
from beartype.roar import BeartypeCallHintParamViolation
from pymilvus import DataType
from pymilvus.client.search_result import Hit

from core.utils.milvus_conn import MilvusConnection, decode_milvus_tag_fields


def _source_row() -> dict[str, Any]:
    return {
        "pk": "chunk-original",
        "id": "chunk-original",
        "doc_id": "doc-owned",
        "kb_id": "kb-owned",
        "content_with_weight": "Original content and image caption.",
        "img_id": "kb-owned/original.png",
        "tag_kwd": '["保留标签"]',
        "tag_feas": '{"保留标签": 0.75}',
        "source_id": "source-a\nsource-b",
        "position_int": "00000001_0000000a_00000014_0000001e_00000028",
        "page_num_int": "00000001",
        "top_int": "0000001e",
        "important_kwd": ["original"],
        "q_2_vec": [0.25, -0.5],
        "available_int": 1,
        "pagerank_fea": 99.0,
        "create_time": "2026-10-06 12:00:00",
        "create_timestamp_flt": 1791259200.0,
    }


class _RowStore:
    """In-memory RPC boundary with schema checks before an atomic upsert."""

    def __init__(self, row: dict[str, Any]) -> None:
        self.rows = {row["pk"]: copy.deepcopy(row)}
        self.write_error: Exception | None = None
        self.schema_error: Exception | None = None
        self.write_count: int | None = None
        self.query_filters: list[str] = []
        self.delete_filters: list[str] = []
        self.varchar_fields = {key for key, value in row.items() if isinstance(value, str)}
        self.function_outputs: set[str] = set()
        self.weight_type = DataType.FLOAT

    def has_collection(self, collection_name: str) -> bool:
        assert collection_name == "scratch"
        return True

    def describe_collection(self, collection_name: str) -> dict[str, Any]:
        assert collection_name == "scratch"
        if self.schema_error:
            raise self.schema_error
        fields = [{"name": name, "type": DataType.VARCHAR, "params": {"max_length": 65535}} for name in sorted(self.varchar_fields)]
        next(field for field in fields if field["name"] == "pk")["is_primary"] = True
        fields.extend(
            [
                {"name": "q_2_vec", "type": DataType.FLOAT_VECTOR, "params": {"dim": 2}},
                {"name": "available_int", "type": DataType.INT64},
                {"name": "pagerank_fea", "type": self.weight_type},
                {"name": "create_timestamp_flt", "type": DataType.DOUBLE},
                {"name": "important_kwd", "type": DataType.ARRAY, "element_type": DataType.VARCHAR},
            ]
        )
        fields.extend({"name": name, "type": DataType.SPARSE_FLOAT_VECTOR, "is_function_output": True} for name in self.function_outputs)
        return {"fields": fields}

    @staticmethod
    def _matches(row: dict[str, Any], expression: str) -> bool:
        for part in expression.split(" && "):
            if " == " in part:
                name, value = part.split(" == ", 1)
                if row.get(name) != ast.literal_eval(value):
                    return False
            elif " in " in part:
                name, value = part.split(" in ", 1)
                if row.get(name) not in ast.literal_eval(value):
                    return False
            else:
                raise AssertionError(f"Unexpected filter in test RPC: {part}")
        return True

    def query(self, collection_name: str, expr: str, output_fields: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        assert collection_name == "scratch"
        assert output_fields == ["*"] or "q_2_vec" in output_fields
        self.query_filters.append(expr)
        return [copy.deepcopy(row) for row in self.rows.values() if self._matches(row, expr)]

    def delete(self, collection_name: str, expression: str, **kwargs: Any) -> Any:
        assert collection_name == "scratch"
        self.delete_filters.append(expression)
        keys = [key for key, row in self.rows.items() if self._matches(row, expression)]
        for key in keys:
            del self.rows[key]
        return type("DeleteResult", (), {"delete_count": len(keys)})()

    def _write(self, rows: list[dict[str, Any]], count_name: str) -> Any:
        if self.write_error:
            raise self.write_error
        for row in rows:
            if self.function_outputs.intersection(row):
                raise ValueError("Function output fields cannot be supplied to upsert")
            for field in self.varchar_fields:
                if field in row and not isinstance(row[field], str):
                    raise ValueError(f"{field} schema varchar, got {type(row[field]).__name__}")
            if len(row["q_2_vec"]) != 2:
                raise ValueError("vector dimension does not match schema")
            if self.weight_type == DataType.INT64 and not isinstance(row["pagerank_fea"], int):
                raise ValueError("pagerank_fea schema int64, got non-integer")
        count = len(rows) if self.write_count is None else self.write_count
        if count == len(rows):
            self.rows.update({row["pk"]: copy.deepcopy(row) for row in rows})
        return type("MutationResult", (), {count_name: count})()

    def insert_rows(self, collection_name: str, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
        assert collection_name == "scratch"
        return self._write(rows, "insert_count")

    def upsert_rows(self, collection_name: str, rows: list[dict[str, Any]], **kwargs: Any) -> Any:
        assert collection_name == "scratch"
        return self._write(rows, "upsert_count")


@pytest.fixture
def row_store() -> tuple[Any, _RowStore]:
    cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
    store = cls.__new__(cls)
    store.logger = logging.getLogger("test.milvus.safe_mutations")
    rpc = _RowStore(_source_row())
    store._get_connection = lambda: rpc
    return store, rpc


@pytest.mark.parametrize("payload", [{"tag_kwd": []}, {"content_with_weight": ["invalid varchar"]}, {"q_2_vec": [0.25]}])
def test_update_schema_failure_never_loses_original_row(row_store: tuple[Any, _RowStore], payload: dict[str, Any]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    if "tag_kwd" in payload:
        rpc.write_error = ValueError("Injected schema rejection")
    assert store.update({"id": "chunk-original"}, payload, "scratch", "kb-owned") is False
    assert rpc.rows == before
    assert rpc.delete_filters == []


@pytest.mark.parametrize("tags", [[], ["财务", "重要🚀", '含"引号']])
def test_image_update_serializes_tags_and_preserves_full_row(row_store: tuple[Any, _RowStore], tags: list[str]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows["chunk-original"])
    condition = {"id": "chunk-original", "doc_id": "doc-owned", "kb_id": "kb-owned"}
    payload = {"img_id": "kb-owned/replacement.png", "tag_kwd": tags, "tag_feas": {"财务": 0.8}}
    inputs = copy.deepcopy((condition, payload))
    assert store.update(condition, payload, "scratch", "kb-owned") is True
    expected = {**before, "img_id": payload["img_id"], "tag_kwd": json.dumps(tags, ensure_ascii=False), "tag_feas": json.dumps(payload["tag_feas"], ensure_ascii=False)}
    assert rpc.rows["chunk-original"] == expected
    assert (condition, payload) == inputs
    assert rpc.delete_filters == []


def test_update_normalizes_source_and_positions_without_mutating_input(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    payload = {"source_id": ["source-新", "source-b"], "position_int": [[2, 10, 20, 30, 40]], "page_num_int": [2], "top_int": [30]}
    before = copy.deepcopy(payload)
    assert store.update({"id": "chunk-original"}, payload, "scratch", "kb-owned") is True
    row = rpc.rows["chunk-original"]
    assert row["source_id"] == "source-新\nsource-b"
    assert row["position_int"] == "00000002_0000000a_00000014_0000001e_00000028"
    assert row["page_num_int"] == "00000002" and row["top_int"] == "0000001e"
    assert payload == before


@pytest.mark.parametrize("scope", [{"doc_id": "foreign-document"}, {"kb_id": "foreign-kb"}])
@pytest.mark.parametrize("identifier", [{"id": "chunk-original"}, {"pk": "chunk-original"}])
def test_update_keeps_document_and_dataset_scope(row_store: tuple[Any, _RowStore], scope: dict[str, str], identifier: dict[str, str]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    assert store.update({**identifier, **scope}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is False
    assert rpc.rows == before


@pytest.mark.parametrize("unsupported", [{"exists": "source_id"}, {"must_not": {"exists": "source_id"}}, {"doc_id": {"unexpected": "doc-owned"}}, {"id": []}])
def test_update_unsupported_condition_fails_closed(row_store: tuple[Any, _RowStore], unsupported: dict[str, Any]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    assert store.update({"id": "chunk-original", **unsupported}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is False
    assert rpc.rows == before
    assert rpc.delete_filters == []


def test_update_zero_condition_is_not_ignored(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    assert store.update({"id": "chunk-original", "available_int": 0}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is False
    assert rpc.rows == before


def test_update_short_write_is_failure_and_preserves_source(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    rpc.write_count = 0
    assert store.update({"id": "chunk-original"}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is False
    assert rpc.rows == before


@pytest.mark.parametrize("failure", ["write", "count"])
def test_insert_failed_replacement_keeps_existing_chunk(row_store: tuple[Any, _RowStore], failure: str) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    payload = {**_source_row(), "img_id": "replacement.png", "tag_kwd": []}
    input_before = copy.deepcopy(payload)
    if failure == "write":
        rpc.write_error = ValueError("Injected storage rejection")
    else:
        rpc.write_count = 0
    assert store.insert([payload], "scratch", "kb-owned")
    assert rpc.rows == before
    assert payload == input_before
    assert rpc.delete_filters == []


def test_insert_replacement_preserves_input_and_serializes_fields(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    payload = {**_source_row(), "tag_kwd": ["财务🚀"], "tag_feas": {"财务🚀": 0.6}, "img_id": "replacement.png"}
    before = copy.deepcopy(payload)
    assert store.insert([payload], "scratch", "kb-owned") == []
    assert rpc.rows["chunk-original"] == {**before, "tag_kwd": json.dumps(before["tag_kwd"], ensure_ascii=False), "tag_feas": json.dumps(before["tag_feas"], ensure_ascii=False)}
    assert payload == before
    assert rpc.delete_filters == []


@pytest.mark.parametrize("operation", ["update", "insert"])
def test_schema_lookup_failure_keeps_source(row_store: tuple[Any, _RowStore], operation: str) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    rpc.schema_error = ValueError("Schema unavailable")
    if operation == "update":
        assert store.update({"id": "chunk-original"}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is False
    else:
        assert store.insert([_source_row()], "scratch", "kb-owned")
    assert rpc.rows == before
    assert rpc.delete_filters == []


def test_update_does_not_submit_generated_bm25_fields(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    rpc.function_outputs = {"content_sparse"}
    rpc.rows["chunk-original"]["content_sparse"] = {1: 0.8}
    before = copy.deepcopy(rpc.rows["chunk-original"])
    assert store.update({"id": "chunk-original"}, {"img_id": "replacement.png"}, "scratch", "kb-owned") is True
    assert rpc.rows["chunk-original"] == {key: ("replacement.png" if key == "img_id" else value) for key, value in before.items() if key != "content_sparse"}


@pytest.mark.parametrize("stored, expected", [("", []), ("[]", []), ('["财务", "重要🚀"]', ["财务", "重要🚀"]), (["already decoded"], ["already decoded"]), ("财务, 重要🚀,", ["财务", "重要🚀"])])
@pytest.mark.parametrize("features", ['{"财务": 0.75}', "{'财务': 0.75}"])
def test_get_and_search_fields_decode_tag_storage(row_store: tuple[Any, _RowStore], stored: Any, expected: list[str], features: str) -> None:
    store, rpc = row_store
    rpc.rows["chunk-original"].update({"tag_kwd": stored, "tag_feas": features})
    before = copy.deepcopy(rpc.rows)
    row = store.get("chunk-original", "scratch", ["kb-owned"])
    assert row is not None
    assert row["tag_kwd"] == expected
    assert row["tag_feas"] == {"财务": 0.75}
    fields = store.get_fields([copy.deepcopy(before["chunk-original"])], ["tag_kwd", "tag_feas"])
    assert fields["chunk-original"] == {"pk": "chunk-original", "tag_kwd": expected, "tag_feas": {"财务": 0.75}}
    assert rpc.rows == before


@pytest.mark.parametrize("failure", ["write", "count", "schema"])
def test_feedback_failure_preserves_complete_source(row_store: tuple[Any, _RowStore], failure: str) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    if failure == "write":
        rpc.write_error = ValueError("Injected feedback storage rejection")
    elif failure == "count":
        rpc.write_count = 0
    else:
        rpc.schema_error = ValueError("Feedback schema unavailable")
    assert store.adjust_chunk_pagerank_fea("chunk-original", "scratch", "kb-owned", 5) is False
    assert rpc.rows == before
    assert rpc.delete_filters == []


@pytest.mark.parametrize("delta, expected", [(5, 100), (-200, 0)])
@pytest.mark.parametrize("weight_type", [DataType.FLOAT, DataType.INT64])
def test_feedback_clamps_weight_and_excludes_generated_bm25_output(row_store: tuple[Any, _RowStore], delta: int, expected: int, weight_type: DataType) -> None:
    store, rpc = row_store
    rpc.weight_type = weight_type
    rpc.rows["chunk-original"]["pagerank_fea"] = 99 if weight_type == DataType.INT64 else 99.0
    rpc.function_outputs = {"content_sparse"}
    rpc.rows["chunk-original"]["content_sparse"] = {1: 0.8}
    before = copy.deepcopy(rpc.rows["chunk-original"])
    assert store.adjust_chunk_pagerank_fea("chunk-original", "scratch", "kb-owned", delta) is True
    row = rpc.rows["chunk-original"]
    expected_row = {key: (expected if key == "pagerank_fea" else value) for key, value in before.items() if key != "content_sparse"}
    assert row == expected_row
    assert isinstance(row["pagerank_fea"], int if weight_type == DataType.INT64 else float)
    assert rpc.delete_filters == []


def test_feedback_rejects_foreign_dataset(row_store: tuple[Any, _RowStore]) -> None:
    store, rpc = row_store
    before = copy.deepcopy(rpc.rows)
    assert store.adjust_chunk_pagerank_fea("chunk-original", "scratch", "foreign-kb", 5) is False
    assert rpc.rows == before


def test_search_fields_accept_real_sdk_hit_and_preserve_payload(row_store: tuple[Any, _RowStore]) -> None:
    store, _ = row_store
    hit = Hit(
        {
            "pk": "chunk-original",
            "distance": 0.875,
            "entity": {
                "tag_kwd": '["财务", "重要🚀"]',
                "tag_feas": '{"财务": 0.75}',
                "content_with_weight": "Original content and image caption.",
                "q_2_vec": [0.25, -0.5],
            },
        },
        pk_name="pk",
    )
    before = copy.deepcopy(hit.data)
    fields = store.get_fields([hit], ["tag_kwd", "tag_feas", "_score", "content_with_weight", "q_2_vec"])
    assert fields == {
        "distance": [0.875],
        "chunk-original": {
            "pk": "chunk-original",
            "tag_kwd": ["财务", "重要🚀"],
            "tag_feas": {"财务": 0.75},
            "_score": 0.875,
            "content_with_weight": before["entity"]["content_with_weight"],
            "q_2_vec": [0.25, -0.5],
        },
    }
    assert hit.data == before


def test_tag_decoder_accepts_mapping_and_enforces_runtime_type_check() -> None:
    hit = Hit({"tag_kwd": '["财务"]', "tag_feas": '{"财务": 0.75}'})
    before = copy.deepcopy(hit.data)
    assert decode_milvus_tag_fields(hit) == {"tag_kwd": ["财务"], "tag_feas": {"财务": 0.75}}
    assert hit.data == before
    with pytest.raises(BeartypeCallHintParamViolation):
        decode_milvus_tag_fields([])
