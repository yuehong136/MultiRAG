"""Explicit SQL-only retirement with a permanent generation receipt."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.orm.attributes import flag_modified

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from memory.compaction.domain import MAX_ARTIFACTS, canonical
from memory.compaction.history import drained_compaction_attempt, quiet_compaction_attempt
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, request_hash
from memory.writes.admission import WriterCoordinator

STATES = ("running", "generated", "failed", "stale", "canceled", "persistence_unknown")
TERMINAL = ("generated", "failed", "stale", "canceled")


@dataclass
class RetentionSnapshot:
    digest: str
    source_ids: list[str]
    package_sha256: str | None
    blockers: list[str]
    writer_busy: bool
    writer: MemoryWriterAdmission | None
    publication: MemoryPublicationOutbox | None
    operation: MemoryMutationOperation | None


def utc_timestamp(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


class RetentionService(WriterCoordinator):
    async def get(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def _retention_artifact(self, memory_id: str, artifact_id: str, *, lock: bool = False) -> MemoryCompactionArtifact:
        if not re.fullmatch(r"[a-f0-9]{32}", artifact_id):
            raise RevisionValidationError("invalid_compaction_artifact_id")
        query = (
            select(MemoryCompactionArtifact)
            .where(MemoryCompactionArtifact.memory_id == memory_id, MemoryCompactionArtifact.tenant_id == self.tenant_id, MemoryCompactionArtifact.artifact_id == artifact_id)
            .execution_options(populate_existing=True)
        )
        artifact = await self.db.scalar(query.with_for_update() if lock else query)
        if artifact is None:
            raise RevisionNotFound("compaction_artifact_not_found")
        return artifact

    async def _retention_snapshot(self, artifact: MemoryCompactionArtifact, *, lock: bool = False) -> RetentionSnapshot:
        roots = list(
            await self.db.scalars(
                select(MemoryCompactionRoot)
                .where(MemoryCompactionRoot.artifact_id == artifact.artifact_id, MemoryCompactionRoot.memory_id == artifact.memory_id, MemoryCompactionRoot.tenant_id == self.tenant_id)
                .order_by(MemoryCompactionRoot.ordinal)
                .limit(33)
            )
        )
        if len(roots) > 32:
            raise RevisionConflict("compaction_retention_inventory_incomplete")
        writer_model, publication_model, model = MemoryWriterAdmission, MemoryPublicationOutbox, MemoryCompactionArtifact
        writer_query = select(writer_model).where(writer_model.memory_id == artifact.memory_id, writer_model.operation_id == artifact.operation_id, writer_model.tenant_id == self.tenant_id)
        publication_query = select(publication_model).where(
            publication_model.memory_id == artifact.memory_id, publication_model.operation_id == artifact.operation_id, publication_model.tenant_id == self.tenant_id
        )
        if lock:
            await self.db.scalar(writer_query.with_for_update())
            await self.db.scalar(publication_query.with_for_update())
        query = (
            select(writer_model, publication_model, quiet_compaction_attempt(), drained_compaction_attempt())
            .select_from(model)
            .outerjoin(writer_model, and_(writer_model.memory_id == model.memory_id, writer_model.operation_id == model.operation_id, writer_model.tenant_id == model.tenant_id))
            .outerjoin(
                publication_model,
                and_(publication_model.memory_id == model.memory_id, publication_model.operation_id == model.operation_id, publication_model.tenant_id == model.tenant_id),
            )
            .where(model.artifact_id == artifact.artifact_id)
            .execution_options(populate_existing=True)
        )
        writer, publication, quiet, drained = (await self.db.execute(query)).one()
        operation = await self.db.get(MemoryMutationOperation, (artifact.memory_id, artifact.operation_id), with_for_update=lock, populate_existing=True)
        if operation is not None and (
            operation.tenant_id != self.tenant_id
            or operation.actor_id != artifact.actor_id
            or operation.kind != "context_compact"
            or writer is None
            or operation.request_sha256 != writer.request_sha256
        ):
            operation = None
        fence = await self.db.get(MemoryWriterFence, artifact.memory_id, populate_existing=True)
        bank = await self.db.get(ManagedMemoryState, artifact.memory_id, populate_existing=True)
        if fence is not None and fence.tenant_id != self.tenant_id:
            fence = None
        if bank is not None and bank.tenant_id != self.tenant_id:
            bank = None
        blocked = []
        if artifact.retired_at is not None:
            blocked.append("already_retired")
        if artifact.actor_id != self.actor_id:
            blocked.append("not_creator")
        if bank is None or bank.state != "active" or fence is None or fence.state != "open":
            blocked.append("memory_not_active")
        if artifact.state not in TERMINAL:
            blocked.append("generation_unsettled")
        if writer is None or publication is None or operation is None:
            blocked.append("metadata_unavailable")
        if not drained:
            blocked.append("writer_not_drained")
        package_hash = content_hash(canonical(artifact.package)) if artifact.package is not None else None
        digest = request_hash(
            {
                "actor_id": self.actor_id,
                "memory_id": artifact.memory_id,
                "tenant_id": self.tenant_id,
                "artifact_id": artifact.artifact_id,
                "operation_id": artifact.operation_id,
                "artifact_actor_id": artifact.actor_id,
                "state": artifact.state,
                "request_sha256": artifact.request_sha256,
                "input_manifest_sha256": artifact.input_manifest_sha256,
                "retired_at": utc_timestamp(artifact.retired_at),
                "package_sha256": package_hash,
                "redacted": artifact.redacted,
                "updated_at": utc_timestamp(artifact.updated_at),
                "cancellation_requested": artifact.cancellation_requested,
                "roots": [root.binding for root in roots],
                "writer": (writer.state, writer.publication_state, writer.attempt, writer.active_token, utc_timestamp(writer.finished_at), writer.receipt) if writer is not None else None,
                "publication": (publication.state, publication.attempt, publication.lease_token, publication.receipt) if publication is not None else None,
                "operation": (operation.status, operation.index_sync_status, operation.error_code, operation.request_sha256) if operation is not None else None,
                "fence": (fence.state, fence.generation) if fence is not None else None,
                "bank": (bank.state, bank.generation) if bank is not None else None,
                "blockers": blocked,
            }
        )
        return RetentionSnapshot(digest, [str(root.source_id) for root in roots], package_hash, blocked, not quiet, writer, publication, operation)

    async def retention_preview(self, memory_id: str, artifact_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            artifact = await self._retention_artifact(memory_id, artifact_id)
            snapshot = await self._retention_snapshot(artifact)
            reference = {"memory_id": memory_id, "artifact_id": artifact_id, "operation_id": artifact.operation_id, "input_manifest_sha256": artifact.input_manifest_sha256}
            return {
                **reference,
                "preview_sha256": snapshot.digest,
                "eligible": not snapshot.blockers,
                "blockers": snapshot.blockers,
                "retired_at": utc_timestamp(artifact.retired_at),
                "writer_busy": snapshot.writer_busy,
                "affected": {
                    "consumption_reference": reference,
                    "source_ids": snapshot.source_ids,
                    "source_count": len(snapshot.source_ids),
                    "clears_package": snapshot.package_sha256 is not None,
                    "releases_slots": int(artifact.retired_at is None),
                },
                "audit_preserved": True,
                "snapshot_only": True,
            }
        finally:
            await self._idle()

    async def retire(self, memory_id: str, artifact_id: str, *, expected_preview_sha256: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{64}", expected_preview_sha256):
            raise RevisionValidationError("invalid_compaction_retention_preview")
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            operation_id = (await self._retention_artifact(memory_id, artifact_id)).operation_id
        finally:
            await self._idle()
        async with self._write(operation_id):
            await self._lock_memory_fence(memory_id, create=False)
            artifact = await self._retention_artifact(memory_id, artifact_id, lock=True)
            if artifact.actor_id != self.actor_id or artifact.operation_id != operation_id:
                raise RevisionNotFound("compaction_artifact_not_found")
            if artifact.retired_at is not None:
                if artifact.retirement is None or artifact.retirement.get("preview_sha256") != expected_preview_sha256:
                    raise RevisionConflict("compaction_retirement_request_mismatch")
            else:
                snapshot = await self._retention_snapshot(artifact, lock=True)
                if snapshot.digest != expected_preview_sha256:
                    raise RevisionConflict("compaction_retention_changed")
                writer, publication, operation = snapshot.writer, snapshot.publication, snapshot.operation
                if snapshot.blockers or writer is None or publication is None or operation is None:
                    raise RevisionConflict("compaction_retention_not_eligible")
                now = datetime.now(UTC)
                artifact.retirement = {
                    "preview_sha256": expected_preview_sha256,
                    "retired_by": self.actor_id,
                    "retired_at": now.isoformat(),
                    "prior_state": artifact.state,
                    "source_count": len(snapshot.source_ids),
                    "package_sha256": snapshot.package_sha256,
                    "input_manifest_sha256": artifact.input_manifest_sha256,
                }
                artifact.retired_at, artifact.updated_at = now, now
                artifact.state, artifact.error_code, artifact.redacted, artifact.cancellation_requested, artifact.request = "stale", "compaction_retired", True, True, {}
                artifact.package = None
                flag_modified(artifact, "package")
                await self.db.execute(
                    delete(MemoryCompactionRoot).where(MemoryCompactionRoot.artifact_id == artifact_id, MemoryCompactionRoot.memory_id == memory_id, MemoryCompactionRoot.tenant_id == self.tenant_id)
                )
                writer.publication_state = "canceled"
                writer.request_metadata = {"artifact_id": artifact_id, "retired": True}
                publication.state, publication.error_code = "redacted", "compaction_retired"
                publication.payload = {"action": "context_compact", "reserved_bytes": 0}
                operation.status, operation.error_code, operation.updated_at = "stale", "compaction_retired", now
                operation.result = {"artifact_id": artifact_id, "generated_only": True, "retired_at": now.isoformat()}
        return await self.get(memory_id, operation_id)

    async def retention_inventory(self, memory_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            artifact, writer, outbox = MemoryCompactionArtifact, MemoryWriterAdmission, MemoryPublicationOutbox
            retired = artifact.retired_at.is_not(None)
            query = (
                select(retired, artifact.state, func.count(), func.count().filter(~quiet_compaction_attempt()), func.count().filter(~drained_compaction_attempt()))
                .select_from(artifact)
                .outerjoin(writer, and_(~retired, writer.memory_id == artifact.memory_id, writer.operation_id == artifact.operation_id, writer.tenant_id == artifact.tenant_id))
                .outerjoin(outbox, and_(~retired, outbox.memory_id == artifact.memory_id, outbox.operation_id == artifact.operation_id, outbox.tenant_id == artifact.tenant_id))
                .where(artifact.memory_id == memory_id, artifact.tenant_id == self.tenant_id)
                .group_by(retired, artifact.state)
            )
            by_state = dict.fromkeys(STATES, 0)
            historical = busy = unsettled = 0
            for is_retired, state, count, busy_count, undrained_count in await self.db.execute(query):
                if is_retired:
                    historical += count
                else:
                    by_state[state] = count
                    busy += busy_count
                    unsettled += count if state not in TERMINAL else undrained_count
            retained = sum(by_state.values())
            return {
                "memory_id": memory_id,
                "limit": MAX_ARTIFACTS,
                "unit": "artifact",
                "retained_count": retained,
                "retired_count": historical,
                "available_slots": max(0, MAX_ARTIFACTS - retained),
                "counts_by_state": by_state,
                "writer_busy_count": busy,
                "unsettled_count": unsettled,
                "automatic": False,
                "snapshot_only": True,
            }
        finally:
            await self._idle()
