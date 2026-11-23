"""Metadata-only discovery plans and versioned maintenance analysis coverage."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryMaintenanceDiscovery(Base):
    __tablename__ = "t_ai_memory_maintenance_discoveries"
    __table_args__ = (
        UniqueConstraint("memory_id", "operation_id", name="uq_memory_discovery_operation"),
        UniqueConstraint("discovery_id", "memory_id", name="uq_memory_discovery_scope"),
        Index("ix_memory_discovery_bank", "memory_id", "created_at"),
        {"schema": "usr_ai"},
    )
    discovery_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    has_more: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="planned")
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryMaintenanceBatch(Base):
    __tablename__ = "t_ai_memory_maintenance_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["discovery_id", "memory_id"],
            ["usr_ai.t_ai_memory_maintenance_discoveries.discovery_id", "usr_ai.t_ai_memory_maintenance_discoveries.memory_id"],
            name="fk_memory_batch_discovery",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("batch_id", "memory_id", name="uq_memory_batch_scope"),
        UniqueConstraint("discovery_id", "ordinal", name="uq_memory_batch_ordinal"),
        UniqueConstraint("job_id", name="uq_memory_batch_job"),
        CheckConstraint("ordinal >= 0", name="ck_memory_batch_ordinal"),
        Index("ix_memory_batch_bank", "memory_id", "discovery_id"),
        {"schema": "usr_ai"},
    )
    batch_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    discovery_id: Mapped[str] = mapped_column(String(32), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    create_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    analysis_operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    primary_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    context_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    entry_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="planned")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryMaintenanceCoverage(Base):
    __tablename__ = "t_ai_memory_maintenance_coverage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id", "memory_id"], ["usr_ai.t_ai_memory_maintenance_batches.batch_id", "usr_ai.t_ai_memory_maintenance_batches.memory_id"], name="fk_memory_coverage_batch", ondelete="RESTRICT"
        ),
        UniqueConstraint("memory_id", "profile_sha256", "source_id", "source_revision", "source_generation", "content_sha256", name="uq_memory_coverage_version"),
        CheckConstraint("source_id > 0 AND source_revision > 0 AND source_generation > 0", name="ck_memory_coverage_version"),
        CheckConstraint("expected_fragments >= 0 AND expected_fragments <= 64", name="ck_memory_coverage_fragments"),
        Index("ix_memory_coverage_bank", "memory_id", "profile_sha256", "state"),
        {"schema": "usr_ai"},
    )
    coverage_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    profile_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    expected_fragments: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryMaintenanceFragmentCoverage(Base):
    """Immutable span identity and its current child owner, without source text."""

    __tablename__ = "t_ai_memory_maintenance_fragments"
    __table_args__ = (
        ForeignKeyConstraint(["coverage_key"], ["usr_ai.t_ai_memory_maintenance_coverage.coverage_key"], name="fk_memory_fragment_coverage", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["batch_id", "memory_id"], ["usr_ai.t_ai_memory_maintenance_batches.batch_id", "usr_ai.t_ai_memory_maintenance_batches.memory_id"], name="fk_memory_fragment_batch", ondelete="RESTRICT"
        ),
        UniqueConstraint("coverage_key", "part_index", name="uq_memory_fragment_part"),
        CheckConstraint("part_index >= 0 AND part_index < part_count AND part_count <= 64", name="ck_memory_fragment_part"),
        Index("ix_memory_fragment_owner", "memory_id", "batch_id"),
        {"schema": "usr_ai"},
    )
    fragment_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    coverage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    fragment_id: Mapped[str] = mapped_column(String(64), nullable=False)
    part_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fragment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    batch_id: Mapped[str] = mapped_column(String(32), nullable=False)
    job_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reserved")
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MEMORY_INCREMENTAL_MODELS = (MemoryMaintenanceDiscovery, MemoryMaintenanceBatch, MemoryMaintenanceCoverage)
