"""Failure, replay and connection ownership contracts without external services."""

import importlib
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from infinity.common import ConflictType, InfinityException
from infinity.errors import ErrorCode

from common.doc_store import infinity_conn_base as base
from common.doc_store import infinity_metadata as retry
from core.utils.infinity_conn import InfinityConnection


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (InfinityException(9003, "Resource busy: catalog counter"), True),
        (InfinityException(error_code=9003, error_msg="Resource busy"), True),
        (Exception(9003, "Resource busy"), True),
        (Exception((9003, "Resource busy")), True),
        (Exception(("9003", "Resource busy")), True),
        (Exception("RocksDB: RESOURCE BUSY"), True),
        (InfinityException(9003, "IO error: disk full"), False),
        (InfinityException(9003, "Corruption"), False),
        (InfinityException(9003), False),
        (InfinityException(4005, "Transaction is conflicted"), False),
        (InfinityException(ErrorCode.TABLE_NOT_EXIST, "RocksDB Resource busy"), False),
        (Exception((3022, "RocksDB Resource busy")), False),
        (Exception("Resource busy"), False),
        (TimeoutError("socket timeout"), False),
        (Exception("RocksDB not found"), False),
    ],
)
def test_sdk_and_legacy_error_shapes(exc: Exception, expected: bool) -> None:
    assert retry._is_meta_contention_error(exc) is expected


@pytest.mark.parametrize("attempts", [0, -1, 11, 10**100, True, 1.5, "5", None])
def test_invalid_attempt_budget_never_calls_operation(attempts: Any) -> None:
    op = Mock()
    with pytest.raises(ValueError, match="max_attempts"):
        retry._retry_on_meta_contention("create", op, max_attempts=attempts)
    op.assert_not_called()


@pytest.mark.parametrize("delay", [-1, 1001, 10**100, True, 1.5, "50", None, float("nan"), float("inf")])
def test_invalid_delay_never_calls_operation(delay: Any) -> None:
    op = Mock()
    with pytest.raises(ValueError, match="base_delay"):
        retry._retry_on_meta_contention("create", op, base_delay_ms=delay)
    op.assert_not_called()


@pytest.mark.parametrize(("raw", "expected"), [(None, 5), ("", 5), ("bad", 5), ("1.5", 5), ("-1", 5), ("0", 5), ("11", 5), ("9" * 5000, 5), (" 1 ", 1), ("10", 10)])
def test_env_attempt_bounds(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    monkeypatch.delenv("INFINITY_META_RETRY_MAX", raising=False)
    if raw is not None:
        monkeypatch.setenv("INFINITY_META_RETRY_MAX", raw)
    assert retry._int_env("INFINITY_META_RETRY_MAX", 5, 1, 10) == expected


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("1000", 1000), ("-1", 50), ("1001", 50), ("inf", 50)])
def test_env_delay_bounds(monkeypatch: pytest.MonkeyPatch, raw: str, expected: int) -> None:
    monkeypatch.setenv("INFINITY_META_RETRY_BASE_DELAY_MS", raw)
    assert retry._int_env("INFINITY_META_RETRY_BASE_DELAY_MS", 50, 0, 1000) == expected


@pytest.mark.parametrize(("attempts", "delay", "waits"), [(1, 0, []), (5, 50, [0.075, 0.15, 0.3, 0.6]), (10, 1000, [1.5] * 9), (5, 0, [0] * 4)])
def test_exhaustion_keeps_original_error_and_bounded_backoff(monkeypatch: pytest.MonkeyPatch, attempts: int, delay: int, waits: list[float]) -> None:
    sleep = Mock()
    monkeypatch.setattr(retry.time, "sleep", sleep)
    monkeypatch.setattr(retry.random, "uniform", lambda lower, upper: upper)
    error = InfinityException(9003, "Resource busy")
    op = Mock(side_effect=error)
    with pytest.raises(InfinityException) as caught:
        retry._retry_on_meta_contention("drop_table", op, max_attempts=attempts, base_delay_ms=delay)
    assert caught.value is error
    assert op.call_count == attempts
    assert [c.args[0] for c in sleep.call_args_list] == pytest.approx(waits)


