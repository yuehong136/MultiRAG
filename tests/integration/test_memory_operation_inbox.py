"""Durable operation discovery uses real SQL and current owner/team permission."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.apps.services.memory_operation_service import list_operations, recovery_metadata
from api.db.db_models import Memory, UserTenant
from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError
from tests.support.database import scratch_database


@pytest.fixture
async def inbox(tmp_path: Path) -> AsyncIterator[async_sessionmaker[Any]]:
    with scratch_database(tmp_path / "operation-inbox") as engine:
        asynchronous = create_async_engine(engine.url)
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)
        try:
            async with factory() as db, db.begin():
                db.add(Memory(id="bank", tenant_id="owner", name="owned scratch", permissions="team", embd_id="unused", llm_id="unused"))
                db.add(UserTenant(id="membership", tenant_id="owner", user_id="member", role="normal", invited_by="owner", status="1"))
            yield factory
        finally:
            await asynchronous.dispose()


def operation(identity: str, *, kind: str = "dream_publish", status: str = "accepted", sync: str = "pending", actor: str = "owner", stamp: datetime | None = None) -> MemoryMutationOperation:
    return MemoryMutationOperation(
        memory_id="bank",
        operation_id=identity,
        tenant_id="owner",
        request_sha256="a" * 64,
        actor_id=actor,
        kind=kind,
        status=status,
        index_sync_status=sync,
        result={"private_prompt": "never disclose this body"},
        created_at=stamp or datetime.now(UTC),
        updated_at=stamp or datetime.now(UTC),
    )


async def test_pending_includes_partial_unknown_and_excludes_ready_and_internal_children(inbox: async_sessionmaker[Any]) -> None:
    async with inbox() as db, db.begin():
        db.add_all([operation("pending"), operation("partial", status="stale", sync="unknown"), operation("complete", status="ready", sync="ready"), operation("internal", kind="dream_publish_item")])
    async with inbox() as db:
        value = await list_operations(db, "bank", actor_id="owner")
        assert {item["operation_id"] for item in value["operations"]} == {"pending", "partial"}
        assert all(item["retryable"] and item["can_reconcile"] for item in value["operations"])
        assert "private_prompt" not in str(value) and "never disclose" not in str(value)
        assert not db.in_transaction()
    async with inbox() as db:
        assert len((await list_operations(db, "bank", actor_id="owner", view="all"))["operations"]) == 3


async def test_cursor_is_scoped_and_stably_seeks_equal_timestamps(inbox: async_sessionmaker[Any]) -> None:
    stamp = datetime.now(UTC)
    async with inbox() as db, db.begin():
        db.add_all([operation(identity, stamp=stamp) for identity in ("a", "b", "c")])
    async with inbox() as db:
        first = await list_operations(db, "bank", actor_id="owner", limit=2)
        assert [item["operation_id"] for item in first["operations"]] == ["c", "b"]
    async with inbox() as db, db.begin():
        db.add(operation("newer", stamp=stamp + timedelta(seconds=1)))
    async with inbox() as db:
        second = await list_operations(db, "bank", actor_id="owner", limit=2, cursor=first["next_cursor"])
        assert [item["operation_id"] for item in second["operations"]] == ["a"] and second["next_cursor"] is None
        with pytest.raises(RevisionValidationError):
            await list_operations(db, "bank", actor_id="member", limit=2, cursor=first["next_cursor"])


async def test_team_read_does_not_authorize_replaying_another_actor_and_membership_is_fresh(inbox: async_sessionmaker[Any]) -> None:
    async with inbox() as db, db.begin():
        db.add(operation("owner-command"))
    async with inbox() as db:
        value = await list_operations(db, "bank", actor_id="member")
        assert value["operations"][0]["can_reconcile"] is False
        with pytest.raises(RevisionNotFound):
            await list_operations(db, "bank", actor_id="foreign")
    async with inbox() as db, db.begin():
        await db.execute(delete(UserTenant).where(UserTenant.id == "membership"))
    async with inbox() as db:
        with pytest.raises(RevisionNotFound):
            await list_operations(db, "bank", actor_id="member")


async def test_team_member_cannot_execute_the_owner_operation_discovered_in_inbox(inbox: async_sessionmaker[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service, memory_revision_service

    calls: list[str] = []

    async def must_not_construct(*args: Any, **kwargs: Any) -> None:
        calls.append("provider")
        raise AssertionError("Foreign actor reached execution")

    monkeypatch.setattr(memory_maintenance_service, "publications", must_not_construct)
    async with inbox() as db, db.begin():
        db.add(operation("approved-owner-batch"))
    async with inbox() as db:
        discovered = await list_operations(db, "bank", actor_id="member")
        assert discovered["operations"][0]["operation_id"] == "approved-owner-batch"
        with pytest.raises(RevisionConflict, match="operation_actor_mismatch"):
            await memory_revision_service.reconcile_operation(db, "bank", "approved-owner-batch", actor_id="member")
    assert calls == []


async def test_deleted_bank_only_exposes_minimal_delete_audit_with_current_acl(inbox: async_sessionmaker[Any]) -> None:
    async with inbox() as db, db.begin():
        await db.execute(delete(Memory).where(Memory.id == "bank"))
        db.add(ManagedMemoryState(memory_id="bank", tenant_id="owner", state="deleted", generation=2, capacity_generation=1, reserved_bytes=0, delete_permissions="team"))
        db.add_all([operation("delete-audit", kind="delete_memory", status="ready", sync="ready"), operation("old-analysis", kind="dream_analyze")])
    async with inbox() as db:
        value = await list_operations(db, "bank", actor_id="member", view="all")
        assert value["memory_state"] == "deleted" and [row["operation_id"] for row in value["operations"]] == ["delete-audit"]
        assert (await list_operations(db, "bank", actor_id="owner"))["operations"] == []
        with pytest.raises(RevisionNotFound):
            await list_operations(db, "bank", actor_id="foreign", view="all")


async def test_queued_and_unknown_executions_are_discoverable_without_replaying_work(inbox: async_sessionmaker[Any]) -> None:
    job_id = "a" * 32
    async with inbox() as db, db.begin():
        db.add(operation("durable-analysis", kind="dream_analyze"))
        db.add(
            MemoryMaintenanceJob(
                job_id=job_id,
                memory_id="bank",
                tenant_id="owner",
                actor_id="owner",
                operation_id="create-job",
                request_sha256="b" * 64,
                state="pending",
                analysis_operation_id="durable-analysis",
                policy="general",
                input_budget_tokens=4096,
                output_budget_tokens=1024,
            )
        )
        await db.flush()
        db.add(
            MemoryMaintenanceExecution(
                job_id=job_id,
                memory_id="bank",
                tenant_id="owner",
                actor_id="owner",
                operation_id="durable-analysis",
                request_sha256="c" * 64,
                job_generation=1,
                state="queued",
                claim_token="secret-private-token",
                worker_id="private-worker-identity",
                attempt=0,
            )
        )
    async with inbox() as db:
        queued = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert queued["job_id"] == job_id and queued["execution"]["state"] == "queued" and queued["can_reconcile"] is False
        assert "secret-private-token" not in str(queued) and "private-worker-identity" not in str(queued)
        metadata = await recovery_metadata(db, "bank", "durable-analysis", actor_id="owner", operation={"retryable": True})
        assert metadata["execution"]["state"] == "queued" and metadata["can_reconcile"] is False
    async with inbox() as db, db.begin():
        row = await db.get(MemoryMaintenanceExecution, job_id)
        command = await db.get(MemoryMutationOperation, ("bank", "durable-analysis"))
        job = await db.get(MemoryMaintenanceJob, job_id)
        assert row is not None and command is not None and job is not None
        row.state, job.state = "running", "ready"
        command.status, command.index_sync_status = "ready", "ready"
    async with inbox() as db:
        awaiting_bookkeeping = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert awaiting_bookkeeping["execution"]["state"] == "running" and awaiting_bookkeeping["retryable"] and not awaiting_bookkeeping["can_reconcile"]
        metadata = await recovery_metadata(db, "bank", "durable-analysis", actor_id="owner", operation={"retryable": False})
        assert metadata["retryable"] and not metadata["can_reconcile"]
    async with inbox() as db, db.begin():
        row = await db.get(MemoryMaintenanceExecution, job_id)
        assert row is not None and row.attempt == 0
        row.state, row.receipt = "needs_reconciliation", {"needs_operator_reconciliation": True}
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="durable-analysis",
                tenant_id="owner",
                actor_id="owner",
                kind="dream_analyze",
                request_sha256="c" * 64,
                writer_generation=1,
                state="unknown",
                active_token="unknown-actual-work",
                attempt=1,
            )
        )
    async with inbox() as db:
        unknown = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert unknown["needs_operator_reconciliation"] is True and unknown["can_reconcile"] is False
    async with inbox() as db, db.begin():
        actual = await db.get(MemoryWriterAdmission, ("bank", "durable-analysis"))
        assert actual is not None
        actual.active_token = None
    async with inbox() as db:
        settled = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert settled["needs_operator_reconciliation"] is False and settled["can_reconcile"] is True


async def test_ready_raw_with_pending_dispatch_and_unknown_writer_remains_in_inbox(inbox: async_sessionmaker[Any]) -> None:
    async with inbox() as db, db.begin():
        row = operation("stored-but-undispatched", kind="append_raw", status="ready", sync="ready")
        row.result = {"queue_state": "pending", "private_prompt": "never disclose"}
        db.add(row)
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id=row.operation_id,
                tenant_id="owner",
                actor_id="owner",
                kind="append_raw",
                request_sha256="c" * 64,
                writer_generation=1,
                state="unknown",
                active_token="unsettled-dispatch",
                attempt=1,
            )
        )
    async with inbox() as db:
        value = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert value["status"] == value["index_sync_status"] == "ready" and value["queue_state"] == "pending"
        assert value["writer_busy"] and value["needs_operator_reconciliation"] and not value["can_reconcile"]
        metadata = await recovery_metadata(db, "bank", "stored-but-undispatched", actor_id="owner", operation={"retryable": False})
        assert metadata["writer_busy"] and metadata["queue_state"] == "pending"
    async with inbox() as db, db.begin():
        row = await db.get(MemoryMutationOperation, ("bank", "stored-but-undispatched"))
        actual = await db.get(MemoryWriterAdmission, ("bank", "stored-but-undispatched"))
        assert row is not None and actual is not None
        actual.active_token, actual.state = None, "finished"
    async with inbox() as db:
        settled = (await list_operations(db, "bank", actor_id="owner"))["operations"][0]
        assert settled["retryable"] and settled["can_reconcile"] and not settled["writer_busy"]
        metadata = await recovery_metadata(db, "bank", "stored-but-undispatched", actor_id="owner", operation={"retryable": False})
        assert metadata["retryable"] and metadata["can_reconcile"]
    async with inbox() as db, db.begin():
        row = await db.get(MemoryMutationOperation, ("bank", "stored-but-undispatched"))
        assert row is not None
        row.result = {"queue_state": "submitted"}
    async with inbox() as db:
        assert not (await list_operations(db, "bank", actor_id="owner"))["operations"]
