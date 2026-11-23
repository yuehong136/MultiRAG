"""Invalidate every root dependency without declaring in-flight IO drained."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.compaction.domain import MAX_ARTIFACTS
from memory.revisions.domain import RevisionConflict


async def invalidate(db: AsyncSession, *, memory_id: str, tenant_id: str, source_id: int | None = None, permanent: bool = True) -> dict[str, list[Any]]:
    """Caller owns the existing Memory/fence/source lock order and transaction."""
    query = select(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == memory_id, MemoryCompactionArtifact.tenant_id == tenant_id, MemoryCompactionArtifact.retired_at.is_(None))
    if source_id is not None:
        query = query.where(
            MemoryCompactionArtifact.artifact_id.in_(
                select(MemoryCompactionRoot.artifact_id).where(MemoryCompactionRoot.memory_id == memory_id, MemoryCompactionRoot.tenant_id == tenant_id, MemoryCompactionRoot.source_id == source_id)
            )
        )
    artifacts = list(await db.scalars(query.order_by(MemoryCompactionArtifact.artifact_id).limit(MAX_ARTIFACTS + 1).with_for_update()))
    if len(artifacts) > MAX_ARTIFACTS:
        raise RevisionConflict("compaction_invalidation_inventory_incomplete")
    active, outboxes = [], []
    for artifact in artifacts:
        artifact.state, artifact.package, artifact.cancellation_requested, artifact.updated_at = "stale", None, True, datetime.now(UTC)
        artifact.redacted = artifact.redacted or permanent
        artifact.error_code = "compaction_source_withdrawn" if source_id is not None else "memory_deletion_requested"
        row = await db.get(MemoryWriterAdmission, (memory_id, artifact.operation_id), with_for_update=True)
        if row is not None:
            row.publication_state = "canceled"
            if row.active_token is not None or row.state in {"active", "unknown"}:
                active.append(row.operation_id)
            else:
                row.state = "canceled"
        operation = await db.get(MemoryMutationOperation, (memory_id, artifact.operation_id), with_for_update=True)
        if operation is not None:
            operation.status, operation.index_sync_status, operation.error_code = "stale", "ready", artifact.error_code
            operation.result = {"artifact_id": artifact.artifact_id, "generated_only": True}
        outbox = await db.scalar(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == artifact.operation_id).with_for_update())
        if outbox is not None:
            outbox.state, outbox.error_code = "redacted" if permanent else "stale", artifact.error_code
            outbox.payload = {"action": "context_compact", "reserved_bytes": 0}
            outboxes.append(outbox.id)
    return {"message_ids": [], "outbox_ids": outboxes, "active_operation_ids": active}
