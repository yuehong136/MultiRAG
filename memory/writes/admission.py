"""Durable admission of actual writers, independent of managed-memory activation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory, Task
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, positive_int, request_hash, validate_operation_id
from memory.revisions.service import operation_view
from memory.writes.types import Authorize, WriterDrainSnapshot

MAX_WRITER_INVENTORY = 10000


async def _fence(db: AsyncSession, memory_id: str, tenant_id: str, *, create: bool = False, lock: bool = False) -> MemoryWriterFence | None:
    if create:
        await db.execute(insert(MemoryWriterFence).values(memory_id=memory_id, tenant_id=tenant_id, generation=1, state="open", receipt={}).on_conflict_do_nothing(index_elements=["memory_id"]))
    query = select(MemoryWriterFence).where(MemoryWriterFence.memory_id == memory_id).execution_options(populate_existing=True)
    fence = await db.scalar(query.with_for_update() if lock else query)
    if fence is not None and fence.tenant_id != tenant_id:
        raise RevisionConflict("writer_memory_binding_mismatch")
    return fence


async def _admissions(db: AsyncSession, memory_id: str, tenant_id: str, *, lock: bool = False) -> list[MemoryWriterAdmission]:
    query = (
        select(MemoryWriterAdmission)
        .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == tenant_id)
        .order_by(MemoryWriterAdmission.operation_id)
        .limit(MAX_WRITER_INVENTORY + 1)
        .execution_options(populate_existing=True)
    )
    rows = list((await db.scalars(query.with_for_update() if lock else query)).all())
    if len(rows) > MAX_WRITER_INVENTORY:
        raise RevisionConflict("writer_inventory_incomplete")
    return rows


async def _legacy_tasks(db: AsyncSession, memory_id: str, admissions: list[MemoryWriterAdmission]) -> dict[str, str]:
    rows = list(
        (
            await db.execute(
                select(Task.id, Task.doc_id, Task.task_type, Task.digest, Task.begin_at, Task.create_time)
                .where(Task.doc_id == memory_id, Task.task_type == "memory")
                .order_by(Task.id)
                .limit(MAX_WRITER_INVENTORY + 1)
            )
        ).all()
    )
    if len(rows) > MAX_WRITER_INVENTORY:
        raise RevisionConflict("legacy_task_inventory_incomplete")
    known = {row.task_id for row in admissions if row.task_id is not None}
    return {
        row.id: request_hash(
            {
                "id": row.id,
                "memory_id": row.doc_id,
                "kind": row.task_type,
                "digest": row.digest,
                "begin_at": row.begin_at.isoformat() if row.begin_at is not None else None,
                "create_time": row.create_time,
            }
        )
        for row in rows
        if row.id not in known
    }


def legacy_task_ids_sha256(task_ids: list[str]) -> str:
    return request_hash({"task_ids": sorted(set(task_ids))})


async def writer_drain_snapshot(db: AsyncSession, *, memory_id: str, tenant_id: str) -> WriterDrainSnapshot:
    """Read observed attempts and separately identified administrator attestations."""
    fence = await _fence(db, memory_id, tenant_id)
    rows = await _admissions(db, memory_id, tenant_id)
    legacy = await _legacy_tasks(db, memory_id, rows)
    receipt = fence.receipt if fence is not None else {}
    acknowledged = receipt.get("acknowledged_legacy_tasks", {})
    untracked = sorted(task_id for task_id, binding in legacy.items() if acknowledged.get(task_id) != binding)
    attestation = receipt.get("legacy_quiescence_attestation")
    active = [
        {"operation_id": row.operation_id, "kind": row.kind, "task_id": row.task_id, "active_token": row.active_token, "state": row.state}
        for row in rows
        if row.active_token is not None or row.state == "active"
    ]
    unknown = [{"operation_id": row.operation_id, "kind": row.kind, "task_id": row.task_id, "state": row.state} for row in rows if row.state == "unknown"]
    return {
        "state": fence.state if fence is not None else "open",
        "generation": fence.generation if fence is not None else 1,
        "active_operations": active,
        "unknown_operations": unknown,
        "pending_operation_ids": [row.operation_id for row in rows if row.state == "queued" and row.active_token is None],
        "untracked_task_ids": untracked,
        "untracked_task_ids_sha256": legacy_task_ids_sha256(untracked),
        "legacy_quiescence_attested": attestation is not None,
        "legacy_quiescence_attestation": deepcopy(attestation),
        "drained": not active and not unknown and not untracked,
    }


async def assert_activation_quiescent(db: AsyncSession, *, memory_id: str, tenant_id: str) -> None:
    """Caller holds Memory; historic untracked Tasks are an explicit cutover limit."""
    fence = await _fence(db, memory_id, tenant_id, lock=True)
    if fence is not None and fence.state != "open":
        raise RevisionConflict("memory_writer_closed")
    if fence is not None and fence.reserved_bytes:
        raise RevisionBusy("memory_writer_publication_must_finish_before_activation")
    if any(row.active_token is not None or row.state in {"active", "unknown"} for row in await _admissions(db, memory_id, tenant_id)):
        raise RevisionBusy("memory_writer_must_drain_before_activation")


async def lock_open_writer_fence(db: AsyncSession, *, memory_id: str, tenant_id: str) -> MemoryWriterFence:
    """Caller authorizes first; acquire the shared Memory/fence/bank order, no commit."""
    if await db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == tenant_id).with_for_update()) is None:
        raise RevisionNotFound("memory_not_found")
    fence = await _fence(db, memory_id, tenant_id, create=True, lock=True)
    assert fence is not None
    if fence.state != "open":
        raise RevisionConflict("memory_writer_closed")
    bank = await db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id).with_for_update())
    if bank is not None and bank.state != "active":
        raise RevisionConflict("memory_not_active")
    return fence


async def release_writer_reservation(
    db: AsyncSession, *, memory_id: str, tenant_id: str, outbox: MemoryPublicationOutbox, fence: MemoryWriterFence | None = None, bank: ManagedMemoryState | None = None
) -> None:
    """Caller holds higher locks and outbox; transfer a reservation exactly once."""
    if outbox.memory_id != memory_id or outbox.tenant_id != tenant_id:
        raise RevisionConflict("writer_reservation_scope_mismatch")
    if outbox.receipt.get("reservation_released"):
        return
    reserved = outbox.payload.get("reserved_bytes", 0)
    if type(reserved) is not int or reserved < 0:
        raise RevisionConflict("writer_reservation_invalid")
    if reserved:
        if outbox.payload.get("reservation_owner", "managed_bank") == "writer_fence":
            if fence is None or fence.memory_id != memory_id or fence.tenant_id != tenant_id or fence.reserved_bytes < reserved:
                raise RevisionConflict("writer_capacity_fence_required")
            fence.reserved_bytes -= reserved
            fence.capacity_generation += 1
        else:
            if bank is None or bank.memory_id != memory_id or bank.tenant_id != tenant_id or bank.reserved_bytes < reserved:
                raise RevisionConflict("writer_capacity_bank_required")
            bank.reserved_bytes -= reserved
            bank.capacity_generation += 1
    outbox.receipt = {**outbox.receipt, "reservation_released": True}


async def close_writer_admissions(db: AsyncSession, *, memory_id: str, tenant_id: str, operation_id: str) -> WriterDrainSnapshot:
    """Close under the caller's Memory lock; do not commit or infer crashed IO ended."""
    validate_operation_id(operation_id)
    fence = await _fence(db, memory_id, tenant_id, create=True, lock=True)
    assert fence is not None
    if fence.state != "open" and fence.close_operation_id != operation_id:
        raise RevisionConflict("writer_deletion_operation_mismatch")
    if fence.state == "open":
        fence.state, fence.close_operation_id = "closing", operation_id
        fence.generation += 1
        fence.updated_at = datetime.now(UTC)
    await db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id).with_for_update())
    for row in await _admissions(db, memory_id, tenant_id, lock=True):
        row.publication_state = "canceled"
        if row.active_token is None and row.state not in {"active", "unknown"}:
            row.state = "canceled"
        row.receipt = {**row.receipt, "cancellation_requested": True}
    await db.flush()
    return await writer_drain_snapshot(db, memory_id=memory_id, tenant_id=tenant_id)


