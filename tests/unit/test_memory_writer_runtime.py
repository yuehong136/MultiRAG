"""Uncertain final SQL acknowledgements cannot make a memory task disappear."""

from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_revision_service
from api.db.joint_services import memory_write_runtime as runtime
from memory.revisions.domain import RevisionPersistenceUnknown


@pytest.mark.parametrize("error", [DBAPIError("controlled final task commit", {}, RuntimeError("ack lost")), RevisionPersistenceUnknown("stable-task")])
async def test_task_sql_uncertainty_preserves_original_unacknowledged_identity(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unknown(task: dict[str, Any], *, trusted_internal: bool) -> bool:
        assert task["id"] == "stable-task" and trusted_internal is False
        raise error

    monkeypatch.setattr(runtime, "_handle_coordinated_task", unknown)
    with pytest.raises(runtime.MemoryTaskPending) as pending:
        await runtime.handle_coordinated_task({"id": "stable-task"})
    assert pending.value.operation_id == "stable-task"
    assert pending.value.code == "memory_task_commit_unconfirmed"
    assert pending.value.acknowledge is False


@pytest.mark.parametrize("error", [DBAPIError("controlled state commit", {}, RuntimeError("ack lost")), RuntimeError("controlled late reconciliation failure")])
async def test_legacy_state_late_failure_preserves_server_generated_handle(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class ControlledState:
        async def change(self, memory_id: str, message_id: int, *, operation_id: str, status: bool | None, forget: bool) -> dict[str, Any]:
            seen.append(operation_id)
            raise error

    async def unmanaged(*args: Any, **kwargs: Any) -> bool:
        return False

    async def state(*args: Any, **kwargs: Any) -> ControlledState:
        return ControlledState()

    monkeypatch.setattr(memory_revision_service, "is_managed", unmanaged)
    monkeypatch.setattr(memory_revision_service, "_legacy_state_service", state)
    monkeypatch.setattr(runtime, "supports_coordinated_writes", lambda: True)
    async with AsyncSession() as db:
        with pytest.raises(memory_revision_service.LegacyStateReconciliationRequired) as pending:
            await memory_revision_service.change_legacy_message_state(db, "bank", 42, actor_id="owner", status=False)
    assert seen == [pending.value.operation_id] and seen[0].startswith("legacy-state:")
    assert pending.value.code == "state_change_reconciliation_unconfirmed"
