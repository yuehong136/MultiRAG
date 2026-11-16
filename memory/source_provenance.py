"""Read immutable submission receipts from existing writer outboxes, without writes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.domain import RevisionConflict, request_hash
from memory.structured_sources import SOURCE_FORMAT, submission_metadata


async def source_receipts(db: AsyncSession, *, memory_id: str, tenant_id: str, source_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not source_ids:
        return {}
    rows = (
        await db.execute(
            select(
                MemoryWriterAdmission.source_message_id,
                MemoryWriterAdmission.actor_id,
                MemoryWriterAdmission.operation_id,
                MemoryWriterAdmission.publication_state,
                MemoryWriterAdmission.request_metadata,
                MemoryPublicationOutbox.payload["server_metadata"].label("server_metadata"),
                MemoryPublicationOutbox.payload["request"]["source_format"].as_string().label("source_format"),
                MemoryPublicationOutbox.tenant_id,
            )
            .outerjoin(MemoryPublicationOutbox, (MemoryPublicationOutbox.memory_id == MemoryWriterAdmission.memory_id) & (MemoryPublicationOutbox.operation_id == MemoryWriterAdmission.operation_id))
            .where(
                MemoryWriterAdmission.memory_id == memory_id,
                MemoryWriterAdmission.tenant_id == tenant_id,
                MemoryWriterAdmission.kind == "append_raw",
                MemoryWriterAdmission.source_message_id.in_(source_ids),
            )
            .limit(len(set(source_ids)) + 1)
        )
    ).all()
    result: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    for row in rows:
        if row.source_message_id in seen:
            raise RevisionConflict("source_receipt_binding_ambiguous")
        seen.add(row.source_message_id)
        metadata = row.server_metadata or {}
        marker = row.request_metadata.get("source_format")
        if marker is None and not metadata.get("source_format") and row.source_format is None:
            continue
        if marker != SOURCE_FORMAT or metadata.get("source_format") != SOURCE_FORMAT:
            raise RevisionConflict("structured_source_receipt_invalid")
        if row.tenant_id != tenant_id:
            raise RevisionConflict("source_receipt_tenant_mismatch")
        if row.publication_state != "ready":
            raise RevisionConflict("source_receipt_not_ready")
        if row.request_metadata.get("server_metadata_sha256") != request_hash(metadata) or metadata.get("submitted_by") != row.actor_id or metadata.get("submission_operation_id") != row.operation_id:
            raise RevisionConflict("structured_source_receipt_invalid")
        if row.source_format != SOURCE_FORMAT:
            raise RevisionConflict("structured_source_receipt_invalid")
        if not isinstance(metadata.get("received_at"), str) or not isinstance(metadata.get("valid_at"), str):
            raise RevisionConflict("structured_source_receipt_invalid")
        result[row.source_message_id] = submission_metadata(
            actor_id=row.actor_id, operation_id=row.operation_id, content_sha256=metadata["envelope_sha256"], received_at=metadata["received_at"], reference_time=metadata["valid_at"]
        )
    return result


def bind_submission_receipts(binding: dict[str, Any], receipts: dict[int, dict[str, Any]]) -> None:
    """Persist only immutable receipt digests in the existing entry-binding JSON."""
    pins = {str(source["source_id"]): receipts[source["source_id"]]["provenance_sha256"] for source in binding["sources"] if source["source_id"] in receipts}
    if pins:
        binding["entry"]["source_provenance_sha256"] = pins
