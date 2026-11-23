"""Preparation is pass-local; every row read and every failure remain real."""

import asyncio
from collections import Counter
from threading import Event
from typing import Any

import pytest

from common import settings
from memory.revisions.domain import RevisionStorageUnknown, RevisionValidationError
from memory.revisions.storage import ManagedMessageStore
from memory.utils.milvus_read_preparation import MilvusReadPreparation, SourceQuery
from tests.unit.test_memory_revision_storage import Physical


def test_preparation_is_lazy_and_rows_are_never_cached() -> None:
    calls: list[str] = []
    state = {"content": "before"}

    def prepare() -> SourceQuery:
        calls.append("prepare")

        def query(identities: list[int]) -> list[dict[str, Any]]:
            calls.append("query")
            return [{"message_id": identities[0], "content": state["content"]}]

        return query

    reader = MilvusReadPreparation(prepare)
    assert calls == []
    assert reader.query([1])[0]["content"] == "before"
    state["content"] = "after"
    assert reader.query([1])[0]["content"] == "after"
    reader.close()
    assert calls == ["prepare", "query", "query"]
    with pytest.raises(RuntimeError, match="closed"):
        reader.query([1])
    fresh = MilvusReadPreparation(prepare)
    assert fresh.query([1])[0]["content"] == "after"
    fresh.close()
    assert calls.count("prepare") == 2


def test_collection_absence_is_not_negative_cached() -> None:
    attempts: list[int] = []

    def prepare() -> SourceQuery | None:
        attempts.append(1)
        return None if len(attempts) == 1 else lambda identities: [{"message_id": identities[0]}]

    reader = MilvusReadPreparation(prepare)
    assert reader.query([1]) == []
    assert reader.query([1]) == [{"message_id": 1}] and len(attempts) == 2
    reader.close()


@pytest.mark.parametrize("stage", ["prepare", "query"])
def test_rpc_failure_propagates_and_poisons_only_that_pass(stage: str) -> None:
    calls: list[str] = []

    def prepare() -> SourceQuery:
        calls.append("prepare")
        if stage == "prepare":
            raise OSError("metadata RPC failed")

        def query(identities: list[int]) -> list[dict[str, Any]]:
            calls.append("query")
            raise OSError("query RPC failed")

        return query

    reader = MilvusReadPreparation(prepare)
    with pytest.raises(OSError, match="RPC failed"):
        reader.query([1])
    with pytest.raises(RuntimeError, match="previously failed"):
        reader.query([1])
    assert calls == (["prepare"] if stage == "prepare" else ["prepare", "query"])
    reader.close()


@pytest.mark.parametrize("identities", [[], [True], [0], [-1], [2**63], [1] * 101])
def test_invalid_identity_cannot_start_preparation(identities: list[int]) -> None:
    def forbidden() -> SourceQuery:
        raise AssertionError("Invalid input must not touch the backend")

    reader = MilvusReadPreparation(forbidden)
    with pytest.raises(ValueError):
        reader.query(identities)
    reader.close()


def test_duplicate_input_ids_preserve_canonical_source_query_contract() -> None:
    seen: list[list[int]] = []

    def query(identities: list[int]) -> list[dict[str, Any]]:
        seen.append(identities)
        return []

    reader = MilvusReadPreparation(lambda: query)
    original = [3, 1, 3]
    assert reader.query(original) == [] and original == [3, 1, 3]
    assert seen == [[3, 1]]
    reader.close()


class PreparedPhysical(Physical):
    def __init__(self) -> None:
        super().__init__()
        self.readers: list[MilvusReadPreparation] = []
        self.prepared: list[int] = []
        self.queried: list[int] = []
        self.started: Event | None = None
        self.release: Event | None = None

    def prepare_source_reads(self, fields: list[str], index: str, bank: str) -> MilvusReadPreparation:
        ordinal = len(self.readers)

        def prepare() -> SourceQuery:
            self.prepared.append(ordinal)

            def query(identities: list[int]) -> list[dict[str, Any]]:
                self.queried.append(ordinal)
                if self.started is not None and self.release is not None:
                    self.started.set()
                    if not self.release.wait(5):
                        raise TimeoutError("Controlled read was not released")
                return Physical.query_source_rows(self, fields, identities, index, bank)

            return query

        reader = MilvusReadPreparation(prepare)
        self.readers.append(reader)
        return reader


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch) -> PreparedPhysical:
    value = PreparedPhysical()
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", value)
    return value


