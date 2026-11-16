"""Bounded SQL-only read phases for startup, with no cross-request cache."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import aliased

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_verification_models import MemoryVerificationHead, MemoryVerificationRecord, MemoryVerificationSource
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, message_scope
from memory.verification.domain import MAX_SOURCES
from memory.verification.service import VerificationService


@dataclass
class StartupEntryRead:
    binding: dict[str, Any] = field(default_factory=dict)
    content: str = ""
    projection: dict[str, Any] = field(default_factory=dict)
    assessment: dict[str, Any] = field(default_factory=dict)
    head: tuple[int, str | None] = (0, None)
    record: dict[str, Any] | None = None
    stored_binding: dict[str, Any] | None = None
    record_redacted: bool = False
    error: RevisionConflict | RevisionNotFound | RevisionValidationError | None = None


async def load_startup_entries(service: VerificationService, memory_id: str, entry_ids: list[str], *, closing: bool = False) -> dict[str, StartupEntryRead]:
    """Load each bounded closure completely or fail that entry, never truncate it."""
    db, tenant_id = service.db, service.tenant_id
    if len(entry_ids) > 128 or len(set(entry_ids)) != len(entry_ids):
        raise RevisionValidationError("invalid_startup_entry_selection")
    if closing:
        await db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == tenant_id).with_for_update(read=True))
    await service.authorize(db, memory_id)
    bank_query = select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id)
    bank = await db.scalar(bank_query.with_for_update(read=True) if closing else bank_query)
    if closing and ((bank is not None and bank != "active") or (entry_ids and bank is None)):
        raise RevisionConflict("startup_inventory_changed")
    result = {identity: StartupEntryRead() for identity in entry_ids}
    if not entry_ids:
        return result
    if bank != "active":
        for item in result.values():
            item.error = RevisionConflict("verification_memory_unavailable")
        return result
    rows = list(
        await db.execute(
            select(MemoryEntry, MemoryEntryRevision)
            .outerjoin(MemoryEntryRevision, (MemoryEntryRevision.entry_id == MemoryEntry.entry_id) & (MemoryEntryRevision.revision == MemoryEntry.current_revision))
            .where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == tenant_id, MemoryEntry.entry_id.in_(entry_ids))
            .execution_options(populate_existing=True)
        )
    )
    pairs = []
    for entry, revision in rows:
        item = result[entry.entry_id]
        if revision is None or entry.state != "active" or revision.content is None:
            item.error = RevisionConflict("verification_entry_unavailable")
        else:
            pairs.append((entry, revision))
    seen = {entry.entry_id for entry, _ in rows}
    for identity in set(entry_ids) - seen:
        result[identity].error = RevisionNotFound("entry_not_found")
    if service.ready_entries is not None:
        ready = await service.ready_entries(pairs)
    else:
        ready = set()
        for entry, revision in pairs:
            try:
                await service.ready_entry(entry, revision)
                ready.add((entry.entry_id, revision.revision))
            except RevisionConflict as error:
                result[entry.entry_id].error = error
    keys = [(entry.entry_id, revision.revision) for entry, revision in pairs]
    references: dict[str, list[MemoryRevisionSource]] = defaultdict(list)
    if keys:
        ranked = (
            select(
                MemoryRevisionSource,
                func.row_number().over(partition_by=(MemoryRevisionSource.entry_id, MemoryRevisionSource.revision), order_by=MemoryRevisionSource.source_message_id).label("position"),
            )
            .where(tuple_(MemoryRevisionSource.entry_id, MemoryRevisionSource.revision).in_(keys))
            .subquery()
        )
        ref_alias = aliased(MemoryRevisionSource, ranked)
        for ref in await db.scalars(select(ref_alias).where(ranked.c.position <= MAX_SOURCES + 1).order_by(ref_alias.entry_id, ref_alias.source_message_id).execution_options(populate_existing=True)):
            references[ref.entry_id].append(ref)
    heads = {
        row.entry_id: row
        for row in await db.scalars(
            select(MemoryVerificationHead).where(MemoryVerificationHead.memory_id == memory_id, MemoryVerificationHead.entry_id.in_(entry_ids)).execution_options(populate_existing=True)
        )
    }
    record_ids = [head.verification_id for head in heads.values() if head.verification_id is not None]
    records = (
        {
            row.verification_id: row
            for row in await db.scalars(select(MemoryVerificationRecord).where(MemoryVerificationRecord.verification_id.in_(record_ids)).execution_options(populate_existing=True))
        }
        if record_ids
        else {}
    )
    recorded_refs: dict[str, list[MemoryVerificationSource]] = defaultdict(list)
    if record_ids:
        ranked = (
            select(MemoryVerificationSource, func.row_number().over(partition_by=MemoryVerificationSource.verification_id, order_by=MemoryVerificationSource.source_id).label("position"))
            .where(MemoryVerificationSource.verification_id.in_(record_ids))
            .subquery()
        )
        verification_alias = aliased(MemoryVerificationSource, ranked)
        for verification_ref in await db.scalars(
            select(verification_alias).where(ranked.c.position <= MAX_SOURCES + 1).order_by(verification_alias.verification_id, verification_alias.source_id).execution_options(populate_existing=True)
        ):
            recorded_refs[verification_ref.verification_id].append(verification_ref)
    source_ids = {ref.source_message_id for refs in references.values() for ref in refs} | {ref.source_id for refs in recorded_refs.values() for ref in refs}
    states = (
        {
            row.source_message_id: row
            for row in await db.scalars(
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == tenant_id, MemorySourceState.source_message_id.in_(source_ids))
                .execution_options(populate_existing=True)
            )
        }
        if source_ids
        else {}
    )
    from memory.source_provenance import bind_submission_receipts, source_receipts

    receipts = await source_receipts(db, memory_id=memory_id, tenant_id=tenant_id, source_ids=sorted(identity for identity, source in states.items() if source.state == "active"))
    for entry, revision in pairs:
        item, refs = result[entry.entry_id], references[entry.entry_id]
        if (entry.entry_id, revision.revision) not in ready:
            item.error = item.error or RevisionConflict("dreaming_target_not_ready")
            continue
        if not 1 <= len(refs) <= MAX_SOURCES:
            item.error = RevisionValidationError("verification_source_closure_limit")
            continue
        scope, sources = message_scope(revision.message_snapshot, memory_id), []
        for ref in refs:
            source = states.get(ref.source_message_id)
            if (
                source is None
                or ref.memory_id != memory_id
                or source.state != "active"
                or (source.source_revision, source.content_sha256) != (ref.source_revision, ref.content_sha256)
                or any(source.scope.get(key) != scope[key] for key in ("user_id", "agent_id"))
                or (ref.locator.get("scope") is not None and ref.locator["scope"] != source.scope)
            ):
                item.error = RevisionConflict("verification_source_unavailable")
                break
            sources.append(
                {
                    "source_id": source.source_message_id,
                    "source_revision": source.source_revision,
                    "source_generation": source.generation,
                    "content_sha256": source.content_sha256,
                    "scope": deepcopy(source.scope),
                }
            )
        if item.error is not None:
            continue
        item.binding = {
            "entry": {
                "entry_id": entry.entry_id,
                "revision": entry.current_revision,
                "message_id": entry.current_message_id,
                "generation": entry.generation,
                "content_sha256": revision.content_sha256,
                "scope": scope,
            },
            "sources": sources,
        }
        bind_submission_receipts(item.binding, receipts)
        item.content, item.projection, item.assessment = revision.content, deepcopy(revision.message_snapshot), deepcopy(revision.assessment)
        head = heads.get(entry.entry_id)
        item.head = (head.head_version, head.verification_id) if head is not None else (0, None)
        record = records.get(item.head[1]) if item.head[1] is not None else None
        if record is None:
            continue
        if (record.memory_id, record.tenant_id, record.entry_id) != (memory_id, tenant_id, entry.entry_id):
            item.error = RevisionNotFound("verification_record_not_found")
            continue
        item.record = {column.name: deepcopy(getattr(record, column.name)) for column in record.__table__.columns}
        refs_record = recorded_refs[record.verification_id]
        if not 1 <= len(refs_record) <= MAX_SOURCES:
            item.error = RevisionConflict("verification_source_binding_incomplete")
            continue
        item.stored_binding = {
            "entry": deepcopy(record.entry_binding),
            "sources": [{name: deepcopy(getattr(ref, name)) for name in ("source_id", "source_revision", "source_generation", "content_sha256", "scope", "projection_sha256")} for ref in refs_record],
        }
        item.record_redacted = record.state == "redacted" or any(ref.source_id not in states or states[ref.source_id].state in {"forgotten", "deleted"} for ref in refs_record)
    return result
