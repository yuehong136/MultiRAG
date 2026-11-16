"""Live-memory authorization and strict storage wiring; never construct a model."""

from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_context_service import require_memory
from api.db.joint_services.memory_write_runtime import supports_coordinated_writes
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import RevisionValidationError
from memory.revisions.storage import ManagedMessageStore
from memory.verification.service import VerificationService
from memory.writes.providers import MemoryWriteProviders


async def verifications(db: AsyncSession, memory_id: str, *, actor_id: str) -> VerificationService:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    if not supports_coordinated_writes():
        raise RevisionValidationError("verification_backend_unsupported")
    store = ManagedMessageStore(binding.tenant_id, memory_id)
    permissions = MemoryWriteProviders(db, tenant_id=binding.tenant_id, actor_id=actor_id)
    snapshots = MaintenanceSnapshots(db, tenant_id=binding.tenant_id, storage=store, count_tokens=len)
    return VerificationService(
        db, tenant_id=binding.tenant_id, actor_id=actor_id, authorize=permissions.authorize, storage=store, ready_entry=snapshots._ready_entry, ready_entries=snapshots._ready_entries
    )
