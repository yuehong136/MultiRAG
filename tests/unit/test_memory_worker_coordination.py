"""Worker redelivery retains coordinated extraction identity and queue ownership."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from api.db.memory_writer_models import MemoryWriterAdmission
from core.svr import task_executor as worker


@pytest.fixture(autouse=True)
def worker_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker, "CURRENT_TASKS", {})
    monkeypatch.setattr(worker, "DONE_TASKS", 0)
    monkeypatch.setattr(worker, "FAILED_TASKS", 0)
    monkeypatch.setattr(worker, "UNACKED_ITERATOR", None)
    monkeypatch.setattr(worker, "_MEMORY_RETRY_PENDING", False)
    monkeypatch.setattr(worker, "supports_coordinated_writes", lambda: True)


def admission(**overrides: Any) -> MemoryWriterAdmission:
    fields: dict[str, Any] = {"kind": "extract", "task_id": "task", "operation_id": "task", "memory_id": "bank", "tenant_id": "owner", "source_message_id": 42}
    return MemoryWriterAdmission(**{**fields, **overrides})


@pytest.fixture
def queued(db: Session, monkeypatch: pytest.MonkeyPatch) -> tuple[Task, list[str], dict[str, Any]]:
    events: list[str] = []
    task = Task(id="task", doc_id="bank", task_type="memory", digest="42", progress=0)
    memory = Memory(id="bank", tenant_id="owner")
    payload = {"id": "task", "task_type": "memory", "memory_id": "bank", "tenant_id": "owner", "source_id": 42, "message_dict": {}}
    message = SimpleNamespace(get_message=lambda: payload, ack=lambda: events.append("ack"))
    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter([message]))
    monkeypatch.setattr(worker.TaskService, "get_by_id", lambda *args: task)
    monkeypatch.setattr(worker, "memory_for_task", lambda *args: memory)
    monkeypatch.setattr(worker, "has_canceled", lambda *args: False)
    monkeypatch.setattr(db, "commit", lambda: events.append("commit"))
    return task, events, payload


@pytest.mark.parametrize("field,value", [("kind", "append"), ("task_id", "other"), ("operation_id", "other"), ("memory_id", "other"), ("tenant_id", "other"), ("source_message_id", 43)])
def test_admission_requires_every_persisted_binding(db: Session, monkeypatch: pytest.MonkeyPatch, field: str, value: Any) -> None:
    rows = [admission(), admission(**{field: value}), None, SimpleNamespace(**{field: value})]
    statements: list[Any] = []

    def scalar(statement: Any) -> Any:
        statements.append(statement)
        return rows.pop(0)

    monkeypatch.setattr(db, "scalar", scalar)
    task = Task(id="task", doc_id="bank", task_type="memory")
    memory = Memory(id="bank", tenant_id="owner")
    assert worker._memory_task_has_writer_admission(db, task, memory, 42)
    assert not worker._memory_task_has_writer_admission(db, task, memory, 42)
    assert not worker._memory_task_has_writer_admission(db, task, memory, 42)
    assert not worker._memory_task_has_writer_admission(db, task, memory, 42)
    params = statements[0].compile().params
    assert sorted(map(str, params.values())) == ["42", "bank", "extract", "owner", "task", "task"]


async def test_matching_admission_replays_plan_without_legacy_storage_probe(db: Session, queued: tuple[Task, list[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, events, payload = queued
    monkeypatch.setattr(db, "scalar", lambda *args: admission())
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda *args: pytest.fail("fixed-plan replay entered the legacy duplicate guard"))
    message, task = await worker.collect(db)
    assert message is not None
    assert task["id"] == "task"
    assert task["source_id"] == payload["source_id"]
    assert task["memory_id"] == "bank" and task["tenant_id"] == "owner"
    assert events == []


@pytest.mark.parametrize("progress", [1.0, -1.0])
async def test_terminal_redelivery_keeps_old_ack_without_admission_lookup(db: Session, queued: tuple[Task, list[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch, progress: float) -> None:
    task, events, _ = queued
    task.progress = progress
    monkeypatch.setattr(db, "scalar", lambda *args: pytest.fail("terminal task read writer ledger"))
    assert await worker.collect(db) == (None, None)
    assert events == ["ack"]
    assert worker.DONE_TASKS == int(progress >= 1)
    assert worker.FAILED_TASKS == int(progress < 0)


@pytest.mark.parametrize("row", [None, admission(tenant_id="other")])
async def test_missing_or_mismatched_admission_keeps_legacy_duplicate_guard(
    db: Session, queued: tuple[Task, list[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch, row: MemoryWriterAdmission | None
) -> None:
    _, events, _ = queued
    monkeypatch.setattr(db, "scalar", lambda *args: row)
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda *args: True)
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(worker.TaskService, "update_progress", lambda db, tid, info: updates.append(info))
    assert await worker.collect(db) == (None, None)
    assert events == ["commit", "ack"]
    assert updates[0]["progress"] == -1


@pytest.mark.parametrize("handled", [True, False])
async def test_coordinated_handler_releases_only_the_clean_sync_read_transaction(db: Session, monkeypatch: pytest.MonkeyPatch, handled: bool) -> None:
    events: list[str] = []
    monkeypatch.setattr(db, "in_transaction", lambda: True)
    monkeypatch.setattr(db, "rollback", lambda: events.append("rollback"))

    async def coordinated(task: dict[str, Any]) -> bool:
        assert events == ["rollback"]
        assert task["id"] == "task"
        events.append("coordinated")
        return handled

    async def legacy(session: Session, task: dict[str, Any]) -> None:
        assert session is db
        events.append("legacy")

    monkeypatch.setattr(worker, "handle_coordinated_task", coordinated)
    monkeypatch.setattr(worker, "handle_save_to_memory_task", legacy)
    await worker.do_handle_task(db, {"id": "task", "task_type": "memory"})
    assert events == (["rollback", "coordinated"] if handled else ["rollback", "coordinated", "legacy"])


async def test_pending_sync_changes_are_never_rolled_back_for_external_work(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    db.add(Task(id="uncommitted", doc_id="bank", task_type="memory"))
    monkeypatch.setattr(db, "rollback", lambda: pytest.fail("pending writes were discarded"))
    handler = AsyncMock()
    monkeypatch.setattr(worker, "handle_coordinated_task", handler)
    with pytest.raises(RuntimeError, match="without pending changes"):
        await worker.do_handle_task(db, {"id": "task", "task_type": "memory"})
    handler.assert_not_awaited()
    assert len(db.new) == 1


@pytest.fixture
def handling(db: Session, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[type[BaseException] | None]]:
    events: list[str] = []
    metrics: list[type[BaseException] | None] = []
    task = {"id": "task", "task_type": "memory", "doc_id": "bank", "tenant_id": "owner", "source_id": 42, "memory_id": "bank", "message_dict": {}}

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    monkeypatch.setattr(worker, "db_connection", connection)
    monkeypatch.setattr(worker, "collect", AsyncMock(return_value=(SimpleNamespace(ack=lambda: events.append("ack")), task)))
    monkeypatch.setattr(worker, "finish_runtime", lambda tid: events.append("finished"))
    monkeypatch.setattr(worker.executor_metrics.TaskTimer, "__exit__", lambda self, kind, exc, tb: metrics.append(kind))
    monkeypatch.setattr(db, "commit", lambda: events.append("commit"))
    monkeypatch.setattr(db, "rollback", lambda: events.append("rollback"))
    monkeypatch.setattr(worker, "set_progress", lambda *args, **kwargs: pytest.fail("runtime owns coordinated task progress"))
    return events, metrics


@pytest.mark.parametrize("acknowledge", [False, True])
async def test_pending_operation_preserves_counts_and_commits_before_optional_ack(
    db: Session, handling: tuple[list[str], list[type[BaseException] | None]], monkeypatch: pytest.MonkeyPatch, acknowledge: bool
) -> None:
    events, metrics = handling
    monkeypatch.setattr(worker, "handle_coordinated_task", AsyncMock(side_effect=worker.MemoryTaskPending("stable-operation", "pending", acknowledge=acknowledge)))
    await worker.handle_task()
    assert events == (["finished", "commit", "ack"] if acknowledge else ["finished", "commit"])
    assert worker.DONE_TASKS == worker.FAILED_TASKS == 0
    assert worker.CURRENT_TASKS == {}
    assert worker._MEMORY_RETRY_PENDING is not acknowledge
    assert metrics == []


@pytest.mark.parametrize("kind", [None, RuntimeError])
def test_timer_preserves_existing_metrics_for_nonpending_results(monkeypatch: pytest.MonkeyPatch, kind: type[BaseException] | None) -> None:
    calls: list[type[BaseException] | None] = []
    monkeypatch.setattr(worker.executor_metrics.TaskTimer, "__exit__", lambda self, exc_type, exc, tb: calls.append(exc_type))
    timer = worker._MemoryTaskTimer("memory", None)
    timer.__enter__()
    timer.__exit__(kind, kind("failure") if kind is not None else None, None)
    assert calls == [kind]


async def test_pending_old_state_commit_failure_does_not_ack(db: Session, handling: tuple[list[str], list[type[BaseException] | None]], monkeypatch: pytest.MonkeyPatch) -> None:
    events, _ = handling
    monkeypatch.setattr(worker, "handle_coordinated_task", AsyncMock(side_effect=worker.MemoryTaskPending("stable-operation", "manual", acknowledge=True)))

    def fail_commit() -> None:
        events.append("commit")
        raise ConnectionError("commit failed")

    monkeypatch.setattr(db, "commit", fail_commit)
    with pytest.raises(ConnectionError, match="commit failed"):
        await worker.handle_task()
    assert events == ["finished", "commit", "rollback"]
    assert worker.DONE_TASKS == worker.FAILED_TASKS == 0


async def test_real_cancellation_propagates_without_ack_or_success(db: Session, handling: tuple[list[str], list[type[BaseException] | None]], monkeypatch: pytest.MonkeyPatch) -> None:
    events, _ = handling
    monkeypatch.setattr(worker, "handle_coordinated_task", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await worker.handle_task()
    assert events == ["finished"]
    assert worker.CURRENT_TASKS == {}
    assert worker.DONE_TASKS == worker.FAILED_TASKS == 0


async def test_deferred_retry_rescans_unacked_only_when_worker_is_idle(db: Session, queued: tuple[Task, list[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, events, payload = queued
    rescans: list[str] = []
    monkeypatch.setattr(worker, "_MEMORY_RETRY_PENDING", True)
    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter(()))
    monkeypatch.setattr(worker, "CURRENT_TASKS", {"document-inflight": {"id": "document-inflight"}})
    monkeypatch.setattr(worker.REDIS_CONN, "queue_consumer", lambda *args: None)
    message = SimpleNamespace(get_message=lambda: payload, ack=lambda: events.append("ack"))

    def unacked(*args: Any) -> Iterator[Any]:
        rescans.append("rescan")
        return iter([message])

    monkeypatch.setattr(worker.REDIS_CONN, "get_unacked_iterator", unacked)
    monkeypatch.setattr(db, "scalar", lambda *args: admission())
    assert await worker.collect(db) == (None, None)
    assert rescans == []
    assert worker._MEMORY_RETRY_PENDING
    worker.CURRENT_TASKS.clear()
    _, task = await worker.collect(db)
    assert task["id"] == "task"
    assert rescans == ["rescan"]
    assert not worker._MEMORY_RETRY_PENDING
    assert events == []


async def test_pending_then_executable_redelivery_reuses_task_identity_in_the_same_process(db: Session, queued: tuple[Task, list[str], dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, events, payload = queued

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    message = SimpleNamespace(get_message=lambda: payload, ack=lambda: events.append("ack"))
    monkeypatch.setattr(worker, "db_connection", connection)
    monkeypatch.setattr(db, "scalar", lambda *args: admission())
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda *args: pytest.fail("coordinated replay used legacy probe"))
    monkeypatch.setattr(worker.REDIS_CONN, "get_unacked_iterator", lambda *args: iter([message]))
    monkeypatch.setattr(worker.REDIS_CONN, "queue_consumer", lambda *args: None)
    monkeypatch.setattr(worker, "finish_runtime", lambda tid: events.append("finished"))
    handler = AsyncMock(side_effect=[worker.MemoryTaskPending("task", "retryable"), True])
    monkeypatch.setattr(worker, "handle_coordinated_task", handler)
    await worker.handle_task()
    assert events == ["finished", "commit"]
    assert worker._MEMORY_RETRY_PENDING
    await worker.handle_task()
    assert events == ["finished", "commit", "finished", "commit", "ack"]
    assert handler.await_count == 2
    assert all(call.args[0]["id"] == "task" for call in handler.await_args_list)
    assert worker.DONE_TASKS == 1 and worker.FAILED_TASKS == 0
    assert not worker._MEMORY_RETRY_PENDING
