"""Same-transaction invalidation of isolated maintenance data and admitted work."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.domain import RevisionConflict


async def _publications(db: AsyncSession, memory_id: str, tenant_id: str, job_ids: list[str], *, permanent: bool, source_id: int | None, entry_id: str | None, bank: bool) -> dict[str, list[Any]]:
    operations = list(
        await db.scalars(
            select(MemoryPublicationOutbox.operation_id)
            .where(
                MemoryPublicationOutbox.memory_id == memory_id,
                MemoryPublicationOutbox.tenant_id == tenant_id,
                MemoryPublicationOutbox.payload["action"].as_string() == "dream_publish",
                or_(MemoryPublicationOutbox.payload["job_id"].as_string().in_(job_ids), MemoryPublicationOutbox.payload["request"]["job_id"].as_string().in_(job_ids)),
            )
            .order_by(MemoryPublicationOutbox.operation_id)
            .limit(1001)
        )
    )
    if len(operations) > 1000:
        raise RevisionConflict("maintenance_publication_inventory_incomplete")
    ids: set[int] = set()
    outboxes: list[str] = []
    active: list[str] = []
    for identity in operations:
        row = await db.get(MemoryWriterAdmission, (memory_id, identity), with_for_update=True, populate_existing=True)
        outbox = await db.scalar(
            select(MemoryPublicationOutbox)
            .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == identity)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if row is None or outbox is None or row.tenant_id != tenant_id:
            raise RevisionConflict("maintenance_publication_binding_missing")
        payload = deepcopy(outbox.payload)
        published = payload.get("published") is True
        affected = [item for item in payload.get("items", []) if bank or entry_id == item.get("entry_id") or any(ref.get("source_id") == source_id for ref in item.get("sources", []))]
        if not published:
            affected = list(payload.get("items", []))
            row.publication_state = "canceled"
            if row.active_token is None:
                row.state = "canceled"
            row.receipt = {**row.receipt, "cancellation_requested": True}
            ids.update(row.message_ids)
        elif permanent:
            ids.update(item["message_id"] for item in affected)
        if row.active_token is not None:
            active.append(identity)
        # Parent ownership remains a dependency until its actual RPCs settle.
        outboxes.append(outbox.id)
        manifest = payload.get("manifest")
        if isinstance(manifest, dict):
            manifest["entries"] = []
            for item in manifest.get("inputs", []):
                item["snapshot"] = {}
        if permanent or not published:
            for item in affected:
                item["message"] = {key: value for key, value in item.get("message", {}).items() if key not in {"content", "content_embed", "reason"}}
        outbox.payload = payload
        outbox.error_code = "source_withdrawn" if permanent else "source_state_changed"
        if not published:
            outbox.state = "redacted" if permanent else "stale"
        operation = await db.get(MemoryMutationOperation, (memory_id, identity))
        if operation is not None:
            operation.status, operation.index_sync_status, operation.error_code = "stale", "cleanup_pending", outbox.error_code
    return {"message_ids": sorted(ids), "outbox_ids": outboxes, "active_operation_ids": active}


async def _redact_jobs(
    db: AsyncSession, memory_id: str, tenant_id: str, job_ids: list[str], *, permanent: bool, source_id: int | None = None, entry_id: str | None = None, bank: bool = False
) -> dict[str, list[Any]]:
    jobs = list(
        await db.scalars(
            select(MemoryMaintenanceJob)
            .where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == tenant_id, MemoryMaintenanceJob.job_id.in_(job_ids))
            .order_by(MemoryMaintenanceJob.job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    )
    outbox_ids: list[str] = []
    active: list[str] = []
    for job in jobs:
        target_state = "redacted" if permanent else "stale"
        if job.state not in {target_state, "redacted"}:
            job.state, job.generation, job.updated_at = target_state, job.generation + 1, datetime.now(UTC)
        job.entry_snapshots = []
        job.error_code = "source_withdrawn" if permanent else "source_state_changed"
        job.receipt = {key: value for key, value in job.receipt.items() if key in {"commands", "analysis_attempts"}}
        from memory.maintenance.execution import cancel_execution

        await cancel_execution(db, memory_id=memory_id, job_id=job.job_id, reason=job.error_code)
        inputs = list(await db.scalars(select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job.job_id).order_by(MemoryMaintenanceInput.input_id).with_for_update()))
        for item in inputs:
            item.snapshot = {}
            item.coverage, item.defer_reason = "redacted" if permanent else "stale", job.error_code
            if permanent:
                item.scope = {}
        candidates = list(
            await db.scalars(select(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.job_id == job.job_id).order_by(MemoryMaintenanceCandidate.candidate_id).with_for_update())
        )
        for candidate in candidates:
            if candidate.state != target_state:
                candidate.state, candidate.revision = target_state, candidate.revision + 1
            candidate.content = candidate.base_content = candidate.reason = candidate.diff = None
            candidate.patch = None
            candidate.sources = [
                {key: value for key, value in source.items() if key in {"input_id", "source_id", "source_revision", "source_generation", "content_sha256"}} for source in candidate.sources
            ]
            candidate.assessment = {"kind": "hypothesis", "verification_status": "unverified"}
            candidate.receipt = {key: value for key, value in candidate.receipt.items() if key in {"published_entry_id", "published_revision", "message_id"}}
        if job.analysis_operation_id is None:
            continue
        admission = await db.get(MemoryWriterAdmission, (memory_id, job.analysis_operation_id), with_for_update=True, populate_existing=True)
        if admission is None or admission.tenant_id != tenant_id:
            continue
        admission.publication_state = "canceled"
        if admission.active_token is None:
            admission.state = "canceled"
        else:
            active.append(admission.operation_id)
        admission.receipt = {**admission.receipt, "cancellation_requested": True}
        outbox = await db.scalar(
            select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == admission.operation_id).with_for_update()
        )
        if outbox is not None:
            outbox.state, outbox.error_code = "redacted" if permanent else "stale", job.error_code
            outbox.payload = {key: value for key, value in outbox.payload.items() if key in {"action", "reserved_bytes", "reservation_owner", "published"}}
            outbox_ids.append(outbox.id)
        operation = await db.get(MemoryMutationOperation, (memory_id, admission.operation_id))
        if operation is not None:
            operation.status, operation.index_sync_status, operation.error_code = "stale", "cleanup_pending", job.error_code
            operation.result = {}
    publication = await _publications(db, memory_id, tenant_id, job_ids, permanent=permanent, source_id=source_id, entry_id=entry_id, bank=bank)
    return {
        "message_ids": publication["message_ids"],
        "outbox_ids": list(dict.fromkeys([*outbox_ids, *publication["outbox_ids"]])),
        "active_operation_ids": list(dict.fromkeys([*active, *publication["active_operation_ids"]])),
    }


async def invalidate_for_source(db: AsyncSession, *, memory_id: str, tenant_id: str, source_id: int, permanent: bool) -> dict[str, list[Any]]:
    """Caller holds bank/source locks; include even a nonanchor analysis source."""
    job_ids = sorted(
        set(
            await db.scalars(
                select(MemoryMaintenanceInput.job_id)
                .where(MemoryMaintenanceInput.memory_id == memory_id, MemoryMaintenanceInput.tenant_id == tenant_id, MemoryMaintenanceInput.source_id == source_id)
                .limit(1001)
            )
        )
    )
    if len(job_ids) > 1000:
        raise RevisionConflict("maintenance_invalidation_inventory_incomplete")
    return await _redact_jobs(db, memory_id, tenant_id, job_ids, permanent=permanent, source_id=source_id)


async def invalidate_for_bank(db: AsyncSession, *, memory_id: str, tenant_id: str) -> None:
    """Deletion has already closed the common writer fence before this callback."""
    job_ids = list(
        await db.scalars(
            select(MemoryMaintenanceJob.job_id).where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == tenant_id).order_by(MemoryMaintenanceJob.job_id).limit(1001)
        )
    )
    if len(job_ids) > 1000:
        raise RevisionConflict("maintenance_invalidation_inventory_incomplete")
    await _redact_jobs(db, memory_id, tenant_id, job_ids, permanent=True, bank=True)


async def invalidate_for_entry(db: AsyncSession, *, memory_id: str, tenant_id: str, entry_id: str, permanent: bool) -> dict[str, list[Any]]:
    selected = set(
        await db.scalars(
            select(MemoryMaintenanceJob.job_id)
            .where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == tenant_id, MemoryMaintenanceJob.entry_ids.contains([entry_id]))
            .limit(1001)
        )
    )
    selected.update(
        await db.scalars(
            select(MemoryMaintenanceCandidate.job_id)
            .where(
                MemoryMaintenanceCandidate.memory_id == memory_id,
                MemoryMaintenanceCandidate.tenant_id == tenant_id,
                (MemoryMaintenanceCandidate.target_entry_id == entry_id) | (MemoryMaintenanceCandidate.receipt["published_entry_id"].as_string() == entry_id),
            )
            .limit(1001)
        )
    )
    if len(selected) > 1000:
        raise RevisionConflict("maintenance_invalidation_inventory_incomplete")
    return await _redact_jobs(db, memory_id, tenant_id, sorted(selected), permanent=permanent, entry_id=entry_id)