@pytest.mark.parametrize(
    "error", [InfinityException(9003, "Corruption"), InfinityException(4005, "Transaction is conflicted"), RuntimeError("bad schema"), TimeoutError("timeout"), KeyboardInterrupt()]
)
def test_other_errors_and_interrupts_are_not_retried(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    sleep = Mock()
    monkeypatch.setattr(retry.time, "sleep", sleep)
    op = Mock(side_effect=error)
    with pytest.raises(type(error)) as caught:
        retry._retry_on_meta_contention("create_index", op)
    assert caught.value is error
    assert op.call_count == 1
    sleep.assert_not_called()


class Catalog:
    """Simulate a metadata counter collision independently at every DDL step."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.failure_at: str | None = None
        self.error: BaseException = InfinityException(9003, "Resource busy")
        self.fail_once = True
        self.columns: list[str] = []

    def call(self, key: str, conflict: ConflictType) -> None:
        assert conflict == ConflictType.Ignore
        self.calls[key] += 1
        if key == self.failure_at or (self.fail_once and self.calls[key] == 1):
            raise self.error

    def create_database(self, name: str, conflict: ConflictType) -> "Catalog":
        self.call("database", conflict)
        return self

    def create_table(self, name: str, schema: dict[str, Any], conflict: ConflictType) -> "Catalog":
        self.call("table", conflict)
        return self

    def create_index(self, name: str, info: Any, conflict: ConflictType) -> SimpleNamespace:
        self.call(name, conflict)
        return SimpleNamespace(error_code=0)

    def drop_table(self, name: str, conflict: ConflictType) -> SimpleNamespace:
        # Real dev5 Ignore path returns error responses instead of raising.
        try:
            self.call("drop", conflict)
        except InfinityException as exc:
            return SimpleNamespace(error_code=exc.error_code, error_msg=exc.error_msg)
        return SimpleNamespace(error_code=0)

    def get_database(self, name: str) -> "Catalog":
        return self

    def get_table(self, name: str) -> "Catalog":
        return self

    def list_tables(self) -> SimpleNamespace:
        return SimpleNamespace(table_names=["multirag_existing"])

    def list_indexes(self) -> SimpleNamespace:
        return SimpleNamespace(index_names=["q_vec_idx"])

    def show_columns(self) -> dict[str, list[str]]:
        return {"name": self.columns}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    instance = object.__new__(cls)
    catalog = Catalog()
    instance.connPool = SimpleNamespace(get_conn=Mock(return_value=catalog), release_conn=Mock())
    instance.dbName = "test_db"
    instance.mapping_file_name = "infinity_mapping.json"
    instance.table_name_prefix = "multirag_"
    instance.logger = logging.getLogger(__name__)
    schema = {
        "content": {"type": "varchar", "analyzer": ["rag-coarse", "rag-fine"]},
        "tag": {"type": "varchar", "analyzer": "keyword"},
        "id": {"type": "varchar", "index_type": "secondary"},
        "kb_id": {"type": "varchar", "index_type": {"type": "secondary", "cardinality": "low"}},
    }
    catalog.columns = list(schema)
    (tmp_path / "configs").mkdir()
    for name in [instance.mapping_file_name, "doc_meta_infinity_mapping.json"]:
        (tmp_path / "configs" / name).write_text(json.dumps(schema))
    monkeypatch.setattr(base, "get_project_base_directory", lambda: str(tmp_path))
    monkeypatch.setattr(retry.time, "sleep", Mock())
    return instance


def invoke(store: Any, path: str) -> Any:
    if path == "chunks":
        return store.create_idx("chunks", "kb", 4)
    if path == "metadata":
        return store.create_doc_meta_idx("meta")
    if path == "migration":
        return store._migrate_db(store.connPool.get_conn.return_value)
    return store.delete_idx("meta", "")


@pytest.mark.parametrize("path", ["chunks", "metadata", "delete", "migration"])
def test_all_safe_ddl_steps_retry_and_release(store: Any, path: str) -> None:
    result = invoke(store, path)
    catalog = store.connPool.get_conn.return_value
    assert catalog.calls and all(count == 2 for count in catalog.calls.values())
    if path in {"chunks", "metadata"}:
        assert result is True
    if path == "chunks":
        assert set(catalog.calls) == {"database", "table", "q_vec_idx", "ft_content_rag_coarse", "ft_content_rag_fine", "ft_tag_keyword", "sec_id", "sec_kb_id"}
    if path == "migration":
        store.connPool.release_conn.assert_not_called()  # caller owns this lease
    else:
        store.connPool.release_conn.assert_called_once_with(catalog)


@pytest.mark.parametrize(
    ("path", "step"),
    [
        ("chunks", "database"),
        ("chunks", "table"),
        ("chunks", "q_vec_idx"),
        ("chunks", "ft_tag_keyword"),
        ("chunks", "sec_id"),
        ("chunks", "sec_kb_id"),
        ("metadata", "database"),
        ("metadata", "table"),
        ("metadata", "idx_meta_id"),
        ("metadata", "idx_meta_kb_id"),
        ("delete", "drop"),
    ],
)
@pytest.mark.parametrize("contention", [True, False])
def test_failures_never_report_success_or_leak(store: Any, path: str, step: str, contention: bool) -> None:
    catalog = store.connPool.get_conn.return_value
    catalog.fail_once = False
    catalog.failure_at = step
    catalog.error = InfinityException(9003, "Resource busy" if contention else "Corruption")
    if path == "metadata":
        assert invoke(store, path) is False
    else:
        with pytest.raises(InfinityException):
            invoke(store, path)
    assert catalog.calls[step] == (retry._META_RETRY_MAX if contention else 1)
    store.connPool.release_conn.assert_called_once_with(catalog)


@pytest.mark.parametrize("path", ["chunks", "metadata"])
@pytest.mark.parametrize("invalid_json", [False, True])
def test_missing_or_invalid_mapping_releases(store: Any, tmp_path: Path, path: str, invalid_json: bool) -> None:
    for file in (tmp_path / "configs").iterdir():
        if invalid_json:
            file.write_text("{")
        else:
            file.unlink()
    if path == "metadata":
        assert invoke(store, path) is False
    else:
        with pytest.raises(Exception):
            invoke(store, path)
    store.connPool.release_conn.assert_called_once()


@pytest.fixture
def pool_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import infinity.connection_pool

    monkeypatch.setattr(base.settings, "INFINITY", {"uri": "localhost:23817", "db_name": "test_db"})
    name = "common.doc_store.infinity_conn_pool"
    was_loaded = name in sys.modules
    connection = SimpleNamespace(show_current_node=Mock(return_value=SimpleNamespace(error_code=0, server_status="alive")))
    pool = SimpleNamespace(get_conn=Mock(return_value=connection), release_conn=Mock(), destroy=Mock())

    class FakePool:
        def __new__(cls, *args: Any, **kwargs: Any) -> Any:
            return pool

    monkeypatch.setattr(infinity.connection_pool, "ConnectionPool", FakePool)
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "INFINITY_CONN", SimpleNamespace(get_conn_pool=Mock(return_value=pool), refresh_conn_pool=Mock(return_value=pool)))
    yield module
    if not was_loaded:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("error", [InfinityException(9003, "Resource busy"), RuntimeError("bad mapping"), KeyboardInterrupt()])
def test_startup_migration_is_not_replayed_and_returns_both_leases(store: Any, pool_module: Any, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    pool = pool_module.INFINITY_CONN.get_conn_pool()
    pool.get_conn.reset_mock()
    pool.release_conn.reset_mock()
    migrate = Mock(side_effect=error)
    monkeypatch.setattr(store, "_migrate_db", migrate)
    with pytest.raises(type(error)):
        base.InfinityConnectionBase.__init__(store)
    migrate.assert_called_once()
    assert pool.get_conn.call_count == pool.release_conn.call_count == 2
    pool_module.INFINITY_CONN.refresh_conn_pool.assert_not_called()


@pytest.mark.parametrize("healthy", [True, False])
def test_refresh_returns_lease_before_replacing_pool(pool_module: Any, healthy: bool) -> None:
    cls = next(cell.cell_contents for cell in pool_module.InfinityConnectionPool.__closure__ if isinstance(cell.cell_contents, type))
    wrapper = object.__new__(cls)
    connection = SimpleNamespace(show_current_node=Mock(return_value=SimpleNamespace(error_code=0, server_status="alive")))
    if not healthy:
        connection.show_current_node.side_effect = RuntimeError("disconnected")
    events: list[str] = []
    pool = SimpleNamespace(get_conn=Mock(return_value=connection), release_conn=Mock(side_effect=lambda conn: events.append("release")), destroy=Mock(side_effect=lambda: events.append("destroy")))
    wrapper.conn_pool = pool
    wrapper.infinity_uri = None
    wrapper.pool_max_size = 1
    wrapper.refresh_conn_pool()
    pool.release_conn.assert_called_once_with(connection)
    assert events == (["release"] if healthy else ["release", "destroy"])


@pytest.mark.parametrize("path", ["chunks", "metadata", "delete"])
def test_failed_acquisition_has_no_lease_to_release(store: Any, path: str) -> None:
    store.connPool.get_conn.side_effect = RuntimeError("pool unavailable")
    with pytest.raises(RuntimeError, match="pool unavailable"):
        invoke(store, path)
    store.connPool.release_conn.assert_not_called()


@pytest.mark.parametrize("path", ["chunks", "metadata", "delete"])
def test_interrupt_during_ddl_still_releases(store: Any, path: str) -> None:
    catalog = store.connPool.get_conn.return_value
    catalog.error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        invoke(store, path)
    store.connPool.release_conn.assert_called_once_with(catalog)


def test_nonreplayable_column_migration_is_not_retried(store: Any) -> None:
    catalog = store.connPool.get_conn.return_value
    catalog.columns = []
    error = InfinityException(9003, "Resource busy")
    catalog.add_columns = Mock(side_effect=error)
    with pytest.raises(InfinityException) as caught:
        invoke(store, "migration")
    assert caught.value is error
    catalog.add_columns.assert_called_once()


def test_startup_health_failure_releases_every_lease(store: Any, pool_module: Any) -> None:
    pool = pool_module.INFINITY_CONN.get_conn_pool()
    pool.get_conn.reset_mock()
    pool.release_conn.reset_mock()
    pool.get_conn.return_value.show_current_node.side_effect = RuntimeError("unhealthy")
    with pytest.raises(Exception, match="unhealthy in 120s"):
        base.InfinityConnectionBase.__init__(store)
    assert pool.get_conn.call_count == pool.release_conn.call_count == 24
