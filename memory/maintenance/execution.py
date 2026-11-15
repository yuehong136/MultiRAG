"""Durable SQL dispatch; heartbeat expiry never reclaims actual model work."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, validate_operation_id

logger = logging.getLogger(__name__)
MAX_EXECUTION_ATTEMPTS = 3
SessionFactory = Callable[[], AsyncSession]

if TYPE_CHECKING:
    from memory.maintenance.jobs import JobService


class ExecutionBookkeepingUnknown(RevisionPersistenceUnknown):
    """Stop this worker when its terminal SQL acknowledgement is unconfirmed."""


class JobsFactory(Protocol):
    async def __call__(self, db: AsyncSession, memory_id: str, *, actor_id: str) -> JobService: ...


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def execution_view(row: MemoryMaintenanceExecution, *, actual_attempt_active: bool | None = None) -> dict[str, Any]:
    return {
        "state": row.state,
        "attempt": row.attempt,
        "heartbeat_at": _iso(row.heartbeat_at),
        "queued_at": _iso(row.queued_at),
        "finished_at": _iso(row.finished_at),
        "error_code": row.error_code,
        "needs_operator_reconciliation": row.state == "needs_reconciliation"
        and (actual_attempt_active if actual_attempt_active is not None else row.receipt.get("needs_operator_reconciliation", True)),
    }


async def execution_snapshot(db: AsyncSession, row: MemoryMaintenanceExecution) -> dict[str, Any]:
    actual = (
        await db.execute(
            select(MemoryWriterAdmission.active_token).where(
                MemoryWriterAdmission.memory_id == row.memory_id, MemoryWriterAdmission.operation_id == row.operation_id, MemoryWriterAdmission.tenant_id == row.tenant_id
            )
        )
    ).one_or_none()
    return execution_view(row, actual_attempt_active=actual is None or actual.active_token is not None)


async def register_execution(db: AsyncSession, job: MemoryMaintenanceJob, admission: MemoryWriterAdmission) -> None:
    """Called inside the initial admission transaction, with its job locked."""
    current = await db.get(MemoryMaintenanceExecution, job.job_id)
    if current is not None:
        if (current.memory_id, current.tenant_id, current.actor_id, current.operation_id, current.request_sha256) != (
            job.memory_id,
            admission.tenant_id,
            admission.actor_id,
            admission.operation_id,
            admission.request_sha256,
        ):
            raise RevisionConflict("maintenance_execution_request_mismatch")
        return
    db.add(
        MemoryMaintenanceExecution(
            job_id=job.job_id,
            memory_id=job.memory_id,
            tenant_id=admission.tenant_id,
            actor_id=admission.actor_id,
            operation_id=admission.operation_id,
            request_sha256=admission.request_sha256,
            job_generation=job.generation,
            state="queued",
            attempt=0,
            receipt={},
        )
    )


async def require_execution_permit(db: AsyncSession, job: MemoryMaintenanceJob, operation_id: str, claim_token: str | None, writer_token: str, *, start: bool) -> None:
    """Acquire execution after sources/job and before admission/outbox locks."""
    row = await db.scalar(
        select(MemoryMaintenanceExecution)
        .where(MemoryMaintenanceExecution.job_id == job.job_id, MemoryMaintenanceExecution.memory_id == job.memory_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if row is None:
        if claim_token is not None:
            raise RevisionConflict("maintenance_execution_missing")
        return
    if claim_token is None or row.claim_token != claim_token or row.operation_id != operation_id or row.tenant_id != job.tenant_id or row.job_generation != job.generation:
        raise RevisionBusy("maintenance_execution_claim_required")
    if row.receipt.get("cancel_requested") or row.state not in ({"claimed"} if start else {"running", "needs_reconciliation"}):
        raise RevisionConflict("maintenance_execution_changed")
    if start:
        if row.attempt >= MAX_EXECUTION_ATTEMPTS:
            raise RevisionConflict("maintenance_execution_attempts_exhausted")
        row.state, row.started_at, row.heartbeat_at, row.writer_token = "running", datetime.now(UTC), datetime.now(UTC), writer_token
        row.attempt += 1
    elif row.writer_token != writer_token:
        raise RevisionConflict("maintenance_execution_attempt_changed")


async def cancel_execution(db: AsyncSession, *, memory_id: str, job_id: str, reason: str) -> None:
    """Caller holds job/lifecycle locks; preserve actual work until it settles."""
    row = await db.scalar(
        select(MemoryMaintenanceExecution)
        .where(MemoryMaintenanceExecution.job_id == job_id, MemoryMaintenanceExecution.memory_id == memory_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if row is None:
        return
    actual = (await db.execute(select(MemoryWriterAdmission.active_token).where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.operation_id == row.operation_id))).one_or_none()
    row.receipt = {**row.receipt, "cancel_requested": True}
    row.error_code = reason[:64]
    if actual is None:
        row.state, row.error_code = "needs_reconciliation", "maintenance_admission_missing"
        row.receipt = {**row.receipt, "needs_operator_reconciliation": True}
    elif actual.active_token is None:
        row.state, row.claim_token, row.worker_id, row.finished_at = "canceled", None, None, datetime.now(UTC)
    elif row.state != "succeeded":
        row.state = "running"


class ExecutionService:
    def __init__(self, db: AsyncSession, *, jobs: JobService) -> None:
        if jobs.db is not db:
            raise RuntimeError("Execution service must use its job service session")
        self.db, self.jobs = db, jobs

    async def get(self, memory_id: str, job_id: str) -> dict[str, Any] | None:
        return (await self.jobs.get(memory_id, job_id))["execution"]

    async def submit(self, memory_id: str, job_id: str, *, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        view = await self.jobs.get(memory_id, job_id)
        if view["analysis_operation_id"] is not None and view["analysis_operation_id"] != operation_id:
            raise RevisionConflict("maintenance_analysis_operation_mismatch")
        # A new submission reserves the canonical operation and queue row in one
        # SQL transaction through the existing admission hook. No Redis wakeup.
        if view["analysis_operation_id"] is None:
            if view["state"] not in {"pending", "failed"}:
                raise RevisionConflict("maintenance_job_not_runnable")
            await self.jobs.admit(
                memory_id, operation_id=operation_id, kind="dream_analyze", request={"job_id": job_id, "job_generation": view["generation"]}, server_metadata={"execution_mode": "background"}
            )
        async with self.jobs._write(operation_id):
            await self.jobs._memory_fence(memory_id)
            observed = await self.jobs._job(memory_id, job_id)
            prior = await self.db.get(MemoryMaintenanceExecution, job_id, populate_existing=True)
            validate_fresh = (prior is None and observed.state != "ready") or (
                prior is not None and prior.state in {"failed", "needs_reconciliation"} and observed.state in {"pending", "failed", "running"}
            )
            if validate_fresh:
                await self.jobs.validate_inputs(self.db, memory_id, await self.jobs._manifest(observed))
            job = await self.jobs._job(memory_id, job_id, lock=True)
            row = await self.db.scalar(select(MemoryMaintenanceExecution).where(MemoryMaintenanceExecution.job_id == job_id).execution_options(populate_existing=True).with_for_update())
            admission = await self.jobs._row(memory_id, operation_id, lock=True)
            if admission.actor_id != self.jobs.actor_id or admission.tenant_id != self.jobs.tenant_id or admission.kind != "dream_analyze" or job.analysis_operation_id != operation_id:
                raise RevisionConflict("operation_request_mismatch")
            if row is not None and (row.operation_id != operation_id or row.actor_id != self.jobs.actor_id or row.request_sha256 != admission.request_sha256):
                raise RevisionConflict("operation_request_mismatch")
            if row is None:
                # Existing inline executions may be explicitly moved to the SQL
                # queue only after their real attempt has settled.
                if admission.active_token is not None:
                    raise RevisionBusy("writer_needs_operator_reconciliation")
                if job.state == "ready":
                    return await self.jobs._job_view(job, await self.jobs._inputs(job))
                if not validate_fresh:
                    raise RevisionConflict("maintenance_execution_changed")
                await register_execution(self.db, job, admission)
                await self.db.flush()
                row = await self.db.get(MemoryMaintenanceExecution, job_id)
            assert row is not None
            if row.state in {"failed", "needs_reconciliation"} and admission.active_token is None:
                if job.state == "ready":
                    row.state, row.finished_at, row.claim_token, row.worker_id = "succeeded", datetime.now(UTC), None, None
                    row.error_code = None
                elif job.state in {"pending", "failed", "running"} and admission.publication_state != "canceled":
                    if not validate_fresh:
                        raise RevisionConflict("maintenance_execution_changed")
                    if row.attempt >= MAX_EXECUTION_ATTEMPTS:
                        raise RevisionConflict("maintenance_execution_attempts_exhausted")
                    row.state, row.error_code, row.finished_at, row.claim_token, row.worker_id, row.writer_token = "queued", None, None, None, None, None
                    row.not_before, row.job_generation = datetime.now(UTC), job.generation
                    row.receipt = {key: value for key, value in row.receipt.items() if key != "needs_operator_reconciliation"}
                    job.state, job.error_code, job.updated_at = "pending", None, datetime.now(UTC)
                    operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
                    if operation is not None:
                        operation.status, operation.index_sync_status, operation.error_code = "accepted", "pending", None
            await self.db.flush()
        return await self.jobs.get(memory_id, job_id)


def _identity(worker_id: str, timeout: int) -> None:
    if not isinstance(worker_id, str) or not worker_id or len(worker_id) > 128 or type(timeout) is not int or not 5 <= timeout <= 3600:
        raise RevisionValidationError("invalid_maintenance_worker_options")


async def _idle(db: AsyncSession) -> None:
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Execution polling requires a clean owned session")
    if db.in_transaction():
        await db.rollback()
    db.expire_all()


async def claim_next(db: AsyncSession, *, worker_id: str, claim_timeout_seconds: int = 60) -> dict[str, Any] | None:
    """Only pre-work claims can expire. Running work is never leased away."""
    _identity(worker_id, claim_timeout_seconds)
    await _idle(db)
    cutoff, now = datetime.now(UTC) - timedelta(seconds=claim_timeout_seconds), datetime.now(UTC)
    row = await db.scalar(
        select(MemoryMaintenanceExecution)
        .where(
            or_(
                (MemoryMaintenanceExecution.state == "queued") & (MemoryMaintenanceExecution.not_before <= now),
                (MemoryMaintenanceExecution.state == "claimed") & (MemoryMaintenanceExecution.heartbeat_at < cutoff),
            )
        )
        .order_by(MemoryMaintenanceExecution.queued_at, MemoryMaintenanceExecution.job_id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if row is None:
        await db.rollback()
        return None
    row.state, row.claim_token, row.worker_id, row.claimed_at, row.heartbeat_at, row.finished_at, row.error_code = "claimed", uuid4().hex, worker_id, now, now, None, None
    row.started_at, row.writer_token = None, None
    result = {name: getattr(row, name) for name in ("job_id", "memory_id", "tenant_id", "actor_id", "operation_id", "claim_token")}
    try:
        await finish_owned_work(db.commit())
    except DBAPIError as error:
        await finish_owned_work(db.rollback())
        raise RevisionPersistenceUnknown(result["operation_id"]) from error
    return result


async def heartbeat(db: AsyncSession, *, job_id: str, claim_token: str) -> bool:
    await _idle(db)
    row = await db.scalar(select(MemoryMaintenanceExecution).where(MemoryMaintenanceExecution.job_id == job_id).execution_options(populate_existing=True).with_for_update())
    if row is None or row.claim_token != claim_token or row.state not in {"claimed", "running", "needs_reconciliation"}:
        await db.rollback()
        return False
    row.heartbeat_at = datetime.now(UTC)
    await finish_owned_work(db.commit())
    return True


async def recover_executions(db: AsyncSession, *, claim_timeout_seconds: int = 60, limit: int = 100) -> int:
    """Reconcile durable outcomes, never start models or clear actual tokens."""
    _identity("recovery", claim_timeout_seconds)
    await _idle(db)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise RevisionValidationError("invalid_execution_recovery_limit")
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=claim_timeout_seconds)
    rows = list(
        await db.scalars(
            select(MemoryMaintenanceExecution)
            .where(
                or_((MemoryMaintenanceExecution.state == "running") & (MemoryMaintenanceExecution.heartbeat_at < cutoff), MemoryMaintenanceExecution.state == "needs_reconciliation"),
                MemoryMaintenanceExecution.not_before <= now,
            )
            .order_by(MemoryMaintenanceExecution.queued_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        admission = await db.get(MemoryWriterAdmission, (row.memory_id, row.operation_id), populate_existing=True)
        job = await db.get(MemoryMaintenanceJob, row.job_id, populate_existing=True)
        if admission is None or admission.active_token is not None:
            row.state, row.error_code = "needs_reconciliation", "maintenance_admission_missing" if admission is None else "maintenance_attempt_unconfirmed"
            row.receipt = {**row.receipt, "needs_operator_reconciliation": True}
            row.not_before = now + timedelta(seconds=min(claim_timeout_seconds, 60))
        elif job is None or job.state in {"canceled", "cancel_requested", "redacted", "stale"} or (admission is not None and admission.publication_state == "canceled"):
            row.state, row.error_code, row.finished_at, row.claim_token, row.worker_id = "canceled", "maintenance_job_canceled", datetime.now(UTC), None, None
        elif job.state == "ready":
            row.state, row.error_code, row.finished_at, row.claim_token, row.worker_id = "succeeded", None, datetime.now(UTC), None, None
        elif job.state in {"pending", "running"} and row.started_at is None and row.writer_token is None:
            # This dispatch never crossed the SQL start permit, and the actual
            # token is now confirmed drained. No prior model call is replayed.
            row.state, row.error_code, row.claim_token, row.worker_id, row.not_before = "queued", None, None, None, now
        else:
            row.state, row.error_code, row.finished_at, row.claim_token, row.worker_id = "failed", job.error_code or "maintenance_execution_result_unconfirmed", datetime.now(UTC), None, None
    await finish_owned_work(db.commit())
    return len(rows)


async def _complete(db: AsyncSession, claimed: dict[str, Any], *, failure_code: str | None = None, requeue: bool = False) -> None:
    await _idle(db)
    row = await db.scalar(select(MemoryMaintenanceExecution).where(MemoryMaintenanceExecution.job_id == claimed["job_id"]).execution_options(populate_existing=True).with_for_update())
    if row is None or row.claim_token != claimed["claim_token"]:
        await db.rollback()
        return
    admission = await db.get(MemoryWriterAdmission, (row.memory_id, row.operation_id), populate_existing=True)
    job = await db.get(MemoryMaintenanceJob, row.job_id, populate_existing=True)
    if admission is None or admission.active_token is not None:
        row.state, row.error_code = "needs_reconciliation", "maintenance_admission_missing" if admission is None else "maintenance_attempt_unconfirmed"
        row.receipt = {**row.receipt, "needs_operator_reconciliation": True}
    elif (
        row.receipt.get("cancel_requested")
        or job is None
        or job.state in {"canceled", "cancel_requested", "redacted", "stale"}
        or (admission is not None and admission.publication_state == "canceled")
    ):
        row.state, row.error_code = "canceled", failure_code or "maintenance_job_canceled"
    elif job.state == "ready":
        row.state, row.error_code = "succeeded", None
    elif requeue and row.state == "claimed":
        row.state, row.error_code, row.not_before = "queued", None, datetime.now(UTC) + timedelta(seconds=2)
    else:
        row.state, row.error_code = "failed", failure_code or job.error_code or "maintenance_execution_failed"
    if row.state != "needs_reconciliation":
        row.claim_token, row.worker_id = None, None
        if row.state != "queued":
            row.finished_at = datetime.now(UTC)
    await finish_owned_work(db.commit())


async def process_next(sessionmaker: SessionFactory, jobs_factory: JobsFactory, *, worker_id: str, claim_timeout_seconds: int = 60, heartbeat_seconds: float = 10) -> bool:
    _identity(worker_id, claim_timeout_seconds)
    if not 0 < heartbeat_seconds < claim_timeout_seconds:
        raise RevisionValidationError("invalid_maintenance_heartbeat_interval")
    async with sessionmaker() as db:
        await recover_executions(db, claim_timeout_seconds=claim_timeout_seconds)
        claimed = await claim_next(db, worker_id=worker_id, claim_timeout_seconds=claim_timeout_seconds)
    if claimed is None:
        return False
    stop = asyncio.Event()

    async def keep_alive() -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=heartbeat_seconds)
            except TimeoutError:
                try:
                    async with sessionmaker() as db:
                        if not await heartbeat(db, job_id=claimed["job_id"], claim_token=claimed["claim_token"]):
                            return
                except Exception as error:
                    logger.warning("Maintenance heartbeat unconfirmed (%s)", type(error).__name__)

    alive = asyncio.create_task(keep_alive())
    failure_code, requeue = None, False
    try:
        async with sessionmaker() as db:
            jobs = await jobs_factory(db, claimed["memory_id"], actor_id=claimed["actor_id"])
            if jobs.tenant_id != claimed["tenant_id"]:
                raise RevisionNotFound("memory_not_found")
            await jobs.run(claimed["memory_id"], claimed["job_id"], operation_id=claimed["operation_id"], execution_claim_token=claimed["claim_token"])
    except RevisionBusy as error:
        failure_code, requeue = error.code, True
    except asyncio.CancelledError:
        failure_code = "maintenance_worker_interrupted"
        raise
    except (RevisionConflict, RevisionValidationError, RevisionNotFound) as error:
        failure_code = error.code
    except Exception as error:
        failure_code = "maintenance_execution_unconfirmed"
        logger.warning("Maintenance execution unconfirmed (%s)", type(error).__name__)
    finally:
        stop.set()
        await finish_owned_work(alive)
        async with sessionmaker() as db:
            try:
                await finish_owned_work(_complete(db, claimed, failure_code=failure_code, requeue=requeue))
            except Exception as error:
                raise ExecutionBookkeepingUnknown(claimed["operation_id"]) from error
    return True
