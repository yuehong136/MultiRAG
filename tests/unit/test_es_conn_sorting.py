"""Capture real DSL requests for chunk and memory sorting without an ES service."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.doc_store.doc_store_base import MatchDenseExpr, MatchExpr, OrderByExpr
from core.utils import es_conn as core_es
from memory.utils import es_conn as memory_es


def _connection(backend: str) -> Any:
    factory = core_es.ESConnection if backend == "core" else memory_es.ESConnection
    cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.logger = MagicMock()
    conn.index_exist = MagicMock(return_value=True)
    conn.es = MagicMock()
    conn.es.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}, "timed_out": False}
    return conn


def _search(conn: Any, order: OrderByExpr | None, offset: int = 0, expressions: list[MatchExpr] | None = None) -> dict:
    conn.search(["id"], [], {}, expressions or [], order, offset, 10, "tenant", ["scope1"])
    return conn.es.search.call_args.kwargs["body"]


@pytest.mark.parametrize("backend", ["core", "memory"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_id_only_sort_is_omitted(backend: str, direction: str) -> None:
    order = getattr(OrderByExpr(), direction)("id")
    conn = _connection(backend)
    query = _search(conn, order)
    assert "sort" not in query
    assert query["size"] == 10
    assert order.fields == [("id", 0 if direction == "asc" else 1)]


@pytest.mark.parametrize("backend", ["core", "memory"])
def test_mixed_sort_keeps_other_fields_order_and_options(backend: str) -> None:
    order = OrderByExpr().asc("id").desc("create_timestamp_flt").asc("page_num_int").desc("top_int").asc("docnm_kwd").desc("id")
    query = _search(_connection(backend), order)
    number = {"order": "asc", "unmapped_type": "float"}
    top = {"order": "desc", "unmapped_type": "float"}
    if backend == "core":
        number.update(mode="avg", numeric_type="double")
        top.update(mode="avg", numeric_type="double")
    assert query["sort"] == [
        {"create_timestamp_flt": {"order": "desc", "unmapped_type": "float"}},
        {"page_num_int": number},
        {"top_int": top},
        {"docnm_kwd": {"order": "asc", "unmapped_type": "text"}},
    ]


@pytest.mark.parametrize("backend", ["core", "memory"])
@pytest.mark.parametrize("order", [None, OrderByExpr()])
def test_no_sort_behavior_is_unchanged(backend: str, order: OrderByExpr | None) -> None:
    assert "sort" not in _search(_connection(backend), order)


def test_id_only_deep_page_does_not_enter_search_after() -> None:
    conn = _connection("core")
    conn._search_with_search_after = MagicMock()
    query = _search(conn, OrderByExpr().asc("id"), offset=core_es.MAX_RESULT_WINDOW)
    conn._search_with_search_after.assert_not_called()
    assert "sort" not in query
    assert query["from"] == core_es.MAX_RESULT_WINDOW


def test_mixed_sort_deep_page_preserves_real_search_after_cursor() -> None:
    conn = _connection("core")
    conn.es.search.side_effect = [
        {"hits": {"hits": [{"_id": str(i), "sort": [i]} for i in range(1000)], "total": {"value": 1001}}},
        {"hits": {"hits": [{"_id": "wanted", "sort": [1000]}]}},
    ]
    result = conn.search(["id"], [], {}, [], OrderByExpr().asc("id").asc("_order_id"), 1000, 9001, "tenant", ["kb1"])
    assert result["hits"]["hits"] == [{"_id": "wanted", "sort": [1000]}]
    queries = [call.kwargs["body"] for call in conn.es.search.call_args_list]
    assert len(queries) == 2
    assert queries[1]["search_after"] == [999]
    for query in queries:
        assert query["sort"] == [{"_order_id": {"order": "asc", "unmapped_type": "text"}}]
        assert "from" not in query


def test_dense_search_keeps_existing_pagination() -> None:
    conn = _connection("core")
    conn._search_with_search_after = MagicMock()
    dense = MatchDenseExpr("q_3_vec", [0.1, 0.2, 0.3], "float", "cosine")
    query = _search(conn, OrderByExpr().asc("id").desc("create_timestamp_flt"), core_es.MAX_RESULT_WINDOW, [dense])
    conn._search_with_search_after.assert_not_called()
    assert "knn" in query
    assert query["from"] == core_es.MAX_RESULT_WINDOW
    assert query["sort"] == [{"create_timestamp_flt": {"order": "desc", "unmapped_type": "float"}}]


@pytest.mark.parametrize(("method", "field"), [("get_forgotten_messages", "forget_at"), ("get_missing_field_message", "valid_at")])
def test_memory_maintenance_sort_is_unchanged(method: str, field: str) -> None:
    conn = _connection("memory")
    args = (["id"], "tenant", "memory1")
    if method == "get_missing_field_message":
        args += ("content_ltks",)
    getattr(conn, method)(*args)
    assert conn.es.search.call_args.kwargs["body"]["sort"] == [{field: {"order": "asc", "unmapped_type": "text"}}]
