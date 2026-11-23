"""Retire an unchanged RAW source in a strict, otherwise unmanaged bank."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemoryPublicationOutbox, MemorySourceState
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, positive_int, request_hash, validate_operation_id
from memory.revisions.service import operation_view
from memory.revisions.sources import _fifo_source_snapshot, _raw_snapshot
from memory.writes.admission import WriterCoordinator, invalidate_source_writers, release_writer_reservation
from memory.writes.types import Authorize


class RetentionStorage(Protocol):
    tenant_id: str
    memory_id: str

    async def inventory(self) -> list[dict[str, Any]]: ...

    async def delete(self, message_ids: list[int]) -> None: ...


class FifoRetentionService:
    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str, authorize: Authorize) -> None:
        self.db, self.tenant_id, self.actor_id = db, tenant_id, actor_id
        self.authorize = authorize
        self.writer = WriterCoordinator(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)

    async def _operation(self, memory_id: str, operation_id: str) -> MemoryMutationOperation | None:
        return await self.db.scalar(
            select(MemoryMutationOperation)
            .where(MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.tenant_id == self.tenant_id, MemoryMutationOperation.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )

    async def _outbox(self, memory_id: str, operation_id: str, *, lock: bool = True) -> MemoryPublicationOutbox:
        statement = (
            select(MemoryPublicationOutbox)
            .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.tenant_id == self.tenant_id, MemoryPublicationOutbox.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(statement.with_for_update() if lock else statement)
        if row is None or row.payload.get("action") != "fifo_evict":
            raise RevisionNotFound("fifo_operation_not_found")
        return row

    async def _view(self, operation: MemoryMutationOperation) -> dict[str, Any]:
        outbox_id = await self.db.scalar(
            select(MemoryPublicationOutbox.id).where(MemoryPublicationOutbox.memory_id == operation.memory_id, MemoryPublicationOutbox.operation_id == operation.operation_id)
        )
        return {**operation_view(operation, outbox_id), **deepcopy(operation.result)}

    async def _read_operation(self, memory_id: str, operation_id: str) -> dict[str, Any] | None:
        await self.writer._idle()
        try:
            memory = await self.db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id))
            if memory is None:
                raise RevisionNotFound("memory_not_found")
            await self.authorize(self.db, memory_id)
            operation = await self._operation(memory_id, operation_id)
            if operation is None:
                return None
            return {**await self._view(operation), "_request_sha256": operation.request_sha256}
        finally:
            await self.db.rollback()

    async def get_operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        result = await self._read_operation(memory_id, operation_id)
        if result is None or result["kind"] != "fifo_evict":
            raise RevisionNotFound("fifo_operation_not_found")
        return {key: value for key, value in result.items() if key != "_request_sha256"}

    @staticmethod
    def _target(storage: RetentionStorage, tenant_id: str, memory_id: str) -> None:
        if storage.tenant_id != tenant_id or storage.memory_id != memory_id:
            raise RevisionConflict("fifo_storage_binding_mismatch")

    def _source_matches(self, source: MemorySourceState | None, payload: dict[str, Any]) -> bool:
        return (
            source is not None
            and source.tenant_id == self.tenant_id
            and source.state in {"forgotten", "deleted"}
            and source.generation == payload["source_generation"]
            and source.content_sha256 == payload["source_hash"]
            and source.scope == payload["source_scope"]
        )

    @staticmethod
    def _observed(memory_id: str, source_id: int, messages: list[dict[str, Any]], *, expected_hash: str, expected_scope: dict[str, Any]) -> list[int]:
        ids: set[int] = set()
        seen: set[int] = set()
        for message in messages:
            identity = positive_int(message.get("message_id"), "invalid_physical_message_id")
            if message.get("memory_id") != memory_id or identity in seen:
                raise RevisionConflict("fifo_inventory_binding_mismatch")
            seen.add(identity)
            if identity == source_id:
                raw = _raw_snapshot(memory_id, source_id, message)
                if (raw["content_sha256"], raw["scope"]) != (expected_hash, expected_scope):
                    raise RevisionConflict("fifo_source_changed")
                ids.add(identity)
            elif message.get("source_id") in (source_id, str(source_id)):
                ids.add(identity)
        return sorted(ids)

    async def evict_source(self, memory_id: str, source_raw: dict[str, Any], operation_id: str, storage: RetentionStorage) -> dict[str, Any]:
        validate_operation_id(operation_id)
        source_id = positive_int(source_raw.get("message_id"), "invalid_source_id")
        expected = _fifo_source_snapshot(memory_id, source_id, source_raw)
        digest = request_hash({"kind": "fifo_evict", "actor": self.actor_id, "memory": memory_id, "source": source_id, "hash": expected["content_sha256"], "scope": expected["scope"]})
        existing = await self._read_operation(memory_id, operation_id)
        if existing is not None:
            if existing["kind"] != "fifo_evict" or existing["_request_sha256"] != digest:
                raise RevisionConflict("operation_request_mismatch")
            return await self.reconcile_operation(memory_id, operation_id, storage)
        self._target(storage, self.tenant_id, memory_id)
        inventory = await finish_owned_work(storage.inventory())
        observed = self._observed(memory_id, source_id, inventory, expected_hash=expected["content_sha256"], expected_scope=expected["scope"])
        if source_id not in observed:
            raise RevisionConflict("fifo_source_missing")
        expected = _raw_snapshot(memory_id, source_id, next(message for message in inventory if message["message_id"] == source_id))
        async with self.writer._write(operation_id):
            _, fence = await self.writer._memory_fence(memory_id)
            if await self.db.scalar(select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == memory_id)) is not None:
                raise RevisionConflict("managed_fifo_requires_source_service")
            existing_row = await self._operation(memory_id, operation_id)
            if existing_row is not None:
                if existing_row.request_sha256 != digest or existing_row.kind != "fifo_evict":
                    raise RevisionConflict("operation_request_mismatch")
            else:
                source = await self.db.scalar(
                    select(MemorySourceState)
                    .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                if source is None:
                    source = MemorySourceState(
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        source_message_id=source_id,
                        source_revision=1,
                        generation=1,
                        state=expected["state"],
                        content_sha256=expected["content_sha256"],
                        scope=expected["scope"],
                    )
                    self.db.add(source)
                    await self.db.flush()
                if source.tenant_id != self.tenant_id or (source.content_sha256, source.scope) != (expected["content_sha256"], expected["scope"]):
                    raise RevisionConflict("fifo_source_changed")
                writers = await invalidate_source_writers(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_id=source_id)
                if writers["active_operation_ids"]:
                    raise RevisionBusy("fifo_source_has_active_writer")
                dependencies = list(
                    await self.db.scalars(
                        select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.id.in_(writers["outbox_ids"])).with_for_update()
                    )
                )
                if any(row.receipt.get("active_attempts") for row in dependencies):
                    raise RevisionBusy("fifo_source_has_active_writer")
                source.state, source.generation = "forgotten", source.generation + 1
                source.updated_at = datetime.now(UTC)
                operation = MemoryMutationOperation(
                    memory_id=memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    actor_id=self.actor_id,
                    request_sha256=digest,
                    kind="fifo_evict",
                    status="published",
                    index_sync_status="pending",
                    result={
                        "stop_supply": "withdrawn",
                        "evicted_source": {"source_id": source_id, "content_sha256": source.content_sha256},
                        "tracked_cleanup_confirmed": False,
                        "physical_cleanup_complete": False,
                        "untraceable_copies": True,
                    },
                )
                self.db.add(operation)
                self.db.add(
                    MemoryPublicationOutbox(
                        id=uuid4().hex,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        operation_id=operation_id,
                        bank_generation=fence.generation,
                        state="pending",
                        attempt=0,
                        payload={
                            "action": "fifo_evict",
                            "source_id": source_id,
                            "source_generation": source.generation,
                            "source_hash": source.content_sha256,
                            "source_scope": deepcopy(source.scope),
                            "message_ids": sorted(set(observed) | set(writers["message_ids"])),
                            "dependent_outboxes": writers["outbox_ids"],
                        },
                        receipt={},
                    )
                )
        return await self.reconcile_operation(memory_id, operation_id, storage)

    async def reconcile_operation(self, memory_id: str, operation_id: str, storage: RetentionStorage) -> dict[str, Any]:
        validate_operation_id(operation_id)
        operation = await self.get_operation(memory_id, operation_id)
        if operation["status"] == "ready":
            return operation
        self._target(storage, self.tenant_id, memory_id)
        inventory = await finish_owned_work(storage.inventory())
        token = uuid4().hex
        try:
            async with self.writer._write(operation_id):
                _, fence = await self.writer._memory_fence(memory_id)
                operation_row = await self._operation(memory_id, operation_id)
                if operation_row is None or operation_row.kind != "fifo_evict":
                    raise RevisionNotFound("fifo_operation_not_found")
                row = await self._outbox(memory_id, operation_id, lock=False)
                source_id = row.payload["source_id"]
                source = await self.db.scalar(
                    select(MemorySourceState)
                    .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source_id)
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                writers = await invalidate_source_writers(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_id=source_id)
                if writers["active_operation_ids"]:
                    raise RevisionBusy("fifo_source_has_active_writer")
                row = await self._outbox(memory_id, operation_id)
                if row.receipt.get("active_attempts"):
                    raise RevisionBusy("fifo_needs_operator_reconciliation")
                if not self._source_matches(source, row.payload) or fence.generation != row.bank_generation:
                    raise RevisionConflict("fifo_source_fence_changed")
                row.payload = {
                    **row.payload,
                    "message_ids": sorted(set(row.payload["message_ids"]) | set(writers["message_ids"])),
                    "dependent_outboxes": list(dict.fromkeys([*row.payload["dependent_outboxes"], *writers["outbox_ids"]])),
                }
                dependencies = list(await self.db.scalars(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id.in_(row.payload["dependent_outboxes"])).with_for_update()))
                if any(dependency.receipt.get("active_attempts") for dependency in dependencies):
                    raise RevisionBusy("fifo_source_has_active_writer")
                observed = self._observed(memory_id, source_id, inventory, expected_hash=row.payload["source_hash"], expected_scope=row.payload["source_scope"])
                targets = sorted(set(row.payload["message_ids"]) | set(observed))
                present = {positive_int(message.get("message_id"), "invalid_physical_message_id") for message in inventory}
                delete_ids = [identity for identity in targets if identity in present]
                row.payload = {**row.payload, "message_ids": targets}
                row.receipt = {**row.receipt, "active_attempts": [token]}
                row.lease_token, row.attempt = token, row.attempt + 1
                payload = deepcopy(row.payload)
            try:
                for offset in range(0, len(delete_ids), 100):
                    await finish_owned_work(storage.delete(delete_ids[offset : offset + 100]))
                remaining = await finish_owned_work(storage.inventory())
                if self._observed(memory_id, source_id, remaining, expected_hash=payload["source_hash"], expected_scope=payload["source_scope"]):
                    raise RevisionConflict("fifo_projection_cleanup_unconfirmed")
            except Exception:
                async with self.writer._write(operation_id):
                    operation_row = await self._operation(memory_id, operation_id)
                    if operation_row is None:
                        raise RevisionNotFound("fifo_operation_not_found")
                    operation_row.status, operation_row.index_sync_status, operation_row.error_code = "persistence_unknown", "unknown", "fifo_projection_cleanup_unconfirmed"
                    result = await self._view(operation_row)
                return result
            async with self.writer._write(operation_id):
                _, fence = await self.writer._memory_fence(memory_id)
                source = await self.db.scalar(
                    select(MemorySourceState)
                    .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == payload["source_id"])
                    .execution_options(populate_existing=True)
                    .with_for_update()
                )
                writers = await invalidate_source_writers(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_id=payload["source_id"])
                if writers["active_operation_ids"]:
                    raise RevisionBusy("fifo_source_has_active_writer")
                newly_observed = set(writers["message_ids"]) - set(payload["message_ids"])
                if newly_observed & {message["message_id"] for message in remaining}:
                    raise RevisionConflict("fifo_projection_cleanup_unconfirmed")
                row = await self._outbox(memory_id, operation_id)
                if not self._source_matches(source, payload) or row.lease_token != token or row.bank_generation != fence.generation:
                    raise RevisionConflict("fifo_source_fence_changed")
                bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id).execution_options(populate_existing=True))
                row.payload = {
                    **row.payload,
                    "message_ids": sorted(set(row.payload["message_ids"]) | set(writers["message_ids"])),
                    "dependent_outboxes": list(dict.fromkeys([*row.payload["dependent_outboxes"], *writers["outbox_ids"]])),
                }
                for dependency in await self.db.scalars(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id.in_(row.payload["dependent_outboxes"])).with_for_update()):
                    if dependency.receipt.get("active_attempts"):
                        raise RevisionBusy("fifo_source_has_active_writer")
                    await release_writer_reservation(self.db, memory_id=memory_id, tenant_id=self.tenant_id, outbox=dependency, fence=fence, bank=bank)
                fence.capacity_generation += 1
                if bank is not None:
                    bank.capacity_generation += 1
                row.state, row.lease_token = "ready", None
                row.receipt = {**row.receipt, "reservation_released": True, "cleanup_confirmed": True}
                operation_row = await self._operation(memory_id, operation_id)
                if operation_row is None:
                    raise RevisionNotFound("fifo_operation_not_found")
                operation_row.status, operation_row.index_sync_status, operation_row.error_code = "ready", "ready", None
                operation_row.result = {**operation_row.result, "tracked_cleanup_confirmed": True}
                result = await self._view(operation_row)
            return result
        finally:
            await finish_owned_work(self._finish(memory_id, operation_id, token))

    async def _finish(self, memory_id: str, operation_id: str, token: str) -> None:
        async with self.writer._write(operation_id):
            row = await self._outbox(memory_id, operation_id)
            if token not in row.receipt.get("active_attempts", []):
                return
            row.receipt = {
                **row.receipt,
                "active_attempts": [value for value in row.receipt["active_attempts"] if value != token],
                "finished_attempts": list(dict.fromkeys([*row.receipt.get("finished_attempts", []), token])),
            }
            if row.lease_token == token:
                row.lease_token = None
