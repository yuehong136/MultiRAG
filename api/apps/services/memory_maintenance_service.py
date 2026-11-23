"""Authorized adapters for candidate analysis, explicit review and publication."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services.memory_context_service import require_memory
from api.db.joint_services.memory_write_runtime import supports_coordinated_writes
from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from common.asyncio_utils import finish_owned_work
from common.constants import LLMType
from common.doc_store.doc_store_base import MatchDenseExpr
from common.token_utils import encoder
from memory.maintenance.checkpoints import IncrementalCheckpoints
from memory.maintenance.execution import ExecutionService
from memory.maintenance.incremental import DiscoveryService
from memory.maintenance.jobs import JobService
from memory.maintenance.profiles import analysis_identity
from memory.maintenance.provider import MaintenanceAnalyst
from memory.maintenance.publication import PublicationService
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.maintenance.target_conditions import TargetConditions
from memory.maintenance.targets import SEMANTIC_LIMIT, TargetRecommendations
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionValidationError, vector_dimension
from memory.revisions.identifiers import MEMORY_COUNTER_KEY, allocate_message_id
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService
from memory.writes.providers import MemoryWriteProviders, embedding_binding, release_sql
from memory.writes.types import EmbeddingResult

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
    return _jobs(provider, snapshots)


def _jobs(provider: MemoryWriteProviders, snapshots: MaintenanceSnapshots) -> JobService:
    async def identity(memory_id: str) -> dict[str, str]:
        return await analysis_identity(provider, memory_id)

    checkpoints = IncrementalCheckpoints(provider.db, tenant_id=provider.tenant_id, identity=identity)
    return JobService(
        provider.db,
        tenant_id=provider.tenant_id,
        actor_id=provider.actor_id,
        authorize=provider.authorize,
        snapshotter=snapshots.snapshot,
        validate_inputs=snapshots.validate,
        analyzer=MaintenanceAnalyst(provider).analyze,
        count_tokens=count_tokens,
        validate_binding=checkpoints.validate,
        checkpoint=checkpoints.checkpoint,
        segment_snapshotter=snapshots.segment_snapshot,
        target_conditions=TargetConditions(
            provider.db, tenant_id=provider.tenant_id, authorize=provider.authorize, storage=snapshots.storage, model_binding=provider.model_binding, snapshots=snapshots
        ),
    )


async def discoveries(db: AsyncSession, memory_id: str, *, actor_id: str) -> DiscoveryService:
    provider, _, snapshots = await _bindings(db, memory_id, actor_id)

    async def identity(mid: str) -> dict[str, str]:
        return await analysis_identity(provider, mid)

    return DiscoveryService(db, jobs=_jobs(provider, snapshots), identity=identity, validate_entry=snapshots._ready_entry, segment_planner=snapshots.plan_segments)


async def executions(db: AsyncSession, memory_id: str, *, actor_id: str) -> ExecutionService:
    return ExecutionService(db, jobs=await jobs(db, memory_id, actor_id=actor_id))


async def targets(db: AsyncSession, memory_id: str, *, actor_id: str) -> TargetRecommendations:
    provider, storage, snapshots = await _bindings(db, memory_id, actor_id)
    conditions = TargetConditions(db, tenant_id=provider.tenant_id, authorize=provider.authorize, storage=storage, model_binding=provider.model_binding, snapshots=snapshots)

    async def query_embedding(mid: str, messages: list[dict[str, Any]], expected: ModelBinding | None) -> EmbeddingResult:
        _, prepared = await provider._prepared(mid, LLMType.EMBEDDING.value)
        await release_sql(db)
        if expected is None or embedding_binding(prepared, expected.dimension) != expected:
            raise RevisionConflict("target_model_changed")
        async with llm_bundle_scope():
            bundle = await finish_owned_work(asyncio.to_thread(LLMBundle.from_prepared, prepared))
            vector, _ = await finish_owned_work(asyncio.to_thread(bundle.encode_queries, messages[0]["content"]))
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        dimension = vector_dimension({"content_embed": vector})
        if dimension != expected.dimension:
            raise RevisionConflict("target_model_changed")
        return {"vectors": [[float(value) for value in vector]], "model_binding": expected}

    async def recall(mid: str, user_id: str, agent_id: str, vector: list[float]) -> list[dict[str, Any]]:
        dense = MatchDenseExpr(f"q_{len(vector)}_vec", vector, "float", "cosine", SEMANTIC_LIMIT, {"similarity": 0.0})
        return await finish_owned_work(
            asyncio.to_thread(
                MessageService.search_message,
                [mid],
                {"user_id": [user_id], "agent_id": [agent_id], "message_type": ["semantic", "episodic", "procedural"]},
                [provider.tenant_id],
                [dense],
                SEMANTIC_LIMIT,
                strict=True,
            )
        )

    return TargetRecommendations(db, conditions=conditions, embedder=query_embedding, recall=recall, count_tokens=count_tokens)


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
