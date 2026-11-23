"""Authenticated structured submission and fresh, read-only provenance views."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_context_service import require_memory
from api.apps.services.memory_revision_service import _authorize
from api.db.db_models import Memory
from api.db.joint_services.memory_write_runtime import service_for
from api.db.memory_revision_models import ManagedMemoryState, MemorySourceState
from common.asyncio_utils import finish_owned_work
from common.time_utils import timestamp_to_date
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, is_active, message_scope
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService
from memory.source_provenance import source_receipts
from memory.structured_sources import canonical_source, evidence_snapshot, parse_registered_source, source_envelope
from memory.writes.providers import release_sql


async def append_source(
    db: AsyncSession, memory_id: str, *, actor_id: str, operation_id: str, segments: list[dict[str, str]], event_time: str | None, user_id: str, agent_id: str, session_id: str
) -> dict[str, Any]:
    envelope = source_envelope(segments, event_time)
    scope = {"user_id": user_id, "agent_id": agent_id, "session_id": session_id}
    if any(not isinstance(value, str) or len(value.encode("utf-8")) > 256 for value in scope.values()):
        raise RevisionValidationError("invalid_append_identity")
    service = await service_for(db, memory_id, actor_id=actor_id)
    instant = datetime.now(UTC)
    return await service.append_raw(
        memory_id,
        operation_id=operation_id,
        content=canonical_source(envelope),
        scope=scope,
        valid_at=timestamp_to_date(int(instant.timestamp() * 1000)),
        enqueue_extraction=False,
        structured_received_at=instant.isoformat(),
    )


async def _metadata(db: AsyncSession, memory_id: str, source_id: int, actor_id: str, *, lock: bool = False) -> dict[str, Any]:
    """The closing transaction holds Memory then source, never a physical RPC."""
    try:
        binding = await require_memory(db, memory_id, actor_id=actor_id)
        if lock:
            if await db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == binding.tenant_id).with_for_update()) is None:
                raise RevisionNotFound("source_not_available")
            await _authorize(db, memory_id, tenant_id=binding.tenant_id, actor_id=actor_id)
        bank_query = select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == binding.tenant_id).execution_options(populate_existing=True)
        bank = await db.scalar(bank_query.with_for_update(read=True) if lock else bank_query)
        if bank is not None and bank.state != "active":
            raise RevisionNotFound("source_not_available")
        query = select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source_id).execution_options(populate_existing=True)
        state = await db.scalar(query.with_for_update() if lock else query)
        if state is not None and (state.tenant_id != binding.tenant_id or state.state != "active"):
            raise RevisionNotFound("source_not_available")
        receipts = await source_receipts(db, memory_id=memory_id, tenant_id=binding.tenant_id, source_ids=[source_id])
        return {
            "tenant_id": binding.tenant_id,
            "state": None if state is None else {"source_revision": state.source_revision, "generation": state.generation, "content_sha256": state.content_sha256, "scope": deepcopy(state.scope)},
            "receipt": receipts.get(source_id),
        }
    finally:
        await finish_owned_work(release_sql(db))


async def get_provenance(
    db: AsyncSession,
    memory_id: str,
    source_id: int,
    *,
    actor_id: str,
    include_segments: bool = False,
    expected_source_revision: int | None = None,
    expected_source_generation: int | None = None,
    content_sha256: str | None = None,
    expected_provenance_sha256: str | None = None,
    user_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if type(include_segments) is not bool:
        raise RevisionValidationError("invalid_provenance_options")
    scope = {"user_id": user_id, "agent_id": agent_id, "session_id": session_id}
    if any(value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 256) for value in scope.values()):
        raise RevisionValidationError("invalid_source_scope")
    bound = expected_source_revision is not None or expected_source_generation is not None or expected_provenance_sha256 is not None
    if bound and (expected_source_revision is None or expected_source_generation is None or content_sha256 is None or any(value is None for value in scope.values())):
        raise RevisionValidationError("source_provenance_preconditions_incomplete")
    before = await _metadata(db, memory_id, source_id, actor_id)
    capabilities = await finish_owned_work(asyncio.to_thread(MessageService.get_recent_read_capabilities, tenant_id=before["tenant_id"], memory_id=memory_id))
    if capabilities.get("managed_revisions_supported") is not True:
        raise RevisionValidationError("managed_backend_unsupported")
    physical = await ManagedMessageStore(before["tenant_id"], memory_id).read(source_id)
    if (
        physical is None
        or physical.get("message_type") != "raw"
        or physical.get("source_id") not in (None, 0, "0", "")
        or isinstance(physical.get("source_id"), bool)
        or not is_active(physical)
        or not isinstance(physical.get("content"), str)
    ):
        raise RevisionNotFound("source_not_available")
    actual_scope, digest = message_scope(physical, memory_id), content_hash(physical["content"])
    if any(wanted is not None and actual_scope[key] != wanted for key, wanted in scope.items()):
        raise RevisionNotFound("source_not_available")
    state = before["state"]
    if state is not None and (state["content_sha256"] != digest or state["scope"] != actual_scope):
        raise RevisionConflict("source_provenance_changed")
    if (content_sha256 is not None and digest != content_sha256) or (
        bound and (state is None or state["source_revision"] != expected_source_revision or state["generation"] != expected_source_generation)
    ):
        raise RevisionConflict("source_provenance_changed")
    receipt = before["receipt"]
    if expected_provenance_sha256 is not None and (receipt is None or receipt["provenance_sha256"] != expected_provenance_sha256):
        raise RevisionConflict("source_provenance_changed")
    result: dict[str, Any] = {
        "memory_id": memory_id,
        "source_id": source_id,
        "state": "available",
        "snapshot_only": True,
        "format": "legacy_combined",
        "role_status": "role_unknown",
        "source_revision": str(state["source_revision"]) if state is not None else None,
        "source_generation": str(state["generation"]) if state is not None else None,
        "content_sha256": digest,
        "scope": actual_scope,
        "received_at": None,
        "reference_time": physical.get("valid_at"),
        "event_time": {"value": None, "status": "unknown"},
        "provenance_sha256": None,
        "envelope_sha256": None,
        "provenance": {"kind": "legacy_unknown", "submitted_by": None, "operation_id": None, "authenticated_submitter": False, "speaker_identity_verified": False, "content_verified": False},
        "segments": None,
        "segments_included": False,
        "segment_count": None,
        "declared_roles": [],
    }
    if receipt is not None:
        if state is None or physical.get("valid_at") != receipt["reference_time"]:
            raise RevisionConflict("structured_source_receipt_invalid")
        evidence_snapshot(physical["content"], receipt)
        envelope = parse_registered_source(physical["content"])
        result.update(
            {
                **deepcopy(receipt),
                "format": "structured",
                "event_time": {"value": envelope["event_time"], "status": "unknown" if envelope["event_time"] is None else "caller_declared"},
                "segments": deepcopy(envelope["segments"]) if include_segments else None,
                "segments_included": include_segments,
                "segment_count": len(envelope["segments"]),
                "declared_roles": list(dict.fromkeys(item["role"] for item in envelope["segments"])),
            }
        )
    after = await _metadata(db, memory_id, source_id, actor_id, lock=True)
    if before != after:
        raise RevisionConflict("source_provenance_changed")
    return result
