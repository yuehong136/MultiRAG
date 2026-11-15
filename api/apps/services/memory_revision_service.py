"""Authenticated revision commands with SQL preparation and owned external IO."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_context_service import MemoryReadBinding, _estimate_context_tokens, _prepare_embedding, _release_read_transaction, require_memory
from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation
from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from api.db.services.memory_service import MemoryService
from api.db.services.tenant_llm_service import PreparedTenantLLM
from common.asyncio_utils import finish_owned_work
from common.exceptions import NotFoundException
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionPersistenceUnknown, RevisionValidationError, positive_int
from memory.revisions.projection import reconcile_operation as reconcile_projection
from memory.revisions.service import RevisionService
from memory.revisions.sources import SourceService
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService

_COUNTER_KEY = "id_generator:memory"
_ALLOCATE_ID = """
local current = redis.call('GET', KEYS[1]) or '1'
if not string.match(current, '^%d+$') then
    return redis.error_reply('Invalid memory counter')
end
current = string.gsub(current, '^0+', '')
if current == '' then current = '0' end
local minimum = ARGV[1]
if string.len(current) < string.len(minimum)
   or (string.len(current) == string.len(minimum) and current < minimum) then
    redis.call('SET', KEYS[1], minimum)
elseif redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('SET', KEYS[1], current)
end
redis.call('INCRBY', KEYS[1], 1)
return redis.call('GET', KEYS[1])
"""


class LegacyStateReconciliationRequired(RevisionConflict):
    """Preserve the recovery handle generated for a legacy state command."""

    def __init__(self, operation_id: str, code: str = "state_change_requires_reconciliation") -> None:
        self.operation_id = operation_id
        super().__init__(code)


async def _allocate_message_id(*, minimum_message_id: int) -> int:
    """Allocate exactly with Redis integer arithmetic, including above 2**53."""
    positive_int(minimum_message_id, "invalid_message_id")
    if minimum_message_id == 2**63 - 1:
        raise RevisionValidationError("message_id_exhausted")
    value = await finish_owned_work(asyncio.to_thread(REDIS_CONN.REDIS.eval, _ALLOCATE_ID, 1, _COUNTER_KEY, str(minimum_message_id)))
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise RuntimeError("Memory ID allocation was not confirmed")
    return positive_int(int(value), "invalid_message_id")


def _model_reference(prepared: PreparedTenantLLM, dimension: int) -> ModelBinding:
    """Persist an opaque config fingerprint and public model identity, never credentials."""
    config = prepared.model_config
    # Usage counters and audit timestamps change on every successful embedding;
    # only identity and provider-construction inputs define this preparation.
    fields = ("id", "tenant_id", "llm_factory", "llm_name", "mdl_type", "model_type", "api_key", "api_base", "region", "max_tokens", "instance_id")
    encoded = json.dumps({field: config.get(field) for field in fields}, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return ModelBinding(
        tenant_model_id=config.get("id"),
        model_reference=f"{config['llm_name']}@{config['llm_factory']}",
        dimension=dimension,
        config_reference=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


async def _authorize(db: AsyncSession, memory_id: str, *, tenant_id: str, actor_id: str) -> MemoryReadBinding:
    tenant_ids = await db.run_sync(lambda session: MemoryService.accessible_tenant_ids(session, actor_id))  # TODO(async-phase4)
    row = (
        await db.execute(
            select(Memory.id, Memory.tenant_id, Memory.embd_id, Memory.tenant_embd_id).where(
                Memory.id == memory_id,
                Memory.tenant_id == tenant_id,
                or_(Memory.tenant_id == actor_id, and_(Memory.permissions == "team", Memory.tenant_id.in_(tenant_ids))),
            )
        )
    ).one_or_none()
    if row is None:
        raise NotFoundException("Memory not found.")
    return MemoryReadBinding(row.id, row.tenant_id, row.embd_id, row.tenant_embd_id)


async def _prepare_model(db: AsyncSession, memory_id: str, dimension: int, *, tenant_id: str, actor_id: str) -> tuple[PreparedTenantLLM, ModelBinding]:
    binding = await _authorize(db, memory_id, tenant_id=tenant_id, actor_id=actor_id)
    prepared = await db.run_sync(lambda session: _prepare_embedding(session, binding))  # TODO(async-phase4)
    return prepared, _model_reference(prepared, dimension)


async def _embed_revision(db: AsyncSession, memory_id: str, content: str, expected: ModelBinding | dict[str, Any], *, tenant_id: str, actor_id: str) -> dict[str, Any]:
    expected_binding = expected if isinstance(expected, ModelBinding) else ModelBinding(**expected)
    prepared, current = await _prepare_model(db, memory_id, expected_binding.dimension, tenant_id=tenant_id, actor_id=actor_id)
    await _release_read_transaction(db)
    if current != expected_binding:
        raise RevisionConflict("embedding_model_changed")
    async with llm_bundle_scope():
        bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, prepared))
        vectors, _ = await finish_owned_work(asyncio.to_thread(bundle.encode, [content]))
    if hasattr(vectors, "tolist"):
        vectors = vectors.tolist()
    if not isinstance(vectors, list) or len(vectors) != 1:
        raise RevisionConflict("embedding_result_mismatch")
    return {"vector": vectors[0], "model_binding": asdict(current)}


async def _components(db: AsyncSession, memory_id: str, actor_id: str) -> tuple[RevisionService, ManagedMessageStore]:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    store = ManagedMessageStore(binding.tenant_id, memory_id)

    async def authorize(session: AsyncSession, requested_memory: str) -> None:
        if requested_memory != memory_id:
            raise NotFoundException("Memory not found.")
        await _authorize(session, memory_id, tenant_id=binding.tenant_id, actor_id=actor_id)

    async def read(requested_memory: str, message_id: int) -> dict[str, Any] | None:
        if requested_memory != memory_id:
            raise NotFoundException("Memory not found.")
        return await store.read(message_id)

    async def model(session: AsyncSession, requested_memory: str, dimension: int) -> ModelBinding:
        _, reference = await _prepare_model(session, requested_memory, dimension, tenant_id=binding.tenant_id, actor_id=actor_id)
        return reference

    async def measure(requested_memory: str) -> int:
        if requested_memory != memory_id:
            raise NotFoundException("Memory not found.")
        return await store.measure_bytes()

    service = RevisionService(
        db,
        tenant_id=binding.tenant_id,
        actor_id=actor_id,
        message_reader=read,
        allocate_message_id=_allocate_message_id,
        count_tokens=_estimate_context_tokens,
        authorize=authorize,
        model_binding=model,
        measure_memory_bytes=measure,
    )
    return service, store


async def adopt_entry(db: AsyncSession, memory_id: str, *, actor_id: str, operation_id: str, legacy_message_id: int) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    await _require_backend(store)
    return await service.adopt_entry(memory_id, operation_id=operation_id, legacy_message_id=legacy_message_id)


async def get_entry(db: AsyncSession, memory_id: str, entry_id: str, *, actor_id: str) -> dict[str, Any]:
    service, _ = await _components(db, memory_id, actor_id)
    return await service.get_entry(memory_id, entry_id)


async def find_entry_by_message(db: AsyncSession, memory_id: str, message_id: int, *, actor_id: str) -> dict[str, Any] | None:
    service, _ = await _components(db, memory_id, actor_id)
    return await service.find_entry_by_message(memory_id, message_id)


async def list_revisions(db: AsyncSession, memory_id: str, entry_id: str, *, actor_id: str, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    service, _ = await _components(db, memory_id, actor_id)
    return await service.list_revisions(memory_id, entry_id, limit=limit, cursor=cursor)


async def get_operation(db: AsyncSession, memory_id: str, operation_id: str, *, actor_id: str) -> dict[str, Any]:
    service, _ = await _components(db, memory_id, actor_id)
    operation = await service.get_operation(memory_id, operation_id)
    metadata = await db.scalar(
        select(MemoryMutationOperation.result).where(
            MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.tenant_id == service.tenant_id, MemoryMutationOperation.operation_id == operation_id
        )
    )
    await _release_read_transaction(db)
    fields = {"source", "origin", "stop_supply", "cleanup_scope", "physical_cleanup_complete", "projection_cleanup_status", "untraceable_copies", "reconciliation_operation_id"}
    return {**operation, **{key: value for key, value in (metadata or {}).items() if key in fields}}


async def _reconcile(db: AsyncSession, service: RevisionService, store: ManagedMessageStore, memory_id: str, operation_id: str, actor_id: str) -> dict[str, Any]:
    operation = await service.get_operation(memory_id, operation_id)
    kind = await db.scalar(select(MemoryMutationOperation.kind).where(MemoryMutationOperation.memory_id == memory_id, MemoryMutationOperation.operation_id == operation_id))
    await _release_read_transaction(db)
    if operation.get("index_sync_status") == "ready":
        return operation
    if kind in {"withdraw_source", "change_state", "delete_memory", "adopt_source"}:
        source_service = _sources(db, service, store, actor_id)
        return await source_service.reconcile_operation(memory_id, operation_id, storage=store)

    async def embed(content: str, expected: ModelBinding | dict[str, Any]) -> dict[str, Any]:
        return await _embed_revision(db, memory_id, content, expected, tenant_id=service.tenant_id, actor_id=actor_id)

    return await reconcile_projection(service, memory_id, operation_id, embedder=embed, physical_reader=store.read, physical_writer=store.write, status_writer=store.set_status)


async def reconcile_operation(db: AsyncSession, memory_id: str, operation_id: str, *, actor_id: str) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    return await _reconcile(db, service, store, memory_id, operation_id, actor_id)


async def revise_entry(
    db: AsyncSession,
    memory_id: str,
    entry_id: str,
    *,
    actor_id: str,
    operation_id: str,
    expected_revision: int,
    content: str | None = None,
    patch: dict[str, Any] | None = None,
    reason: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    result = await service.revise_entry(memory_id, entry_id, operation_id=operation_id, expected_revision=expected_revision, content=content, patch=patch, reason=reason, dry_run=dry_run)
    if dry_run:
        return result
    return await _reconcile(db, service, store, memory_id, operation_id, actor_id)


def _sources(db: AsyncSession, service: RevisionService, store: ManagedMessageStore, actor_id: str) -> SourceService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        if memory_id != store.memory_id:
            raise NotFoundException("Memory not found.")
        await _authorize(session, memory_id, tenant_id=service.tenant_id, actor_id=actor_id)

    async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
        if memory_id != store.memory_id:
            raise NotFoundException("Memory not found.")
        return await store.read(message_id)

    return SourceService(db, tenant_id=service.tenant_id, actor_id=actor_id, authorize=authorize, message_reader=read)


async def get_source_state(db: AsyncSession, memory_id: str, source_id: int, *, actor_id: str) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    return await _sources(db, service, store, actor_id).get_source_state(memory_id, source_id)


async def adopt_source(db: AsyncSession, memory_id: str, source_id: int, *, actor_id: str, operation_id: str, expected_content_sha256: str) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    await _require_backend(store)
    return await _sources(db, service, store, actor_id).adopt_source(memory_id, source_id, operation_id=operation_id, expected_content_sha256=expected_content_sha256)


async def _require_backend(store: ManagedMessageStore) -> None:
    capabilities = await finish_owned_work(asyncio.to_thread(MessageService.get_recent_read_capabilities, tenant_id=store.tenant_id, memory_id=store.memory_id))
    if capabilities.get("managed_revisions_supported") is not True:
        raise RevisionValidationError("managed_backend_unsupported")


async def withdraw_source(db: AsyncSession, memory_id: str, source_id: int, *, actor_id: str, operation_id: str, expected_source_revision: int, reason: str) -> dict[str, Any]:
    service, store = await _components(db, memory_id, actor_id)
    sources = _sources(db, service, store, actor_id)
    await sources.withdraw_source(memory_id, source_id, operation_id=operation_id, expected_source_revision=expected_source_revision, reason=reason)
    return await sources.reconcile_operation(memory_id, operation_id, storage=store)


async def is_managed(db: AsyncSession, memory_id: str, *, actor_id: str) -> bool:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    found = await db.scalar(select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == binding.tenant_id))
    await _release_read_transaction(db)
    return found is not None


async def change_legacy_message_state(db: AsyncSession, memory_id: str, message_id: int, *, actor_id: str, status: bool | None = None, forget: bool = False) -> bool | None:
    """Return None for the unchanged unmanaged path; coordinate managed changes."""
    if not await is_managed(db, memory_id, actor_id=actor_id):
        return None
    service, store = await _components(db, memory_id, actor_id)
    sources = _sources(db, service, store, actor_id)
    operation_id = "legacy-state:" + uuid4().hex
    try:
        await sources.change_physical_state(memory_id, message_id, operation_id=operation_id, status=status, forget=forget)
    except RevisionPersistenceUnknown as error:
        raise LegacyStateReconciliationRequired(operation_id, error.code) from error
    try:
        operation = await sources.reconcile_operation(memory_id, operation_id, storage=store)
    except RevisionPersistenceUnknown as error:
        raise LegacyStateReconciliationRequired(operation_id, error.code) from error
    except RevisionConflict as error:
        raise LegacyStateReconciliationRequired(operation_id, error.code) from error
    except Exception as error:
        # The command is already durable; later SQL/IO failures must retain its
        # server-generated recovery handle without exposing exception details.
        raise LegacyStateReconciliationRequired(operation_id, "state_change_reconciliation_unconfirmed") from error
    if operation["index_sync_status"] != "ready":
        raise LegacyStateReconciliationRequired(operation_id, operation.get("error_code") or "state_change_requires_reconciliation")
    return True
