"""Async SQL decisions for managed revisions; external storage is explicitly injected."""

from __future__ import annotations

import base64
import binascii
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import (
    MAX_HISTORY_PAGE_SIZE,
    ModelBinding,
    RevisionBusy,
    RevisionCapacityError,
    RevisionConflict,
    RevisionNotFound,
    RevisionPersistenceUnknown,
    RevisionValidationError,
    content_hash,
    message_scope,
    positive_int,
    request_hash,
    revised_content,
    validate_adoption,
    validate_operation_id,
    validate_projection,
)

MessageReader = Callable[[str, int], Awaitable[dict[str, Any] | None]]
Authorize = Callable[[AsyncSession, str], Awaitable[None]]
BindingReader = Callable[[AsyncSession, str, int], Awaitable[ModelBinding]]
MeasureMemory = Callable[[str], Awaitable[int]]


@runtime_checkable
class MessageIdAllocator(Protocol):
    async def __call__(self, *, minimum_message_id: int) -> int: ...


def operation_view(operation: MemoryMutationOperation, outbox_id: str | None = None) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "kind": operation.kind,
        "status": operation.status,
        "entry_id": operation.entry_id,
        "new_revision": operation.new_revision,
        "index_sync_status": operation.index_sync_status,
        "retryable": operation.status in {"accepted", "published", "persistence_unknown", "failed"},
        "error_code": operation.error_code,
        "outbox_id": outbox_id,
    }


def _model_binding(value: dict[str, Any]) -> ModelBinding:
    return ModelBinding(**value)


