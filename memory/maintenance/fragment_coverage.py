"""SQL effective part completion: saved analysis is insufficient until drained."""

from typing import Any

from sqlalchemy import and_, case, func, or_, select

from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceFragmentCoverage
from api.db.memory_maintenance_models import MemoryMaintenanceJob
from api.db.memory_writer_models import MemoryWriterAdmission


def fragment_inventory(memory_id: str, tenant_id: str) -> Any:
    fragment, job, execution, admission = MemoryMaintenanceFragmentCoverage, MemoryMaintenanceJob, MemoryMaintenanceExecution, MemoryWriterAdmission
    complete = and_(fragment.state == "analyzed", job.state == "ready", execution.state == "succeeded", admission.operation_id.is_not(None), admission.active_token.is_(None))
    live = or_(admission.active_token.is_not(None), execution.state.in_(["queued", "claimed", "running", "needs_reconciliation"]))
    terminal = or_(job.state.in_(["failed", "canceled", "stale", "redacted"]), MemoryMaintenanceBatch.state.in_(["failed", "canceled", "stale"]))
    return (
        select(
            fragment.coverage_key,
            func.count().label("total"),
            func.sum(case((complete, 1), else_=0)).label("completed"),
            func.sum(case((fragment.state == "blocked", 1), else_=0)).label("blocked"),
            func.sum(case((live, 1), else_=0)).label("live"),
            func.sum(case((terminal, 1), else_=0)).label("terminal"),
        )
        .outerjoin(job, and_(job.job_id == fragment.job_id, job.memory_id == memory_id, job.tenant_id == tenant_id))
        .outerjoin(MemoryMaintenanceBatch, MemoryMaintenanceBatch.batch_id == fragment.batch_id)
        .outerjoin(execution, and_(execution.job_id == job.job_id, execution.memory_id == memory_id, execution.tenant_id == tenant_id))
        .outerjoin(admission, and_(admission.memory_id == memory_id, admission.tenant_id == tenant_id, admission.operation_id == job.analysis_operation_id))
        .where(fragment.memory_id == memory_id, fragment.tenant_id == tenant_id)
        .group_by(fragment.coverage_key)
        .subquery()
    )
