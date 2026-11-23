"""Memory task ownership and configuration equivalence regressions."""

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.apps.services import memory_api_service as api
from api.db.db_models import Memory, Task
from api.db.joint_services import memory_message_service as mms
from api.db.memory_revision_models import ManagedMemoryState
from api.db.memory_writer_models import MemoryWriterAdmission
from api.db.services.task_service import TaskService
from common.exceptions import ArgumentException
from core.svr import task_executor as worker
from memory.revisions.domain import RevisionConflict


@pytest.fixture
def owned_task(monkeypatch: pytest.MonkeyPatch) -> Task:
    memory = Memory(id="memory", tenant_id="owner")
    task = Task(id="task", doc_id="memory", digest="42", task_type="memory", progress=0)
    monkeypatch.setattr(mms.MemoryService, "get_by_memory_id", lambda db, mid: memory if mid == "memory" else None)
    monkeypatch.setattr(TaskService, "get_by_id", lambda db, tid: task if tid == "task" else None)
    monkeypatch.setattr(worker, "_memory_task_has_extracted_messages", lambda *args: False)
    return task


@pytest.mark.parametrize("change", [{"tenant_id": None}, {"tenant_id": "foreign"}, {"memory_id": "foreign"}, {"memory_id": "missing"}, {"source_id": 43}, {"source_id": True}])
@pytest.mark.parametrize("progress", [0.0, 1.0])
async def test_invalid_queue_cannot_run_memory_work(db: Session, owned_task: Task, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], progress: float) -> None:
    owned_task.progress = progress
    payload = {"id": "task", "task_type": "memory", "tenant_id": "owner", "memory_id": "memory", "source_id": 42, "message_dict": {}} | change
    acknowledged: list[bool] = []

    class Message:
        def get_message(self) -> dict[str, Any]:
            return payload

        def ack(self) -> None:
            acknowledged.append(True)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid queued work reached model, storage or progress mutation")

    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter([Message()]))
    monkeypatch.setattr(TaskService, "mark_started", forbidden)
    monkeypatch.setattr(mms, "save_extracted_to_memory_only", forbidden)
    assert await worker.collect(db) == (None, None)
    assert acknowledged == [True]
    assert await mms.handle_save_to_memory_task(db, payload) == (False, "Memory task ownership does not match.")


async def test_collect_propagates_db_owner(db: Session, owned_task: Task, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"id": "task", "task_type": "memory", "tenant_id": "owner", "memory_id": "memory", "source_id": 42, "message_dict": {"user_id": "subject"}}

    class Message:
        def get_message(self) -> dict[str, Any]:
            return payload

    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter([Message()]))
    monkeypatch.setattr(worker, "has_canceled", lambda tid: False)
    _, task = await worker.collect(db)
    assert task["tenant_id"] == "owner"
    assert task["message_dict"]["user_id"] == "subject"
    assert task["doc_id"] == task["memory_id"] == "memory"


@pytest.mark.parametrize("value,valid", [(None, False), ("invalid", False), ("2026-10-29 12:00:00", True), ("2026-10-29T12:00:00+08:00", True), (datetime(2026, 10, 29), True)])
def test_existing_begin_at_normalization(value: Any, valid: bool) -> None:
    assert isinstance(TaskService._coerce_begin_at(value), datetime) is valid


@pytest.fixture
def configured_memory(db: Session, monkeypatch: pytest.MonkeyPatch) -> Memory:
    memory = Memory(id="memory", tenant_id="owner", memory_type=3, embd_id="a" * 32, tenant_embd_id="a" * 32, llm_id="b" * 32, tenant_llm_id="b" * 32)
    monkeypatch.setattr(api, "_require_memory_access", lambda db, user_id, mid: memory)

    def resolve(db: Session, tenant_id: str, values: dict[str, Any], *, strict: bool = False) -> dict[str, Any]:
        assert tenant_id == "owner"
        for key in ("embd_id", "llm_id"):
            if key in values:
                values[f"tenant_{key}"] = values[key]
        return values

    monkeypatch.setattr(api, "ensure_tenant_model_id_for_params", resolve)

    def scalar(statement: Any) -> Any:
        entity = statement.column_descriptions[0]["entity"]
        if entity is Memory:
            assert statement._for_update_arg is not None
            return memory
        assert entity in {ManagedMemoryState, MemoryWriterAdmission}
        return None

    monkeypatch.setattr(db, "scalar", scalar)
    return memory


