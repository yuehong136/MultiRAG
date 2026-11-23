"""Independent context artifacts and ordered root dependencies; no message rows."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryCompactionArtifact(Base):
    __tablename__ = "t_ai_memory_compaction_artifacts"
    __table_args__ = (
        UniqueConstraint("memory_id", "operation_id", name="uq_memory_compaction_operation"),
        UniqueConstraint("artifact_id", "memory_id", name="uq_memory_compaction_scope"),
        CheckConstraint("state IN ('running','generated','failed','stale','canceled','persistence_unknown')", name="ck_memory_compaction_state"),
        CheckConstraint(
            "(retired_at IS NULL AND retirement IS NULL) OR (retired_at IS NOT NULL AND retirement IS NOT NULL AND package IS NULL AND redacted AND cancellation_requested AND state = 'stale')",
            name="ck_memory_compaction_retirement",
        ),
        Index("ix_memory_compaction_list", "memory_id", "created_at", "artifact_id"),
        Index("ix_memory_compaction_retention", "memory_id", "tenant_id", "retired_at", "state"),
        {"schema": "usr_ai"},
    )
    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    input_manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    package: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retirement: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryCompactionRoot(Base):
    __tablename__ = "t_ai_memory_compaction_roots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["artifact_id", "memory_id"],
            ["usr_ai.t_ai_memory_compaction_artifacts.artifact_id", "usr_ai.t_ai_memory_compaction_artifacts.memory_id"],
            name="fk_memory_compaction_root",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("artifact_id", "source_id", name="uq_memory_compaction_root_source"),
        CheckConstraint("ordinal >= 0 AND source_id > 0 AND source_revision > 0 AND source_generation > 0", name="ck_memory_compaction_root_version"),
        Index("ix_memory_compaction_source", "memory_id", "tenant_id", "source_id"),
        {"schema": "usr_ai"},
    )
    artifact_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    provenance_sha256: Mapped[str | None] = mapped_column(String(64))
    binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


MEMORY_COMPACTION_MODELS = (MemoryCompactionArtifact, MemoryCompactionRoot)
