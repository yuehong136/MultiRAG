"""Durable source withdrawal and coordinated legacy management for managed banks."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import (
    ModelBinding,
    RevisionBusy,
    RevisionConflict,
    RevisionNotFound,
    RevisionPersistenceUnknown,
    RevisionValidationError,
    content_hash,
    is_active,
    message_scope,
    positive_int,
    request_hash,
    validate_operation_id,
    vector_dimension,
)
from memory.revisions.service import operation_view
from memory.revisions.storage import ManagedMessageStore
from memory.writes.admission import assert_activation_quiescent, invalidate_source_writers, release_writer_reservation
from memory.writes.service import verify_approved_message

Authorize = Callable[[AsyncSession, str], Awaitable[None]]
MessageReader = Callable[[str, int], Awaitable[dict[str, Any] | None]]
_PERMANENT = {"forgotten", "deleted"}


def source_view(source: MemorySourceState) -> dict[str, Any]:
    return {
        "managed": True,
        "memory_id": source.memory_id,
        "source_id": source.source_message_id,
        "source_revision": source.source_revision,
        "generation": source.generation,
        "state": source.state,
        "content_sha256": source.content_sha256,
        "scope": deepcopy(source.scope),
    }


def _raw_snapshot(memory_id: str, source_id: int, message: dict[str, Any] | None) -> dict[str, Any]:
    if message is None:
        raise RevisionNotFound("source_not_found")
    if (
        message.get("message_id") != source_id
        or message.get("message_type") != "raw"
        or "source_id" not in message
        or isinstance(message["source_id"], bool)
        or message["source_id"] not in (None, 0, "0", "")
    ):
        raise RevisionConflict("raw_source_required")
    scope = message_scope(message, memory_id)
    content = message.get("content")
    if not isinstance(content, str):
        raise RevisionConflict("invalid_source_content")
    state = "forgotten" if message.get("forget_at") not in (None, "", "-") else "active" if message.get("status") in (True, 1, "1") else "inactive"
    return {"managed": False, "memory_id": memory_id, "source_id": source_id, "source_revision": None, "generation": None, "state": state, "content_sha256": content_hash(content), "scope": scope}


def _fifo_source_snapshot(memory_id: str, source_id: int, message: dict[str, Any]) -> dict[str, Any]:
    """Bind retries to a fixed source without retaining its forgotten body."""
    if "content" in message:
        return _raw_snapshot(memory_id, source_id, message)
    if (
        message.get("message_id") != source_id
        or message.get("message_type") != "raw"
        or "source_id" not in message
        or isinstance(message["source_id"], bool)
        or message["source_id"] not in (None, 0, "0", "")
    ):
        raise RevisionConflict("raw_source_required")
    digest = message.get("content_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RevisionValidationError("invalid_source_hash")
    return {"memory_id": memory_id, "source_id": source_id, "content_sha256": digest, "scope": message_scope(message, memory_id)}


class SourceService:
    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str, authorize: Authorize, message_reader: MessageReader) -> None:
        self.db, self.tenant_id, self.actor_id = db, tenant_id, actor_id
        self.authorize, self.message_reader = authorize, message_reader

    async def _idle(self) -> None:
        if self.db.new or self.db.dirty or self.db.deleted:
            raise RuntimeError("Source service requires a session without pending writes")
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
        except IntegrityError as exc:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("concurrent_source_operation") from exc
        except BaseException:
            await finish_owned_work(self.db.rollback())
            raise
        try:
            await finish_owned_work(self.db.commit())
        except IntegrityError as exc:
            await self.db.rollback()
            raise RevisionConflict("concurrent_source_operation") from exc
        except DBAPIError as exc:
            await self.db.rollback()
            raise RevisionPersistenceUnknown(operation_id) from exc

    async def _memory(self, memory_id: str, *, lock: bool = False) -> Memory:
        query = select(Memory).where(Memory.id == memory_id, Memory.tenant_id == self.tenant_id).execution_options(populate_existing=True)
        row = await self.db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise RevisionNotFound("memory_not_found")
        await self.authorize(self.db, memory_id)
        return row

    async def _bank(self, memory_id: str, *, create: bool = False, deleting: bool = False) -> ManagedMemoryState:
        await self._memory(memory_id, lock=create)
        if create:
            await assert_activation_quiescent(self.db, memory_id=memory_id, tenant_id=self.tenant_id)
            await self.db.execute(
                insert(ManagedMemoryState)
                .values(memory_id=memory_id, tenant_id=self.tenant_id, generation=1, state="active", reserved_bytes=0, updated_at=datetime.now(UTC))
                .on_conflict_do_nothing(index_elements=["memory_id"])
            )
        row = await self.db.scalar(
            select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id).execution_options(populate_existing=True).with_for_update()
        )
        if row is None:
            raise RevisionNotFound("managed_memory_not_found")
        if row.state != "active" and not (deleting and row.state == "deleting"):
            raise RevisionConflict("memory_not_active")
        return row

    async def _source(self, memory_id: str, source_id: int, *, lock: bool = False) -> MemorySourceState | None:
        query = (
            select(MemorySourceState)
            .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.tenant_id, MemorySourceState.source_message_id == source_id)
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(query.with_for_update() if lock else query)

    async def _operation(self, memory_id: str, operation_id: str, digest: str | None = None) -> MemoryMutationOperation | None:
        row = await self.db.scalar(
            select(MemoryMutationOperation)
            .where(MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.tenant_id == self.tenant_id, MemoryMutationOperation.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
        if row is not None and digest is not None and row.request_sha256 != digest:
            raise RevisionConflict("operation_request_mismatch")
        return row

    async def _view(self, operation: MemoryMutationOperation) -> dict[str, Any]:
        outbox_id = await self.db.scalar(
            select(MemoryPublicationOutbox.id).where(MemoryPublicationOutbox.memory_id == operation.memory_id, MemoryPublicationOutbox.operation_id == operation.operation_id)
        )
        return {**operation_view(operation, outbox_id), **deepcopy(operation.result)}

    async def _replay(self, memory_id: str, operation_id: str, digest: str) -> dict[str, Any] | None:
        await self._idle()
        try:
            await self._memory(memory_id)
            row = await self._operation(memory_id, operation_id, digest)
            return await self._view(row) if row is not None else None
        finally:
            await self.db.rollback()

    async def get_source_state(self, memory_id: str, source_id: int) -> dict[str, Any]:
        positive_int(source_id, "invalid_source_id")
        await self._idle()
        try:
            await self._memory(memory_id)
            source = await self._source(memory_id, source_id)
            if source is not None:
                return source_view(source)
        finally:
            await self.db.rollback()
        return _raw_snapshot(memory_id, source_id, await self.message_reader(memory_id, source_id))

    async def adopt_source(self, memory_id: str, source_id: int, *, operation_id: str, expected_content_sha256: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(source_id, "invalid_source_id")
        if not isinstance(expected_content_sha256, str) or len(expected_content_sha256) != 64 or any(char not in "0123456789abcdef" for char in expected_content_sha256):
            raise RevisionValidationError("invalid_source_hash")
        digest = request_hash({"kind": "adopt_source", "actor": self.actor_id, "memory": memory_id, "source": source_id, "hash": expected_content_sha256})
        replay = await self._replay(memory_id, operation_id, digest)
        if replay is not None:
            return replay
        observed = _raw_snapshot(memory_id, source_id, await self.message_reader(memory_id, source_id))
        if observed["content_sha256"] != expected_content_sha256:
            raise RevisionConflict("source_content_changed")
        async with self._write(operation_id):
            await self._bank(memory_id, create=True)
            existing = await self._operation(memory_id, operation_id, digest)
            if existing is not None:
                return await self._view(existing)
            source = await self._source(memory_id, source_id, lock=True)
            if source is None:
                source = MemorySourceState(
                    memory_id=memory_id,
                    source_message_id=source_id,
                    tenant_id=self.tenant_id,
                    source_revision=1,
                    generation=1,
                    content_sha256=expected_content_sha256,
                    state=observed["state"],
                    scope=observed["scope"],
                )
                self.db.add(source)
                await self.db.flush()
            elif source.content_sha256 != expected_content_sha256 or source.scope != observed["scope"]:
                raise RevisionConflict("source_state_changed")
            operation = MemoryMutationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="adopt_source",
                status="ready",
                index_sync_status="ready",
                result={"source": source_view(source)},
            )
            self.db.add(operation)
            result = await self._view(operation)
        return result

    async def _invalidate_pending(self, memory_id: str, entry_ids: set[str], *, revision_pairs: set[tuple[str, int]] | None = None, permanent: bool = False) -> list[str]:
        if not entry_ids:
            return []
        rows = list(
            await self.db.scalars(
                select(MemoryPublicationOutbox)
                .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.entry_id.in_(entry_ids))
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )
        affected: list[str] = []
        for row in rows:
            if row.payload.get("action") != "replace":
                continue
            if revision_pairs is not None and (row.entry_id, row.revision) not in revision_pairs:
                continue
            if not permanent and (row.state in {"ready", "sync_pending"} or row.payload.get("published") is True):
                continue
            if row.state != "ready" or permanent:
                row.state, row.error_code = "redacted" if permanent else "stale", "source_withdrawn" if permanent else "source_state_changed"
                operation = await self._operation(memory_id, row.operation_id)
                if operation is not None:
                    operation.status, operation.index_sync_status, operation.error_code = "stale", "cleanup_pending", row.error_code
                if permanent:
                    payload = deepcopy(row.payload)
                    if isinstance(payload.get("message"), dict):
                        payload["message"] = {key: value for key, value in payload["message"].items() if key not in {"content", "content_embed", "reason"}}
                    row.payload = payload
                affected.append(row.id)
        return affected

    async def _state_dependencies(self, memory_id: str, *, entry_ids: set[str] | None = None, source_id: int | None = None) -> list[str]:
        query = select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.payload["action"].as_string() != "replace")
        if entry_ids is not None:
            query = query.where(MemoryPublicationOutbox.entry_id.in_(entry_ids))
        if source_id is not None:
            query = query.where(MemoryPublicationOutbox.payload["source_id"].as_integer() == source_id)
        return [row.id for row in await self.db.scalars(query.execution_options(populate_existing=True))]

    async def _tracked_source_targets(self, memory_id: str, source_id: int) -> tuple[list[int], list[str]]:
        refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.memory_id == memory_id, MemoryRevisionSource.source_message_id == source_id)))
        pairs = {(ref.entry_id, ref.revision) for ref in refs}
        entry_ids = {entry_id for entry_id, _ in pairs}
        revisions = list(await self.db.scalars(select(MemoryEntryRevision).where(MemoryEntryRevision.memory_id == memory_id, MemoryEntryRevision.entry_id.in_(entry_ids)))) if entry_ids else []
        targets = {source_id, *(row.message_id for row in revisions if (row.entry_id, row.revision) in pairs)}
        outboxes = (
            list(await self.db.scalars(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.entry_id.in_(entry_ids)))) if entry_ids else []
        )
        dependencies = [row.id for row in outboxes if (row.entry_id, row.revision) in pairs]
        dependencies.extend(await self._state_dependencies(memory_id, source_id=source_id))
        return sorted(targets), list(dict.fromkeys(dependencies))

    async def _queue_source_sync(self, bank: ManagedMemoryState, source: MemorySourceState, prior_outbox_id: str) -> str:
        """Append a current-generation correction; never replay a superseded request."""
        digest = request_hash({"memory": source.memory_id, "source": source.source_message_id, "generation": source.generation})
        operation_id = "source-sync:" + digest
        if await self._operation(source.memory_id, operation_id) is None:
            permanent = source.state in _PERMANENT
            prior = await self.db.get(MemoryPublicationOutbox, prior_outbox_id, populate_existing=True)
            inherited = list(prior.payload.get("dependent_outboxes", [])) if prior is not None else []
            writer_cleanup_ids = list(prior.payload.get("writer_cleanup_ids", [])) if prior is not None else []
            targets, dependencies = await self._tracked_source_targets(source.memory_id, source.source_message_id) if permanent else ([source.source_message_id], [])
            if permanent:
                writers = await invalidate_source_writers(self.db, memory_id=source.memory_id, tenant_id=self.tenant_id, source_id=source.source_message_id)
                targets = sorted(set(targets) | set(writers["message_ids"]))
                dependencies = list(dict.fromkeys([*dependencies, *writers["outbox_ids"]]))
            self.db.add(
                MemoryMutationOperation(
                    memory_id=source.memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    actor_id=self.actor_id,
                    request_sha256=digest,
                    kind="change_state",
                    status="published",
                    index_sync_status="pending",
                    result={"origin": "generation_reconciliation", "physical_cleanup_complete": False},
                )
            )
            self.db.add(
                MemoryPublicationOutbox(
                    id=uuid4().hex,
                    memory_id=source.memory_id,
                    tenant_id=self.tenant_id,
                    operation_id=operation_id,
                    bank_generation=bank.generation,
                    state="pending",
                    attempt=0,
                    payload={
                        "action": "withdraw_source" if permanent else "change_state",
                        "source_id": source.source_message_id,
                        "source_generation": source.generation,
                        "message_ids": targets,
                        "status": source.state == "active",
                        "dependent_outboxes": list(dict.fromkeys([*dependencies, *inherited, prior_outbox_id])),
                        "writer_cleanup_ids": writer_cleanup_ids,
                    },
                    receipt={},
                )
            )
        return operation_id

    async def _queue_entry_sync(self, bank: ManagedMemoryState, entry: MemoryEntry, prior_outbox_id: str) -> str:
        digest = request_hash({"memory": entry.memory_id, "entry": entry.entry_id, "generation": entry.generation, "message": entry.current_message_id})
        operation_id = "entry-sync:" + digest
        if await self._operation(entry.memory_id, operation_id) is None:
            dependencies = await self._state_dependencies(entry.memory_id, entry_ids={entry.entry_id})
            prior = await self.db.get(MemoryPublicationOutbox, prior_outbox_id, populate_existing=True)
            inherited = list(prior.payload.get("dependent_outboxes", [])) if prior is not None else []
            self.db.add(
                MemoryMutationOperation(
                    memory_id=entry.memory_id,
                    operation_id=operation_id,
                    tenant_id=self.tenant_id,
                    actor_id=self.actor_id,
                    request_sha256=digest,
                    kind="change_state",
                    status="published",
                    index_sync_status="pending",
                    result={"origin": "generation_reconciliation", "physical_cleanup_complete": False},
                )
            )
            self.db.add(
                MemoryPublicationOutbox(
                    id=uuid4().hex,
                    memory_id=entry.memory_id,
                    tenant_id=self.tenant_id,
                    operation_id=operation_id,
                    entry_id=entry.entry_id,
                    revision=entry.current_revision,
                    message_id=entry.current_message_id,
                    expected_revision=entry.current_revision,
                    entry_generation=entry.generation,
                    bank_generation=bank.generation,
                    state="pending",
                    attempt=0,
                    payload={
                        "action": "change_state",
                        "message_ids": [entry.current_message_id],
                        "status": entry.state == "active",
                        "forget": entry.state == "withdrawn",
                        "dependent_outboxes": list(dict.fromkeys([*dependencies, *inherited, prior_outbox_id])),
                    },
                    receipt={},
                )
            )
        return operation_id

    async def _entry_fence(self, row: MemoryPublicationOutbox) -> MemoryEntry | None:
        if row.entry_id is None:
            return None
        # The bank lock is already held. Lock all evidence before the entry.
        refs = list(
            await self.db.scalars(
                select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == row.entry_id, MemoryRevisionSource.revision == row.revision).order_by(MemoryRevisionSource.source_message_id)
            )
        )
        sources = [await self._source(row.memory_id, ref.source_message_id, lock=True) for ref in refs]
        entry = await self.db.scalar(
            select(MemoryEntry).where(MemoryEntry.memory_id == row.memory_id, MemoryEntry.entry_id == row.entry_id).execution_options(populate_existing=True).with_for_update()
        )
        if entry is None:
            raise RevisionConflict("entry_not_found")
        if row.payload.get("status") is True and entry.state == "active":
            for ref, source in zip(refs, sources, strict=True):
                if source is None or source.state != "active" or (source.source_revision, source.content_sha256) != (ref.source_revision, ref.content_sha256):
                    raise RevisionConflict("source_not_active")
        return entry

    @staticmethod
    def _same_entry_generation(row: MemoryPublicationOutbox, entry: MemoryEntry) -> bool:
        return (entry.current_revision, entry.current_message_id, entry.generation) == (row.revision, row.message_id, row.entry_generation)

    async def withdraw_source(
        self,
        memory_id: str,
        source_id: int,
        *,
        operation_id: str,
        expected_source_revision: int,
        reason: str,
        _request_sha256: str | None = None,
        _legacy_baseline: dict[str, Any] | None = None,
        _require_quiescent: bool = False,
    ) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(source_id, "invalid_source_id")
        positive_int(expected_source_revision, "invalid_source_revision")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
            raise RevisionValidationError("invalid_withdrawal_reason")
        digest = _request_sha256 or request_hash({"kind": "withdraw_source", "actor": self.actor_id, "memory": memory_id, "source": source_id, "revision": expected_source_revision, "reason": reason})
        replay = await self._replay(memory_id, operation_id, digest)
        if replay is not None:
            return replay
        async with self._write(operation_id):
            bank = await self._bank(memory_id)
            operation = await self._operation(memory_id, operation_id, digest)
            if operation is not None:
                return await self._view(operation)
            source = await self._source(memory_id, source_id, lock=True)
            if source is None and _legacy_baseline is not None:
                # Legacy forget is itself the explicit write command. Register its
                # observed baseline and tombstone under the SAME public operation
                # commit; an auxiliary adoption commit would lose the recovery ID.
                if _legacy_baseline.get("memory_id") != memory_id or _legacy_baseline.get("source_id") != source_id or expected_source_revision != 1:
                    raise RevisionConflict("source_state_changed")
                source = MemorySourceState(
                    memory_id=memory_id,
                    tenant_id=self.tenant_id,
                    source_message_id=source_id,
                    source_revision=1,
                    generation=1,
                    content_sha256=_legacy_baseline["content_sha256"],
                    scope=_legacy_baseline["scope"],
                    state=_legacy_baseline["state"],
                )
                self.db.add(source)
                await self.db.flush()
            if source is None:
                raise RevisionConflict("source_adoption_required")
            if _legacy_baseline is not None and (source.content_sha256 != _legacy_baseline["content_sha256"] or source.scope != _legacy_baseline["scope"]):
                raise RevisionConflict("source_state_changed")
            if source.source_revision != expected_source_revision:
                raise RevisionConflict("expected_source_revision_mismatch")
            source.generation += 1
            source.state, source.updated_at = "forgotten", datetime.now(UTC)
            writers = await invalidate_source_writers(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_id=source_id)
            if _require_quiescent and writers["active_operation_ids"]:
                raise RevisionBusy("fifo_source_has_active_writer")
            refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.memory_id == memory_id, MemoryRevisionSource.source_message_id == source_id)))
            pairs = {(ref.entry_id, ref.revision) for ref in refs}
            entry_ids = {entry_id for entry_id, _ in pairs}
            entries = (
                list(
                    await self.db.scalars(
                        select(MemoryEntry)
                        .where(MemoryEntry.memory_id == memory_id, MemoryEntry.entry_id.in_(entry_ids))
                        .order_by(MemoryEntry.entry_id)
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                )
                if entry_ids
                else []
            )
            for entry in entries:
                if (entry.entry_id, entry.current_revision) in pairs:
                    entry.state, entry.generation = "withdrawn", entry.generation + 1
            targets = {source_id, *writers["message_ids"]}
            if entry_ids:
                revisions = list(
                    await self.db.scalars(
                        select(MemoryEntryRevision)
                        .where(MemoryEntryRevision.memory_id == memory_id, MemoryEntryRevision.entry_id.in_(entry_ids))
                        .execution_options(populate_existing=True)
                        .with_for_update()
                    )
                )
                for revision in revisions:
                    if (revision.entry_id, revision.revision) in pairs:
                        targets.add(revision.message_id)
                        revision.content = None
                        revision.assessment = {key: value for key, value in revision.assessment.items() if key in {"kind", "verification_status"}}
                        revision.message_snapshot = {key: value for key, value in revision.message_snapshot.items() if key not in {"content", "content_embed", "reason"}}
            pending = await self._invalidate_pending(memory_id, entry_ids, revision_pairs=pairs, permanent=True)
            _, tracked_dependencies = await self._tracked_source_targets(memory_id, source_id)
            pending = list(dict.fromkeys([*pending, *tracked_dependencies, *writers["outbox_ids"]]))
            if _require_quiescent and pending:
                dependencies = await self.db.scalars(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.id.in_(pending)).with_for_update())
                if any(row.receipt.get("active_attempts") for row in dependencies):
                    raise RevisionBusy("fifo_source_has_active_writer")
            operation = MemoryMutationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="withdraw_source",
                status="published",
                index_sync_status="pending",
                result={"stop_supply": "withdrawn", "source": source_view(source), "cleanup_scope": "tracked_revisions_and_bounded_source_inventory", "physical_cleanup_complete": False},
            )
            self.db.add(operation)
            self.db.add(
                MemoryPublicationOutbox(
                    id=uuid4().hex,
                    memory_id=memory_id,
                    tenant_id=self.tenant_id,
                    operation_id=operation_id,
                    bank_generation=bank.generation,
                    state="pending",
                    attempt=0,
                    payload={"action": "withdraw_source", "source_id": source_id, "source_generation": source.generation, "message_ids": sorted(targets), "dependent_outboxes": pending},
                    receipt={},
                )
            )
            await self.db.flush()
            result = await self._view(operation)
        return result

    async def evict_source(self, memory_id: str, source_raw: dict[str, Any], operation_id: str, *, storage: ManagedMessageStore) -> dict[str, Any]:
        """Retire the fixed, unchanged source closure before reporting FIFO capacity."""
        validate_operation_id(operation_id)
        source_id = positive_int(source_raw.get("message_id"), "invalid_source_id")
        expected = _fifo_source_snapshot(memory_id, source_id, source_raw)
        digest = request_hash({"kind": "fifo_source_eviction", "actor": self.actor_id, "memory": memory_id, "source": source_id, "hash": expected["content_sha256"], "scope": expected["scope"]})
        replay = await self._replay(memory_id, operation_id, digest)
        if replay is not None:
            return await self.reconcile_operation(memory_id, operation_id, storage=storage)
        current = _raw_snapshot(memory_id, source_id, await self.message_reader(memory_id, source_id))
        if current["content_sha256"] != expected["content_sha256"] or current["scope"] != expected["scope"]:
            raise RevisionConflict("fifo_source_changed")
        expected = current
        source = await self.get_source_state(memory_id, source_id)
        await self.withdraw_source(
            memory_id,
            source_id,
            operation_id=operation_id,
            expected_source_revision=source["source_revision"] if source["managed"] else 1,
            reason="capacity_fifo",
            _request_sha256=digest,
            _legacy_baseline=expected,
            _require_quiescent=True,
        )
        return await self.reconcile_operation(memory_id, operation_id, storage=storage)

    async def _adopt_generated_entry(self, memory_id: str, physical: dict[str, Any], raw: dict[str, Any] | None) -> MemoryEntryRevision:
        """Register one drained, approved fact inside its management operation."""
        message_id = positive_int(physical.get("message_id"), "invalid_message_id")
        source_id = positive_int(physical.get("source_id"), "raw_ancestor_required")
        observed = _raw_snapshot(memory_id, source_id, raw)
        source = await self._source(memory_id, source_id, lock=True)
        if source is None or source.state != "active" or observed["state"] != "active" or source.content_sha256 != observed["content_sha256"] or source.scope != observed["scope"]:
            raise RevisionConflict("generated_source_changed")
        admissions = list(
            await self.db.scalars(
                select(MemoryWriterAdmission)
                .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.message_ids.contains([message_id]))
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        )
        if len(admissions) != 1:
            raise RevisionConflict("entry_adoption_required")
        producer = admissions[0]
        if producer.tenant_id != self.tenant_id or producer.kind != "extract" or producer.publication_state != "ready" or producer.source_message_id != source_id:
            raise RevisionConflict("entry_adoption_required")
        if producer.active_token is not None or producer.state == "active":
            raise RevisionBusy("generated_writer_still_inflight")
        outbox = await self.db.scalar(
            select(MemoryPublicationOutbox)
            .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == producer.operation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if outbox is None or outbox.tenant_id != self.tenant_id or outbox.state != "ready" or outbox.payload.get("approved") is not True:
            raise RevisionConflict("entry_adoption_required")
        if outbox.receipt.get("active_attempts"):
            raise RevisionBusy("generated_writer_still_inflight")
        payload = outbox.payload
        approved = [item for item in payload.get("messages", []) if isinstance(item, dict) and item.get("message_id") == message_id]
        binding_value, source_binding = payload.get("model_binding"), payload.get("source_binding")
        if len(approved) != 1 or not isinstance(binding_value, dict) or not isinstance(source_binding, dict):
            raise RevisionConflict("generated_approval_missing")
        if (
            source_binding.get("source_id") != source_id
            or source_binding.get("revision") != source.source_revision
            or source_binding.get("content_sha256") != source.content_sha256
            or source_binding.get("scope") != source.scope
            or message_scope(physical, memory_id) != source.scope
        ):
            raise RevisionConflict("generated_source_changed")
        binding = ModelBinding(**binding_value)
        verify_approved_message(physical, approved[0], active=is_active(physical))
        if binding.dimension != vector_dimension(physical):
            raise RevisionConflict("generated_model_dimension_changed")
        entry = MemoryEntry(
            entry_id=uuid4().hex,
            memory_id=memory_id,
            tenant_id=self.tenant_id,
            legacy_message_id=message_id,
            current_revision=1,
            next_revision=2,
            current_message_id=message_id,
            generation=1,
            state="active" if is_active(physical) else "inactive",
        )
        snapshot = {key: deepcopy(value) for key, value in physical.items() if key not in {"content", "content_embed"}}
        snapshot.update(model_binding=binding.to_dict(), embedding_dimension=binding.dimension)
        revision = MemoryEntryRevision(
            entry_id=entry.entry_id,
            revision=1,
            memory_id=memory_id,
            tenant_id=self.tenant_id,
            message_id=message_id,
            parent_revision=None,
            content=physical["content"],
            content_sha256=content_hash(physical["content"]),
            origin="generated_adoption",
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
                source_revision=source.source_revision,
                source_generation=source.generation,
                content_sha256=source.content_sha256,
                relation="context",
                locator={"kind": "memory_message", "memory_id": memory_id, "message_id": source_id},
            )
        )
        await self.db.flush()
        return revision

    async def _redact_generated_messages(self, memory_id: str, message_ids: set[int]) -> None:
        """Erase only forgotten facts from a completed extraction's shared plan."""
        for producer in await self.db.scalars(select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == memory_id)):
            if not message_ids.intersection(producer.message_ids):
                continue
            outbox = await self.db.scalar(
                select(MemoryPublicationOutbox)
                .where(
                    MemoryPublicationOutbox.memory_id == memory_id,
                    MemoryPublicationOutbox.operation_id == producer.operation_id,
                )
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if outbox is None:
                continue
            payload = deepcopy(outbox.payload)
            payload["messages"] = [
                {key: value for key, value in item.items() if key not in {"content", "content_embed"}} if item.get("message_id") in message_ids else item for item in payload.get("messages", [])
            ]
            outbox.payload = payload

    async def change_physical_state(self, memory_id: str, message_id: int, *, operation_id: str, status: bool | None = None, forget: bool = False) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(message_id, "invalid_message_id")
        if (status is None) == (forget is False) or (status is not None and type(status) is not bool) or type(forget) is not bool:
            raise RevisionValidationError("one_physical_state_change_required")
        digest = request_hash({"kind": "change_state", "actor": self.actor_id, "memory": memory_id, "message": message_id, "status": status, "forget": forget})
        replay = await self._replay(memory_id, operation_id, digest)
        if replay is not None:
            return replay
        physical = await self.message_reader(memory_id, message_id)
        if physical is None:
            raise RevisionNotFound("message_not_found")
        scope = message_scope(physical, memory_id)
        raw_state = _raw_snapshot(memory_id, message_id, physical) if physical.get("message_type") == "raw" else None
        ancestor = None if raw_state is not None else await self.message_reader(memory_id, positive_int(physical.get("source_id"), "raw_ancestor_required"))
        if physical.get("message_type") == "raw" and forget:
            snapshot = await self.get_source_state(memory_id, message_id)
            return await self.withdraw_source(
                memory_id,
                message_id,
                operation_id=operation_id,
                expected_source_revision=snapshot["source_revision"] if snapshot["managed"] else 1,
                reason="legacy_forget",
                _request_sha256=digest,
                _legacy_baseline=raw_state,
            )
        async with self._write(operation_id):
            bank = await self._bank(memory_id)
            existing = await self._operation(memory_id, operation_id, digest)
            if existing is not None:
                return await self._view(existing)
            entry_identity: dict[str, Any] = {}
            writer_cleanup_ids: list[int] = []
            if physical.get("message_type") == "raw":
                source = await self._source(memory_id, message_id, lock=True)
                if source is None:
                    source = MemorySourceState(
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        source_message_id=message_id,
                        source_revision=1,
                        generation=1,
                        content_sha256=content_hash(physical["content"]),
                        scope=scope,
                        state=raw_state["state"] if raw_state is not None else "inactive",
                    )
                    self.db.add(source)
                    await self.db.flush()
                if source.state in _PERMANENT or source.content_sha256 != content_hash(physical["content"]) or source.scope != scope:
                    raise RevisionConflict("source_permanently_withdrawn_or_changed")
                source.generation += 1
                source.state = "active" if status else "inactive"
                source.updated_at = datetime.now(UTC)
                refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.memory_id == memory_id, MemoryRevisionSource.source_message_id == message_id)))
                pending = await self._invalidate_pending(memory_id, {ref.entry_id for ref in refs})
                pending.extend(await self._state_dependencies(memory_id, source_id=message_id))
                writers = await invalidate_source_writers(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_id=message_id, permanent=False)
                pending.extend(writers["outbox_ids"])
                writer_cleanup_ids = writers["message_ids"]
                source_generation = source.generation
            else:
                revision = await self.db.scalar(select(MemoryEntryRevision).where(MemoryEntryRevision.memory_id == memory_id, MemoryEntryRevision.message_id == message_id))
                if revision is None:
                    revision = await self._adopt_generated_entry(memory_id, physical, ancestor)
                refs = list(
                    await self.db.scalars(
                        select(MemoryRevisionSource)
                        .where(MemoryRevisionSource.entry_id == revision.entry_id, MemoryRevisionSource.revision == revision.revision)
                        .order_by(MemoryRevisionSource.source_message_id)
                    )
                )
                for ref in refs:
                    source = await self._source(memory_id, ref.source_message_id, lock=True)
                    if status is True and (source is None or source.state != "active" or source.source_revision != ref.source_revision or source.content_sha256 != ref.content_sha256):
                        raise RevisionConflict("source_not_active")
                entry = await self.db.scalar(select(MemoryEntry).where(MemoryEntry.entry_id == revision.entry_id).execution_options(populate_existing=True).with_for_update())
                if entry is None or entry.current_message_id != message_id:
                    raise RevisionConflict("historical_projection_state_change_requires_audit_api")
                if entry.state == "withdrawn":
                    raise RevisionConflict("entry_permanently_withdrawn")
                entry.generation += 1
                entry.state = "withdrawn" if forget else "active" if status else "inactive"
                entry.updated_at = datetime.now(UTC)
                from memory.maintenance.lifecycle import invalidate_for_entry

                maintenance = await invalidate_for_entry(self.db, memory_id=memory_id, tenant_id=self.tenant_id, entry_id=entry.entry_id, permanent=forget)
                pending = await self._invalidate_pending(memory_id, {entry.entry_id}, permanent=forget)
                pending.extend(maintenance["outbox_ids"])
                writer_cleanup_ids = maintenance["message_ids"]
                pending.extend(await self._state_dependencies(memory_id, entry_ids={entry.entry_id}))
                if forget:
                    versions = list(
                        await self.db.scalars(select(MemoryEntryRevision).where(MemoryEntryRevision.entry_id == entry.entry_id).execution_options(populate_existing=True).with_for_update())
                    )
                    for version in versions:
                        version.content = None
                        version.assessment = {key: value for key, value in version.assessment.items() if key in {"kind", "verification_status"}}
                        version.message_snapshot = {key: value for key, value in version.message_snapshot.items() if key not in {"content", "content_embed", "reason"}}
                    await self._redact_generated_messages(memory_id, {version.message_id for version in versions})
                entry_identity = {
                    "entry_id": entry.entry_id,
                    "revision": entry.current_revision,
                    "message_id": message_id,
                    "expected_revision": entry.current_revision,
                    "entry_generation": entry.generation,
                }
                source_generation = None
            operation = MemoryMutationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="change_state",
                status="published",
                index_sync_status="pending",
                result={"stop_supply": "withdrawn" if forget else "active" if status else "inactive", "physical_cleanup_complete": False},
            )
            self.db.add(operation)
            self.db.add(
                MemoryPublicationOutbox(
                    id=uuid4().hex,
                    memory_id=memory_id,
                    tenant_id=self.tenant_id,
                    operation_id=operation_id,
                    **entry_identity,
                    bank_generation=bank.generation,
                    state="pending",
                    attempt=0,
                    payload={
                        "action": "change_state",
                        "message_ids": [message_id],
                        "status": status is True,
                        "forget": forget,
                        "source_id": message_id if physical.get("message_type") == "raw" else None,
                        "source_generation": source_generation,
                        "dependent_outboxes": list(dict.fromkeys(pending)),
                        "writer_cleanup_ids": writer_cleanup_ids,
                    },
                    receipt={},
                )
            )
            await self.db.flush()
            result = await self._view(operation)
        return result

    async def delete_memory(self, memory_id: str, *, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        await self._idle()
        try:
            await self._bank(memory_id)
            raise RevisionConflict("managed_delete_requires_writer_coordination")
        finally:
            await self.db.rollback()

    async def reconcile_operation(self, memory_id: str, operation_id: str, *, storage: ManagedMessageStore) -> dict[str, Any]:
        token = uuid4().hex
        try:
            return await self._reconcile_once(memory_id, operation_id, storage=storage, token=token)
        finally:
            await finish_owned_work(self._finish_attempt(memory_id, operation_id, token))

    async def _finish_attempt(self, memory_id: str, operation_id: str, token: str) -> None:
        async with self._write(operation_id):
            row = await self.db.scalar(
                select(MemoryPublicationOutbox)
                .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id, MemoryPublicationOutbox.tenant_id == self.tenant_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if row is None or token not in row.receipt.get("active_attempts", []):
                return
            receipt = deepcopy(row.receipt)
            receipt["active_attempts"] = [value for value in receipt.get("active_attempts", []) if value != token]
            receipt["finished_attempts"] = list(dict.fromkeys([*receipt.get("finished_attempts", []), token]))
            row.receipt = receipt
            if row.lease_token == token:
                row.lease_token, row.lease_until = None, None

    async def _refresh_source_writers(self, row: MemoryPublicationOutbox, source: MemorySourceState) -> bool:
        writers = await invalidate_source_writers(self.db, memory_id=row.memory_id, tenant_id=self.tenant_id, source_id=source.source_message_id, permanent=source.state in _PERMANENT)
        payload = deepcopy(row.payload)
        targets = "message_ids" if payload.get("action") == "withdraw_source" else "writer_cleanup_ids"
        payload[targets] = sorted(set(payload.get(targets, [])) | set(writers["message_ids"]))
        payload["dependent_outboxes"] = list(dict.fromkeys([*payload.get("dependent_outboxes", []), *writers["outbox_ids"]]))
        row.payload = payload
        return bool(writers["active_operation_ids"])

    async def _reconcile_once(self, memory_id: str, operation_id: str, *, storage: ManagedMessageStore, token: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        async with self._write(operation_id):
            bank = await self._bank(memory_id, deleting=True)
            operation = await self._operation(memory_id, operation_id)
            if operation is None:
                raise RevisionNotFound("operation_not_found")
            if operation.status == "ready":
                return await self._view(operation)
            row = await self.db.scalar(
                select(MemoryPublicationOutbox)
                .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if row is None or row.payload.get("action") not in {"withdraw_source", "change_state"}:
                raise RevisionConflict("invalid_source_operation")
            if row.lease_until is not None and row.lease_until > datetime.now(UTC):
                raise RevisionBusy("source_cleanup_in_progress")
            if row.receipt.get("active_attempts"):
                raise RevisionBusy("source_cleanup_previous_attempt_unconfirmed")
            if row.attempt >= 5:
                raise RevisionBusy("source_cleanup_attempts_exhausted")
            if row.bank_generation != bank.generation:
                raise RevisionConflict("source_operation_generation_changed")
            pending = list(await self.db.scalars(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id.in_(row.payload.get("dependent_outboxes", [])))))
            if any(item.receipt.get("active_attempts") for item in pending):
                operation.index_sync_status, operation.error_code = "unknown", "older_publication_still_inflight"
                return await self._view(operation)
            if row.payload.get("source_id") is not None:
                source = await self._source(memory_id, row.payload["source_id"], lock=True)
                if source is None or source.generation != row.payload.get("source_generation"):
                    row.state = "stale"
                    operation.status, operation.error_code = "stale", "source_generation_changed"
                    if source is not None:
                        operation.result = {**operation.result, "reconciliation_operation_id": await self._queue_source_sync(bank, source, row.id)}
                    return await self._view(operation)
                if await self._refresh_source_writers(row, source):
                    operation.index_sync_status, operation.error_code = "unknown", "older_publication_still_inflight"
                    return await self._view(operation)
            entry = await self._entry_fence(row)
            if entry is not None and not self._same_entry_generation(row, entry):
                row.state = "stale"
                operation.status, operation.error_code = "stale", "entry_generation_changed"
                operation.result = {**operation.result, "reconciliation_operation_id": await self._queue_entry_sync(bank, entry, row.id)}
                return await self._view(operation)
            row.lease_token, row.lease_until, row.attempt = token, datetime.now(UTC) + timedelta(seconds=120), row.attempt + 1
            row.receipt = {**row.receipt, "active_attempts": [*row.receipt.get("active_attempts", []), token]}
            payload, outbox_id = deepcopy(row.payload), row.id
            retired_ids = [
                item.message_id
                for item in pending
                if item.payload.get("action") == "replace"
                and item.state in {"stale", "redacted"}
                and item.message_id is not None
                and (item.state == "redacted" or item.payload.get("published") is not True)
            ]
            retired_ids.extend(positive_int(value, "invalid_cleanup_message_id") for value in payload.get("writer_cleanup_ids", []))
            if payload.get("forget") and row.entry_id is not None:
                retired_ids.extend(await self.db.scalars(select(MemoryEntryRevision.message_id).where(MemoryEntryRevision.entry_id == row.entry_id)))
        try:
            targets = set(retired_ids)
            if payload["action"] == "withdraw_source":
                targets.update(payload["message_ids"])
                targets.update(await finish_owned_work(storage.source_dependents(payload["source_id"])))
            elif payload.get("forget"):
                targets.update(payload["message_ids"])
            values = sorted(targets)
            for offset in range(0, len(values), 100):
                await finish_owned_work(storage.delete(values[offset : offset + 100]))
            if payload["action"] != "withdraw_source" and not payload.get("forget"):
                target_id = payload["message_ids"][0]
                canceled_pending_raw = payload["status"] is False and target_id in payload.get("writer_cleanup_ids", [])
                already_absent = canceled_pending_raw and await finish_owned_work(self.message_reader(memory_id, target_id)) is None
                if not already_absent:
                    await finish_owned_work(storage.set_status(target_id, payload["status"]))
        except Exception:
            async with self._write(operation_id):
                row = await self.db.get(MemoryPublicationOutbox, outbox_id, with_for_update=True, populate_existing=True)
                operation = await self._operation(memory_id, operation_id)
                if row is None or operation is None or row.lease_token != token:
                    raise RevisionConflict("source_cleanup_lease_changed")
                row.state, row.lease_token, row.lease_until, row.error_code = "unknown", None, None, "source_projection_unconfirmed"
                operation.index_sync_status, operation.error_code = "unknown", row.error_code
                return await self._view(operation)
        async with self._write(operation_id):
            bank = await self._bank(memory_id, deleting=True)
            row = await self.db.get(MemoryPublicationOutbox, outbox_id, with_for_update=True, populate_existing=True)
            operation = await self._operation(memory_id, operation_id)
            if row is None or operation is None or row.lease_token != token or row.bank_generation != bank.generation:
                raise RevisionConflict("source_cleanup_fence_changed")
            if payload.get("source_id") is not None:
                source = await self._source(memory_id, payload["source_id"], lock=True)
                if source is None or source.generation != payload["source_generation"]:
                    row.state, row.lease_token, row.lease_until = "unknown", None, None
                    operation.index_sync_status, operation.error_code = "unknown", "source_generation_changed_after_io"
                    if source is not None:
                        operation.result = {**operation.result, "reconciliation_operation_id": await self._queue_source_sync(bank, source, row.id)}
                    return await self._view(operation)
                target_field = "message_ids" if payload.get("action") == "withdraw_source" else "writer_cleanup_ids"
                if await self._refresh_source_writers(row, source) or set(row.payload.get(target_field, [])) != set(payload.get(target_field, [])):
                    row.state, row.lease_token, row.lease_until = "unknown", None, None
                    operation.index_sync_status, operation.error_code = "unknown", "source_cleanup_inventory_changed"
                    return await self._view(operation)
                payload = deepcopy(row.payload)
            entry = await self._entry_fence(row)
            if entry is not None and not self._same_entry_generation(row, entry):
                row.state, row.lease_token, row.lease_until = "unknown", None, None
                operation.index_sync_status, operation.error_code = "unknown", "entry_generation_changed_after_io"
                operation.result = {**operation.result, "reconciliation_operation_id": await self._queue_entry_sync(bank, entry, row.id)}
                return await self._view(operation)
            dependencies = list(
                await self.db.scalars(
                    select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id.in_(payload.get("dependent_outboxes", []))).execution_options(populate_existing=True).with_for_update()
                )
            )
            if any(item.receipt.get("active_attempts") for item in dependencies):
                row.state, row.lease_token, row.lease_until = "unknown", None, None
                operation.index_sync_status, operation.error_code = "unknown", "older_publication_still_inflight"
                return await self._view(operation)
            for dependency in dependencies:
                await release_writer_reservation(self.db, memory_id=memory_id, tenant_id=self.tenant_id, outbox=dependency, bank=bank)
            row.state, row.lease_token, row.lease_until = "ready", None, None
            bank.capacity_generation += 1
            row.receipt = {**row.receipt, "confirmed_at": datetime.now(UTC).isoformat(), "scope": "tracked_projections_and_bounded_inventory"}
            operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
            operation.result = {
                **operation.result,
                "physical_cleanup_complete": payload["action"] == "delete_bank",
                "projection_cleanup_status": "confirmed",
                "untraceable_copies": payload["action"] != "delete_bank",
            }
            result = await self._view(operation)
        return result