@pytest.mark.parametrize("settings", [{"embd_id": "a" * 32}, {"memory_type": ["semantic", "raw"]}, {"memory_type": [" RAW ", "semantic", "raw"], "embd_id": "a" * 32}])
def test_semantically_equal_update_is_noop(db: Session, configured_memory: Memory, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]) -> None:
    monkeypatch.setattr(api, "get_memory_size_cache", lambda *args: pytest.fail("no-op touched memory store"))
    monkeypatch.setattr(api.MemoryService, "update_memory", lambda *args: pytest.fail("no-op wrote configuration"))
    assert api.update_memory(db, "memory", settings, user_id="owner")[0] is True


@pytest.mark.parametrize("settings", [{"embd_id": "c" * 32}, {"memory_type": ["raw"]}])
def test_occupied_memory_still_rejects_real_change(db: Session, configured_memory: Memory, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]) -> None:
    monkeypatch.setattr(api, "get_memory_size_cache", lambda *args: 100)
    with pytest.raises(ArgumentException, match="isn't empty"):
        api.update_memory(db, "memory", settings, user_id="owner")


@pytest.mark.parametrize("blocker", ["deleting", "active_writer"])
def test_configuration_change_requires_actual_writer_quiescence(db: Session, configured_memory: Memory, monkeypatch: pytest.MonkeyPatch, blocker: str) -> None:
    def scalar(statement: Any) -> Any:
        entity = statement.column_descriptions[0]["entity"]
        if entity is Memory:
            assert statement._for_update_arg is not None
            return configured_memory
        if entity is ManagedMemoryState:
            return "deleting" if blocker == "deleting" else None
        assert entity is MemoryWriterAdmission
        return "original-writer" if blocker == "active_writer" else None

    monkeypatch.setattr(db, "scalar", scalar)
    monkeypatch.setattr(api, "get_memory_size_cache", lambda *args: pytest.fail("blocked change reached message storage"))
    monkeypatch.setattr(api.MemoryService, "update_memory", lambda *args: pytest.fail("blocked change wrote configuration"))
    with pytest.raises(RevisionConflict, match="memory_change_requires_writer_quiescence"):
        api.update_memory(db, "memory", {"embd_id": "c" * 32}, user_id="owner")


@pytest.mark.parametrize("value", [3, "raw,semantic", [1], ["unsupported"]])
def test_memory_type_normalization_preserves_wire_boundary(db: Session, configured_memory: Memory, value: Any) -> None:
    with pytest.raises(ArgumentException):
        api.update_memory(db, "memory", {"memory_type": value}, user_id="owner")


def test_empty_memory_can_replace_its_model_binding(db: Session, configured_memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    changed: list[dict[str, Any]] = []
    monkeypatch.setattr(api, "get_memory_size_cache", lambda *args: 0)
    monkeypatch.setattr(api.MemoryService, "update_memory", lambda db, tenant, mid, values: changed.append(values))
    monkeypatch.setattr(api.MemoryService, "get_by_memory_id", lambda db, mid: configured_memory)
    assert api.update_memory(db, "memory", {"embd_id": "c" * 32}, user_id="owner")[0]
    assert changed == [{"embd_id": "c" * 32, "tenant_embd_id": "c" * 32}]


def test_occupied_unbound_memory_requires_migration(db: Session, configured_memory: Memory, monkeypatch: pytest.MonkeyPatch) -> None:
    configured_memory.tenant_embd_id = None
    monkeypatch.setattr(api, "get_memory_size_cache", lambda *args: 100)
    monkeypatch.setattr(api.MemoryService, "update_memory", lambda *args: pytest.fail("unverified binding wrote configuration"))
    with pytest.raises(ArgumentException, match="isn't empty"):
        api.update_memory(db, "memory", {"embd_id": "a" * 32}, user_id="owner")
