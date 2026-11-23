"""Human verification events and version-bound dependencies, separate from facts."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryVerificationRecord(Base):
    __tablename__ = "t_ai_memory_verification_records"
    __table_args__ = (
        UniqueConstraint("verification_id", "memory_id", name="uq_memory_verification_record_scope"),
        UniqueConstraint("memory_id", "entry_id", "record_version", name="uq_memory_verification_entry_version"),
        CheckConstraint("record_version > 0", name="ck_memory_verification_record_version"),
        CheckConstraint("result IN ('human_confirmed','disputed','needs_review')", name="ck_memory_verification_result"),
        CheckConstraint("state IN ('active','revoked','redacted')", name="ck_memory_verification_state"),
        Index("ix_memory_verification_history", "memory_id", "entry_id", "record_version"),
        {"schema": "usr_ai"},
    )
    verification_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_id: Mapped[str] = mapped_column(String(32), nullable=False)
    record_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_binding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(String(24), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    applicability: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    checked_at_is_user_reported: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[str | None] = mapped_column(String(32))
    revocation_reason: Mapped[str | None] = mapped_column(Text)


class MemoryVerificationHead(Base):
    __tablename__ = "t_ai_memory_verification_heads"
    __table_args__ = (CheckConstraint("head_version > 0", name="ck_memory_verification_head_version"), {"schema": "usr_ai"})
    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    head_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verification_id: Mapped[str] = mapped_column(String(32), nullable=False)


class MemoryVerificationSource(Base):
    __tablename__ = "t_ai_memory_verification_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["verification_id", "memory_id"],
            ["usr_ai.t_ai_memory_verification_records.verification_id", "usr_ai.t_ai_memory_verification_records.memory_id"],
            name="fk_memory_verification_source_record",
            ondelete="RESTRICT",
        ),
        CheckConstraint("source_id > 0 AND source_revision > 0 AND source_generation > 0", name="ck_memory_verification_source_version"),
        Index("ix_memory_verification_source", "memory_id", "tenant_id", "source_id"),
        {"schema": "usr_ai"},
    )
    verification_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    projection_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class MemoryVerificationOperation(Base):
    __tablename__ = "t_ai_memory_verification_operations"
    __table_args__ = ({"schema": "usr_ai"},)
    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_id: Mapped[str] = mapped_column(String(32), nullable=False)
    head_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MEMORY_VERIFICATION_MODELS = (MemoryVerificationRecord, MemoryVerificationHead, MemoryVerificationSource, MemoryVerificationOperation)
