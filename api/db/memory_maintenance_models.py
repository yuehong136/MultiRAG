"""Isolated maintenance inputs and reviewable candidates, never current memory."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryMaintenanceJob(Base):
    __tablename__ = "t_ai_memory_maintenance_jobs"
    __table_args__ = (
        UniqueConstraint("memory_id", "operation_id", name="uq_memory_maintenance_create"),
        UniqueConstraint("job_id", "memory_id", name="uq_memory_maintenance_job_scope"),
        Index("ix_memory_maintenance_jobs_bank", "memory_id", "created_at"),
        CheckConstraint("generation > 0 AND input_budget_tokens > 0 AND output_budget_tokens > 0 AND estimated_input_tokens >= 0", name="ck_memory_maintenance_job_counters"),
        {"schema": "usr_ai"},
    )
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    analysis_operation_id: Mapped[str | None] = mapped_column(String(128))
    source_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    entry_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    entry_snapshots: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    input_budget_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_budget_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    estimated_input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    model_reference: Mapped[str | None] = mapped_column(String(256))
    config_reference: Mapped[str | None] = mapped_column(String(256))
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryMaintenanceInput(Base):
    __tablename__ = "t_ai_memory_maintenance_inputs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "memory_id"], ["usr_ai.t_ai_memory_maintenance_jobs.job_id", "usr_ai.t_ai_memory_maintenance_jobs.memory_id"], name="fk_memory_maintenance_input_job", ondelete="RESTRICT"
        ),
        UniqueConstraint("job_id", "source_id", name="uq_memory_maintenance_input_source"),
        Index("ix_memory_maintenance_input_source", "memory_id", "source_id"),
        CheckConstraint("source_id > 0 AND source_revision > 0 AND source_generation > 0", name="ck_memory_maintenance_input_counters"),
        {"schema": "usr_ai"},
    )
    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    input_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    valid_at: Mapped[str | None] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    coverage: Mapped[str] = mapped_column(String(24), nullable=False, default="included")
    defer_reason: Mapped[str | None] = mapped_column(String(64))


class MemoryMaintenanceCandidate(Base):
    __tablename__ = "t_ai_memory_maintenance_candidates"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "memory_id"], ["usr_ai.t_ai_memory_maintenance_jobs.job_id", "usr_ai.t_ai_memory_maintenance_jobs.memory_id"], name="fk_memory_maintenance_candidate_job", ondelete="RESTRICT"
        ),
        Index("ix_memory_maintenance_candidate_job", "job_id", "state"),
        CheckConstraint("revision > 0 AND (expected_revision IS NULL OR expected_revision > 0)", name="ck_memory_maintenance_candidate_revision"),
        {"schema": "usr_ai"},
    )
    candidate_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_entry_id: Mapped[str | None] = mapped_column(String(32))
    expected_revision: Mapped[int | None] = mapped_column(BigInteger)
    message_type: Mapped[str | None] = mapped_column(String(24))
    content: Mapped[str | None] = mapped_column(Text)
    base_content: Mapped[str | None] = mapped_column(Text)
    base_content_sha256: Mapped[str | None] = mapped_column(String(64))
    patch: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    diff: Mapped[str | None] = mapped_column(Text)
    source_input_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text)
    assessment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    review_actor_id: Mapped[str | None] = mapped_column(String(32))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_operation_id: Mapped[str | None] = mapped_column(String(128))
    publication_operation_id: Mapped[str | None] = mapped_column(String(128))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MEMORY_MAINTENANCE_MODELS = (MemoryMaintenanceJob, MemoryMaintenanceInput, MemoryMaintenanceCandidate)
