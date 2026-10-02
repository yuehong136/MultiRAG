"""Relationship lookup must finish before any availability write."""

from typing import Any

import pytest

from core.utils.infinity_conn import InfinityConnection
from tests.unit.test_infinity_missing_table_update import FakeDatabase, FakeTable, make_store


class RelationshipTable(FakeTable):
    def __init__(self, count: int = 5, error: Exception | None = None) -> None:
        super().__init__()
        self.count, self.read_error = count, error
        self.fields: list[str] = []

    def show_columns(self) -> Any:
        return type("Columns", (), {"rows": lambda self: [(field, "varchar", "", None) for field in ("id", "doc_id", "mom_id")]})()

    def output(self, fields: list[str]) -> "RelationshipTable":
        self.fields = fields
        return self

    def filter(self, condition: str) -> "RelationshipTable":
        return self

    def to_result(self) -> tuple[dict[str, list[Any]], dict[str, Any], dict[str, Any]]:
        if self.read_error:
            raise self.read_error
        if self.fields == ["count(*)"]:
            return {"count(*)": [self.count]}, {}, {}
        return {"id": ["old", "child", "new", "ordinary", "foreign"], "doc_id": ["doc", "doc", "doc", "doc", "other"], "mom_id": ["", "old", "new", "", "ordinary"]}, {}, {}


def test_infinity_legacy_relationships_keep_ordinary_rows_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    table = RelationshipTable()
    store, pool = make_store(InfinityConnection, FakeDatabase(table), monkeypatch)
    # Use the actual condition builder for doc/parent scope, rather than the
    # missing-table fixture's fixed filter.
    from common.doc_store.infinity_conn_base import InfinityConnectionBase

    monkeypatch.setattr(store, "equivalent_condition_to_str", InfinityConnectionBase.equivalent_condition_to_str.__get__(store))
    assert store.update({"doc_id": "doc"}, {"available_int": 1}, "index", "kb")
    hidden, visible = table.updates
    assert hidden[1] == {"available_int": 0} and visible[1] == {"available_int": 1}
    assert "'old'" in hidden[0] and "'new'" in hidden[0] and "ordinary" not in hidden[0] and "other" not in hidden[0]
    assert "NOT" in visible[0] and pool.release_count == 1


@pytest.mark.parametrize("count,error", [(6, None), (5, RuntimeError("controlled relationship read failure"))])
def test_infinity_incomplete_relationships_do_not_write(count: int, error: Exception | None, monkeypatch: pytest.MonkeyPatch) -> None:
    table = RelationshipTable(count, error)
    store, pool = make_store(InfinityConnection, FakeDatabase(table), monkeypatch)
    with pytest.raises((ValueError, RuntimeError)):
        store.update({"doc_id": "doc"}, {"available_int": 1}, "index", "kb")
    assert table.updates == [] and pool.release_count == 1