async def finalize_writer_deletion(db: AsyncSession, *, memory_id: str, tenant_id: str, operation_id: str) -> None:
    """Keep ownership/attestation audit after the caller confirms physical absence."""
    fence = await _fence(db, memory_id, tenant_id, lock=True)
    if fence is None or fence.close_operation_id != operation_id or fence.state not in {"closing", "deleted"}:
        raise RevisionConflict("writer_deletion_fence_changed")
    await db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id).with_for_update())
    if not (await writer_drain_snapshot(db, memory_id=memory_id, tenant_id=tenant_id))["drained"]:
        raise RevisionBusy("memory_writers_not_drained")
    for row in await _admissions(db, memory_id, tenant_id, lock=True):
        row.publication_state, row.state = "canceled", "canceled"
        row.request_metadata = {key: value for key, value in row.request_metadata.items() if key in {"content_sha256", "source_content_sha256", "source_format", "server_metadata_sha256"}}
        row.receipt = {key: value for key, value in row.receipt.items() if key in {"finished_tokens", "outcome", "cancellation_requested", "operator_quiescence_attestations"}}
    fence.state, fence.updated_at = "deleted", datetime.now(UTC)
    fence.reserved_bytes = 0
    fence.capacity_generation += 1


async def acknowledge_legacy_quiescence(
    db: AsyncSession, *, memory_id: str, tenant_id: str, expected_generation: int, expected_untracked_task_ids_sha256: str, evidence_reference: str
) -> WriterDrainSnapshot:
    """Operator-only attestation, never an automatic proof or an HTTP user flag."""
    positive_int(expected_generation, "invalid_writer_generation")
    if not isinstance(evidence_reference, str) or not evidence_reference.strip() or len(evidence_reference.encode("utf-8")) > 2048:
        raise RevisionValidationError("operator_evidence_required")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Operator attestation requires a clean session")
    if db.in_transaction():
        await db.rollback()
    try:
        await db.begin()
        await db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == tenant_id).with_for_update())
        fence = await _fence(db, memory_id, tenant_id, lock=True)
        if fence is None or fence.generation != expected_generation or fence.state != "closing":
            raise RevisionConflict("operator_attestation_generation_changed")
        snapshot = await writer_drain_snapshot(db, memory_id=memory_id, tenant_id=tenant_id)
        if snapshot["untracked_task_ids_sha256"] != expected_untracked_task_ids_sha256:
            raise RevisionConflict("operator_attestation_task_set_changed")
        bindings = await _legacy_tasks(db, memory_id, await _admissions(db, memory_id, tenant_id))
        selected = {task_id: bindings[task_id] for task_id in snapshot["untracked_task_ids"]}
        receipt = {
            "generation": fence.generation,
            "task_ids_sha256": expected_untracked_task_ids_sha256,
            "evidence_reference": evidence_reference,
            "acknowledged_at": datetime.now(UTC).isoformat(),
            "verification": "operator_attestation",
        }
        fence.receipt = {**fence.receipt, "acknowledged_legacy_tasks": {**fence.receipt.get("acknowledged_legacy_tasks", {}), **selected}, "legacy_quiescence_attestation": receipt}
        await db.flush()
        result = await writer_drain_snapshot(db, memory_id=memory_id, tenant_id=tenant_id)
        await finish_owned_work(db.commit())
        return result
    except BaseException:
        await finish_owned_work(db.rollback())
        raise


