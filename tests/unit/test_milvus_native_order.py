"""Native scalar pagination reaches real SDK query requests with unchanged totals."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from pymilvus.client.prepare import Prepare
from pymilvus.client.ts_utils import construct_guarantee_ts
from pymilvus.client.types import get_consistency_level
from pymilvus.orm.types import DataType

from common.doc_store import milvus_capabilities
from common.doc_store.doc_store_base import OrderByExpr
from core.utils import milvus_conn


@pytest.mark.parametrize(
    ("sdk", "server", "expected"),
    [
        ("2.5.11", "3.0.6", False),
        ("3.0.3", "2.6.22", False),
        ("3.0.3", "3.0.6", True),
        ("3.0.3", "v3.0.6", True),
        ("3.0.3", "3.0-20261110-63100f8f5b4", True),
        ("3.0.3", "3.0-20261110-63100f8f5b", True),
        ("3.0.3", "3.0-20261110-63100f8f5b-extra", False),
        ("3.0.3rc1", "3.0.6", False),
        ("3.0.3", "3.0.6-rc1", False),
        ("3.0.3", "3.0-unknown-build", False),
    ],
)
def test_shared_capability_requires_supported_sdk_and_actual_server(monkeypatch: pytest.MonkeyPatch, sdk: str, server: str, expected: bool) -> None:
    probe = MagicMock(return_value=server)
    monkeypatch.setattr(milvus_capabilities, "version", lambda name: sdk)
    monkeypatch.setattr(milvus_capabilities.utility, "get_server_version", probe)

    assert milvus_capabilities.supports_native_query_order_by("owned-alias") is expected
    if sdk in {"2.5.11", "3.0.3rc1"}:
        probe.assert_not_called()
    else:
        probe.assert_called_once_with(using="owned-alias", timeout=5)


def test_shared_capability_does_not_hide_failed_server_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(milvus_capabilities, "version", lambda name: "3.0.3")
    monkeypatch.setattr(milvus_capabilities.utility, "get_server_version", MagicMock(side_effect=ConnectionError("server unavailable")))

    with pytest.raises(ConnectionError, match="unavailable"):
        milvus_capabilities.supports_native_query_order_by("owned-alias")


class QueryConnection:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [
            {"pk": "third", "chunk_order_int": 3, "pagerank_fea": 100},
            {"pk": "first", "chunk_order_int": 1, "pagerank_fea": 50},
            {"pk": "second", "chunk_order_int": 2, "pagerank_fea": 0},
        ]
        self.page = [self.rows[2], self.rows[0]]
        self.fields: list[dict[str, Any]] = [
            {"name": "pk", "type": DataType.VARCHAR, "is_primary": True},
            {"name": "chunk_order_int", "type": DataType.INT64},
            {"name": "pagerank_fea", "type": DataType.INT64},
        ]
        self.calls: list[dict[str, Any]] = []
        self.requests: list[Any] = []
        self.failure: Exception | None = None
        self.failure_on_call: int | None = None
        self.schema_failure: Exception | None = None

    def has_collection(self, collection_name: str) -> bool:
        return True

    def describe_collection(self, collection_name: str) -> dict[str, Any]:
        if self.schema_failure is not None:
            raise self.schema_failure
        return {"fields": self.fields}

    def query(self, collection_name: str, expr: str, *, output_fields: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"collection": collection_name, "expr": expr, "output_fields": output_fields, **kwargs})
        request_options = dict(kwargs)
        use_default = construct_guarantee_ts(collection_name, request_options)
        self.requests.append(Prepare.query_request(collection_name, expr, output_fields, None, use_default_consistency=use_default, **request_options))
        if self.failure is not None and (self.failure_on_call is None or self.failure_on_call == len(self.calls)):
            raise self.failure
        if output_fields == ["count(*)"]:
            return [{"count(*)": len(self.rows)}]
        return self.page if "order_by" in kwargs else self.rows


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, QueryConnection]:
    cls = next(cell.cell_contents for cell in milvus_conn.MilvusConnection.__closure__ if isinstance(cell.cell_contents, type))
    connection = object.__new__(cls)
    connection.logger = MagicMock()
    connection._using = "owned-alias"
    rpc = QueryConnection()
    connection._get_connection = lambda: rpc
    monkeypatch.setattr(milvus_conn, "supports_native_query_order_by", lambda using: using == "owned-alias")
    return connection, rpc


def test_native_page_serializes_order_offset_limit_and_preserves_total(store: tuple[Any, QueryConnection], monkeypatch: pytest.MonkeyPatch) -> None:
    connection, rpc = store
    monkeypatch.setattr(connection, "_sort_by_order_fields", MagicMock(side_effect=AssertionError("Native order must not sort a full candidate set")))

    rows, total = connection.search(["pk"], [], {"kb_id": ["owned"]}, [], OrderByExpr().asc("chunk_order_int").asc("pk"), 1, 2, "chunks", ["owned"])

    assert [row["pk"] for row in rows] == ["second", "third"] and total == 3
    count_request, page_request = rpc.requests
    assert list(count_request.output_fields) == ["count(*)"]
    assert not {"limit", "offset", "order_by_fields"} & {item.key for item in count_request.query_params}
    assert count_request.expr == page_request.expr == "kb_id in ['owned']"
    assert page_request.consistency_level == count_request.consistency_level == get_consistency_level("Strong")
    assert {item.key: item.value for item in page_request.query_params}.items() >= {"limit": "2", "offset": "1", "order_by_fields": "chunk_order_int:asc,pk:asc"}.items()
    assert rpc.calls[1]["output_fields"] == ["pk", "chunk_order_int"]


def test_native_empty_offset_page_keeps_matching_total(store: tuple[Any, QueryConnection]) -> None:
    connection, rpc = store
    rpc.page = []

    assert connection.search(["pk"], [], {}, [], OrderByExpr().asc("chunk_order_int"), 20, 2, "chunks", []) == ([], 3)


def test_explicit_native_order_still_precedes_rank_boost(store: tuple[Any, QueryConnection]) -> None:
    connection, _ = store

    rows, _ = connection.search(["pk", "pagerank_fea"], [], {}, [], OrderByExpr().asc("chunk_order_int"), 1, 2, "chunks", [], rank_feature={"pagerank_fea": 1.0})

    assert [row["pk"] for row in rows] == ["second", "third"]


def test_native_wildcard_projection_preserves_all_fields(store: tuple[Any, QueryConnection]) -> None:
    connection, rpc = store

    connection.search(["*"], [], {}, [], OrderByExpr().asc("chunk_order_int"), 0, 2, "chunks", [])

    assert rpc.calls[-1]["output_fields"] == ["*"]


@pytest.mark.parametrize("case", ["old_server", "nullable_desc", "encoded_integer", "position", "aggregation", "aggregate_projection", "multiple_collections", "unlimited", "large_offset"])
def test_incomparable_or_unsupported_queries_keep_legacy_fallback(store: tuple[Any, QueryConnection], monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    connection, rpc = store
    order = OrderByExpr().asc("chunk_order_int")
    indexes, aggregates, offset, limit = ["chunks"], [], 0, 2
    selected = ["pk"]
    if case == "old_server":
        monkeypatch.setattr(milvus_conn, "supports_native_query_order_by", lambda using: False)
    elif case == "nullable_desc":
        rpc.fields[1]["nullable"] = True
        order = OrderByExpr().desc("chunk_order_int")
    elif case == "encoded_integer":
        rpc.fields[1]["type"] = DataType.VARCHAR
    elif case == "position":
        rpc.fields[1] = {"name": "page_num_int", "type": DataType.VARCHAR}
        order = OrderByExpr().asc("page_num_int")
    elif case == "aggregation":
        aggregates = ["chunk_order_int"]
    elif case == "aggregate_projection":
        selected = ["count(*)"]
    elif case == "multiple_collections":
        indexes.append("other")
    elif case == "unlimited":
        limit = 0
    else:
        offset = 16384

    connection.search(selected, [], {}, [], order, offset, limit, indexes, [], agg_fields=aggregates)

    assert rpc.calls and all(call["limit"] == 10000 for call in rpc.calls)
    assert not any("order_by" in call or "offset" in call for call in rpc.calls)
    assert all(request.consistency_level == get_consistency_level("Strong") for request in rpc.requests)
    assert not any(request.use_default_consistency for request in rpc.requests)


def test_nullable_ascending_order_has_the_same_nulls_last_semantics() -> None:
    fields = {"chunk_order_int": {"type": DataType.INT64, "nullable": True}}

    assert milvus_conn.build_milvus_native_query_order_by(OrderByExpr().asc("chunk_order_int"), fields, True) == ["chunk_order_int:asc"]
    assert milvus_conn.build_milvus_native_query_order_by(OrderByExpr().desc("chunk_order_int"), fields, True) is None


@pytest.mark.parametrize("failure_on_call", [1, 2])
def test_native_count_or_page_failure_does_not_become_an_empty_success(store: tuple[Any, QueryConnection], failure_on_call: int) -> None:
    connection, rpc = store
    rpc.failure = ConnectionError("native query failed")
    rpc.failure_on_call = failure_on_call

    with pytest.raises(ConnectionError, match="native query failed"):
        connection.search(["pk"], [], {}, [], OrderByExpr().asc("chunk_order_int"), 0, 2, "chunks", [])


def test_native_schema_probe_failure_cannot_fall_back_to_partial_sort(store: tuple[Any, QueryConnection]) -> None:
    connection, rpc = store
    rpc.schema_failure = ConnectionError("schema RPC unavailable")

    with pytest.raises(ConnectionError, match="schema RPC unavailable"):
        connection.search(["pk"], [], {}, [], OrderByExpr().asc("chunk_order_int"), 0, 2, "chunks", [])

    assert rpc.calls == []
