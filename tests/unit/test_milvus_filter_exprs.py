import logging

from pymilvus.orm.types import DataType

import core.utils.milvus_conn as milvus_conn_module
from common.doc_store.doc_store_base import OrderByExpr
from common.doc_store.milvus_conn_base import MilvusConnectionBase


class _DummyMilvusStore(MilvusConnectionBase):
    def search(self, *args, **kwargs):
        raise NotImplementedError

    def get(self, *args, **kwargs):
        raise NotImplementedError

    def insert(self, *args, **kwargs):
        raise NotImplementedError

    def update(self, *args, **kwargs):
        raise NotImplementedError

    def delete(self, *args, **kwargs):
        raise NotImplementedError

    def get_fields(self, *args, **kwargs):
        raise NotImplementedError


class _FakeSchemaConn:
    def describe_collection(self, _collection_name):
        return {
            "fields": [
                {"name": "pk"},
                {"name": "doc_id"},
                {"name": "chunk_order_int"},
            ]
        }


NATIVE_SORT_SCHEMA_FIELDS = {
    "chunk_order_int": {"name": "chunk_order_int", "type": DataType.INT64},
    "create_timestamp_flt": {"name": "create_timestamp_flt", "type": DataType.FLOAT},
    "page_num_int": {"name": "page_num_int", "type": DataType.JSON},
}


def test_build_filter_clause_uses_array_contains_any_for_array_fields() -> None:
    clause = milvus_conn_module.build_milvus_filter_clause("entities_kwd", ["初三阶段", "初二阶段"])

    assert clause == "ARRAY_CONTAINS_ANY(entities_kwd, ['初三阶段','初二阶段'])"


def test_build_filter_clause_keeps_in_for_scalar_list_fields() -> None:
    clause = milvus_conn_module.build_milvus_filter_clause("kb_id", ["kb-1", "kb-2"])

    assert clause == "kb_id in ['kb-1','kb-2']"


def test_filter_collection_output_fields_keeps_only_schema_fields() -> None:
    store = object.__new__(_DummyMilvusStore)
    store.logger = logging.getLogger("test.milvus")

    fields = store._filter_collection_output_fields(
        _FakeSchemaConn(),
        "collection",
        ["doc_id", "chunk_order_int", "missing_old_field"],
    )

    assert fields == ["doc_id", "chunk_order_int"]


def test_native_query_order_builder_is_disabled_until_milvus_3_upgrade() -> None:
    order_by = OrderByExpr().asc("chunk_order_int")

    native_order = milvus_conn_module.build_milvus_native_query_order_by(
        order_by,
        NATIVE_SORT_SCHEMA_FIELDS,
        native_order_supported=False,
    )

    assert native_order is None


def test_native_query_order_builder_formats_scalar_fields_when_enabled() -> None:
    order_by = OrderByExpr().asc("chunk_order_int").desc("create_timestamp_flt")

    native_order = milvus_conn_module.build_milvus_native_query_order_by(
        order_by,
        NATIVE_SORT_SCHEMA_FIELDS,
        native_order_supported=True,
    )

    assert native_order == ["chunk_order_int:asc", "create_timestamp_flt:desc"]


def test_native_query_order_builder_rejects_non_scalar_fields() -> None:
    order_by = OrderByExpr().asc("chunk_order_int").asc("page_num_int")

    native_order = milvus_conn_module.build_milvus_native_query_order_by(
        order_by,
        NATIVE_SORT_SCHEMA_FIELDS,
        native_order_supported=True,
    )

    assert native_order is None


def test_sort_milvus_results_uses_chunk_order_before_position_fields() -> None:
    order_by = OrderByExpr().asc("chunk_order_int").asc("page_num_int").asc("top_int").desc("create_timestamp_flt")
    rows = [
        {"pk": "third", "chunk_order_int": 2, "page_num_int": "00000001", "top_int": "00000005"},
        {"pk": "second", "chunk_order_int": 1, "page_num_int": "00000003", "top_int": "00000001"},
        {"pk": "first", "chunk_order_int": 0, "page_num_int": "00000002", "top_int": "00000001"},
    ]

    sorted_rows = milvus_conn_module.sort_milvus_results_by_order_fields(rows, order_by)

    assert [row["pk"] for row in sorted_rows] == ["first", "second", "third"]


def test_sort_milvus_results_keeps_missing_order_fields_last() -> None:
    order_by = OrderByExpr().asc("chunk_order_int").asc("page_num_int")
    rows = [
        {"pk": "missing", "page_num_int": "00000001"},
        {"pk": "later", "chunk_order_int": 2, "page_num_int": "00000001"},
        {"pk": "earlier", "chunk_order_int": 1, "page_num_int": "00000001"},
    ]

    sorted_rows = milvus_conn_module.sort_milvus_results_by_order_fields(rows, order_by)

    assert [row["pk"] for row in sorted_rows] == ["earlier", "later", "missing"]
