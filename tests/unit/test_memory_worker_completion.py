"""Memory workers must finish without Document bookkeeping or duplicate replay."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from core.svr import task_executor as worker


@pytest.mark.parametrize("commit_failure", [False, True])
async def test_memory_handle_task_commits_before_acknowledging_without_kb_id(db: Session, monkeypatch: pytest.MonkeyPatch, commit_failure: bool) -> None:
    events: list[str] = []
    task = {"id": "task", "task_type": "memory", "doc_id": "bank", "tenant_id": "owner", "source_id": 42, "memory_id": "bank", "message_dict": {}}

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    async def save(*args: Any) -> tuple[bool, str]:
        events.append("saved")
        return True, "saved"

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("memory task entered Document/KB bookkeeping")

    def commit() -> None:
        events.append("commit")
        if commit_failure:
            raise ConnectionError("SQL commit failed")

    monkeypatch.setattr(worker, "db_connection", connection)
    monkeypatch.setattr(worker, "collect", AsyncMock(return_value=(SimpleNamespace(ack=lambda: events.append("ack")), task)))
    monkeypatch.setattr(worker, "handle_save_to_memory_task", save)
    monkeypatch.setattr(worker, "finish_runtime", lambda tid: events.append("finished"))
    monkeypatch.setattr(worker, "record_task_pipeline", forbidden)
    monkeypatch.setattr(worker.PipelineOperationLogService, "record_pipeline_operation", forbidden)
    monkeypatch.setattr(db, "commit", commit)
    monkeypatch.setattr(db, "rollback", lambda: events.append("rollback"))
    monkeypatch.setattr(worker, "CURRENT_TASKS", {})
    if commit_failure:
        with pytest.raises(ConnectionError, match="SQL commit failed"):
            await worker.handle_task()
        assert events == ["saved", "finished", "commit", "rollback"]
        return
    await worker.handle_task()
    assert events.count("saved") == events.count("ack") == events.count("commit") == 1
    assert events.index("saved") < events.index("commit") < events.index("ack")
    assert worker.CURRENT_TASKS == {}


@pytest.fixture
def queued_memory(db: Session, monkeypatch: pytest.MonkeyPatch) -> tuple[Task, list[str]]:
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
    return task, events


@pytest.mark.parametrize("progress", [1.0, -1.0])
async def test_terminal_memory_redelivery_does_not_touch_message_storage(db: Session, queued_memory: tuple[Task, list[str]], monkeypatch: pytest.MonkeyPatch, progress: float) -> None:
    task, events = queued_memory
    task.progress = progress
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda *args: pytest.fail("terminal task read storage"))
    assert await worker.collect(db) == (None, None)
    assert events == ["ack"]


async def test_uncertain_memory_results_stop_replay_without_claiming_complete(db: Session, queued_memory: tuple[Task, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    task, events = queued_memory
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda owner, bank, source: (owner, bank, source) == ("owner", "bank", 42))
    monkeypatch.setattr(worker.TaskService, "update_progress", lambda db, tid, info: updates.append(info))
    assert await worker.collect(db) == (None, None)
    assert events == ["commit", "ack"]
    assert updates[0]["progress"] == -1
    assert "reconciliation" in updates[0]["progress_msg"]


async def test_uncertain_memory_read_failure_preserves_pending_message(db: Session, queued_memory: tuple[Task, list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    _, events = queued_memory

    def unavailable(*args: Any) -> bool:
        raise ConnectionError("message store unavailable")

    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", unavailable)
    with pytest.raises(ConnectionError, match="unavailable"):
        await worker.collect(db)
    assert events == []


def test_uncertain_result_probe_includes_inactive_and_forgotten_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def search(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
        seen.append(kwargs)
        return [{"message_id": 43}], 1

    monkeypatch.setattr(worker.settings, "msgStoreConn", SimpleNamespace(search=search))
    assert worker._memory_task_has_extracted_messages("owner", "bank", 42)
    assert seen[0]["condition"] == {"source_id": 42}
    assert seen[0]["memory_ids"] == ["bank"]
    assert seen[0]["index_names"] == "memory_owner"
    assert seen[0]["hide_forgotten"] is False
