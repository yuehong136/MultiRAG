"""All-bank ownership of external memory writer attempts."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryWriterFence(Base):
    __tablename__ = "t_ai_memory_writer_fences"
    __table_args__ = (CheckConstraint("generation > 0 AND capacity_generation > 0 AND reserved_bytes >= 0", name="ck_memory_writer_generation"), {"schema": "usr_ai"})

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    capacity_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    close_operation_id: Mapped[str | None] = mapped_column(String(128))
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryWriterAdmission(Base):
    __tablename__ = "t_ai_memory_writer_admissions"
    __table_args__ = (
        UniqueConstraint("memory_id", "task_id", name="uq_memory_writer_task"),
        Index("ix_memory_writer_drain", "memory_id", "state", "active_token"),
        Index("ix_memory_writer_source", "memory_id", "source_message_id"),
        CheckConstraint("writer_generation > 0 AND attempt >= 0 AND (source_message_id IS NULL OR source_message_id > 0)", name="ck_memory_writer_counters"),
        {"schema": "usr_ai"},
    )

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    writer_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    publication_state: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    message_ids: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    task_id: Mapped[str | None] = mapped_column(String(32))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    active_token: Mapped[str | None] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MEMORY_WRITER_MODELS = (MemoryWriterFence, MemoryWriterAdmission)
