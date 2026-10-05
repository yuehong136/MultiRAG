"""Infinity RPC failures must not become empty or partial search successes."""

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode

from common.constants import PAGERANK_FLD
from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
from core.utils.infinity_conn import InfinityConnection


def make_store() -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store.logger = MagicMock()
    store.dbName = "scratch"
    pool = MagicMock()
    store.connPool = pool
    database = pool.get_conn.return_value.get_database.return_value
    table = database.get_table.return_value
    table.show_columns.return_value.rows.return_value = []
    for method in ("output", "match_dense", "offset", "limit", "option"):
        getattr(table, method).return_value = table
    table.to_df.return_value = (pd.DataFrame([{"id": "visible", "SIMILARITY": 0.9, PAGERANK_FLD: 0.0}]), {"total_hits_count": 1})
    return store, pool, database, table


def search(store: Any, condition: dict[str, Any], kb_ids: list[str]) -> tuple[pd.DataFrame, int]:
    return store.search(["id"], [], condition, [MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10)], OrderByExpr(), 0, 10, ["chunks"], kb_ids)


@pytest.mark.parametrize("phase", ["condition_table", "schema", "scatter_table", "second_table", "query"])
@pytest.mark.parametrize("sdk_error", [False, True])
def test_search_failure_propagates_and_releases_connection(phase: str, sdk_error: bool) -> None:
    store, pool, database, table = make_store()
    failure = InfinityException(9003, "RPC failed") if sdk_error else ConnectionError("table RPC transport failure")
    condition = {"doc_id": ["doc"], "available_int": 1}
    kb_ids = ["kb"]
    if phase == "condition_table":
        database.get_table.side_effect = failure
    elif phase == "schema":
        table.show_columns.side_effect = failure
    elif phase == "scatter_table":
        condition = {}
        database.get_table.side_effect = failure
    elif phase == "second_table":
        kb_ids.append("second")
        # The first lookup builds the condition; the next queries one table.
        database.get_table.side_effect = [table, table, failure]
    else:
        table.to_df.side_effect = failure

    with pytest.raises(type(failure)) as caught:
        search(store, condition, kb_ids)

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


@pytest.mark.parametrize("has_condition", [False, True])
def test_confirmed_missing_tables_preserve_unparsed_dataset_compatibility(has_condition: bool) -> None:
    store, pool, database, _ = make_store()
    database.get_table.side_effect = InfinityException(ErrorCode.TABLE_NOT_EXIST, "missing table")

    result = search(store, {"doc_id": ["doc"]} if has_condition else {}, ["kb"])

    assert store.get_total(result) == 0
    assert store.get_doc_ids(result) == []
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


def test_successful_query_with_no_matches_is_empty() -> None:
    store, pool, _, table = make_store()
    table.to_df.return_value = (pd.DataFrame(), {"total_hits_count": 0})

    result = search(store, {"doc_id": ["absent"], "available_int": 1}, ["kb"])

    assert store.get_total(result) == 0
    assert store.get_doc_ids(result) == []
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


def test_schema_table_not_exist_is_not_a_confirmed_absent_table() -> None:
    store, pool, _, table = make_store()
    failure = InfinityException(ErrorCode.TABLE_NOT_EXIST, "table disappeared during schema RPC")
    table.show_columns.side_effect = failure

    with pytest.raises(InfinityException) as caught:
        search(store, {"doc_id": ["doc"]}, ["kb"])

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)
