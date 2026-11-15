"""Explicit operator attestations for attempts whose process has disappeared.

These functions perform no remote IO. The operator must independently establish
that every process and remote request for the selected attempt has settled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, positive_int, validate_operation_id


async def acknowledge_attempt_quiescence(
    db: AsyncSession,
    *,
    memory_id: str,
    tenant_id: str,
    operation_id: str,
    expected_generation: int,
    expected_attempt: int,
    expected_active_token: str,
    evidence_reference: str,
) -> dict[str, Any]:
    """Drain exactly one attested attempt; retain its publication for reconciliation."""
    validate_operation_id(operation_id)
    positive_int(expected_generation, "invalid_writer_generation")
    positive_int(expected_attempt, "invalid_writer_attempt")
    if not isinstance(expected_active_token, str) or len(expected_active_token) != 32:
        raise RevisionValidationError("writer_attempt_token_required")
    if not isinstance(evidence_reference, str) or not evidence_reference.strip() or len(evidence_reference.encode("utf-8")) > 2048:
        raise RevisionValidationError("operator_evidence_required")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Operator attestation requires a clean session")
    if db.in_transaction():
        await db.rollback()
    try:
        await db.begin()
        if await db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == tenant_id).with_for_update()) is None:
            raise RevisionNotFound("memory_not_found")
        fence = await db.scalar(select(MemoryWriterFence).where(MemoryWriterFence.memory_id == memory_id, MemoryWriterFence.tenant_id == tenant_id).with_for_update())
        if fence is None or fence.generation != expected_generation or fence.state not in {"open", "closing"}:
            raise RevisionConflict("operator_attestation_generation_changed")
        await db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == tenant_id).with_for_update())
        admission = await db.scalar(
            select(MemoryWriterAdmission)
            .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == tenant_id, MemoryWriterAdmission.operation_id == operation_id)
            .with_for_update()
        )
        if admission is None:
            raise RevisionNotFound("writer_operation_not_found")
        previous = admission.receipt.get("operator_quiescence_attestations", [])
        attested = next((item for item in previous if item.get("active_token") == expected_active_token and item.get("attempt") == expected_attempt), None)
        if attested is not None and admission.active_token is None and admission.attempt == expected_attempt:
            result = {"operation_id": operation_id, "attempt": expected_attempt, "verification": "operator_attestation", "publication_state": admission.publication_state}
            await db.rollback()
            return result
        if admission.active_token != expected_active_token or admission.attempt != expected_attempt:
            raise RevisionConflict("operator_attestation_attempt_changed")
        outbox = await db.scalar(
            select(MemoryPublicationOutbox)
            .where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.tenant_id == tenant_id, MemoryPublicationOutbox.operation_id == operation_id)
            .with_for_update()
        )
        if outbox is None:
            raise RevisionNotFound("writer_publication_not_found")
        attestation = {
            "active_token": expected_active_token,
            "attempt": expected_attempt,
            "generation": expected_generation,
            "evidence_reference": evidence_reference,
            "acknowledged_at": datetime.now(UTC).isoformat(),
            "verification": "operator_attestation",
        }
        admission.active_token, admission.finished_at = None, datetime.now(UTC)
        admission.state = "canceled" if admission.publication_state == "canceled" else "drained"
        admission.receipt = {
            **admission.receipt,
            "finished_tokens": [*admission.receipt.get("finished_tokens", []), expected_active_token],
            "outcome": "operator_attested_quiescence",
            "operator_quiescence_attestations": [*previous, attestation],
        }
        outbox.receipt = {
            **outbox.receipt,
            "active_attempts": [value for value in outbox.receipt.get("active_attempts", []) if value != expected_active_token],
            "finished_attempts": [*outbox.receipt.get("finished_attempts", []), expected_active_token],
        }
        result = {"operation_id": operation_id, "attempt": expected_attempt, "verification": "operator_attestation", "publication_state": admission.publication_state}
        await db.flush()
        await finish_owned_work(db.commit())
        return result
    except DBAPIError as error:
        await finish_owned_work(db.rollback())
        raise RevisionPersistenceUnknown(operation_id) from error
    except BaseException:
        await finish_owned_work(db.rollback())
        raise
