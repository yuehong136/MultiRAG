"""Native chunk ordering against an owned collection, with independent readback."""

import json
import logging
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pymilvus import DataType, MilvusClient, connections

from common.config_utils import CONFIGS
from common.doc_store.doc_store_base import OrderByExpr
from core.utils.milvus_conn import MilvusConnection


class RecordingConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.connection, name)

    def query(self, collection_name: str, expression: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"collection": collection_name, "expression": expression, **kwargs})
        return self.connection.query(collection_name, expression, **kwargs)


@pytest.fixture(scope="module")
def ordered_collection(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    config = CONFIGS["milvus"]
    options = {
        "uri": config["hosts"],
        "user": config.get("username", ""),
        "password": config.get("password", ""),
        "token": config.get("token", ""),
        "db_name": config.get("db_name") or "default",
        "timeout": 30,
    }
    identifier = uuid4().hex
    collection, alias = f"native_order_{identifier}", f"native_order_writer_{identifier}"
    configured_report_dir = request.config.getoption("--test-report-dir")
    report_dir = Path(configured_report_dir) if configured_report_dir else tmp_path_factory.mktemp("milvus-query-order")
    report = report_dir / f"{collection}.json"
    record: dict[str, Any] = {"collection": collection, "collection_absent_after_cleanup": False, "cases": []}

    def save_record() -> None:
        report.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        report.chmod(0o600)

    with ExitStack() as cleanup:
        reader = MilvusClient(**options)
        cleanup.callback(reader.close)

        def drop_owned_collection() -> None:
            if reader.has_collection(collection):
                reader.drop_collection(collection)
            assert reader.has_collection(collection) is False
            record["collection_absent_after_cleanup"] = True
            save_record()

        cleanup.callback(drop_owned_collection)
        connections.connect(alias=alias, **options)
        cleanup.callback(connections.disconnect, alias)
        cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store.logger = logging.getLogger("test.milvus.query_order")
        store._using = alias
        assert store._supports_native_query_order_by(), "This test requires the supported Milvus 3.x SDK/server pair"
        rpc = RecordingConnection(store._get_connection())
        store._get_connection = lambda: rpc
        schema = reader.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_order_int", DataType.INT64, nullable=True)
        schema.add_field("create_timestamp_flt", DataType.DOUBLE)
        schema.add_field("page_num_int", DataType.VARCHAR, max_length=64)
        schema.add_field("q_2_vec", DataType.FLOAT_VECTOR, dim=2)
        indexes = reader.prepare_index_params()
        indexes.add_index("q_2_vec", index_type="AUTOINDEX", metric_type="COSINE")
        reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)
        rows = [
            {"pk": pk, "kb_id": "owned", "chunk_order_int": order, "create_timestamp_flt": timestamp, "page_num_int": page, "q_2_vec": [1.0, 0.0]}
            for pk, order, timestamp, page in [("a", 2, 2.0, "f"), ("b", 1, 1.0, "10"), ("c", 1, 1.0, "1"), ("d", 3, 3.0, "a"), ("e", 4, 4.0, "2"), ("z", None, 5.0, "")]
        ]
        rows.append({"pk": "foreign", "kb_id": "other", "chunk_order_int": 0, "create_timestamp_flt": 99.0, "page_num_int": "0", "q_2_vec": [0.0, 1.0]})
        assert reader.insert(collection, rows)["insert_count"] == len(rows)
        persisted = reader.query(
            collection, filter='kb_id == "owned"', output_fields=["pk", "chunk_order_int", "create_timestamp_flt", "page_num_int"], limit=20, consistency_level="Strong", timeout=30
        )
        assert {row["pk"] for row in persisted} == {"a", "b", "c", "d", "e", "z"}
        record.update(server_version=reader.get_server_version(), persisted_rows=persisted)
        save_record()
        yield {"store": store, "rpc": rpc, "collection": collection, "reader": reader, "record": record, "save_record": save_record}


def _search(env: dict[str, Any], case: str, order: OrderByExpr, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    env["rpc"].calls.clear()
    rows, total = env["store"].search(["pk"], [], {"kb_id": ["owned"]}, [], order, offset, limit, env["collection"], ["owned"])
    env["record"]["cases"].append({"case": case, "requests": env["rpc"].calls.copy(), "rows": rows, "total": total})
    env["save_record"]()
    return rows, total


@pytest.mark.parametrize(
    ("order", "expected"),
    [(OrderByExpr().asc("chunk_order_int").desc("pk"), ["b", "a", "d"]), (OrderByExpr().desc("create_timestamp_flt").asc("pk"), ["e", "d", "a"])],
)
def test_native_page_applies_global_scalar_order_before_offset(ordered_collection: dict[str, Any], order: OrderByExpr, expected: list[str]) -> None:
    rows, total = _search(ordered_collection, "native-page", order, 1, 3)

    assert [row["pk"] for row in rows] == expected and total == 6
    count, page = ordered_collection["rpc"].calls
    assert count["output_fields"] == ["count(*)"] and "limit" not in count and "offset" not in count
    assert page["offset"] == 1 and page["limit"] == 3 and page["order_by"]
    assert count["consistency_level"] == page["consistency_level"] == "Strong"


def test_native_offset_past_end_preserves_total(ordered_collection: dict[str, Any]) -> None:
    rows, total = _search(ordered_collection, "empty-native-page", OrderByExpr().asc("chunk_order_int").asc("pk"), 20, 3)

    assert rows == [] and total == 6
    assert ordered_collection["rpc"].calls[-1]["limit"] == 3


def test_native_wildcard_projection_keeps_vector_and_metadata(ordered_collection: dict[str, Any]) -> None:
    env = ordered_collection
    env["rpc"].calls.clear()

    rows, total = env["store"].search(["*"], [], {"kb_id": ["owned"]}, [], OrderByExpr().asc("chunk_order_int").desc("pk"), 0, 1, env["collection"], ["owned"])

    assert len(rows) == 1 and rows[0]["pk"] == "c" and total == 6
    assert rows[0]["q_2_vec"] == [1.0, 0.0] and rows[0]["page_num_int"] == "1" and rows[0]["kb_id"] == "owned"
    assert env["rpc"].calls[-1]["output_fields"] == ["*"]
    env["record"]["cases"].append({"case": "wildcard-projection", "requests": env["rpc"].calls.copy(), "rows": rows, "total": total})
    env["save_record"]()


@pytest.mark.parametrize(
    ("order", "expected"),
    [(OrderByExpr().desc("chunk_order_int").desc("pk"), ["e", "d", "a", "c", "b", "z"]), (OrderByExpr().asc("page_num_int"), ["c", "e", "d", "a", "b", "z"])],
)
def test_nullable_descending_and_encoded_position_keep_logical_order(ordered_collection: dict[str, Any], order: OrderByExpr, expected: list[str]) -> None:
    rows, total = _search(ordered_collection, "logical-fallback", order, 0, 6)

    assert [row["pk"] for row in rows] == expected and total == 6
    assert len(ordered_collection["rpc"].calls) == 1
    request = ordered_collection["rpc"].calls[0]
    assert request["limit"] == 10000 and "order_by" not in request and "offset" not in request
