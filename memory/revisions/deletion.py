"""Stop, drain and confirm one bank deletion without losing its recovery handle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory, Task
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.services.memory_service import MemoryService
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, request_hash, validate_operation_id
from memory.revisions.service import operation_view
from memory.revisions.storage import ManagedMessageStore
from memory.writes.admission import close_writer_admissions, finalize_writer_deletion
from memory.writes.types import WriterDrainSnapshot


class DeletionStorage(Protocol):
    tenant_id: str
    memory_id: str

    async def drop(self) -> None: ...


class MemoryDeletionService:
    """Only deletion operations may use the deleted bank's minimal ACL tombstone."""

    def __init__(self, db: AsyncSession, *, actor_id: str) -> None:
        self.db = db
        self.actor_id = actor_id

    async def _idle(self) -> None:
        if self.db.new or self.db.dirty or self.db.deleted:
            raise RuntimeError("Deletion requires a session without pending writes")
        if self.db.in_transaction():
            await self.db.rollback()
        self.db.expire_all()

    @asynccontextmanager
    async def _write(self, operation_id: str) -> AsyncIterator[None]:
        await self._idle()
        await self.db.begin()
        try:
            yield
            await self.db.flush()
        except IntegrityError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("concurrent_memory_deletion") from error
        except BaseException:
            await finish_owned_work(self.db.rollback())
            raise
        try:
            await finish_owned_work(self.db.commit())
        except IntegrityError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("concurrent_memory_deletion") from error
        except DBAPIError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionPersistenceUnknown(operation_id) from error

    async def _authorize(self, memory_id: str, *, lock: bool = False, create_for_deletion: str | None = None) -> tuple[Memory | None, ManagedMemoryState]:
        statement = select(Memory).where(Memory.id == memory_id).execution_options(populate_existing=True)
        memory = await self.db.scalar(statement.with_for_update() if lock else statement)
        bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id).execution_options(populate_existing=True))
        if bank is None and (memory is None or create_for_deletion is None):
            raise RevisionNotFound("memory_not_found")
        if bank is not None and ((memory is None and bank.state != "deleted") or (memory is not None and memory.tenant_id != bank.tenant_id)):
            raise RevisionNotFound("memory_not_found")
        permissions: str | None
        if memory is not None:
            tenant_id, permissions = memory.tenant_id, memory.permissions
        elif bank is not None:
            tenant_id, permissions = bank.tenant_id, bank.delete_permissions
        else:
            raise RevisionNotFound("memory_not_found")
        tenants = await self.db.run_sync(lambda session: MemoryService.accessible_tenant_ids(session, self.actor_id))
        if self.actor_id != tenant_id and not (permissions == "team" and tenant_id in tenants):
            raise RevisionNotFound("memory_not_found")
        if bank is None:
            # This is a deletion-only activation in the request transaction:
            # writers close first and no active revision opt-in is published.
            if not lock or create_for_deletion is None:
                raise RevisionNotFound("memory_not_found")
            await close_writer_admissions(self.db, memory_id=memory_id, tenant_id=tenant_id, operation_id=create_for_deletion)
            bank = ManagedMemoryState(memory_id=memory_id, tenant_id=tenant_id, state="active", generation=1, capacity_generation=1, reserved_bytes=0)
            self.db.add(bank)
            await self.db.flush()
        return memory, bank

    async def _bank(self, memory_id: str, tenant_id: str) -> ManagedMemoryState:
        bank = await self.db.scalar(
            select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id).execution_options(populate_existing=True).with_for_update()
        )
        if bank is None:
            raise RevisionNotFound("memory_not_found")
        return bank

    async def _operation(self, memory_id: str, operation_id: str) -> MemoryMutationOperation | None:
        return await self.db.scalar(
            select(MemoryMutationOperation).where(MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.operation_id == operation_id).execution_options(populate_existing=True)
        )

    async def _outboxes(self, memory_id: str) -> list[MemoryPublicationOutbox]:
        return list(
            await self.db.scalars(
                select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id).order_by(MemoryPublicationOutbox.id).execution_options(populate_existing=True).with_for_update()
            )
        )

    async def _view(self, operation: MemoryMutationOperation) -> dict[str, Any]:
        outbox_id = await self.db.scalar(
            select(MemoryPublicationOutbox.id).where(MemoryPublicationOutbox.memory_id == operation.memory_id, MemoryPublicationOutbox.operation_id == operation.operation_id)
        )
        return {**operation_view(operation, outbox_id), **deepcopy(operation.result)}

    @staticmethod
    def _blocked(snapshot: WriterDrainSnapshot, outboxes: list[MemoryPublicationOutbox], *, own_id: str | None = None) -> tuple[dict[str, int], list[str], bool]:
        tokens = {token for row in outboxes if row.id != own_id for token in row.receipt.get("active_attempts", [])}
        for writer in snapshot["active_operations"]:
            tokens.add(writer.get("active_token") or "writer:" + writer["operation_id"])
        counts = {
            "active_attempts": len(tokens),
            "unknown_attempts": len(snapshot["unknown_operations"]),
            "pending_appends": len(snapshot["pending_operation_ids"]),
            "untracked_tasks": len(snapshot["untracked_task_ids"]),
        }
        blocked = [key for key, count in counts.items() if count]
        if not snapshot["drained"] and not blocked:
            blocked.append("writer_fence_not_drained")
        return counts, blocked, bool(counts["unknown_attempts"] or counts["untracked_tasks"])

    @staticmethod
    def _attestation(snapshot: WriterDrainSnapshot) -> dict[str, Any]:
        attested = snapshot["legacy_quiescence_attested"]
        return {
            "legacy_quiescence_attested": attested,
            "drain_verification": "operator_attestation_and_tracked_attempts" if attested else "tracked_attempts",
            "legacy_quiescence_attestation": deepcopy(snapshot["legacy_quiescence_attestation"]),
        }

    async def get_operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        await self._idle()
        try:
            _, bank = await self._authorize(memory_id)
            operation = await self._operation(memory_id, operation_id)
            if operation is None or operation.kind != "delete_memory" or operation.tenant_id != bank.tenant_id:
                raise RevisionNotFound("operation_not_found")
            return await self._view(operation)
        finally:
            await self.db.rollback()

    async def request_delete(self, memory_id: str, *, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        digest = request_hash({"kind": "delete_memory", "memory": memory_id, "actor": self.actor_id})
        async with self._write(operation_id):
            memory, observed = await self._authorize(memory_id, lock=True, create_for_deletion=operation_id)
            operation = await self._operation(memory_id, operation_id)
            if operation is not None:
                if operation.kind != "delete_memory" or operation.request_sha256 != digest:
                    raise RevisionConflict("operation_request_mismatch")
                return await self._view(operation)
            if memory is None or observed.state != "active":
                raise RevisionConflict("memory_deletion_already_requested")
            snapshot = await close_writer_admissions(self.db, memory_id=memory_id, tenant_id=observed.tenant_id, operation_id=operation_id)
            bank = await self._bank(memory_id, observed.tenant_id)
            if bank.state != "active":
                raise RevisionConflict("memory_deletion_already_requested")
            bank.state, bank.generation, bank.updated_at = "deleting", bank.generation + 1, datetime.now(UTC)
            bank.delete_permissions, bank.delete_operation_id = memory.permissions, operation_id
            for source in await self.db.scalars(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id).order_by(MemorySourceState.source_message_id).with_for_update()):
                source.state, source.generation = "deleted", source.generation + 1
            for entry in await self.db.scalars(select(MemoryEntry).where(MemoryEntry.memory_id == memory_id).order_by(MemoryEntry.entry_id).with_for_update()):
                entry.state, entry.generation = "withdrawn", entry.generation + 1
            from memory.maintenance.lifecycle import invalidate_for_bank

            await invalidate_for_bank(self.db, memory_id=memory_id, tenant_id=bank.tenant_id)
            prior = await self._outboxes(memory_id)
            for row in prior:
                row.state, row.error_code = "redacted", "memory_deletion_requested"
                row.payload = {key: value for key, value in row.payload.items() if key in {"action", "reserved_bytes", "reservation_owner", "published"}}
                previous = await self._operation(memory_id, row.operation_id)
                if previous is not None:
                    previous.status, previous.index_sync_status, previous.error_code = "stale", "cleanup_pending", "memory_deletion_requested"
            counts, blocked, operator = self._blocked(snapshot, prior)
            operation = MemoryMutationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=bank.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="delete_memory",
                status="published",
                index_sync_status="pending",
                result={
                    "lifecycle": "deleting",
                    "stop_supply": "stopped",
                    "tracked_cleanup_confirmed": False,
                    "physical_cleanup_complete": False,
                    "untraceable_copies": True,
                    "writer_drain": counts,
                    "blocked_by": blocked,
                    "needs_operator_reconciliation": operator,
                    **self._attestation(snapshot),
                },
            )
            self.db.add(operation)
            self.db.add(
                MemoryPublicationOutbox(
                    id=uuid4().hex,
                    memory_id=memory_id,
                    tenant_id=bank.tenant_id,
                    operation_id=operation_id,
                    bank_generation=bank.generation,
                    state="pending",
                    attempt=0,
                    payload={"action": "delete_bank"},
                    receipt={},
                )
            )
            await self.db.flush()
            result = await self._view(operation)
        return result

    async def reconcile_operation(self, memory_id: str, operation_id: str, *, storage: DeletionStorage | None = None) -> dict[str, Any]:
        token = uuid4().hex
        try:
            return await self._reconcile(memory_id, operation_id, token, storage)
        finally:
            await finish_owned_work(self._finish_attempt(memory_id, operation_id, token))

    async def _reconcile(self, memory_id: str, operation_id: str, token: str, storage: DeletionStorage | None) -> dict[str, Any]:
        validate_operation_id(operation_id)
        async with self._write(operation_id):
            memory, observed = await self._authorize(memory_id, lock=True)
            operation = await self._operation(memory_id, operation_id)
            if operation is None or operation.kind != "delete_memory":
                raise RevisionNotFound("operation_not_found")
            if operation.status == "ready":
                return await self._view(operation)
            if memory is None:
                raise RevisionConflict("memory_deletion_state_unknown")
            snapshot = await close_writer_admissions(self.db, memory_id=memory_id, tenant_id=observed.tenant_id, operation_id=operation_id)
            bank = await self._bank(memory_id, observed.tenant_id)
            rows = await self._outboxes(memory_id)
            own = next((row for row in rows if row.operation_id == operation_id), None)
            if own is None or own.payload.get("action") != "delete_bank" or bank.state != "deleting" or bank.delete_operation_id != operation_id or own.bank_generation != bank.generation:
                raise RevisionConflict("memory_deletion_fence_changed")
            if own.receipt.get("active_attempts"):
                raise RevisionBusy("memory_deletion_needs_operator_reconciliation")
            counts, blocked, operator = self._blocked(snapshot, rows, own_id=own.id)
            operation.result = {**operation.result, "writer_drain": counts, "blocked_by": blocked, "needs_operator_reconciliation": operator, **self._attestation(snapshot)}
            if blocked:
                operation.error_code = "memory_deletion_waiting_for_writers"
                return await self._view(operation)
            own.attempt += 1
            own.lease_token, own.lease_until = token, datetime.now(UTC) + timedelta(seconds=120)
            own.receipt = {**own.receipt, "active_attempts": [token]}
            outbox_id, tenant_id = own.id, bank.tenant_id
        target = storage if storage is not None else ManagedMessageStore(tenant_id, memory_id)
        if target.memory_id != memory_id or target.tenant_id != tenant_id:
            raise RevisionConflict("memory_deletion_storage_binding_mismatch")
        try:
            await finish_owned_work(target.drop())
        except Exception:
            async with self._write(operation_id):
                operation = await self._operation(memory_id, operation_id)
                if operation is None:
                    raise RevisionNotFound("operation_not_found")
                operation.status, operation.index_sync_status, operation.error_code = "persistence_unknown", "unknown", "memory_drop_unconfirmed"
                return await self._view(operation)
        async with self._write(operation_id):
            memory, observed = await self._authorize(memory_id, lock=True)
            if memory is None:
                raise RevisionConflict("memory_deletion_state_unknown")
            snapshot = await close_writer_admissions(self.db, memory_id=memory_id, tenant_id=observed.tenant_id, operation_id=operation_id)
            bank = await self._bank(memory_id, observed.tenant_id)
            rows = await self._outboxes(memory_id)
            own = next((row for row in rows if row.id == outbox_id), None)
            operation = await self._operation(memory_id, operation_id)
            if own is None or operation is None or own.lease_token != token or own.bank_generation != bank.generation or bank.state != "deleting" or bank.delete_operation_id != operation_id:
                raise RevisionConflict("memory_deletion_fence_changed")
            _, blocked, _ = self._blocked(snapshot, rows, own_id=own.id)
            if blocked:
                raise RevisionConflict("memory_deletion_writer_drain_changed")
            await finalize_writer_deletion(self.db, memory_id=memory_id, tenant_id=bank.tenant_id, operation_id=operation_id)
            for revision in await self.db.scalars(select(MemoryEntryRevision).where(MemoryEntryRevision.memory_id == memory_id).with_for_update()):
                revision.content, revision.message_snapshot = None, {}
                revision.assessment = {key: value for key, value in revision.assessment.items() if key in {"kind", "verification_status", "actor_id", "operation_id"}}
            for source in await self.db.scalars(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id).with_for_update()):
                source.scope = {}
            for reference in await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.memory_id == memory_id)):
                reference.locator = {}
            for row in rows:
                row.payload = {key: value for key, value in row.payload.items() if key == "action"}
                row.receipt = {**row.receipt, "reservation_released": True}
            for previous in await self.db.scalars(select(MemoryMutationOperation).where(MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.operation_id != operation_id)):
                previous.result = {}
            await self.db.execute(delete(Task).where(Task.doc_id == memory_id, Task.task_type == "memory"))
            deleted = await self.db.execute(delete(Memory).where(Memory.id == memory_id, Memory.tenant_id == tenant_id))
            if deleted.rowcount != 1:  # type: ignore[attr-defined]
                raise RevisionConflict("memory_deletion_not_confirmed")
            bank.state, bank.capacity_generation, bank.reserved_bytes = "deleted", bank.capacity_generation + 1, 0
            bank.updated_at = datetime.now(UTC)
            own.state, own.lease_token, own.lease_until, own.error_code = "ready", None, None, None
            operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
            operation.result = {**operation.result, "lifecycle": "deleted", "tracked_cleanup_confirmed": True, "blocked_by": [], "needs_operator_reconciliation": False}
            result = await self._view(operation)
        return result

    async def _finish_attempt(self, memory_id: str, operation_id: str, token: str) -> None:
        async with self._write(operation_id):
            row = await self.db.scalar(
                select(MemoryPublicationOutbox)
                .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if row is None or token not in row.receipt.get("active_attempts", []):
                return
            receipt = deepcopy(row.receipt)
            receipt["active_attempts"] = [attempt for attempt in receipt.get("active_attempts", []) if attempt != token]
            receipt["finished_attempts"] = list(dict.fromkeys([*receipt.get("finished_attempts", []), token]))
            row.receipt = receipt
            if row.lease_token == token:
                row.lease_token, row.lease_until = None, None
