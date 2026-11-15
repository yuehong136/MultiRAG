"""Deletion never equates lease expiry, a dropped reply, or a stop with cleanup."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from memory.revisions import deletion
from memory.revisions.domain import RevisionBusy
from memory.writes.types import WriterDrainSnapshot


def drained(**changes: Any) -> WriterDrainSnapshot:
    return {
        "state": "closing",
        "generation": 2,
        "active_operations": [],
        "unknown_operations": [],
        "pending_operation_ids": [],
        "untracked_task_ids": [],
        "untracked_task_ids_sha256": "a" * 64,
        "legacy_quiescence_attested": False,
        "legacy_quiescence_attestation": None,
        "drained": True,
        **changes,
    }


def test_actual_attempts_and_untracked_tasks_cannot_disappear_by_lease_expiry() -> None:
    row = SimpleNamespace(id="old", lease_until=None, state="ready", receipt={"active_attempts": ["unfinished"]})
    counts, blocked, operator = deletion.MemoryDeletionService._blocked(drained(untracked_task_ids=["legacy-task"], drained=False), [row])
    assert counts["active_attempts"] == 1 and counts["untracked_tasks"] == 1
    assert set(blocked) == {"active_attempts", "untracked_tasks"} and operator is True


def test_false_drain_without_explained_counts_stays_blocked() -> None:
    _, blocked, _ = deletion.MemoryDeletionService._blocked(drained(drained=False), [])
    assert blocked == ["writer_fence_not_drained"]


class Decisions:
    def __init__(self) -> None:
        self.transaction = False
        self.events: list[str] = []
        self.memory = SimpleNamespace(tenant_id="tenant")
        self.bank = SimpleNamespace(tenant_id="tenant", generation=2, state="deleting", delete_operation_id="delete", capacity_generation=1, reserved_bytes=100)
        self.operation = SimpleNamespace(
            operation_id="delete",
            kind="delete_memory",
            status="published",
            index_sync_status="pending",
            error_code=None,
            result={"lifecycle": "deleting", "physical_cleanup_complete": False, "untraceable_copies": True},
        )
        self.outbox = SimpleNamespace(
            id="outbox", operation_id="delete", bank_generation=2, payload={"action": "delete_bank"}, receipt={}, attempt=0, lease_token=None, lease_until=None, state="pending"
        )
        self.old = SimpleNamespace(id="old", operation_id="old", payload={"action": "replace", "reserved_bytes": 100}, receipt={"reservation_released": False}, state="redacted")
        self.snapshot = drained()
        self.db = SimpleNamespace(scalars=AsyncMock(return_value=[]), execute=self.execute)
        self.service = deletion.MemoryDeletionService(self.db, actor_id="actor")  # type: ignore[arg-type]
        self.service._write = self.write  # type: ignore[method-assign]
        self.service._authorize = AsyncMock(side_effect=self.authorize)  # type: ignore[method-assign]
        self.service._operation = AsyncMock(return_value=self.operation)  # type: ignore[method-assign]
        self.service._bank = AsyncMock(return_value=self.bank)  # type: ignore[method-assign]
        self.service._outboxes = AsyncMock(return_value=[self.old, self.outbox])  # type: ignore[method-assign]
        self.service._view = self.view  # type: ignore[method-assign]
        self.service._finish_attempt = self.finish  # type: ignore[method-assign]

    @asynccontextmanager
    async def write(self, operation_id: str) -> Any:
        assert not self.transaction
        self.transaction = True
        try:
            yield
        finally:
            self.transaction = False

    async def authorize(self, *args: Any, **kwargs: Any) -> tuple[Any, Any]:
        return self.memory, self.bank

    async def execute(self, query: Any) -> Any:
        self.events.append("sql-delete")
        return SimpleNamespace(rowcount=1)

    async def view(self, operation: Any) -> dict[str, Any]:
        return {"status": operation.status, "operation_id": operation.operation_id, "index_sync_status": operation.index_sync_status, **deepcopy(operation.result)}

    async def finish(self, memory_id: str, operation_id: str, token: str) -> None:
        if token not in self.outbox.receipt.get("active_attempts", []):
            return
        self.events.append("attempt-finished")
        self.outbox.receipt["active_attempts"] = [value for value in self.outbox.receipt["active_attempts"] if value != token]

    async def close(self, *args: Any, **kwargs: Any) -> WriterDrainSnapshot:
        return self.snapshot


@pytest.fixture
def decisions(monkeypatch: pytest.MonkeyPatch) -> Decisions:
    value = Decisions()
    monkeypatch.setattr(deletion, "close_writer_admissions", value.close)
    monkeypatch.setattr(deletion, "finalize_writer_deletion", AsyncMock())
    return value


class Storage:
    tenant_id = "tenant"
    memory_id = "bank"

    def __init__(self, decisions: Decisions, *, lose_reply: bool = False) -> None:
        self.decisions = decisions
        self.lose_reply = lose_reply
        self.drops = 0

    async def drop(self) -> None:
        assert not self.decisions.transaction, "Milvus IO must not retain SQL"
        self.drops += 1
        self.decisions.events.append("drop-confirmed")
        if self.lose_reply:
            raise TimeoutError("drop may have committed")


async def test_delete_confirms_drop_before_sql_finalization(decisions: Decisions) -> None:
    target = Storage(decisions)
    result = await decisions.service.reconcile_operation("bank", "delete", storage=target)
    assert result["status"] == "ready" and result["lifecycle"] == "deleted"
    assert result["tracked_cleanup_confirmed"] is True and result["physical_cleanup_complete"] is False
    assert decisions.events == ["drop-confirmed", "sql-delete", "sql-delete", "attempt-finished"]
    assert decisions.bank.reserved_bytes == 0 and decisions.old.receipt["reservation_released"] is True


async def test_unknown_drop_retains_memory_and_original_operation(decisions: Decisions) -> None:
    result = await decisions.service.reconcile_operation("bank", "delete", storage=Storage(decisions, lose_reply=True))
    assert result["status"] == "persistence_unknown" and result["operation_id"] == "delete"
    assert decisions.bank.state == "deleting" and "sql-delete" not in decisions.events
    assert decisions.events[-1] == "attempt-finished"


async def test_actual_old_attempt_blocks_drop_even_if_outbox_is_ready(decisions: Decisions) -> None:
    decisions.old.receipt["active_attempts"] = ["old-token"]
    result = await decisions.service.reconcile_operation("bank", "delete", storage=Storage(decisions))
    assert result["status"] == "published" and result["blocked_by"] == ["active_attempts"]
    assert "drop-confirmed" not in decisions.events and "sql-delete" not in decisions.events


async def test_expired_own_attempt_is_not_automatically_replayed(decisions: Decisions) -> None:
    decisions.outbox.receipt["active_attempts"] = ["crashed-token"]
    with pytest.raises(RevisionBusy, match="operator_reconciliation"):
        await decisions.service.reconcile_operation("bank", "delete", storage=Storage(decisions))
    assert "drop-confirmed" not in decisions.events
    assert decisions.outbox.receipt["active_attempts"] == ["crashed-token"]


async def test_cancelled_drop_drains_before_attempt_is_released(decisions: Decisions) -> None:
    started, release = asyncio.Event(), asyncio.Event()

    class DelayedStorage(Storage):
        async def drop(self) -> None:
            started.set()
            await release.wait()
            await super().drop()

    pending = asyncio.create_task(decisions.service.reconcile_operation("bank", "delete", storage=DelayedStorage(decisions)))
    try:
        await started.wait()
        pending.cancel()
        await asyncio.sleep(0)
        assert "attempt-finished" not in decisions.events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert decisions.events == ["drop-confirmed", "attempt-finished"]
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
