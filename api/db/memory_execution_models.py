"""SQL queue metadata for explicitly submitted memory analysis executions."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryMaintenanceExecution(Base):
    __tablename__ = "t_ai_memory_maintenance_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "memory_id"], ["usr_ai.t_ai_memory_maintenance_jobs.job_id", "usr_ai.t_ai_memory_maintenance_jobs.memory_id"], name="fk_memory_execution_job_scope", ondelete="RESTRICT"
        ),
        UniqueConstraint("memory_id", "operation_id", name="uq_memory_execution_operation"),
        Index("ix_memory_execution_queue", "state", "not_before", "queued_at"),
        CheckConstraint("job_generation > 0 AND attempt >= 0", name="ck_memory_execution_counters"),
        {"schema": "usr_ai"},
    )
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    job_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    claim_token: Mapped[str | None] = mapped_column(String(32))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    writer_token: Mapped[str | None] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    error_code: Mapped[str | None] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


MEMORY_EXECUTION_MODELS = (MemoryMaintenanceExecution,)
