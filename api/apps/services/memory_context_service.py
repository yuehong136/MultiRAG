"""Authorized memory reads and bounded query context without changing stored data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services.memory_api_service import _accessible_memories, _require_memory_access, _split_filter_values
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from common.constants import LLMType
from common.doc_store.doc_store_base import FusionExpr
from common.exceptions import NotFoundException
from common.token_utils import num_tokens_from_string
from memory.services.context import ContextBudget, ContextScope, QueryContext, SourceInfo, assemble_query_context, build_source_excerpt, message_in_scope, resolve_message_source
from memory.services.messages import MessageService, RecentMessagePage
from memory.services.query import MsgTextQuery, get_vector


@dataclass(frozen=True)
class MemoryReadBinding:
    """Plain authorized model binding; ORM instances never cross worker threads."""

    memory_id: str
    tenant_id: str
    embd_id: str
    tenant_embd_id: int | None


def _binding(memory: Any) -> MemoryReadBinding:
    return MemoryReadBinding(memory.id, memory.tenant_id, memory.embd_id, memory.tenant_embd_id)


async def _release_read_transaction(db: AsyncSession) -> None:
    """Return this read endpoint's SQL connection before slower external IO."""
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Memory reads require a session without pending writes")
    if db.in_transaction():
        await db.rollback()


async def require_memory(db: AsyncSession, memory_id: str, *, actor_id: str) -> MemoryReadBinding:
    """Use the existing owner/team policy before any message-store access."""
    binding = await db.run_sync(lambda session: _binding(_require_memory_access(session, actor_id, memory_id)))  # TODO(async-phase4)
    await _release_read_transaction(db)
    return binding


async def _visible_bindings(db: AsyncSession, memory_ids: list[str], *, actor_id: str) -> list[MemoryReadBinding]:
    ids = _split_filter_values(memory_ids)
    bindings = await db.run_sync(lambda session: [_binding(memory) for memory in _accessible_memories(session, actor_id, ids)])  # TODO(async-phase4)
    await _release_read_transaction(db)
    return bindings


def _embedding_bundle(db: Session, binding: MemoryReadBinding) -> LLMBundle:
    if binding.tenant_embd_id:
        config = get_model_config_by_id(db, binding.tenant_embd_id, tenant_id=binding.tenant_id, model_type="embedding")
    else:
        config = get_model_config_by_type_and_name(db, binding.tenant_id, LLMType.EMBEDDING.value, binding.embd_id)
    bundle = LLMBundle(db, binding.tenant_id, config)
    bundle.db = None
    return bundle


async def _search(
    db: AsyncSession,
    bindings: list[MemoryReadBinding],
    query: str,
    *,
    scope: ContextScope,
    top_n: int,
    similarity_threshold: float,
    keywords_similarity_weight: float,
) -> list[dict[str, Any]]:
    if not bindings:
        return []
    bundle = await db.run_sync(lambda session: _embedding_bundle(session, bindings[0]))  # TODO(async-phase4)
    await _release_read_transaction(db)

    def retrieve() -> list[dict[str, Any]]:
        dense = get_vector(query, bundle, topk=top_n, similarity=similarity_threshold)
        text, _ = MsgTextQuery().question(query, min_match=similarity_threshold)
        fusion = FusionExpr("weighted_sum", top_n, {"weights": f"{1 - keywords_similarity_weight},{keywords_similarity_weight}"})
        condition: dict[str, Any] = {}
        for name in ("user_id", "agent_id", "session_id"):
            value = getattr(scope, name)
            if value is not None:
                condition[name] = [value]
        messages = MessageService.search_message(
            [binding.memory_id for binding in bindings],
            condition,
            list(dict.fromkeys(binding.tenant_id for binding in bindings)),
            [text, dense, fusion],
            top_n,
        )
        return [message for message in messages if message_in_scope(message, scope)][:top_n]

    return await asyncio.to_thread(retrieve)