async def invalidate_source_writers(db: AsyncSession, *, memory_id: str, tenant_id: str, source_id: int, permanent: bool = True) -> dict[str, list[Any]]:
    """Caller holds bank/source locks; never acquire the higher Memory/fence locks."""
    rows = list(
        (
            await db.scalars(
                select(MemoryWriterAdmission)
                .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == tenant_id, MemoryWriterAdmission.source_message_id == source_id)
                .with_for_update()
            )
        ).all()
    )
    ids: set[int] = set()
    outbox_ids: list[str] = []
    active_operation_ids: list[str] = []
    for row in rows:
        # A reviewed batch can contain independent published items. Its own
        # lifecycle hook cancels an unpublished batch or retires affected items.
        if row.kind == "dream_publish":
            continue
        if not permanent and row.publication_state in {"published", "ready"}:
            continue
        if row.active_token is not None or row.state in {"active", "unknown"}:
            active_operation_ids.append(row.operation_id)
        ids.update(row.message_ids)
        row.publication_state = "canceled"
        if row.active_token is None:
            row.state = "canceled"
        row.receipt = {**row.receipt, "cancellation_requested": True}
        if permanent:
            row.request_metadata = {key: value for key, value in row.request_metadata.items() if key in {"content_sha256", "source_content_sha256", "source_format", "server_metadata_sha256"}}
        outbox = await db.scalar(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == row.operation_id).with_for_update())
        if outbox is not None:
            if row.active_token is not None and row.active_token not in outbox.receipt.get("active_attempts", []):
                outbox.receipt = {**outbox.receipt, "active_attempts": [*outbox.receipt.get("active_attempts", []), row.active_token]}
            if permanent:
                outbox.payload = {key: value for key, value in outbox.payload.items() if key in {"action", "reserved_bytes", "reservation_owner", "published"}}
            outbox.state, outbox.error_code = "redacted" if permanent else "stale", "source_withdrawn" if permanent else "source_state_changed"
            outbox_ids.append(outbox.id)
        operation = await db.get(MemoryMutationOperation, (memory_id, row.operation_id))
        if operation is not None:
            operation.status, operation.index_sync_status, operation.error_code = "stale", "cleanup_pending", "source_withdrawn" if permanent else "source_state_changed"
            operation.result = {key: value for key, value in operation.result.items() if key in {"raw_source_id", "extraction_task_id", "queue_state", "evicted_sources"}}
    from memory.maintenance.lifecycle import invalidate_for_source

    maintenance = await invalidate_for_source(db, memory_id=memory_id, tenant_id=tenant_id, source_id=source_id, permanent=permanent)
    return {
        "outbox_ids": list(dict.fromkeys([*outbox_ids, *maintenance["outbox_ids"]])),
        "message_ids": sorted(ids | set(maintenance["message_ids"])),
        "active_operation_ids": list(dict.fromkeys([*active_operation_ids, *maintenance["active_operation_ids"]])),
    }


