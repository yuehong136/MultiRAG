"""Authorized adapters for candidate analysis, explicit review and publication."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_context_service import require_memory
from api.db.joint_services.memory_write_runtime import supports_coordinated_writes
from common.token_utils import encoder
from memory.maintenance.jobs import JobService
from memory.maintenance.provider import MaintenanceAnalyst
from memory.maintenance.publication import PublicationService
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import RevisionValidationError
from memory.revisions.identifiers import MEMORY_COUNTER_KEY, allocate_message_id
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import MemoryWriteProviders

_COUNTER_KEY = MEMORY_COUNTER_KEY


def count_tokens(content: str) -> int:
    return len(encoder.encode(content, disallowed_special=()))


async def _bindings(db: AsyncSession, memory_id: str, actor_id: str) -> tuple[MemoryWriteProviders, ManagedMessageStore, MaintenanceSnapshots]:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    if not supports_coordinated_writes():
        raise RevisionValidationError("maintenance_backend_unsupported")
    provider = MemoryWriteProviders(db, tenant_id=binding.tenant_id, actor_id=actor_id)
    storage = ManagedMessageStore(binding.tenant_id, memory_id)
    snapshots = MaintenanceSnapshots(db, tenant_id=binding.tenant_id, storage=storage, count_tokens=count_tokens)
    return provider, storage, snapshots


async def jobs(db: AsyncSession, memory_id: str, *, actor_id: str) -> JobService:
    provider, _, snapshots = await _bindings(db, memory_id, actor_id)
    return JobService(
        db,
        tenant_id=provider.tenant_id,
        actor_id=actor_id,
        authorize=provider.authorize,
        snapshotter=snapshots.snapshot,
        validate_inputs=snapshots.validate,
        analyzer=MaintenanceAnalyst(provider).analyze,
        count_tokens=count_tokens,
    )


async def publications(db: AsyncSession, memory_id: str, *, actor_id: str) -> PublicationService:
    provider, storage, snapshots = await _bindings(db, memory_id, actor_id)

    async def allocate(*, minimum_message_id: int) -> int:
        return await allocate_message_id(minimum_message_id=minimum_message_id, counter_key=_COUNTER_KEY)

    return PublicationService(
        db,
        tenant_id=provider.tenant_id,
        actor_id=actor_id,
        authorize=provider.authorize,
        model_binding=provider.model_binding,
        embedder=provider.embed,
        storage=storage,
        allocate_message_id=allocate,
        count_tokens=count_tokens,
        validate_inputs=snapshots.validate,
    )
