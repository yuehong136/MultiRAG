"""Concurrent real SDK metadata operations in an owned database, with readback."""

import logging
import os
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from typing import Any
from uuid import uuid4

import infinity
import pytest
from infinity.common import ConflictType, InfinityException, NetworkAddress
from infinity.connection_pool import ConnectionPool

from common.config_utils import CONFIGS
from common.doc_store.infinity_metadata import _retry_on_meta_contention
from core.utils.infinity_conn import InfinityConnection


class TrackedPool:
    def __init__(self, uri: NetworkAddress, workers: int) -> None:
        self.pool = ConnectionPool(uri, max_size=workers)
        self.lock = Lock()
        self.active: set[int] = set()
        self.acquired = 0
        self.released = 0

    def get_conn(self) -> Any:
        conn = self.pool.get_conn()
        with self.lock:
            assert id(conn) not in self.active
            self.active.add(id(conn))
            self.acquired += 1
        return conn

    def release_conn(self, conn: Any) -> None:
        with self.lock:
            self.active.remove(id(conn))  # catches double release
            self.released += 1
        self.pool.release_conn(conn)


@pytest.fixture
def metadata_store() -> Iterator[tuple[Any, Any, TrackedPool]]:
    uri = os.environ.get("INFINITY_TEST_URI", CONFIGS["infinity"]["uri"])
    host, port = uri.rsplit(":", 1)
    address = NetworkAddress(host, int(port))
    reader = infinity.connect(address)
    pool = None
    name = "meta_retry_" + uuid4().hex
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    store.dbName = name
    store.mapping_file_name = "infinity_mapping.json"
    store.table_name_prefix = "multirag_"
    store.logger = logging.getLogger("test.infinity.metadata")
    try:
        pool = TrackedPool(address, 12)
        store.connPool = pool
        yield store, reader, pool
    finally:
        try:
            try:
                if pool is not None:
                    assert not pool.active
                    assert pool.acquired == pool.released
            finally:
                if pool is not None:
                    pool.pool.destroy()
                reader.get_database("default_db")
                _retry_on_meta_contention("cleanup database", lambda: reader.drop_database(name, ConflictType.Ignore))
                assert name not in reader.list_databases().db_names
        finally:
            reader.disconnect()


@pytest.mark.parametrize("shared_table", [False, True])
def test_concurrent_create_and_drop_with_independent_readback(
    metadata_store: tuple[Any, Any, TrackedPool], shared_table: bool, caplog: pytest.LogCaptureFixture, record_property: Callable[[str, object], None]
) -> None:
    store, reader, pool = metadata_store
    workers = 12
    caplog.set_level(logging.INFO, logger=store.logger.name)
    barrier = Barrier(workers, timeout=30)

    def create(worker: int) -> None:
        suffix = "shared" if shared_table else str(worker)
        barrier.wait()
        assert store.create_idx("chunks", suffix, 4) is True
        assert store.create_doc_meta_idx("meta_" + suffix) is True

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(create, range(workers)))
    suffixes = ["shared"] if shared_table else [str(i) for i in range(workers)]
    db = reader.get_database(store.dbName)
    assert set(db.list_tables().table_names) == {name for suffix in suffixes for name in ["chunks_" + suffix, "meta_" + suffix]}
    for suffix in suffixes:
        assert {"q_vec_idx", "sec_kb_id", "sec_available_int", "ft_content_rag_coarse", "ft_content_rag_fine"} <= set(db.get_table("chunks_" + suffix).list_indexes().index_names)
        assert set(db.get_table("meta_" + suffix).list_indexes().index_names) == {f"idx_meta_{suffix}_id", f"idx_meta_{suffix}_kb_id"}
    assert pool.acquired == pool.released == workers * 2
    assert not pool.active

    def drop(worker: int) -> list[int]:
        suffix = "shared" if shared_table else str(worker)
        barrier.wait()
        errors = []
        for index, dataset in [("chunks", suffix), ("meta_" + suffix, "")]:
            try:
                store.delete_idx(index, dataset)
            except InfinityException as exc:
                # Concurrent drops of the SAME object can fail with a separate
                # transaction conflict. This must surface, not become success
                # or widen the RocksDB-only retry policy.
                assert shared_table and exc.error_code == 4005
                errors.append(exc.error_code)
        return errors

    with ThreadPoolExecutor(max_workers=workers) as executor:
        drop_errors = list(executor.map(drop, range(workers)))
    assert db.list_tables().table_names == []
    assert pool.acquired == pool.released == workers * 4
    assert not pool.active
    record_property("workers", workers)
    record_property("leases_acquired_released", pool.released)
    record_property("observed_contention_retries", sum("contention (" in record.message for record in caplog.records))
    record_property("surfaced_same_table_drop_conflicts", sum(len(errors) for errors in drop_errors))
