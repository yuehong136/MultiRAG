"""Infinity JSON conversion stays local and never mutates a shared RAPTOR payload."""

import copy
import json
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from core.utils.infinity_conn import InfinityConnection


@pytest.fixture
def store() -> tuple[Any, MagicMock, MagicMock]:
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    connection = object.__new__(cls)
    pool = MagicMock()
    table = pool.get_conn.return_value.get_database.return_value.get_table.return_value
    table.show_columns.return_value = SimpleNamespace(rows=lambda: [("id", "Varchar", "", ""), ("extra", "Varchar", "", "")])
    connection.connPool = pool
    connection.dbName = "scratch"
    connection.logger = logging.getLogger(__name__)
    return connection, table, pool


@pytest.mark.parametrize("payload", [{"raptor_method": "psi", "flags": [False, 0, "中文"]}, {}, '{"raptor_method":"raptor"}', "", None])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_extra_conversion_preserves_input(store: tuple[Any, MagicMock, MagicMock], payload: Any, operation: str) -> None:
    connection, table, pool = store
    values = {"extra": payload}
    original = copy.deepcopy(values)
    if operation == "insert":
        row = {"id": "summary", "pk": "summary", **values}
        before = copy.deepcopy(row)
        assert connection.insert([row], "chunks", "kb") == []
        written = table.insert.call_args.args[0][0]
        assert row == before
        assert "pk" not in written
    else:
        assert connection.update({"id": "summary"}, values, "chunks", "kb") is True
        written = table.update.call_args.args[1]
    assert values == original
    assert written["extra"] == (json.dumps(payload) if isinstance(payload, dict) else payload or "")
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_invalid_extra_fails_before_any_write(store: tuple[Any, MagicMock, MagicMock], operation: str) -> None:
    connection, table, pool = store
    with pytest.raises(TypeError):
        if operation == "insert":
            connection.insert([{"id": "existing", "extra": {"invalid": {1}}}], "chunks", "kb")
        else:
            connection.update({"id": "existing"}, {"extra": {"invalid": {1}}}, "chunks", "kb")
    table.delete.assert_not_called()
    table.insert.assert_not_called()
    table.update.assert_not_called()
    pool.release_conn.assert_called_once_with(pool.get_conn.return_value)
