"""Explicit human conclusions; validity is re-read, never promoted to fact truth."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_verification_models import MemoryVerificationHead, MemoryVerificationOperation, MemoryVerificationRecord, MemoryVerificationSource
from common.asyncio_utils import finish_owned_work
from memory.maintenance.target_conditions import projection_digest
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, content_hash, is_active, message_scope, request_hash, validate_operation_id
from memory.revisions.storage import ManagedMessageStore
from memory.verification.domain import MAX_SOURCES, TODO_PHYSICAL_LIMIT, TODO_SCAN_LIMIT, bounded_text, create_request, cursor_decode, cursor_encode, head_version, identifier
from memory.writes.admission import lock_open_writer_fence
from memory.writes.providers import release_sql
from memory.writes.types import Authorize

ReadyEntry = Callable[[MemoryEntry, MemoryEntryRevision], Awaitable[None]]


class _ReadLimit(Exception):
    pass


def _public_binding(binding: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(binding)
    for key in ("revision", "message_id", "generation"):
        result["entry"][key] = str(result["entry"][key])
    for source in result["sources"]:
        for key in ("source_id", "source_revision", "source_generation"):
            source[key] = str(source[key])
    return result


def _without_projection(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": {k: v for k, v in binding["entry"].items() if k != "projection_sha256"},
        "sources": [{k: v for k, v in source.items() if k != "projection_sha256"} for source in binding["sources"]],
    }


class VerificationService:
    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str, authorize: Authorize, storage: ManagedMessageStore, ready_entry: ReadyEntry) -> None:
        self.db, self.tenant_id, self.actor_id, self.authorize, self.storage, self.ready_entry = db, tenant_id, actor_id, authorize, storage, ready_entry

    async def _idle(self) -> None:
        await release_sql(self.db)
        self.db.expire_all()

    @asynccontextmanager
    async def _write(self, operation_id: str) -> AsyncIterator[None]:
        await self._idle()
        try:
            yield
            await self.db.flush()
        except IntegrityError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("verification_concurrent_change") from error
        except BaseException:
            await finish_owned_work(self.db.rollback())
            raise
        try:
            await finish_owned_work(self.db.commit())
        except IntegrityError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionConflict("verification_concurrent_change") from error
        except DBAPIError as error:
            await finish_owned_work(self.db.rollback())
            raise RevisionPersistenceUnknown(operation_id) from error

    async def _metadata(self, memory_id: str, entry_id: str, *, lock: bool = False) -> tuple[dict[str, Any], str]:
        await self.authorize(self.db, memory_id)
        bank = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
        if bank != "active":
            raise RevisionConflict("verification_memory_unavailable")
        query = select(MemoryEntry).where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, MemoryEntry.entry_id == entry_id).execution_options(populate_existing=True)
        entry = await self.db.scalar(query)
        if entry is None:
            raise RevisionNotFound("entry_not_found")
        revision = await self.db.get(MemoryEntryRevision, (entry_id, entry.current_revision), populate_existing=True)
        if revision is None or entry.state != "active" or revision.content is None:
            raise RevisionConflict("verification_entry_unavailable")
        await self.ready_entry(entry, revision)
        refs = list(
            await self.db.scalars(
                select(MemoryRevisionSource)
                .where(MemoryRevisionSource.entry_id == entry_id, MemoryRevisionSource.revision == entry.current_revision)
                .order_by(MemoryRevisionSource.source_message_id)
                .limit(MAX_SOURCES + 1)
            )
        )
        if not 1 <= len(refs) <= MAX_SOURCES:
            raise RevisionValidationError("verification_source_closure_limit")
        scope = message_scope(revision.message_snapshot, memory_id)
        sources = []
        for ref in refs:
            source_query = (
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.tenant_id, MemorySourceState.source_message_id == ref.source_message_id)
                .execution_options(populate_existing=True)
            )
            source = await self.db.scalar(source_query.with_for_update() if lock else source_query)
            if (
                source is None
                or ref.memory_id != memory_id
                or source.state != "active"
                or (source.source_revision, source.content_sha256) != (ref.source_revision, ref.content_sha256)
                or any(source.scope.get(key) != scope[key] for key in ("user_id", "agent_id"))
                or (ref.locator.get("scope") is not None and ref.locator["scope"] != source.scope)
            ):
                raise RevisionConflict("verification_source_unavailable")
            sources.append(
                {
                    "source_id": source.source_message_id,
                    "source_revision": source.source_revision,
                    "source_generation": source.generation,
                    "content_sha256": source.content_sha256,
                    "scope": deepcopy(source.scope),
                }
            )
        if lock:
            # The caller already holds Memory -> writer fence -> bank. Sources precede entry.
            await self.db.scalar(query.with_for_update())
        binding = {
            "entry": {
                "entry_id": entry_id,
                "revision": entry.current_revision,
                "message_id": entry.current_message_id,
                "generation": entry.generation,
                "content_sha256": revision.content_sha256,
                "scope": scope,
            },
            "sources": sources,
        }
        return binding, revision.content

    async def _snapshot(self, memory_id: str, entry_id: str, *, physical_budget: list[int] | None = None) -> tuple[dict[str, Any], str]:
        await self._idle()
        try:
            binding, content = await self._metadata(memory_id, entry_id)
            revision = await self.db.get(MemoryEntryRevision, (entry_id, binding["entry"]["revision"]), populate_existing=True)
            expected_projection = deepcopy(revision.message_snapshot) if revision is not None else {}
        finally:
            await release_sql(self.db)
        if physical_budget is not None and physical_budget[0] < len(binding["sources"]) + 1:
            raise _ReadLimit
        for expected in [*binding["sources"], binding["entry"]]:
            raw = "source_id" in expected
            identity = expected["source_id"] if raw else expected["message_id"]
            if physical_budget is not None:
                physical_budget[0] -= 1
            message = await self.storage.read(identity)
            if (
                message is None
                or message.get("memory_id") != memory_id
                or message.get("message_id") != identity
                or not is_active(message)
                or message_scope(message, memory_id) != expected["scope"]
                or not isinstance(message.get("content"), str)
                or content_hash(message["content"]) != expected["content_sha256"]
                or (raw and (message.get("message_type") != "raw" or "source_id" not in message or isinstance(message["source_id"], bool) or message["source_id"] not in (None, 0, "", "0")))
                or (not raw and message.get("message_type") not in {"semantic", "episodic", "procedural"})
            ):
                raise RevisionConflict("verification_projection_changed")
            if not raw:
                for key in ("message_type", "source_id", "valid_at", "invalid_at"):
                    if key not in expected_projection:
                        continue
                    actual, approved = message.get(key), expected_projection[key]
                    if key == "source_id":
                        equal = not isinstance(actual, bool) and str(actual or 0) == str(approved or 0)
                    elif key in {"valid_at", "invalid_at"}:
                        equal = (None if actual in (None, "", "-") else actual) == (None if approved in (None, "", "-") else approved)
                    else:
                        equal = actual == approved
                    if not equal:
                        raise RevisionConflict("verification_projection_changed")
            expected["projection_sha256"] = projection_digest(message)
        await self._idle()
        try:
            fresh, _ = await self._metadata(memory_id, entry_id)
            if fresh != _without_projection(binding):
                raise RevisionConflict("verification_binding_changed")
        finally:
            await release_sql(self.db)
        return binding, content

    async def snapshot(self, memory_id: str, entry_id: str, *, physical_budget: list[int] | None = None) -> dict[str, Any]:
        identifier(entry_id)
        binding, content = await self._snapshot(memory_id, entry_id, physical_budget=physical_budget)
        await self.authorize(self.db, memory_id)
        head = await self.db.get(MemoryVerificationHead, (memory_id, entry_id), populate_existing=True)
        version, current_id = (head.head_version, head.verification_id) if head is not None else (0, None)
        await release_sql(self.db)
        current = await self._view(memory_id, current_id, physical_budget=physical_budget) if current_id is not None else None
        await self._idle()
        try:
            closing, _ = await self._metadata(memory_id, entry_id)
            latest = await self.db.get(MemoryVerificationHead, (memory_id, entry_id), populate_existing=True)
            latest_head = (latest.head_version, latest.verification_id) if latest is not None else (0, None)
            if closing != _without_projection(binding):
                raise RevisionConflict("verification_binding_changed")
            if latest_head != (version, current_id):
                raise RevisionConflict("verification_head_changed")
            if current is not None and not current["can_actions"]["record"]:
                raise RevisionConflict("verification_entry_unavailable")
        finally:
            await release_sql(self.db)
        return {
            "memory_id": memory_id,
            "entry_id": entry_id,
            "binding_sha256": request_hash(binding),
            "binding": _public_binding(binding),
            "entry_content": content,
            "head_version": str(version),
            "current_record": current,
            "can_record": True,
            "snapshot_only": True,
        }

    async def _operation(self, memory_id: str, operation_id: str, digest: str | None = None) -> dict[str, Any] | None:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            row = await self.db.get(MemoryVerificationOperation, (memory_id, operation_id), populate_existing=True)
            if row is None or row.tenant_id != self.tenant_id:
                return None
            if digest is not None and (row.actor_id != self.actor_id or row.request_sha256 != digest):
                raise RevisionConflict("operation_request_mismatch")
            value = {name: getattr(row, name) for name in ("operation_id", "kind", "verification_id", "head_version")}
        finally:
            await release_sql(self.db)
        return {**value, "status": "ready", "head_version": str(value["head_version"]), "record": await self._view(memory_id, value["verification_id"])}

    async def operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        value = await self._operation(memory_id, operation_id)
        if value is None:
            raise RevisionNotFound("verification_operation_not_found")
        return value

    async def record(self, memory_id: str, entry_id: str, *, operation_id: str, **options: Any) -> dict[str, Any]:
        identifier(entry_id)
        validate_operation_id(operation_id)
        command = create_request(**options)
        digest = request_hash({"kind": "verification_record", "entry_id": entry_id, **command})
        if replay := await self._operation(memory_id, operation_id, digest):
            return replay
        binding, _ = await self._snapshot(memory_id, entry_id)
        if request_hash(binding) != command["expected_binding_sha256"]:
            raise RevisionConflict("verification_binding_changed")
        async with self._write(operation_id):
            await self.authorize(self.db, memory_id)
            await lock_open_writer_fence(self.db, memory_id=memory_id, tenant_id=self.tenant_id)
            previous = await self.db.get(MemoryVerificationOperation, (memory_id, operation_id), populate_existing=True)
            if previous is not None:
                if (previous.actor_id, previous.request_sha256) != (self.actor_id, digest):
                    raise RevisionConflict("operation_request_mismatch")
            else:
                await self._record_locked(memory_id, entry_id, operation_id, binding, command, digest)
        return await self.operation(memory_id, operation_id)

    async def _record_locked(self, memory_id: str, entry_id: str, operation_id: str, binding: dict[str, Any], command: dict[str, Any], digest: str) -> None:
        locked, _ = await self._metadata(memory_id, entry_id, lock=True)
        if locked != _without_projection(binding):
            raise RevisionConflict("verification_binding_changed")
        head = await self.db.get(MemoryVerificationHead, (memory_id, entry_id), with_for_update=True, populate_existing=True)
        version = head.head_version if head is not None else 0
        if version != command["expected_head_version"] or version >= 2**63 - 1:
            raise RevisionConflict("verification_head_changed")
        identity, now = uuid4().hex, datetime.now(UTC)
        checked = datetime.fromisoformat(command["checked_at"]) if command["checked_at"] is not None else now
        self.db.add(
            MemoryVerificationRecord(
                verification_id=identity,
                memory_id=memory_id,
                tenant_id=self.tenant_id,
                entry_id=entry_id,
                record_version=version + 1,
                entry_binding=deepcopy(binding["entry"]),
                binding_sha256=request_hash(binding),
                result=command["result"],
                method=command["method"],
                applicability=command["applicability"],
                notes=command["notes"],
                evidence=command["evidence"],
                checked_at=checked,
                checked_at_is_user_reported=command["checked_at"] is not None,
                recorded_at=now,
                actor_id=self.actor_id,
                state="active",
            )
        )
        await self.db.flush()
        for source in binding["sources"]:
            self.db.add(MemoryVerificationSource(verification_id=identity, memory_id=memory_id, tenant_id=self.tenant_id, **source))
        if head is None:
            self.db.add(MemoryVerificationHead(memory_id=memory_id, entry_id=entry_id, tenant_id=self.tenant_id, head_version=version + 1, verification_id=identity))
        else:
            head.head_version, head.verification_id = version + 1, identity
        self.db.add(
            MemoryVerificationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="verification_record",
                verification_id=identity,
                head_version=version + 1,
            )
        )

    async def revoke(self, memory_id: str, verification_id: str, *, operation_id: str, expected_head_version: str | int, reason: str) -> dict[str, Any]:
        identifier(verification_id)
        validate_operation_id(operation_id)
        expected, reason = head_version(expected_head_version), bounded_text(reason, 1024, nonblank=True)
        digest = request_hash({"kind": "verification_revoke", "verification_id": verification_id, "expected_head_version": expected, "reason": reason})
        if replay := await self._operation(memory_id, operation_id, digest):
            return replay
        async with self._write(operation_id):
            await self.authorize(self.db, memory_id)
            await lock_open_writer_fence(self.db, memory_id=memory_id, tenant_id=self.tenant_id)
            previous = await self.db.get(MemoryVerificationOperation, (memory_id, operation_id), populate_existing=True)
            if previous is not None:
                if (previous.actor_id, previous.request_sha256) != (self.actor_id, digest):
                    raise RevisionConflict("operation_request_mismatch")
            else:
                await self._revoke_locked(memory_id, verification_id, operation_id, expected, reason, digest)
        return await self.operation(memory_id, operation_id)

    async def _revoke_locked(self, memory_id: str, verification_id: str, operation_id: str, expected: int, reason: str, digest: str) -> None:
        row = await self.db.get(MemoryVerificationRecord, verification_id, populate_existing=True)
        if row is None or (row.memory_id, row.tenant_id) != (memory_id, self.tenant_id):
            raise RevisionNotFound("verification_record_not_found")
        head = await self.db.get(MemoryVerificationHead, (memory_id, row.entry_id), with_for_update=True, populate_existing=True)
        if head is None or (head.head_version, head.verification_id, row.state) != (expected, verification_id, "active") or head.head_version >= 2**63 - 1:
            raise RevisionConflict("verification_head_changed")
        row.state, row.revoked_at, row.revoked_by, row.revocation_reason = "revoked", datetime.now(UTC), self.actor_id, reason
        head.head_version += 1
        self.db.add(
            MemoryVerificationOperation(
                memory_id=memory_id,
                operation_id=operation_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                request_sha256=digest,
                kind="verification_revoke",
                verification_id=verification_id,
                head_version=head.head_version,
            )
        )

    async def _view(self, memory_id: str, verification_id: str, *, physical_budget: list[int] | None = None) -> dict[str, Any]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            row = await self.db.get(MemoryVerificationRecord, verification_id, populate_existing=True)
            if row is None or (row.memory_id, row.tenant_id) != (memory_id, self.tenant_id):
                raise RevisionNotFound("verification_record_not_found")
            record = {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
            head = await self.db.get(MemoryVerificationHead, (memory_id, row.entry_id), populate_existing=True)
            current_id, version = (head.verification_id, head.head_version) if head else (None, 0)
            refs = list(
                await self.db.scalars(
                    select(MemoryVerificationSource).where(MemoryVerificationSource.verification_id == verification_id).order_by(MemoryVerificationSource.source_id).limit(MAX_SOURCES + 1)
                )
            )
            if not 1 <= len(refs) <= MAX_SOURCES:
                raise RevisionConflict("verification_source_binding_incomplete")
            binding = {
                "entry": record["entry_binding"],
                "sources": [{name: deepcopy(getattr(ref, name)) for name in ("source_id", "source_revision", "source_generation", "content_sha256", "scope", "projection_sha256")} for ref in refs],
            }
            states = list(
                await self.db.scalars(
                    select(MemorySourceState).where(
                        MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.tenant_id, MemorySourceState.source_message_id.in_([ref.source_id for ref in refs])
                    )
                )
            )
            entry = await self.db.get(MemoryEntry, row.entry_id, populate_existing=True)
            redacted = row.state == "redacted" or len(states) != len(refs) or any(source.state in {"forgotten", "deleted"} for source in states) or entry is None or entry.state == "withdrawn"
        finally:
            await release_sql(self.db)
        current = current_id == verification_id
        reasons: list[str] = []
        can_record, fresh = False, None
        if redacted:
            record["state"] = "redacted"
            effective, reasons = "redacted", ["record_redacted"]
        elif not current:
            effective, reasons = "superseded", ["record_superseded"]
        else:
            try:
                fresh, _ = await self._snapshot(memory_id, record["entry_id"], physical_budget=physical_budget)
                can_record = True
            except (RevisionConflict, RevisionNotFound) as error:
                reasons = ["projection_changed" if "projection" in error.code else "source_unavailable" if "source" in error.code else "entry_unavailable"]
            if record["state"] == "revoked":
                effective, reasons = "revoked", [*reasons, "record_revoked"]
            elif fresh is None or request_hash(fresh) != record["binding_sha256"]:
                effective = "stale"
                if fresh is not None:
                    reasons = ["entry_changed"] if fresh["entry"] != binding["entry"] else ["source_changed"]
            else:
                effective = record["result"]
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            latest = await self.db.get(MemoryVerificationHead, (memory_id, record["entry_id"]), populate_existing=True)
            closing_record = (
                await self.db.execute(
                    select(MemoryVerificationRecord.state, MemoryVerificationRecord.revoked_at, MemoryVerificationRecord.revoked_by, MemoryVerificationRecord.revocation_reason).where(
                        MemoryVerificationRecord.verification_id == verification_id
                    )
                )
            ).one_or_none()
            if closing_record is None:
                raise RevisionNotFound("verification_record_not_found")
            record.update({key: getattr(closing_record, key) for key in ("state", "revoked_at", "revoked_by", "revocation_reason")})
            state = closing_record.state
            previous_version = version
            current = latest is not None and latest.verification_id == verification_id
            version = latest.head_version if latest is not None else 0
            if state == "redacted":
                record["state"] = "redacted"
                redacted, effective, reasons, can_record = True, "redacted", ["record_redacted"], False
            elif not current:
                effective, reasons, can_record = "superseded", ["record_superseded"], False
            elif state == "revoked":
                record["state"] = "revoked"
                effective, reasons = "revoked", ["record_revoked"]
            elif version != previous_version:
                effective, reasons, can_record = "stale", ["entry_changed"], False
            if current and fresh is not None and effective in {"human_confirmed", "disputed", "needs_review"}:
                try:
                    closing, _ = await self._metadata(memory_id, record["entry_id"])
                    if closing != _without_projection(fresh):
                        effective, reasons, can_record = "stale", ["source_changed"], False
                except (RevisionConflict, RevisionNotFound):
                    effective, reasons, can_record = "stale", ["entry_unavailable"], False
        finally:
            await release_sql(self.db)
        if redacted:
            record["state"] = "redacted"
        valid = current and effective in {"human_confirmed", "disputed", "needs_review"}
        body = {key: record[key] for key in ("result", "method", "checked_at_is_user_reported", "actor_id", "state", "revoked_by")}
        return {
            **body,
            "verification_id": verification_id,
            "memory_id": memory_id,
            "entry_id": record["entry_id"],
            "record_version": str(record["record_version"]),
            "head_version": str(version),
            "is_current": current,
            "binding_sha256": record["binding_sha256"],
            "binding": _public_binding(binding),
            "applicability": None if redacted else record["applicability"],
            "notes": None if redacted else record["notes"],
            "evidence": [] if redacted else record["evidence"],
            "revocation_reason": None if redacted else record["revocation_reason"],
            **{name: record[name].isoformat() if record[name] is not None else None for name in ("checked_at", "recorded_at", "revoked_at")},
            "effective_status": effective,
            "valid_for_current": valid,
            "review_required": current and effective in {"disputed", "needs_review", "stale", "revoked"},
            "stale_reasons": reasons,
            "can_actions": {"record": can_record, "revoke": current and not redacted and record["state"] == "active"},
            "snapshot_only": True,
        }

    async def history(self, memory_id: str, entry_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        identifier(entry_id)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RevisionValidationError("invalid_verification_page_limit")
        digest = request_hash({"kind": "verification_history", "memory_id": memory_id, "tenant_id": self.tenant_id, "actor_id": self.actor_id, "entry_id": entry_id, "limit": limit})
        after = cursor_decode(cursor, digest)
        if after is not None and (type(after) is not int or after <= 0):
            raise RevisionValidationError("invalid_verification_cursor")
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            query = select(MemoryVerificationRecord.verification_id, MemoryVerificationRecord.record_version).where(
                MemoryVerificationRecord.memory_id == memory_id, MemoryVerificationRecord.tenant_id == self.tenant_id, MemoryVerificationRecord.entry_id == entry_id
            )
            if after is not None:
                query = query.where(MemoryVerificationRecord.record_version < after)
            rows = (await self.db.execute(query.order_by(MemoryVerificationRecord.record_version.desc()).limit(limit + 1))).all()
            selected, more = [(row.verification_id, row.record_version) for row in rows[:limit]], len(rows) > limit
        finally:
            await release_sql(self.db)
        return {
            "records": [await self._view(memory_id, key) for key, _ in selected],
            "next_cursor": cursor_encode(digest, selected[-1][1]) if more and selected else None,
            "has_more": more,
            "snapshot_only": True,
        }

    async def todos(self, memory_id: str, *, limit: int = 10, cursor: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise RevisionValidationError("invalid_verification_page_limit")
        digest = request_hash({"kind": "verification_todos", "memory_id": memory_id, "tenant_id": self.tenant_id, "actor_id": self.actor_id, "limit": limit})
        after = cursor_decode(cursor, digest)
        if after is not None:
            if not isinstance(after, str):
                raise RevisionValidationError("invalid_verification_cursor")
            identifier(after)
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            query = select(MemoryVerificationHead.entry_id, MemoryVerificationHead.verification_id).where(
                MemoryVerificationHead.memory_id == memory_id, MemoryVerificationHead.tenant_id == self.tenant_id
            )
            if after is not None:
                query = query.where(MemoryVerificationHead.entry_id > after)
            rows = [(row.entry_id, row.verification_id) for row in (await self.db.execute(query.order_by(MemoryVerificationHead.entry_id).limit(TODO_SCAN_LIMIT + 1))).all()]
        finally:
            await release_sql(self.db)
        items, scanned, physical, last = [], 0, [TODO_PHYSICAL_LIMIT], after
        for entry_id, verification_id in rows[:TODO_SCAN_LIMIT]:
            try:
                view = await self._view(memory_id, verification_id, physical_budget=physical)
            except _ReadLimit:
                break
            scanned += 1
            last = entry_id
            if view["is_current"] and view["review_required"]:
                items.append(view)
            if len(items) == limit:
                break
        more = scanned < len(rows)
        return {
            "items": items,
            "next_cursor": cursor_encode(digest, last) if more and last is not None else None,
            "has_more": more,
            "scan_limited": more and len(items) < limit,
            "scanned_count": scanned,
            "inventory_scope": "latest_registered_verification_heads",
            "snapshot_only": True,
        }
