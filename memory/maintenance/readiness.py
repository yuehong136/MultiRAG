"""Shared publication readiness for single and bounded batch readers."""

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission

EntryRevisionPair = tuple[MemoryEntry, MemoryEntryRevision]


def revision_is_ready(revision: MemoryEntryRevision, *, published: bool, admission_count: int) -> bool:
    if revision.origin == "consolidated":
        return published and admission_count == 1
    return revision.parent_revision is None or published


async def ready_entry_pairs(db: AsyncSession, tenant_id: str, pairs: list[EntryRevisionPair]) -> set[tuple[str, int]]:
    """Keep readiness decisions shared without fetching unbounded JSON ledgers."""
    if not pairs:
        return set()
    keys = [(entry.entry_id, revision.revision) for entry, revision in pairs]
    needs_outbox = [key for key, (_, revision) in zip(keys, pairs, strict=True) if revision.origin == "consolidated" or revision.parent_revision is not None]
    published = (
        set(
            (
                await db.execute(
                    select(MemoryPublicationOutbox.entry_id, MemoryPublicationOutbox.revision)
                    .where(tuple_(MemoryPublicationOutbox.entry_id, MemoryPublicationOutbox.revision).in_(needs_outbox), MemoryPublicationOutbox.state == "ready")
                    .distinct()
                )
            ).all()
        )
        if needs_outbox
        else set()
    )
    consolidated = [key for key, (_, revision) in zip(keys, pairs, strict=True) if revision.origin == "consolidated"]
    counts: dict[tuple[str, int], int] = {}
    if consolidated:
        count = (
            select(func.count())
            .select_from(MemoryWriterAdmission)
            .where(
                MemoryWriterAdmission.memory_id == MemoryEntryRevision.memory_id,
                MemoryWriterAdmission.tenant_id == tenant_id,
                MemoryWriterAdmission.kind == "dream_publish",
                MemoryWriterAdmission.publication_state == "ready",
                MemoryWriterAdmission.message_ids.contains(func.jsonb_build_array(MemoryEntryRevision.message_id)),
            )
            .correlate(MemoryEntryRevision)
            .scalar_subquery()
        )
        rows = await db.execute(select(MemoryEntryRevision.entry_id, MemoryEntryRevision.revision, count).where(tuple_(MemoryEntryRevision.entry_id, MemoryEntryRevision.revision).in_(consolidated)))
        counts = {(entry_id, revision): int(total) for entry_id, revision, total in rows}
    return {
        key
        for key, (entry, revision) in zip(keys, pairs, strict=True)
        if entry.state == "active" and revision.content is not None and revision_is_ready(revision, published=key in published, admission_count=counts.get(key, 0))
    }
