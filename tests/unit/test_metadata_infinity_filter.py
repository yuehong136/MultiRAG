"""Candidate safety and Infinity lease/error contracts without infrastructure."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode
from sqlalchemy.orm import Session

from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from common.metadata_infinity_filter import build_infinity_filter, iter_infinity_metadata


@pytest.mark.parametrize("operator", ["≠", "not in", "not contains", "empty", "not empty", "contains", "start with", "end with"])
def test_expanded_array_and_text_clauses_only_prune_presence(operator: str) -> None:
    query = build_infinity_filter([{"key": "tag", "op": operator, "value": "Alice"}], "and", ["kb"])
    assert "JSON_CONTAINS" not in query and "NOT " not in query
    assert "JSON_EXTRACT_STRING(meta_fields, '$.tag') != 'null'" in query
    assert query.startswith("kb_id IN ('kb') AND ")


@pytest.mark.parametrize("value", [["ALICE"], [0, "0"], [10**1000], [], "0,1"])
def test_unprovable_membership_keeps_exact_source_candidates(value: Any) -> None:
    assert "JSON_CONTAINS" not in build_infinity_filter([{"key": "x", "op": "in", "value": value}], "or", ["kb"])


def test_literal_scope_and_boolean_numeric_membership_are_distinct() -> None:
    query = build_infinity_filter([{"key": "x", "op": "in", "value": [0, False, True, 1.5]}], "and", ["O'Reilly"])
    assert "kb_id IN ('O''Reilly')" in query
    assert "'$.x', 0)" in query and "'$.x', 'false')" in query and "'$.x', 'true')" in query
    assert build_infinity_filter([], "and", []) == "1 = 0"
    for value in ("", "unsafe\\scope", "unsafe\nvalue"):
        with pytest.raises(ValueError):
            build_infinity_filter([], "and", [value])


def test_nonliteral_key_cannot_narrow_an_or_or_inject_sql() -> None:
    filters = [{"key": "normal", "op": "in", "value": [0]}, {"key": "x') OR 1=1 --", "op": "in", "value": [1]}]
    assert build_infinity_filter(filters, "or", ["kb"]) == "kb_id IN ('kb')"
    assert "1=1" not in build_infinity_filter(filters, "and", ["kb"])
    with pytest.raises(ValueError):
        build_infinity_filter(filters, "xor", ["kb"])


def _connector(data: dict[str, Any] | None = None) -> tuple[Any, Mock, Mock]:
    table = Mock()
    table.output.return_value = table
    table.filter.return_value = table
    table.to_result.return_value = (data or {"id": ["doc"], "kb_id": ["kb"], "meta_fields": ['{"x": false}']}, {}, {})
    connection = Mock()
    connection.get_database.return_value.get_table.return_value = table
    pool = Mock()
    pool.get_conn.return_value = connection
    return SimpleNamespace(connPool=pool, dbName="owned"), connection, table


@pytest.mark.parametrize("failure", ["database", "table", "query", "malformed", "scope", "duplicate", "json"])
def test_errors_are_not_empty_fallback_and_release_exactly_once(failure: str) -> None:
    connector, connection, table = _connector()
    if failure == "database":
        connection.get_database.side_effect = ConnectionError("unavailable")
    elif failure == "table":
        connection.get_database.return_value.get_table.side_effect = InfinityException(9003, "read failed")
    elif failure == "query":
        table.to_result.side_effect = TimeoutError("unavailable")
    else:
        data = {"id": ["doc"], "kb_id": ["foreign" if failure == "scope" else "kb"], "meta_fields": ["bad json" if failure == "json" else "{}"]}
        if failure == "malformed":
            data["id"] = []
        if failure == "duplicate":
            data = {key: value * 2 for key, value in data.items()}
        table.to_result.return_value = (data, {}, {})
    with pytest.raises((RuntimeError, ConnectionError, TimeoutError, ValueError, InfinityException)):
        list(iter_infinity_metadata(connector, "index", ["kb"]))
    connector.connPool.release_conn.assert_called_once_with(connection)


def test_confirmed_missing_table_and_empty_selection_are_empty() -> None:
    connector, connection, _ = _connector()
    assert list(iter_infinity_metadata(connector, "index", [])) == []
    connector.connPool.get_conn.assert_not_called()
    connection.get_database.return_value.get_table.side_effect = InfinityException(ErrorCode.TABLE_NOT_EXIST, "absent")
    assert list(iter_infinity_metadata(connector, "index", ["kb"])) == []
    connector.connPool.release_conn.assert_called_once_with(connection)


def test_early_iterator_close_releases_the_lease() -> None:
    connector, connection, _ = _connector()
    rows = iter_infinity_metadata(connector, "index", ["kb"])
    assert next(rows) == ("doc", {"x": False})
    rows.close()
    connector.connPool.release_conn.assert_called_once_with(connection)


def test_infinity_dispatch_verifies_original_typed_values(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    connector, connection, table = _connector({"id": ["false", "zero"], "kb_id": ["kb", "kb"], "meta_fields": ['{"x": false}', '{"x": 0}']})
    monkeypatch.setattr(settings, "DOC_ENGINE", "infinity")
    monkeypatch.setitem(settings.__dict__, "docStoreConn", connector)
    monkeypatch.setattr(DocMetadataService, "_metadata_groups", staticmethod(lambda _db, _kbs: {"tenant": ["kb"]}))
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(EngineMetadataStore))
    assert DocMetadataService.filter_doc_ids_by_meta_pushdown(db, ["kb"], [{"key": "x", "op": "in", "value": [False]}]) == ["false"]
    connector.connPool.release_conn.assert_called_once_with(connection)
    table.to_result.assert_called_once()