class RevisionService:
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        actor_id: str,
        message_reader: MessageReader,
        allocate_message_id: MessageIdAllocator,
        count_tokens: Callable[[str], int],
        authorize: Authorize,
        model_binding: BindingReader,
        measure_memory_bytes: MeasureMemory,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.message_reader = message_reader
        self.allocate_message_id = allocate_message_id
        self.count_tokens = count_tokens
        self.authorize = authorize
        self.model_binding = model_binding
        self.measure_memory_bytes = measure_memory_bytes
        self._commit_operation_id = ""

    async def _idle(self) -> None:
        if self.db.new or self.db.dirty or self.db.deleted:
            raise RuntimeError("Revision service requires a session without pending writes")
        if self.db.in_transaction():
            await self.db.rollback()
        self.db.expire_all()

    @asynccontextmanager
    async def _write(self, operation_id: str) -> AsyncIterator[None]:
        await self._idle()
        self._commit_operation_id = operation_id
        await self.db.begin()
        try:
            yield
            await self.db.flush()
        except IntegrityError as exc:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("concurrent_revision_or_operation") from exc
        except BaseException:
            await finish_owned_work(self.db.rollback())
            raise
        try:
            await finish_owned_work(self.db.commit())
        except IntegrityError as exc:
            await self.db.rollback()
            raise RevisionConflict("concurrent_revision_or_operation") from exc
        except DBAPIError as exc:
            await self.db.rollback()
            raise RevisionPersistenceUnknown(self._commit_operation_id) from exc

    async def _memory(self, memory_id: str, *, lock: bool = False) -> Memory:
        statement = select(Memory).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id).execution_options(populate_existing=True)
        memory = await self.db.scalar(statement.with_for_update() if lock else statement)
        if memory is None:
            raise RevisionNotFound("memory_not_found")
        await self.authorize(self.db, memory_id)
        return memory

    async def _bank(self, memory_id: str, *, create: bool = False) -> tuple[Memory, ManagedMemoryState]:
        memory = await self._memory(memory_id, lock=create)
        if create:
            from memory.writes.admission import assert_activation_quiescent

            await assert_activation_quiescent(self.db, memory_id=memory_id, tenant_id=self.tenant_id)
            await self.db.execute(
                insert(ManagedMemoryState)
                .values(memory_id=memory_id, tenant_id=self.tenant_id, generation=1, state="active", reserved_bytes=0, updated_at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["memory_id"])
            )
        bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id).with_for_update())
        if bank is None:
            raise RevisionNotFound("managed_memory_not_found")
        if bank.state != "active":
            raise RevisionConflict("memory_not_active")
        return memory, bank

    async def _operation(self, memory_id: str, operation_id: str, digest: str | None = None) -> MemoryMutationOperation | None:
        operation = await self.db.scalar(
            select(MemoryMutationOperation).where(
                MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.tenant_id == self.tenant_id, MemoryMutationOperation.operation_id == operation_id
            )
        )
        if operation is not None and digest is not None and operation.request_sha256 != digest:
            raise RevisionConflict("operation_request_mismatch")
        return operation

    async def _operation_view(self, operation: MemoryMutationOperation) -> dict[str, Any]:
        outbox_id = await self.db.scalar(
            select(MemoryPublicationOutbox.id).where(MemoryPublicationOutbox.memory_id == operation.memory_id, MemoryPublicationOutbox.operation_id == operation.operation_id)
        )
        return operation_view(operation, outbox_id)

    async def _replay(self, memory_id: str, operation_id: str, digest: str) -> dict[str, Any] | None:
        await self._idle()
        try:
            await self._memory(memory_id)
            operation = await self._operation(memory_id, operation_id, digest)
            return await self._operation_view(operation) if operation is not None else None
        finally:
            await self.db.rollback()

    def _request(self, memory_id: str, kind: str, **values: Any) -> str:
        return request_hash({"tenant_id": self.tenant_id, "actor_id": self.actor_id, "memory_id": memory_id, "kind": kind, **values})

    async def adopt_entry(self, memory_id: str, *, operation_id: str, legacy_message_id: int) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(legacy_message_id, "invalid_message_id")
        digest = self._request(memory_id, "adopt", legacy_message_id=legacy_message_id)
        replay = await self._replay(memory_id, operation_id, digest)
        if replay is not None:
            return replay
        message = await self.message_reader(memory_id, legacy_message_id)
        if message is None:
            raise RevisionNotFound("message_not_found")
        source_id = positive_int(message.get("source_id"), "raw_ancestor_required")
        source = await self.message_reader(memory_id, source_id)
        if source is None:
            raise RevisionConflict("source_not_found")
        scope, dimension = validate_adoption(memory_id, legacy_message_id, message, source)
        result: dict[str, Any]
        async with self._write(operation_id):
            _, bank = await self._bank(memory_id, create=True)
            operation = await self._operation(memory_id, operation_id, digest)
            if operation is not None:
                result = await self._operation_view(operation)
            else:
                state = await self.db.scalar(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source_id).with_for_update())
                if state is None:
                    state = MemorySourceState(
                        memory_id=memory_id,
                        source_message_id=source_id,
                        tenant_id=self.tenant_id,
                        source_revision=1,
                        generation=1,
                        content_sha256=content_hash(source["content"]),
                        state="active",
                        scope=scope,
                    )
                    self.db.add(state)
                elif state.tenant_id != self.tenant_id or state.state != "active" or state.content_sha256 != content_hash(source["content"]) or state.scope != scope:
                    raise RevisionConflict("source_state_changed")
                existing = await self.db.scalar(select(MemoryEntry).where(MemoryEntry.memory_id == memory_id, MemoryEntry.legacy_message_id == legacy_message_id).with_for_update())
                if existing is None:
                    binding = await self.model_binding(self.db, memory_id, dimension)
                    if binding.dimension != dimension:
                        raise RevisionConflict("model_dimension_changed")
                    entry = MemoryEntry(
                        entry_id=uuid4().hex,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        legacy_message_id=legacy_message_id,
                        current_revision=1,
                        next_revision=2,
                        current_message_id=legacy_message_id,
                        generation=1,
                        state="active",
                    )
                    snapshot = {key: deepcopy(value) for key, value in message.items() if key not in {"content_embed", "content"}}
                    snapshot.update(model_binding=binding.to_dict(), embedding_dimension=dimension)
                    revision = MemoryEntryRevision(
                        entry_id=entry.entry_id,
                        revision=1,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        message_id=legacy_message_id,
                        parent_revision=None,
                        content=message["content"],
                        content_sha256=content_hash(message["content"]),
                        origin="legacy_adoption",
                        assessment={"kind": "unknown", "verification_status": "unverified"},
                        message_snapshot=snapshot,
                    )
                    self.db.add(entry)
                    await self.db.flush()
                    self.db.add(revision)
                    await self.db.flush()
                    self.db.add(
                        MemoryRevisionSource(
                            entry_id=entry.entry_id,
                            revision=1,
                            memory_id=memory_id,
                            source_message_id=source_id,
                            source_revision=state.source_revision,
                            source_generation=state.generation,
                            content_sha256=state.content_sha256,
                            relation="context",
                            locator={"kind": "memory_message", "memory_id": memory_id, "message_id": source_id},
                        )
                    )
                else:
                    entry = existing
                    if entry.tenant_id != self.tenant_id:
                        raise RevisionNotFound("entry_not_found")
                operation = MemoryMutationOperation(
                    memory_id=memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    request_sha256=digest,
                    actor_id=self.actor_id,
                    kind="adopt",
                    status="ready",
                    entry_id=entry.entry_id,
                    new_revision=entry.current_revision,
                    index_sync_status="ready",
                    result={"adopted_generation": bank.generation},
                )
                self.db.add(operation)
                result = operation_view(operation)
        return result

    async def _entry(self, memory_id: str, entry_id: str, *, lock: bool = False) -> MemoryEntry:
        statement = select(MemoryEntry).where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, MemoryEntry.entry_id == entry_id)
        entry = await self.db.scalar(statement.with_for_update() if lock else statement)
        if entry is None:
            raise RevisionNotFound("entry_not_found")
        return entry

    async def _revision(self, entry_id: str, revision: int) -> MemoryEntryRevision:
        row = await self.db.scalar(
            select(MemoryEntryRevision).where(MemoryEntryRevision.entry_id == entry_id, MemoryEntryRevision.revision == revision, MemoryEntryRevision.tenant_id == self.tenant_id)
        )
        if row is None:
            raise RevisionNotFound("revision_not_found")
        return row

    async def _sources(self, entry_id: str, revision: int, *, lock: bool = False, require_generation: bool = True, captured_generations: dict[int, int] | None = None) -> list[MemoryRevisionSource]:
        refs = list(
            (
                await self.db.scalars(
                    select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == entry_id, MemoryRevisionSource.revision == revision).order_by(MemoryRevisionSource.source_message_id)
                )
            ).all()
        )
        if not refs:
            raise RevisionConflict("source_manifest_missing")
        target = await self._revision(entry_id, revision)
        from memory.source_provenance import source_receipts

        receipts = await source_receipts(
            self.db, memory_id=target.memory_id, tenant_id=self.tenant_id, source_ids=[ref.source_message_id for ref in refs if ref.locator.get("source_provenance_sha256") is not None]
        )
        expected_scope = message_scope(target.message_snapshot, target.memory_id)
        for ref in refs:
            reference_scope = ref.locator.get("scope", expected_scope)
            receipt_pin = ref.locator.get("source_provenance_sha256")
            if receipt_pin is not None and receipts.get(ref.source_message_id, {}).get("provenance_sha256") != receipt_pin:
                raise RevisionConflict("source_provenance_changed")
            if not isinstance(reference_scope, dict) or set(reference_scope) != {"user_id", "agent_id", "session_id"}:
                raise RevisionConflict("source_scope_invalid")
            statement = select(MemorySourceState).where(
                MemorySourceState.memory_id == ref.memory_id, MemorySourceState.source_message_id == ref.source_message_id, MemorySourceState.tenant_id == self.tenant_id
            )
            state = await self.db.scalar(statement.with_for_update() if lock else statement)
            if (
                state is None
                or state.state != "active"
                or state.source_revision != ref.source_revision
                or (require_generation and state.generation != ref.source_generation)
                or state.content_sha256 != ref.content_sha256
                or state.scope != reference_scope
            ):
                raise RevisionConflict("source_state_changed")
            if captured_generations is not None:
                captured_generations[ref.source_message_id] = state.generation
        return refs

    async def revise_entry(
        self,
        memory_id: str,
        entry_id: str,
        *,
        operation_id: str,
        expected_revision: int,
        content: str | None = None,
        patch: dict[str, Any] | None = None,
        reason: str = "",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(expected_revision)
        if type(dry_run) is not bool or not isinstance(reason, str) or not reason.strip() or len(reason.encode("utf-8")) > 2048:
            raise RevisionValidationError("invalid_revision_reason_or_mode")
        digest = self._request(memory_id, "revise", entry_id=entry_id, expected_revision=expected_revision, content=content, patch=patch, reason=reason)
        if not dry_run:
            replay = await self._replay(memory_id, operation_id, digest)
            if replay is not None:
                return replay
        await self._idle()
        try:
            await self._memory(memory_id)
            entry = await self._entry(memory_id, entry_id)
            if entry.current_revision != expected_revision or entry.state != "active":
                raise RevisionConflict("expected_revision_mismatch")
            current = await self._revision(entry_id, expected_revision)
            if current.content is None:
                raise RevisionConflict("revision_content_unavailable")
            generations: dict[int, int] = {}
            refs = await self._sources(entry_id, expected_revision, require_generation=False, captured_generations=generations)
            final, tokens = revised_content(current.content, content=content, patch=patch, count_tokens=self.count_tokens)
            previous_content = current.content
            message = deepcopy(current.message_snapshot)
            binding = _model_binding(message.pop("model_binding"))
            message.pop("embedding_dimension", None)
            message.update(content=final, status=False)
            source_checks = [
                {"source_id": ref.source_message_id, "source_revision": ref.source_revision, "generation": generations[ref.source_message_id], "content_sha256": ref.content_sha256, "state": "active"}
                for ref in refs
            ]
            current_message_id = entry.current_message_id
            inherited_sources = [{key: deepcopy(getattr(ref, key)) for key in ("source_message_id", "source_revision", "source_generation", "content_sha256", "relation", "locator")} for ref in refs]
            capacity_generation = await self.db.scalar(select(ManagedMemoryState.capacity_generation).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
            if capacity_generation is None:
                raise RevisionConflict("managed_capacity_state_missing")
        finally:
            await self.db.rollback()
        if dry_run:
            return {
                "dry_run": True,
                "entry_id": entry_id,
                "expected_revision": expected_revision,
                "current_revision": expected_revision,
                "content": final,
                "content_sha256": content_hash(final),
                "estimated_tokens": tokens,
                "source_checks": source_checks,
                "diff": {"old_text": previous_content, "new_text": final},
            }
        message_id = positive_int(await self.allocate_message_id(minimum_message_id=current_message_id), "invalid_allocated_message_id")
        if message_id <= current_message_id or await self.message_reader(memory_id, message_id) is not None:
            raise RevisionConflict("projection_message_id_collision")
        observed_bytes = await self.measure_memory_bytes(memory_id)
        if type(observed_bytes) is not int or observed_bytes < 0:
            raise RevisionCapacityError("memory_capacity_unknown")
        # Match the physical inventory's existing Python object-size accounting.
        # Vector values are decoded as Python floats, not eight-byte wire values.
        reserved_bytes = sys.getsizeof(final) + sys.getsizeof(0.0) * binding.dimension + 4096
        message.update(message_id=message_id, id=f"{memory_id}_{message_id}")
        outbox_id = uuid4().hex
        result: dict[str, Any]
        async with self._write(operation_id):
            memory, bank = await self._bank(memory_id)
            operation = await self._operation(memory_id, operation_id, digest)
            if operation is not None:
                result = await self._operation_view(operation)
            else:
                latest_generations: dict[int, int] = {}
                await self._sources(entry_id, expected_revision, lock=True, require_generation=False, captured_generations=latest_generations)
                entry = await self._entry(memory_id, entry_id, lock=True)
                if entry.current_revision != expected_revision or entry.state != "active":
                    raise RevisionConflict("expected_revision_mismatch")
                fresh_binding = await self.model_binding(self.db, memory_id, binding.dimension)
                if fresh_binding != binding:
                    raise RevisionConflict("model_binding_changed")
                if bank.capacity_generation != capacity_generation:
                    raise RevisionConflict("capacity_measurement_changed")
                if observed_bytes + bank.reserved_bytes + reserved_bytes > memory.memory_size:
                    raise RevisionCapacityError("insufficient_revision_capacity")
                if await self.db.scalar(select(MemoryEntryRevision.entry_id).where(MemoryEntryRevision.memory_id == memory_id, MemoryEntryRevision.message_id == message_id)):
                    raise RevisionConflict("projection_message_id_collision")
                revision_number = entry.next_revision
                entry.next_revision += 1
                bank.reserved_bytes += reserved_bytes
                snapshot = {key: deepcopy(value) for key, value in message.items() if key != "content"}
                snapshot.update(model_binding=binding.to_dict(), embedding_dimension=binding.dimension)
                self.db.add(
                    MemoryEntryRevision(
                        entry_id=entry_id,
                        revision=revision_number,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        message_id=message_id,
                        parent_revision=expected_revision,
                        content=final,
                        content_sha256=content_hash(final),
                        origin="manual",
                        assessment={"kind": "user_asserted", "origin": "manual", "verification_status": "unverified", "actor_id": self.actor_id, "operation_id": operation_id, "reason": reason},
                        message_snapshot=snapshot,
                    )
                )
                await self.db.flush()
                for ref in inherited_sources:
                    self.db.add(MemoryRevisionSource(entry_id=entry_id, revision=revision_number, memory_id=memory_id, **{**ref, "source_generation": latest_generations[ref["source_message_id"]]}))
                operation = MemoryMutationOperation(
                    memory_id=memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    request_sha256=digest,
                    actor_id=self.actor_id,
                    kind="revise",
                    status="accepted",
                    entry_id=entry_id,
                    new_revision=revision_number,
                    index_sync_status="pending",
                    result={},
                )
                self.db.add(operation)
                self.db.add(
                    MemoryPublicationOutbox(
                        id=outbox_id,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        operation_id=operation_id,
                        entry_id=entry_id,
                        revision=revision_number,
                        message_id=message_id,
                        expected_revision=expected_revision,
                        bank_generation=bank.generation,
                        entry_generation=entry.generation,
                        state="pending",
                        attempt=0,
                        payload={"action": "replace", "old_message_id": current_message_id, "message": message, "model_binding": binding.to_dict(), "reserved_bytes": reserved_bytes},
                        receipt={},
                    )
                )
                result = operation_view(operation, outbox_id)
        return result

    async def get_operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        await self._idle()
        try:
            await self._memory(memory_id)
            operation = await self._operation(memory_id, operation_id)
            if operation is None:
                raise RevisionNotFound("operation_not_found")
            return await self._operation_view(operation)
        finally:
            await self.db.rollback()

    async def operation_snapshot(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        return await self.get_operation(memory_id, operation_id)

    async def _revision_view(self, revision: MemoryEntryRevision) -> dict[str, Any]:
        refs = list(
            (
                await self.db.scalars(
                    select(MemoryRevisionSource)
                    .where(MemoryRevisionSource.entry_id == revision.entry_id, MemoryRevisionSource.revision == revision.revision)
                    .order_by(MemoryRevisionSource.source_message_id)
                )
            ).all()
        )
        return {
            "revision": revision.revision,
            "parent_revision": revision.parent_revision,
            "message_id": revision.message_id,
            "content": revision.content,
            "content_sha256": revision.content_sha256,
            "origin": revision.origin,
            "assessment": deepcopy(revision.assessment),
            "redacted": revision.content is None,
            "sources": [
                {
                    "source_id": ref.source_message_id,
                    "source_revision": ref.source_revision,
                    "generation": ref.source_generation,
                    "content_sha256": ref.content_sha256,
                    "relation": ref.relation,
                    "locator": deepcopy(ref.locator),
                }
                for ref in refs
            ],
        }

    async def _entry_view(self, entry: MemoryEntry) -> dict[str, Any]:
        revision = await self._revision(entry.entry_id, entry.current_revision)
        return {
            "entry_id": entry.entry_id,
            "memory_id": entry.memory_id,
            "legacy_message_id": entry.legacy_message_id,
            "current_revision": entry.current_revision,
            "current_message_id": entry.current_message_id,
            "generation": entry.generation,
            "state": entry.state,
            "current": await self._revision_view(revision),
        }

    async def get_entry(self, memory_id: str, entry_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self._memory(memory_id)
            return await self._entry_view(await self._entry(memory_id, entry_id))
        finally:
            await self.db.rollback()

    async def find_entry_by_message(self, memory_id: str, message_id: int) -> dict[str, Any] | None:
        positive_int(message_id, "invalid_message_id")
        await self._idle()
        try:
            await self._memory(memory_id)
            entry = await self.db.scalar(
                select(MemoryEntry)
                .outerjoin(MemoryEntryRevision, MemoryEntryRevision.entry_id == MemoryEntry.entry_id)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, or_(MemoryEntry.legacy_message_id == message_id, MemoryEntryRevision.message_id == message_id))
                .limit(1)
            )
            return await self._entry_view(entry) if entry is not None else None
        finally:
            await self.db.rollback()

    async def list_revisions(self, memory_id: str, entry_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE_SIZE:
            raise RevisionValidationError("invalid_history_page_size")
        before: int | None = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 256:
                    raise ValueError
                decoded = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True).decode()
                scope, position = decoded.rsplit(":", 1)
                if scope != f"{memory_id}:{entry_id}" or not position.isascii() or not position.isdecimal():
                    raise ValueError
                before = positive_int(int(position), "invalid_history_cursor")
            except (ValueError, UnicodeError, binascii.Error) as exc:
                raise RevisionValidationError("invalid_history_cursor") from exc
        await self._idle()
        try:
            await self._memory(memory_id)
            await self._entry(memory_id, entry_id)
            statement = select(MemoryEntryRevision).where(MemoryEntryRevision.entry_id == entry_id, MemoryEntryRevision.tenant_id == self.tenant_id)
            if before is not None:
                statement = statement.where(MemoryEntryRevision.revision < before)
            rows = list((await self.db.scalars(statement.order_by(MemoryEntryRevision.revision.desc()).limit(limit + 1))).all())
            selected = rows[:limit]
            next_cursor = base64.urlsafe_b64encode(f"{memory_id}:{entry_id}:{selected[-1].revision}".encode()).decode().rstrip("=") if len(rows) > limit else None
            return {"revisions": [await self._revision_view(row) for row in selected], "next_cursor": next_cursor}
        finally:
            await self.db.rollback()

    async def _outbox(self, outbox_id: str, *, lock: bool = False) -> MemoryPublicationOutbox:
        statement = select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == outbox_id, MemoryPublicationOutbox.tenant_id == self.tenant_id)
        row = await self.db.scalar(statement.with_for_update() if lock else statement)
        if row is None:
            raise RevisionNotFound("publication_not_found")
        self._commit_operation_id = row.operation_id
        return row

    async def _publication_fence(self, row: MemoryPublicationOutbox, *, published: bool = False) -> tuple[ManagedMemoryState, MemoryEntry]:
        _, bank = await self._bank(row.memory_id)
        await self.db.refresh(row, with_for_update=True)
        if row.payload.get("action") != "replace" or row.entry_id is None or row.revision is None or row.expected_revision is None or row.entry_generation is None or row.message_id is None:
            raise RevisionConflict("invalid_revision_publication")
        if row.state in {"stale", "canceled", "redacted"} or bank.generation != row.bank_generation:
            raise RevisionConflict("publication_fence_changed")
        await self._sources(row.entry_id, row.revision, lock=True, require_generation=not published)
        entry = await self._entry(row.memory_id, row.entry_id, lock=True)
        expected = row.revision if published else row.expected_revision
        generation = row.entry_generation + 1 if published else row.entry_generation
        if entry.current_revision != expected or (not published and entry.generation != generation) or entry.state != "active" or (published and entry.current_message_id != row.message_id):
            raise RevisionConflict("expected_revision_mismatch")
        binding = _model_binding(row.payload["model_binding"])
        if await self.model_binding(self.db, row.memory_id, binding.dimension) != binding:
            raise RevisionConflict("model_binding_changed")
        return bank, entry

    @staticmethod
    def _lease(row: MemoryPublicationOutbox, token: str | None) -> None:
        if token is None or row.lease_token != token or row.lease_until is None or row.lease_until <= datetime.now(UTC):
            raise RevisionBusy("publication_lease_changed")

    async def _publication_view(self, row: MemoryPublicationOutbox) -> dict[str, Any]:
        return {
            "outbox_id": row.id,
            "memory_id": row.memory_id,
            "tenant_id": row.tenant_id,
            "operation_id": row.operation_id,
            "entry_id": row.entry_id,
            "revision": row.revision,
            "message_id": row.message_id,
            "expected_revision": row.expected_revision,
            "state": row.state,
            "lease_token": row.lease_token,
            "attempt": row.attempt,
            "payload": deepcopy(row.payload),
            "receipt": deepcopy(row.receipt),
        }

    async def publication_snapshot(self, outbox_id: str, *, lease_token: str | None = None) -> dict[str, Any]:
        await self._idle()
        try:
            row = await self._outbox(outbox_id)
            await self._publication_fence(row, published=row.state in {"sync_pending", "ready"} or row.payload.get("published") is True)
            if lease_token is not None:
                self._lease(row, lease_token)
            return await self._publication_view(row)
        finally:
            await self.db.rollback()

    async def claim_publication(self, memory_id: str, operation_id: str, *, lease_seconds: int = 120) -> dict[str, Any]:
        if type(lease_seconds) is not int or not 10 <= lease_seconds <= 600:
            raise RevisionValidationError("invalid_publication_lease")
        async with self._write(operation_id):
            await self._bank(memory_id)
            operation = await self._operation(memory_id, operation_id)
            if operation is None:
                raise RevisionNotFound("operation_not_found")
            row = await self.db.scalar(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id).with_for_update())
            if row is None:
                raise RevisionNotFound("publication_not_found")
            if row.state == "ready":
                return await self._publication_view(row)
            await self._publication_fence(row, published=row.state == "sync_pending" or row.payload.get("published") is True)
            now = datetime.now(UTC)
            if row.lease_until is not None and row.lease_until > now:
                raise RevisionBusy("publication_in_progress")
            if row.receipt.get("active_attempts"):
                raise RevisionBusy("publication_needs_operator_reconciliation")
            if row.attempt >= 5:
                raise RevisionBusy("publication_attempts_exhausted")
            row.lease_token = uuid4().hex
            row.lease_until = now + timedelta(seconds=lease_seconds)
            row.attempt += 1
            row.receipt = {**row.receipt, "active_attempts": [*row.receipt.get("active_attempts", []), row.lease_token]}
            if row.payload.get("published") is True:
                row.state = "sync_pending"
            result = await self._publication_view(row)
        return result

    async def record_stage(self, outbox_id: str, staged_message: dict[str, Any], *, lease_token: str | None = None) -> dict[str, Any]:
        async with self._write(outbox_id):
            row = await self._outbox(outbox_id)
            await self._publication_fence(row)
            self._lease(row, lease_token)
            if row.state not in {"pending", "unknown", "failed", "staged"}:
                raise RevisionConflict("invalid_publication_stage")
            row.receipt = {**row.receipt, **validate_projection(staged_message, row.payload["message"], _model_binding(row.payload["model_binding"]), active=False)}
            row.state = "staged"
            operation = await self._operation(row.memory_id, row.operation_id)
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "accepted", "staged", None
            result = operation_view(operation, row.id)
        return result

    async def publish_revision(self, outbox_id: str, *, lease_token: str | None = None) -> dict[str, Any]:
        async with self._write(outbox_id):
            row = await self._outbox(outbox_id)
            _, entry = await self._publication_fence(row)
            self._lease(row, lease_token)
            if row.state != "staged" or row.receipt.get("content_sha256") != content_hash(row.payload["message"]["content"]):
                raise RevisionConflict("projection_not_verified")
            assert row.revision is not None and row.message_id is not None
            entry.current_revision, entry.current_message_id = row.revision, row.message_id
            entry.generation += 1
            entry.updated_at = datetime.now(UTC)
            row.state = "sync_pending"
            operation = await self._operation(row.memory_id, row.operation_id)
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "published", "sync_pending", None
            result = operation_view(operation, row.id)
        return result

    async def mark_ready(self, outbox_id: str, active_message: dict[str, Any], *, old_message_inactive: bool, lease_token: str | None = None) -> dict[str, Any]:
        async with self._write(outbox_id):
            row = await self._outbox(outbox_id)
            bank, _ = await self._publication_fence(row, published=True)
            self._lease(row, lease_token)
            if row.state != "sync_pending" or old_message_inactive is not True:
                raise RevisionConflict("publication_sync_incomplete")
            row.receipt = {**row.receipt, **validate_projection(active_message, row.payload["message"], _model_binding(row.payload["model_binding"]), active=True)}
            row.state, row.lease_token, row.lease_until = "ready", None, None
            reserved_bytes = row.payload["reserved_bytes"]
            if type(reserved_bytes) is not int or reserved_bytes < 0 or bank.reserved_bytes < reserved_bytes:
                raise RevisionConflict("capacity_reservation_mismatch")
            bank.reserved_bytes -= reserved_bytes
            bank.capacity_generation += 1
            row.receipt = {**row.receipt, "reservation_released": True}
            operation = await self._operation(row.memory_id, row.operation_id)
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
            result = operation_view(operation, row.id)
        return result

    async def mark_unknown(self, outbox_id: str, error_code: str, *, lease_token: str | None = None) -> dict[str, Any]:
        return await self._mark_error(outbox_id, error_code, "unknown", lease_token)

    async def finish_publication_attempt(self, outbox_id: str, *, lease_token: str) -> None:
        """Record drained external work even after withdrawal; never release another attempt."""
        async with self._write(outbox_id):
            row = await self._outbox(outbox_id)
            bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == row.memory_id, ManagedMemoryState.tenant_id == self.tenant_id).with_for_update())
            if bank is None:
                raise RevisionNotFound("managed_memory_not_found")
            await self.db.refresh(row, with_for_update=True)
            active = row.receipt.get("active_attempts", [])
            finished = row.receipt.get("finished_attempts", [])
            if lease_token in finished:
                return
            if lease_token not in active:
                raise RevisionBusy("publication_attempt_not_owned")
            row.receipt = {**row.receipt, "active_attempts": [token for token in active if token != lease_token], "finished_attempts": [*finished, lease_token], "attempt_finished": True}
            if row.lease_token == lease_token:
                row.lease_token, row.lease_until = None, None

    async def fail_operation(self, outbox_id: str, error_code: str, *, stale: bool = False, lease_token: str | None = None) -> dict[str, Any]:
        return await self._mark_error(outbox_id, error_code, "stale" if stale else "failed", lease_token)

    async def _mark_error(self, outbox_id: str, error_code: str, state: str, lease_token: str | None) -> dict[str, Any]:
        if not isinstance(error_code, str) or not error_code or len(error_code) > 64 or any(not (character.isascii() and (character.isalnum() or character == "_")) for character in error_code):
            raise RevisionValidationError("invalid_error_code")
        async with self._write(outbox_id):
            row = await self._outbox(outbox_id)
            await self._bank(row.memory_id)
            await self.db.refresh(row, with_for_update=True)
            self._lease(row, lease_token)
            if row.state in {"ready", "stale", "canceled", "redacted"}:
                raise RevisionConflict("publication_already_terminal")
            published = row.state == "sync_pending" or row.payload.get("published", False)
            row.payload = {**row.payload, "published": published}
            row.state, row.error_code, row.lease_token, row.lease_until = "sync_pending" if published else state, error_code, None, None
            operation = await self._operation(row.memory_id, row.operation_id)
            assert operation is not None
            operation.status = "persistence_unknown" if state == "unknown" else "published" if published else state
            operation.index_sync_status, operation.error_code = "unknown" if state == "unknown" else "sync_pending" if published else "failed", error_code
            result = operation_view(operation, row.id)
        return result
