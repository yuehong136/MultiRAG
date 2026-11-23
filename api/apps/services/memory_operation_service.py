"""Read-only, authorized discovery of durable public memory operations."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from api.db.services.memory_service import MemoryService
from memory.maintenance.execution import execution_snapshot, execution_view
from memory.revisions.domain import RevisionNotFound, RevisionValidationError, request_hash
from memory.revisions.service import operation_view

OperationView = Literal["pending", "all"]
_OPEN_STATUS = {"accepted", "pending", "preparing", "queued", "running", "active", "published", "reconciling", "persistence_unknown", "cancel_requested", "sync_pending", "failed"}
_OPEN_SYNC = {"pending", "unknown", "sync_pending", "cleanup_pending", "blocked", "preparing"}
_CLOSED_OUTBOX = {"ready", "redacted", "canceled", "failed", "stale"}


def _iso(value: datetime) -> str:
    return value.isoformat()


def _decode_cursor(cursor: str | None, scope: str) -> tuple[datetime, str] | None:
    if cursor is None:
        return None
    try:
        if len(cursor) > 1024:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or set(value) != {"scope", "created_at", "operation_id"} or value["scope"] != scope:
            raise ValueError
        stamp = datetime.fromisoformat(value["created_at"])
        operation_id = value["operation_id"]
        if stamp.tzinfo is None or not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
            raise ValueError
        return stamp, operation_id
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise RevisionValidationError("invalid_operation_cursor") from error


def _cursor(scope: str, row: MemoryMutationOperation) -> str:
    data = {"scope": scope, "created_at": _iso(row.created_at), "operation_id": row.operation_id}
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()


async def _authorized_scope(db: AsyncSession, memory_id: str, actor_id: str) -> tuple[str, str]:
    memory = (await db.execute(select(Memory.tenant_id, Memory.permissions).where(Memory.id == memory_id))).one_or_none()
    bank = await db.get(ManagedMemoryState, memory_id)
    if memory is not None:
        tenant_id, permissions = memory
        if bank is not None and bank.tenant_id != tenant_id:
            raise RevisionNotFound("memory_not_found")
        state = bank.state if bank is not None else "active"
    elif bank is not None and bank.state == "deleted":
        tenant_id, permissions, state = bank.tenant_id, bank.delete_permissions, "deleted"
    else:
        raise RevisionNotFound("memory_not_found")
    tenants = await db.run_sync(lambda session: MemoryService.accessible_tenant_ids(session, actor_id))  # TODO(async-phase4)
    if actor_id != tenant_id and not (permissions == "team" and tenant_id in tenants):
        raise RevisionNotFound("memory_not_found")
    return tenant_id, state


def _pending_filter() -> Any:
    attempts = MemoryPublicationOutbox.receipt["active_attempts"]
    active_count = case((func.jsonb_typeof(attempts) == "array", func.jsonb_array_length(attempts)), else_=0)
    pending_outbox = exists().where(
        MemoryPublicationOutbox.memory_id == MemoryMutationOperation.memory_id,
        MemoryPublicationOutbox.tenant_id == MemoryMutationOperation.tenant_id,
        MemoryPublicationOutbox.operation_id == MemoryMutationOperation.operation_id,
        or_(MemoryPublicationOutbox.state.not_in(_CLOSED_OUTBOX), active_count > 0),
    )
    active_writer = exists().where(
        MemoryWriterAdmission.memory_id == MemoryMutationOperation.memory_id,
        MemoryWriterAdmission.tenant_id == MemoryMutationOperation.tenant_id,
        MemoryWriterAdmission.operation_id == MemoryMutationOperation.operation_id,
        MemoryWriterAdmission.active_token.is_not(None),
    )
    pending_dispatch = and_(MemoryMutationOperation.kind == "append_raw", MemoryMutationOperation.result["queue_state"].as_string().not_in({"not_required", "submitted"}))
    pending_execution = exists().where(
        MemoryMaintenanceExecution.memory_id == MemoryMutationOperation.memory_id,
        MemoryMaintenanceExecution.tenant_id == MemoryMutationOperation.tenant_id,
        MemoryMaintenanceExecution.operation_id == MemoryMutationOperation.operation_id,
        MemoryMaintenanceExecution.state.in_({"queued", "claimed", "running", "needs_reconciliation"}),
    )
    return or_(MemoryMutationOperation.status.in_(_OPEN_STATUS), MemoryMutationOperation.index_sync_status.in_(_OPEN_SYNC), pending_outbox, active_writer, pending_dispatch, pending_execution)


def _safe_view(
    row: MemoryMutationOperation,
    *,
    actor_id: str,
    execution: dict[str, Any] | None,
    job_id: str | None,
    outbox_id: str | None,
    writer_state: str | None = None,
    writer_active: bool = False,
    outbox_active: bool = False,
) -> dict[str, Any]:
    result = row.result or {}
    operator_needed = bool(
        result.get("needs_operator_reconciliation")
        or (execution and execution["needs_operator_reconciliation"])
        or (writer_active and writer_state == "unknown")
        or (row.error_code and ("operator" in row.error_code or "untracked" in row.error_code))
    )
    item_states = result.get("item_states", [])
    unfinished_items = isinstance(item_states, list) and any(isinstance(item, dict) and item.get("state") in _OPEN_SYNC for item in item_states)
    queue_state = result.get("queue_state")
    pending_dispatch = row.kind == "append_raw" and queue_state not in {None, "not_required", "submitted"}
    execution_pending = bool(execution and execution["state"] in {"queued", "claimed", "running", "needs_reconciliation"})
    retryable = row.status in _OPEN_STATUS or row.index_sync_status in _OPEN_SYNC or unfinished_items or pending_dispatch or execution_pending
    writer_busy = writer_active or outbox_active
    execution_active = bool(execution and execution["state"] in {"queued", "claimed", "running"})
    value = operation_view(row, outbox_id)
    value.update(
        retryable=retryable,
        job_id=job_id if isinstance(job_id, str) and len(job_id) == 32 else None,
        execution=execution,
        needs_operator_reconciliation=operator_needed,
        can_reconcile=retryable and row.actor_id == actor_id and not operator_needed and not execution_active and not writer_busy,
        writer_busy=writer_busy,
        queue_state=queue_state if isinstance(queue_state, str) else None,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
    )
    if row.kind == "context_compact":
        value.update(retryable=False, can_reconcile=False)
    return value


async def recovery_metadata(db: AsyncSession, memory_id: str, operation_id: str, *, actor_id: str, operation: dict[str, Any]) -> dict[str, Any]:
    """Add recovery controls without overriding the operation's business result."""
    try:
        tenant_id, state = await _authorized_scope(db, memory_id, actor_id)
        row = await db.get(MemoryMutationOperation, (memory_id, operation_id))
        if row is None or row.tenant_id != tenant_id or row.kind == "dream_publish_item" or (state == "deleted" and row.kind != "delete_memory"):
            raise RevisionNotFound("operation_not_found")
        execution = await db.scalar(select(MemoryMaintenanceExecution).where(MemoryMaintenanceExecution.memory_id == memory_id, MemoryMaintenanceExecution.operation_id == operation_id))
        admission = await db.get(MemoryWriterAdmission, (memory_id, operation_id))
        outbox = await db.scalar(select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id))
        job_id = await db.scalar(select(MemoryMaintenanceJob.job_id).where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.analysis_operation_id == operation_id))
        value = _safe_view(
            row,
            actor_id=actor_id,
            execution=await execution_snapshot(db, execution) if execution is not None else None,
            job_id=job_id or (row.result or {}).get("job_id"),
            outbox_id=None,
            writer_state=admission.state if admission is not None else None,
            writer_active=admission is not None and admission.active_token is not None,
            outbox_active=bool(outbox is not None and (outbox.receipt or {}).get("active_attempts")),
        )
        pending_dispatch = row.kind == "append_raw" and value["queue_state"] not in {None, "not_required", "submitted"}
        execution_pending = bool(value["execution"] and value["execution"]["state"] in {"queued", "claimed", "running", "needs_reconciliation"})
        value["retryable"] = bool(operation.get("retryable")) or pending_dispatch or execution_pending
        value["can_reconcile"] = value["can_reconcile"] and value["retryable"]
        return {key: value[key] for key in ("retryable", "can_reconcile", "needs_operator_reconciliation", "writer_busy", "queue_state", "execution", "job_id", "created_at", "updated_at")}
    finally:
        await db.rollback()