async def test_pass_entry_is_lazy_and_nested_context_restores_outer_preparation(prepared: PreparedPhysical) -> None:
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        assert prepared.prepared == []
    assert prepared.prepared == []
    async with store.prepared_read_pass():
        assert await store.read(1) is not None
        async with store.prepared_read_pass():
            assert await store.read(1) is not None
        assert await store.read(1) is not None
    assert prepared.prepared == [1, 2] and prepared.queried == [1, 2, 1]
    async with store.prepared_read_pass():
        assert await store.read(1) is not None
    assert prepared.prepared == [1, 2, 3]
    for reader in prepared.readers:
        with pytest.raises(RuntimeError, match="closed"):
            reader.query([1])


async def test_concurrent_requests_on_one_store_have_independent_preparation(prepared: PreparedPhysical) -> None:
    store = ManagedMessageStore("tenant", "bank")
    ready = [asyncio.Event(), asyncio.Event()]

    async def request(ordinal: int) -> None:
        async with store.prepared_read_pass():
            ready[ordinal].set()
            await ready[1 - ordinal].wait()
            assert await store.read(1) is not None
            await asyncio.sleep(0)
            assert await store.read(1) is not None

    await asyncio.gather(request(0), request(1))
    assert sorted(prepared.prepared) == [0, 1]
    assert Counter(prepared.queried) == {0: 2, 1: 2}
    # The parent context has no inherited preparation after either request.
    assert await store.read(1) is not None
    assert len(prepared.queried) == 4


async def test_cancellation_drains_query_before_closing_and_restores_context(prepared: PreparedPhysical) -> None:
    prepared.started, prepared.release = Event(), Event()
    store = ManagedMessageStore("tenant", "bank")

    async def request() -> None:
        async with store.prepared_read_pass():
            await store.read(1)

    task = asyncio.create_task(request())
    try:
        assert await asyncio.to_thread(prepared.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        # A repeated cancellation must not abandon the same owned query.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        prepared.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(RuntimeError, match="closed"):
        prepared.readers[0].query([1])
    prepared.started = prepared.release = None
    async with store.prepared_read_pass():
        assert await store.read(1) is not None
    assert prepared.prepared == [0, 1]


async def test_prepared_scope_is_read_only_without_affecting_writer_outside(prepared: PreparedPhysical) -> None:
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        for write in [store.write({}), store.set_status(1, False), store.ensure_index("bank", 2), store.delete([1]), store.drop()]:
            with pytest.raises(RevisionValidationError, match="physical_read_pass_is_read_only"):
                await write
    assert prepared.insert_count == 0 and prepared.rows[1]["status"] is True
    await store.write({**prepared.rows[1], "status": False})
    assert prepared.insert_count == 1 and prepared.rows[1]["status"] is False


@pytest.mark.parametrize("when", ["before", "during"])
async def test_backend_change_cannot_rebind_preparation(prepared: PreparedPhysical, monkeypatch: pytest.MonkeyPatch, when: str) -> None:
    store = ManagedMessageStore("tenant", "bank")
    other = Physical()
    if when == "during":
        original = prepared.get_fields

        def decode(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, dict[str, Any]]:
            monkeypatch.setitem(settings.__dict__, "msgStoreConn", other)
            return original(rows, fields)

        monkeypatch.setattr(prepared, "get_fields", decode)
    async with store.prepared_read_pass():
        if when == "before":
            monkeypatch.setitem(settings.__dict__, "msgStoreConn", other)
        with pytest.raises(RevisionStorageUnknown, match="physical_read_backend_changed"):
            await store.read(1)
    assert prepared.queried == ([] if when == "before" else [0])


async def test_backend_without_preparation_keeps_each_strict_read_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Physical()
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", backend)
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        first = await store.read(1)
        backend.rows[1]["content"] = "Changed after the first actual query"
        second = await store.read(1)
    assert first is not None and second is not None
    assert first["content"] == "source" and second["content"] == "Changed after the first actual query"


async def test_query_failure_closes_preparation_without_using_legacy_fallback(prepared: PreparedPhysical, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared.failure = TimeoutError("controlled query failure")

    def forbidden(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("A failed prepared query must not use the legacy query")

    monkeypatch.setattr(prepared, "query_source_rows", forbidden)
    store = ManagedMessageStore("tenant", "bank")
    async with store.prepared_read_pass():
        with pytest.raises(TimeoutError, match="controlled query failure"):
            await store.read(1)
    with pytest.raises(RuntimeError, match="closed"):
        prepared.readers[0].query([1])
