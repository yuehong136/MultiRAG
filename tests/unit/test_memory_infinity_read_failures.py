"""Real memory Infinity reads distinguish missing tables from failed storage reads."""

from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode

from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from memory.services.messages import MessageService
from memory.utils.infinity_conn import InfinityConnection


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MagicMock, MagicMock, MagicMock]:
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    connection = object.__new__(cls)
    connection.logger = MagicMock()
    connection.dbName = "owned-unit"
    pool = MagicMock()
    connection.connPool = pool
    database = pool.get_conn.return_value.get_database.return_value
    table = database.get_table.return_value
    row = {"id": "bank_1", "message_id": 1, "memory_id": "bank", "message_type_kwd": "raw", "source_id": 0, "content": "source body", "status_int": 1}
    table.show_columns.return_value.rows.return_value = [(field, "Varchar", "", "") for field in row]
    for method in ("output", "filter", "sort", "offset", "limit", "option"):
        getattr(table, method).return_value = table
    table.to_df.return_value = (pd.DataFrame([row]), {"total_hits_count": 1})
    connection.equivalent_condition_to_str = MagicMock(return_value="message_id > 0")
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", connection)
    return connection, pool, database, table


@pytest.mark.parametrize("phase", ["table", "query"])
@pytest.mark.parametrize("failure", [ConnectionError("transport failed"), PermissionError("forbidden"), InfinityException(ErrorCode.TXN_CONFLICT, "transaction conflict")])
def test_source_lookup_storage_failure_propagates_instead_of_missing(store: tuple[Any, MagicMock, MagicMock, MagicMock], phase: str, failure: Exception) -> None:
    _, pool, database, table = store
    if phase == "table":
        database.get_table.side_effect = failure
    else:
        table.to_df.side_effect = failure

    with pytest.raises(type(failure)) as caught:
        MessageService.get_source_messages("owner", "bank", [1])

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


def test_confirmed_missing_source_table_preserves_absence_and_releases(store: tuple[Any, MagicMock, MagicMock, MagicMock]) -> None:
    _, pool, database, _ = store
    database.get_table.side_effect = InfinityException(ErrorCode.TABLE_NOT_EXIST, "missing table")

    assert MessageService.get_source_messages("owner", "bank", [1]) == {}
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


def test_source_lookup_preserves_output_and_query_predicate(store: tuple[Any, MagicMock, MagicMock, MagicMock]) -> None:
    _, pool, database, table = store

    sources = MessageService.get_source_messages("owner", "bank", [1])

    assert sources[1]["message_id"] == 1 and sources[1]["content"] == "source body"
    assert sources[1]["message_type"] == "raw" and sources[1]["status"] == 1
    database.get_table.assert_called_once_with("memory_owner_bank")
    table.filter.assert_called_once_with("id = 'bank_1'")
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


@pytest.mark.parametrize("phase", ["condition_table", "predicate", "scatter_table", "second_table", "schema", "query"])
@pytest.mark.parametrize("failure", [ConnectionError("RPC failed"), InfinityException(ErrorCode.TXN_CONFLICT, "transaction conflict")])
def test_bounded_search_storage_failure_is_never_an_empty_or_partial_success(store: tuple[Any, MagicMock, MagicMock, MagicMock], phase: str, failure: Exception) -> None:
    connection, pool, database, table = store
    condition: dict[str, Any] = {"message_id": [1]}
    memories = ["bank"]
    if phase == "condition_table":
        database.get_table.side_effect = failure
    elif phase == "predicate":
        connection.equivalent_condition_to_str.side_effect = failure
    elif phase == "scatter_table":
        condition = {}
        database.get_table.side_effect = failure
    elif phase == "second_table":
        memories.append("second")
        database.get_table.side_effect = [table, table, failure]
    elif phase == "schema":
        table.show_columns.side_effect = failure
    else:
        table.to_df.side_effect = failure

    with pytest.raises(type(failure)) as caught:
        connection.search(["message_id"], [], condition, [], OrderByExpr(), 0, 100, ["memory_owner"], memories, hide_forgotten=False)

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


@pytest.mark.parametrize("has_condition", [False, True])
def test_confirmed_missing_search_table_remains_empty(store: tuple[Any, MagicMock, MagicMock, MagicMock], has_condition: bool) -> None:
    connection, pool, database, _ = store
    database.get_table.side_effect = InfinityException(ErrorCode.TABLE_NOT_EXIST, "missing table")

    result, total = connection.search(["message_id"], [], {"message_id": [1]} if has_condition else {}, [], OrderByExpr(), 0, 100, ["memory_owner"], ["bank"], hide_forgotten=False)

    assert result.empty and total == 0
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


@pytest.mark.parametrize("phase", ["table", "query"])
def test_recent_bounded_page_propagates_real_adapter_failure(store: tuple[Any, MagicMock, MagicMock, MagicMock], phase: str) -> None:
    _, pool, database, table = store
    failure = ConnectionError("bounded page RPC failed")
    if phase == "table":
        database.get_table.side_effect = failure
    else:
        table.to_df.side_effect = failure

    with pytest.raises(ConnectionError) as caught:
        MessageService.get_recent_page("owner", "bank", "agent", "session", 10)

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


def test_table_not_exist_from_predicate_construction_is_still_a_failure(store: tuple[Any, MagicMock, MagicMock, MagicMock]) -> None:
    connection, pool, _, _ = store
    failure = InfinityException(ErrorCode.TABLE_NOT_EXIST, "table disappeared after lookup")
    connection.equivalent_condition_to_str.side_effect = failure

    with pytest.raises(InfinityException) as caught:
        connection.search(["message_id"], [], {"message_id": [1]}, [], OrderByExpr(), 0, 100, ["memory_owner"], ["bank"], hide_forgotten=False)

    assert caught.value is failure
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)
