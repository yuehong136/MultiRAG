from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.services.connector_service import connector_doc_id_candidates, resolve_connector_doc_id
from common.constants import FileSource
from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.models import SlimDocument
from core.svr import sync_data_source as sync_module


class SnapshotConnector:
    def __init__(self, failure: bool = False, empty: bool = False) -> None:
        self.failure = failure
        self.empty = empty
        self.calls = 0

    def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
        self.calls += 1
        if not self.empty:
            yield [SlimDocument(id="remote-1")]
        if self.failure:
            raise PermissionError("second page denied")
        if not self.empty:
            yield [SlimDocument(id="remote-2")]


def test_snapshot_requires_complete_pagination_and_accepts_empty() -> None:
    assert [doc.id for doc in collect_slim_document_snapshot(SnapshotConnector())] == ["remote-1", "remote-2"]
    assert collect_slim_document_snapshot(SnapshotConnector(empty=True)) == ()
    with pytest.raises(PermissionError, match="second page"):
        collect_slim_document_snapshot(SnapshotConnector(failure=True))


def test_snapshot_rejects_invalid_ids() -> None:
    class InvalidConnector:
        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            yield [SlimDocument(id=" ")]

    with pytest.raises(ValueError, match="Invalid document"):
        collect_slim_document_snapshot(InvalidConnector())


def test_document_identity_preserves_owned_history_and_scopes_new_rows() -> None:
    old, connector_scoped, current = connector_doc_id_candidates("kb-1", "connector-1", "remote-1")
    for candidate in (old, connector_scoped):
        assert resolve_connector_doc_id("kb-1", "connector-1", "remote-1", {candidate}) == candidate
    assert resolve_connector_doc_id("kb-1", "connector-1", "remote-1", set()) == current
    assert resolve_connector_doc_id("kb-2", "connector-1", "remote-1", set()) != current
    assert resolve_connector_doc_id("kb-1", "connector-2", "remote-1", set()) != current


@pytest.fixture
def sync_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: list[Any] = []

    @contextmanager
    def database() -> Iterator[Session]:
        with Session() as db:
            yield db

    monkeypatch.setattr(sync_module, "db_connection", database)
    monkeypatch.setattr(sync_module.SyncLogsService, "raise_if_cancelled", lambda *args: None)
    monkeypatch.setattr(sync_module.DocumentService, "list_doc_headers_by_kb_and_source_type", lambda *args: [])
    monkeypatch.setattr(sync_module.KnowledgebaseService, "get_by_id", lambda *args: object())
    monkeypatch.setattr(sync_module.SyncLogsService, "increase_docs", lambda *args: None)
    monkeypatch.setattr(sync_module.ConnectorService, "cleanup_stale_documents_for_task", lambda *args: calls.append(("cleanup", args[-1])) or (1, []))
    monkeypatch.setattr(sync_module.SyncLogsService, "start", lambda *args: calls.append("start"))
    monkeypatch.setattr(sync_module.SyncLogsService, "fail", lambda *args: calls.append("fail"))
    monkeypatch.setattr(sync_module.SyncLogsService, "complete_and_schedule_next", lambda *args: calls.append("complete"))
    return {"calls": calls, "monkeypatch": monkeypatch}


def task() -> dict[str, Any]:
    return {
        "id": "task-1",
        "connector_id": "connector-1",
        "kb_id": "kb-1",
        "tenant_id": "tenant-1",
        "poll_range_start": datetime(2026, 1, 1, tzinfo=UTC),
        "reindex": "0",
        "auto_parse": False,
        "timeout_secs": 5,
    }


class FakeSync(sync_module.SyncBase):
    SOURCE_NAME = FileSource.S3

    def __init__(self, connector: SnapshotConnector, *, documents: bool = False, enabled: bool = True) -> None:
        super().__init__({"sync_deleted_files": enabled})
        self.connector = connector
        self.documents = documents
        self.generated = False

    async def _generate(self, task: dict[str, Any]) -> Iterator[list[Any]]:
        def batches() -> Iterator[list[Any]]:
            self.generated = True
            if self.documents:
                yield [SimpleNamespace(id="remote-1", doc_updated_at=datetime(2026, 2, 1, tzinfo=UTC), semantic_identifier="doc", extension=".txt", size_bytes=3, blob=b"doc", metadata={})]

        return batches()


@pytest.mark.parametrize("source", sorted(sync_module.DELETED_FILE_SYNC_SOURCES))
async def test_complete_empty_snapshot_prunes_and_completes_for_each_enabled_source(sync_env: dict[str, Any], source: str) -> None:
    sync = FakeSync(SnapshotConnector(empty=True))
    sync.SOURCE_NAME = source
    await sync(task())
    assert sync_env["calls"] == ["start", ("cleanup", ()), "complete"]


async def test_listing_failure_never_writes_deletes_or_completes(sync_env: dict[str, Any]) -> None:
    sync = FakeSync(SnapshotConnector(failure=True), documents=True)
    await sync(task())
    assert sync_env["calls"] == ["start", "fail"]
    assert not sync.generated


@pytest.mark.parametrize("mode", ["disabled", "first", "reindex"])
async def test_initial_reindex_and_disabled_sync_do_not_prune(sync_env: dict[str, Any], mode: str) -> None:
    connector = SnapshotConnector()
    sync = FakeSync(connector, enabled=mode != "disabled")
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await sync(current)
    assert connector.calls == 0
    assert sync_env["calls"] == ["start", "complete"]


async def test_ingestion_errors_skip_pruning_and_completion(sync_env: dict[str, Any]) -> None:
    sync_env["monkeypatch"].setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda *args: (["upload failed"], []))
    await FakeSync(SnapshotConnector(), documents=True)(task())
    assert sync_env["calls"] == ["start", "fail"]


async def test_cleanup_errors_fail_without_scheduling_next(sync_env: dict[str, Any]) -> None:
    sync_env["monkeypatch"].setattr(sync_module.ConnectorService, "cleanup_stale_documents_for_task", lambda *args: (0, ["delete failed"]))
    await FakeSync(SnapshotConnector())(task())
    assert sync_env["calls"] == ["start", "fail"]


async def test_objects_ingested_after_empty_listing_are_protected_from_pruning(sync_env: dict[str, Any]) -> None:
    sync_env["monkeypatch"].setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda *args: ([], [args[2][0]["id"]]))
    await FakeSync(SnapshotConnector(empty=True), documents=True)(task())
    assert sync_env["calls"][0] == "start"
    assert sync_env["calls"][1][0] == "cleanup"
    assert [doc.id for doc in sync_env["calls"][1][1]] == ["remote-1"]
    assert sync_env["calls"][-1] == "complete"


async def test_timeout_during_listing_cannot_publish_partial_snapshot(sync_env: dict[str, Any]) -> None:
    import asyncio
    import threading

    release, finished = threading.Event(), threading.Event()

    class SlowSource(SnapshotConnector):
        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            try:
                yield [SlimDocument(id="remote-1")]
                assert release.wait(2)
                yield [SlimDocument(id="remote-2")]
            finally:
                finished.set()

    sync = FakeSync(SlowSource(), documents=True)
    current = task()
    current["timeout_secs"] = 0.01
    try:
        await sync(current)
        assert sync_env["calls"] == ["start", "fail"]
        assert not sync.generated
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 2)
    assert sync_env["calls"] == ["start", "fail"]