async def _sources(bindings: list[MemoryReadBinding], candidates: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    sources: dict[tuple[str, str], dict[str, Any]] = {}
    for binding in bindings:
        ids: set[int] = set()
        for candidate in candidates:
            if candidate.get("memory_id") != binding.memory_id:
                continue
            value = candidate.get("source_id")
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value > 0:
                ids.add(value)
            elif isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
                ids.add(int(value))
        if ids:
            found = await asyncio.to_thread(MessageService.get_source_messages, binding.tenant_id, binding.memory_id, sorted(ids))
            sources.update({(binding.memory_id, str(message_id)): source for message_id, source in found.items()})
    return sources


async def recent_messages(
    db: AsyncSession,
    memory_id: str,
    *,
    actor_id: str,
    limit: int = 10,
    agent_id: str = "",
    session_id: str = "",
    subject_id: str | None = None,
    message_type: str | None = None,
    status: bool | None = None,
    cursor: str | None = None,
) -> RecentMessagePage:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    return await asyncio.to_thread(
        MessageService.get_recent_page,
        binding.tenant_id,
        memory_id,
        agent_id,
        session_id,
        limit,
        message_type=message_type,
        user_id=subject_id,
        status=status,
        cursor=cursor,
    )


async def source_excerpt(
    db: AsyncSession,
    memory_id: str,
    source_id: int,
    *,
    actor_id: str,
    offset: int = 0,
    limit: int = 512,
    content_sha256: str | None = None,
    subject_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
) -> SourceInfo:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    sources = await asyncio.to_thread(MessageService.get_source_messages, binding.tenant_id, memory_id, [source_id])
    source = sources.get(source_id)
    if source is None:
        raise NotFoundException("Source not found.")
    scope = ContextScope(frozenset({memory_id}), user_id=subject_id, agent_id=agent_id, session_id=session_id)
    result = build_source_excerpt(source, scope=scope, offset=offset, limit=limit, content_sha256=content_sha256)
    if result["state"] != "available":
        raise NotFoundException("Source not found.")
    return result


@llm_bundle_scope()
async def query_context(
    db: AsyncSession,
    memory_id: str,
    query: str,
    *,
    actor_id: str,
    budget_tokens: int,
    max_items: int = 5,
    candidate_limit: int = 20,
    source_excerpt_chars: int = 512,
    include_sources: bool = True,
    subject_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    similarity_threshold: float = 0.2,
    keywords_similarity_weight: float = 0.7,
) -> QueryContext:
    binding = await require_memory(db, memory_id, actor_id=actor_id)
    scope = ContextScope(frozenset({memory_id}), user_id=subject_id, agent_id=agent_id, session_id=session_id)
    candidates = await _search(
        db,
        [binding],
        query,
        scope=scope,
        top_n=candidate_limit,
        similarity_threshold=similarity_threshold,
        keywords_similarity_weight=keywords_similarity_weight,
    )
    sources = await _sources([binding], candidates)
    return assemble_query_context(
        candidates,
        sources,
        scope=scope,
        budget=ContextBudget(budget_tokens, max_items=max_items, source_excerpt_chars=source_excerpt_chars),
        count_tokens=num_tokens_from_string,
        include_sources=include_sources,
    )


@llm_bundle_scope()
async def search_with_sources(
    db: AsyncSession,
    memory_ids: list[str],
    query: str,
    *,
    actor_id: str,
    top_n: int = 5,
    subject_id: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    source_excerpt_chars: int = 512,
    similarity_threshold: float = 0.2,
    keywords_similarity_weight: float = 0.7,
) -> list[dict[str, Any]]:
    bindings = await _visible_bindings(db, memory_ids, actor_id=actor_id)
    if not bindings:
        return []
    scope = ContextScope(frozenset(binding.memory_id for binding in bindings), user_id=subject_id, agent_id=agent_id, session_id=session_id)
    candidates = await _search(
        db,
        bindings,
        query,
        scope=scope,
        top_n=top_n,
        similarity_threshold=similarity_threshold,
        keywords_similarity_weight=keywords_similarity_weight,
    )
    sources = await _sources(bindings, candidates)
    return [
        {**candidate, "source": resolve_message_source(candidate, sources, scope=scope, excerpt_chars=source_excerpt_chars)}
        for candidate in candidates
        if candidate.get("memory_id") in scope.memory_ids
    ]
