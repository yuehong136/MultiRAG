"""Independent SQL state for managed memory revisions and external publication."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db.db_models import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ManagedMemoryState(Base):
    __tablename__ = "t_ai_memory_managed_states"
    __table_args__ = (CheckConstraint("generation > 0 AND capacity_generation > 0 AND reserved_bytes >= 0", name="ck_memory_managed_counters"), {"schema": "usr_ai"})

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    capacity_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemorySourceState(Base):
    __tablename__ = "t_ai_memory_source_states"
    __table_args__ = (CheckConstraint("source_revision > 0 AND generation > 0 AND source_message_id > 0", name="ck_memory_source_counters"), {"schema": "usr_ai"})

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    scope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryEntry(Base):
    __tablename__ = "t_ai_memory_entries"
    __table_args__ = (
        UniqueConstraint("memory_id", "legacy_message_id", name="uq_memory_entry_legacy_message"),
        UniqueConstraint("entry_id", "memory_id", name="uq_memory_entry_scope"),
        CheckConstraint("current_revision > 0 AND next_revision > current_revision AND generation > 0 AND current_message_id > 0", name="ck_memory_entry_counters"),
        {"schema": "usr_ai"},
    )

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    legacy_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    next_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=2)
    current_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryEntryRevision(Base):
    __tablename__ = "t_ai_memory_entry_revisions"
    __table_args__ = (
        ForeignKeyConstraint(["entry_id", "memory_id"], ["usr_ai.t_ai_memory_entries.entry_id", "usr_ai.t_ai_memory_entries.memory_id"], name="fk_memory_revision_entry_scope", ondelete="RESTRICT"),
        UniqueConstraint("memory_id", "message_id", name="uq_memory_revision_message"),
        CheckConstraint("revision > 0 AND message_id > 0", name="ck_memory_revision_counters"),
        {"schema": "usr_ai"},
    )

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parent_revision: Mapped[int | None] = mapped_column(BigInteger)
    content: Mapped[str | None] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    assessment: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    message_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryRevisionSource(Base):
    __tablename__ = "t_ai_memory_revision_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["usr_ai.t_ai_memory_entry_revisions.entry_id", "usr_ai.t_ai_memory_entry_revisions.revision"], name="fk_memory_revision_source_revision", ondelete="RESTRICT"
        ),
        Index("ix_memory_revision_source_lookup", "memory_id", "source_message_id"),
        CheckConstraint("source_revision > 0 AND source_generation > 0 AND source_message_id > 0", name="ck_memory_revision_source_counters"),
        {"schema": "usr_ai"},
    )

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    relation: Mapped[str] = mapped_column(String(24), nullable=False, default="context")
    locator: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class MemoryMutationOperation(Base):
    __tablename__ = "t_ai_memory_mutation_operations"
    __table_args__ = ({"schema": "usr_ai"},)

    memory_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    entry_id: Mapped[str | None] = mapped_column(String(32))
    new_revision: Mapped[int | None] = mapped_column(BigInteger)
    index_sync_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    error_code: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class MemoryPublicationOutbox(Base):
    __tablename__ = "t_ai_memory_publication_outbox"
    __table_args__ = (
        UniqueConstraint("memory_id", "operation_id", name="uq_memory_publication_operation"),
        ForeignKeyConstraint(
            ["entry_id", "revision"], ["usr_ai.t_ai_memory_entry_revisions.entry_id", "usr_ai.t_ai_memory_entry_revisions.revision"], name="fk_memory_publication_revision", ondelete="RESTRICT"
        ),
        Index("ix_memory_publication_ready", "state", "lease_until", "created_at"),
        CheckConstraint(
            "(revision IS NULL OR revision > 0) AND (message_id IS NULL OR message_id > 0) AND (expected_revision IS NULL OR expected_revision > 0) "
            "AND bank_generation > 0 AND (entry_generation IS NULL OR entry_generation > 0) AND attempt >= 0",
            name="ck_memory_publication_counters",
        ),
        {"schema": "usr_ai"},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_id: Mapped[str | None] = mapped_column(String(32))
    revision: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    expected_revision: Mapped[int | None] = mapped_column(BigInteger)
    bank_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entry_generation: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


MEMORY_REVISION_MODELS = (ManagedMemoryState, MemorySourceState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemoryMutationOperation, MemoryPublicationOutbox)
