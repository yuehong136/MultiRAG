"""Same-transaction erasure of verification notes when their evidence is removed."""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_verification_models import MemoryVerificationRecord, MemoryVerificationSource


async def redact_for_source(db: AsyncSession, *, memory_id: str, tenant_id: str, source_id: int) -> None:
    selected = select(MemoryVerificationSource.verification_id).where(
        MemoryVerificationSource.memory_id == memory_id, MemoryVerificationSource.tenant_id == tenant_id, MemoryVerificationSource.source_id == source_id
    )
    await db.execute(
        update(MemoryVerificationRecord)
        .where(MemoryVerificationRecord.memory_id == memory_id, MemoryVerificationRecord.tenant_id == tenant_id, MemoryVerificationRecord.verification_id.in_(selected))
        .values(state="redacted", applicability=None, notes=None, evidence=[], revocation_reason=None)
    )


async def redact_for_entry(db: AsyncSession, *, memory_id: str, tenant_id: str, entry_id: str) -> None:
    await db.execute(
        update(MemoryVerificationRecord)
        .where(MemoryVerificationRecord.memory_id == memory_id, MemoryVerificationRecord.tenant_id == tenant_id, MemoryVerificationRecord.entry_id == entry_id)
        .values(state="redacted", applicability=None, notes=None, evidence=[], revocation_reason=None)
    )


async def redact_for_bank(db: AsyncSession, *, memory_id: str, tenant_id: str) -> None:
    await db.execute(
        update(MemoryVerificationRecord)
        .where(MemoryVerificationRecord.memory_id == memory_id, MemoryVerificationRecord.tenant_id == tenant_id)
        .values(state="redacted", applicability=None, notes=None, evidence=[], revocation_reason=None)
    )