async def authorize_reconciliation(db: AsyncSession, memory_id: str, operation_id: str, *, actor_id: str) -> str:
    """Discovery permission does not grant execution of another actor's command."""
    from memory.revisions.domain import RevisionConflict

    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Reconciliation requires a session without pending changes")
    try:
        tenant_id, state = await _authorized_scope(db, memory_id, actor_id)
        row = await db.get(MemoryMutationOperation, (memory_id, operation_id))
        if row is None or row.tenant_id != tenant_id or (state == "deleted" and row.kind != "delete_memory"):
            raise RevisionNotFound("operation_not_found")
        if row.actor_id != actor_id:
            raise RevisionConflict("operation_actor_mismatch")
        return row.kind
    finally:
        await db.rollback()


async def list_operations(db: AsyncSession, memory_id: str, *, actor_id: str, view: OperationView = "pending", limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    """Return safe handles; discovery never runs a model or reconciles work."""
    if view not in {"pending", "all"} or type(limit) is not int or not 1 <= limit <= 100:
        raise RevisionValidationError("invalid_operation_page")
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Operation discovery requires a session without pending changes")
    if db.in_transaction():
        await db.rollback()
    try:
        tenant_id, memory_state = await _authorized_scope(db, memory_id, actor_id)
        scope = request_hash({"memory_id": memory_id, "tenant_id": tenant_id, "actor_id": actor_id, "view": view, "limit": limit})
        boundary = _decode_cursor(cursor, scope)
        statement = select(MemoryMutationOperation).where(
            MemoryMutationOperation.memory_id == memory_id,
            MemoryMutationOperation.tenant_id == tenant_id,
            MemoryMutationOperation.kind != "dream_publish_item",
        )
        if memory_state == "deleted":
            statement = statement.where(MemoryMutationOperation.kind == "delete_memory")
        if view == "pending":
            statement = statement.where(_pending_filter())
        if boundary is not None:
            stamp, identity = boundary
            statement = statement.where(or_(MemoryMutationOperation.created_at < stamp, and_(MemoryMutationOperation.created_at == stamp, MemoryMutationOperation.operation_id < identity)))
        rows = list((await db.scalars(statement.order_by(MemoryMutationOperation.created_at.desc(), MemoryMutationOperation.operation_id.desc()).limit(limit + 1))).all())
        selected = rows[:limit]
        identities = [row.operation_id for row in selected]
        executions = {
            row.operation_id: row
            for row in await db.scalars(
                select(MemoryMaintenanceExecution).where(
                    MemoryMaintenanceExecution.memory_id == memory_id, MemoryMaintenanceExecution.tenant_id == tenant_id, MemoryMaintenanceExecution.operation_id.in_(identities)
                )
            )
        }
        attempts: dict[str, tuple[str, str | None]] = {
            identity: (state, token)
            for identity, state, token in (
                await db.execute(
                    select(MemoryWriterAdmission.operation_id, MemoryWriterAdmission.state, MemoryWriterAdmission.active_token).where(
                        MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == tenant_id, MemoryWriterAdmission.operation_id.in_(identities)
                    )
                )
            ).all()
        }
        jobs = {
            row.analysis_operation_id: row.job_id
            for row in await db.scalars(
                select(MemoryMaintenanceJob).where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == tenant_id, MemoryMaintenanceJob.analysis_operation_id.in_(identities))
            )
        }
        outboxes = {
            row.operation_id: row
            for row in await db.scalars(
                select(MemoryPublicationOutbox).where(
                    MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.tenant_id == tenant_id, MemoryPublicationOutbox.operation_id.in_(identities)
                )
            )
        }
        operations = []
        for row in selected:
            writer_state, token = attempts.get(row.operation_id, (None, None))
            outbox = outboxes.get(row.operation_id)
            execution = execution_view(executions[row.operation_id], actual_attempt_active=token is not None) if row.operation_id in executions else None
            operations.append(
                _safe_view(
                    row,
                    actor_id=actor_id,
                    execution=execution,
                    job_id=jobs.get(row.operation_id) or (row.result or {}).get("job_id"),
                    outbox_id=outbox.id if outbox is not None else None,
                    writer_state=writer_state,
                    writer_active=token is not None,
                    outbox_active=bool(outbox is not None and (outbox.receipt or {}).get("active_attempts")),
                )
            )
        return {"operations": operations, "next_cursor": _cursor(scope, selected[-1]) if len(rows) > limit else None, "limit": limit, "view": view, "memory_state": memory_state}
    finally:
        await db.rollback()