class WriterCoordinator:
    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str, authorize: Authorize) -> None:
        self.db, self.tenant_id, self.actor_id, self.authorize = db, tenant_id, actor_id, authorize

    async def _idle(self) -> None:
        if self.db.new or self.db.dirty or self.db.deleted:
            raise RuntimeError("Writer service requires a clean session")
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
            raise RevisionConflict("concurrent_writer_operation") from error
        except BaseException:
            await finish_owned_work(self.db.rollback())
            raise
        try:
            await finish_owned_work(self.db.commit())
        except DBAPIError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionPersistenceUnknown(operation_id) from error

    async def _memory_fence(self, memory_id: str) -> tuple[Memory, MemoryWriterFence]:
        memory = await self.db.scalar(select(Memory).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id).execution_options(populate_existing=True).with_for_update())
        if memory is None:
            raise RevisionNotFound("memory_not_found")
        await self.authorize(self.db, memory_id)
        fence = await _fence(self.db, memory_id, self.tenant_id, create=True, lock=True)
        assert fence is not None
        if fence.state != "open":
            raise RevisionConflict("memory_writer_closed")
        bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id).with_for_update())
        if bank is not None and bank.state != "active":
            raise RevisionConflict("memory_not_active")
        return memory, fence

    async def _row(self, memory_id: str, operation_id: str, *, lock: bool = False) -> MemoryWriterAdmission:
        query = (
            select(MemoryWriterAdmission)
            .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.operation_id == operation_id, MemoryWriterAdmission.tenant_id == self.tenant_id)
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise RevisionNotFound("writer_operation_not_found")
        return row

    async def _outbox(self, row: MemoryWriterAdmission) -> MemoryPublicationOutbox:
        value = await self.db.scalar(
            select(MemoryPublicationOutbox)
            .where(MemoryPublicationOutbox.memory_id == row.memory_id, MemoryPublicationOutbox.operation_id == row.operation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if value is None:
            raise RevisionNotFound("writer_publication_not_found")
        return value

    async def _view(self, row: MemoryWriterAdmission) -> dict[str, Any]:
        outbox = await self._outbox(row)
        return {
            "operation_id": row.operation_id,
            "memory_id": row.memory_id,
            "tenant_id": row.tenant_id,
            "actor_id": row.actor_id,
            "kind": row.kind,
            "state": row.state,
            "publication_state": row.publication_state,
            "writer_generation": row.writer_generation,
            "active_token": row.active_token,
            "message_ids": list(row.message_ids),
            "source_id": row.source_message_id,
            "task_id": row.task_id,
            "outbox_id": outbox.id,
            "payload": deepcopy(outbox.payload),
            "receipt": deepcopy(outbox.receipt),
        }

    async def _configure_admission(self, row: MemoryWriterAdmission, fence: MemoryWriterFence, request: dict[str, Any]) -> None:
        """Specialized SQL metadata only, inside the initial admission transaction."""
        return None

    async def admit(
        self, memory_id: str, *, operation_id: str, kind: str, request: dict[str, Any], task_id: str | None = None, source_id: int | None = None, server_metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        validate_operation_id(operation_id)
        if not kind or len(kind) > 24:
            raise RevisionValidationError("invalid_writer_kind")
        if source_id is not None:
            positive_int(source_id, "invalid_source_id")
        digest = request_hash({"tenant_id": self.tenant_id, "actor_id": self.actor_id, "memory_id": memory_id, "kind": kind, "request": request})
        async with self._write(operation_id):
            _, fence = await self._memory_fence(memory_id)
            row = await self.db.get(MemoryWriterAdmission, (memory_id, operation_id))
            if row is not None:
                if row.request_sha256 != digest or row.tenant_id != self.tenant_id:
                    raise RevisionConflict("operation_request_mismatch")
            else:
                if await self.db.get(MemoryMutationOperation, (memory_id, operation_id)) is not None:
                    raise RevisionConflict("operation_request_mismatch")
                row = MemoryWriterAdmission(
                    memory_id=memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    actor_id=self.actor_id,
                    kind=kind,
                    request_sha256=digest,
                    writer_generation=fence.generation,
                    state="queued",
                    publication_state="pending",
                    message_ids=[],
                    task_id=task_id,
                    source_message_id=source_id,
                    request_metadata={"source_format": server_metadata["source_format"], "server_metadata_sha256": request_hash(server_metadata)}
                    if server_metadata and server_metadata.get("source_format")
                    else {},
                    receipt={},
                )
                self.db.add(row)
                self.db.add(
                    MemoryMutationOperation(
                        memory_id=memory_id,
                        operation_id=operation_id,
                        tenant_id=self.tenant_id,
                        actor_id=self.actor_id,
                        kind=kind,
                        request_sha256=digest,
                        status="accepted",
                        index_sync_status="pending",
                        result={},
                    )
                )
                self.db.add(
                    MemoryPublicationOutbox(
                        id=uuid4().hex,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        operation_id=operation_id,
                        bank_generation=1,
                        state="pending",
                        attempt=0,
                        payload={"action": kind, "request": deepcopy(request), "server_metadata": deepcopy(server_metadata or {}), "approved": False, "reserved_bytes": 0},
                        receipt={},
                    )
                )
                await self._configure_admission(row, fence, request)
                await self.db.flush()
            result = await self._view(row)
        return result

    async def claim(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        async with self._write(operation_id):
            memory, fence = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.writer_generation != fence.generation or row.publication_state == "canceled" or row.state == "canceled":
                raise RevisionConflict("writer_generation_changed")
            if row.active_token is not None or row.state in {"active", "unknown"}:
                raise RevisionBusy("writer_needs_operator_reconciliation")
            if any(
                (other.active_token is not None or other.state in {"active", "unknown"}) and other.publication_state != "ready"
                for other in await _admissions(self.db, memory_id, self.tenant_id)
                if other.operation_id != operation_id
            ):
                raise RevisionBusy("memory_writer_in_progress")
            row.active_token, row.state = uuid4().hex, "active"
            row.attempt += 1
            row.started_at, row.finished_at = datetime.now(UTC), None
            outbox = await self._outbox(row)
            outbox.receipt = {**outbox.receipt, "active_attempts": [*outbox.receipt.get("active_attempts", []), row.active_token]}
            result = await self._view(row)
            result["memory_config"] = {
                name: deepcopy(getattr(memory, name))
                for name in ("memory_size", "forgetting_policy", "memory_type", "embd_id", "tenant_embd_id", "llm_id", "tenant_llm_id", "temperature", "system_prompt", "user_prompt")
            }
            result["managed"] = await self.db.scalar(select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == memory_id)) is not None
        return result

    async def snapshot(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            return await self._view(await self._row(memory_id, operation_id))
        finally:
            await self.db.rollback()

    async def finish(self, memory_id: str, operation_id: str, *, token: str, outcome: str = "drained") -> None:
        async with self._write(operation_id):
            fence = await _fence(self.db, memory_id, self.tenant_id, lock=True)
            if fence is None:
                raise RevisionNotFound("writer_fence_not_found")
            row = await self._row(memory_id, operation_id, lock=True)
            finished = row.receipt.get("finished_tokens", [])
            if token in finished:
                return
            if row.active_token != token:
                raise RevisionBusy("writer_attempt_not_owned")
            row.active_token, row.finished_at = None, datetime.now(UTC)
            row.state = "canceled" if row.publication_state == "canceled" else "drained"
            row.receipt = {**row.receipt, "finished_tokens": [*finished, token], "outcome": outcome}
            outbox = await self._outbox(row)
            outbox.receipt = {
                **outbox.receipt,
                "active_attempts": [value for value in outbox.receipt.get("active_attempts", []) if value != token],
                "finished_attempts": [*outbox.receipt.get("finished_attempts", []), token],
            }

    async def operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            if operation is None or operation.tenant_id != self.tenant_id:
                raise RevisionNotFound("operation_not_found")
            outbox = await self._outbox(await self._row(memory_id, operation_id))
            return {**operation_view(operation, outbox.id), **deepcopy(operation.result)}
        finally:
            await self.db.rollback()
