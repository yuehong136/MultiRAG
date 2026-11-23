"""Bounded, read-only related current-entry suggestions for explicit selection.

Round-robin seats preserve the semantic first hit alongside lexical recall,
informed by Hindsight's interleave regression. No upstream code is copied.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from memory.maintenance.target_conditions import TargetConditions, exact_scope, projection_digest, public_conditions
from memory.revisions.domain import RevisionConflict, RevisionValidationError, content_hash, is_active, message_scope, positive_int, vector_dimension
from memory.writes.providers import release_sql
from memory.writes.types import Embedder

SEMANTIC_LIMIT = 64
LEXICAL_SCAN_LIMIT = 128
MAX_EXAMINED = 32
MAX_PHYSICAL_READS = 128
VectorRecall = Callable[[str, str, str, list[float]], Awaitable[list[dict[str, Any]]]]


def lexical_terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return terms


def lexical_score(query: str, content: str) -> float:
    left, right = lexical_terms(query), lexical_terms(content)
    return len(left & right) / min(len(left), len(right)) if left and right else 0.0


def interleave_targets(semantic_ids: list[str], lexical_ids: list[str]) -> list[str]:
    """Keep each arm's first candidate; deduplicate logical IDs, never text."""
    result, seen = [], set()
    for index in range(max(len(semantic_ids), len(lexical_ids))):
        for values in (semantic_ids, lexical_ids):
            if index < len(values) and values[index] not in seen:
                result.append(values[index])
                seen.add(values[index])
    return result


def recommendation_request(
    *, source_ids: list[int], user_id: str, agent_id: str, query: str | None = None, retrieval_mode: str = "hybrid", limit: int = 5, budget_tokens: int = 4096, query_budget_tokens: int = 2048
) -> dict[str, Any]:
    if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 8:
        raise RevisionValidationError("invalid_target_sources")
    identities = sorted(positive_int(item) for item in source_ids)
    if len(set(identities)) != len(identities):
        raise RevisionValidationError("invalid_target_sources")
    exact_scope({"user_id": user_id, "agent_id": agent_id, "session_id": ""})
    if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 4096):
        raise RevisionValidationError("invalid_target_query")
    if (
        retrieval_mode not in {"hybrid", "lexical"}
        or type(limit) is not int
        or not 1 <= limit <= 8
        or type(budget_tokens) is not int
        or not 1024 <= budget_tokens <= 8192
        or type(query_budget_tokens) is not int
        or not 256 <= query_budget_tokens <= 4096
    ):
        raise RevisionValidationError("invalid_target_budget")
    return {
        "source_ids": identities,
        "user_id": user_id,
        "agent_id": agent_id,
        "query": query,
        "retrieval_mode": retrieval_mode,
        "limit": limit,
        "budget_tokens": budget_tokens,
        "query_budget_tokens": query_budget_tokens,
    }


class TargetRecommendations:
    def __init__(self, db: AsyncSession, *, conditions: TargetConditions, embedder: Embedder, recall: VectorRecall, count_tokens: Callable[[str], int]) -> None:
        self.db, self.conditions, self.embedder, self.recall, self.count_tokens = db, conditions, embedder, recall, count_tokens

    async def _bank(self, memory_id: str) -> None:
        await self.conditions.authorize(self.db, memory_id)
        state = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.conditions.tenant_id))
        if state != "active":
            raise RevisionConflict("target_requires_managed_memory")

    async def recommend(self, memory_id: str, **options: Any) -> dict[str, Any]:
        import json

        request = recommendation_request(**options)
        await release_sql(self.db)
        reads: dict[int, dict[str, Any] | None] = {}

        async def read(identity: int) -> dict[str, Any] | None:
            if identity not in reads:
                if len(reads) >= MAX_PHYSICAL_READS:
                    raise RevisionValidationError("target_read_limit")
                reads[identity] = await self.conditions.storage.read(identity)
            return reads[identity]

        try:
            await self._bank(memory_id)
            rows = list(
                await self.db.scalars(
                    select(MemorySourceState)
                    .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.conditions.tenant_id, MemorySourceState.source_message_id.in_(request["source_ids"]))
                    .order_by(MemorySourceState.source_message_id)
                )
            )
            if len(rows) != len(request["source_ids"]):
                raise RevisionConflict("source_adoption_required")
            inputs: list[dict[str, Any]] = []
            for row in rows:
                if row.state != "active" or (row.scope.get("user_id"), row.scope.get("agent_id")) != (request["user_id"], request["agent_id"]):
                    raise RevisionConflict("target_scope_mismatch")
                inputs.append(
                    {
                        "source_id": row.source_message_id,
                        "source_revision": row.source_revision,
                        "source_generation": row.generation,
                        "content_sha256": row.content_sha256,
                        "scope": deepcopy(row.scope),
                    }
                )
            await release_sql(self.db)
            bodies, dimension = [], 0
            for item in inputs:
                physical = await read(item["source_id"])
                if (
                    physical is None
                    or physical.get("message_type") != "raw"
                    or physical.get("source_id") not in (None, 0, "", "0")
                    or not is_active(physical)
                    or message_scope(physical, memory_id) != item["scope"]
                    or content_hash(physical.get("content", "")) != item["content_sha256"]
                ):
                    raise RevisionConflict("target_projection_changed")
                item["projection_sha256"] = projection_digest(physical)
                dimension = dimension or vector_dimension(physical)
                if vector_dimension(physical) != dimension:
                    raise RevisionConflict("target_model_changed")
                bodies.append(physical["content"])
            query = request["query"] if request["query"] is not None else "\n\n".join(bodies)
            if self.count_tokens(query) > request["query_budget_tokens"]:
                raise RevisionValidationError("target_query_budget_exceeded")
            binding = await self.conditions.model_binding(self.db, memory_id, dimension)
            await release_sql(self.db)
            semantic: list[dict[str, Any]] = []
            if request["retrieval_mode"] == "hybrid":
                embedding = await self.embedder(memory_id, [{"content": query}], binding)
                if embedding["model_binding"] != binding or len(embedding["vectors"]) != 1:
                    raise RevisionConflict("target_model_changed")
                semantic = await self.recall(memory_id, request["user_id"], request["agent_id"], embedding["vectors"][0])
                if len(semantic) > SEMANTIC_LIMIT:
                    raise RevisionValidationError("target_recall_limit")
                if any(message_scope(item, memory_id)["user_id"] != request["user_id"] or item["agent_id"] != request["agent_id"] for item in semantic):
                    raise RevisionConflict("target_scope_mismatch")
            await self._bank(memory_id)
            base = (
                select(MemoryEntry, MemoryEntryRevision)
                .join(MemoryEntryRevision, (MemoryEntryRevision.entry_id == MemoryEntry.entry_id) & (MemoryEntryRevision.revision == MemoryEntry.current_revision))
                .where(
                    MemoryEntry.memory_id == memory_id,
                    MemoryEntry.tenant_id == self.conditions.tenant_id,
                    MemoryEntry.state == "active",
                    MemoryEntryRevision.content.is_not(None),
                    MemoryEntryRevision.message_snapshot["user_id"].astext == request["user_id"],
                    MemoryEntryRevision.message_snapshot["agent_id"].astext == request["agent_id"],
                )
                .execution_options(populate_existing=True)
            )
            lexical = list((await self.db.execute(base.order_by(MemoryEntry.updated_at.desc(), MemoryEntry.entry_id).limit(LEXICAL_SCAN_LIMIT + 1))).all())
            truncated_inventory = len(lexical) > LEXICAL_SCAN_LIMIT
            lexical = lexical[:LEXICAL_SCAN_LIMIT]
            physical_ids = [positive_int(item["message_id"]) for item in semantic]
            direct = list((await self.db.execute(base.where(MemoryEntry.current_message_id.in_(physical_ids)))).all()) if physical_ids else []
            pool = {entry.entry_id: (entry, revision) for entry, revision in [*lexical, *direct]}
            by_message = {entry.current_message_id: entry.entry_id for entry, _ in direct}
            semantic_ids = list(dict.fromkeys(by_message[identity] for identity in physical_ids if identity in by_message))
            scores = {entry.entry_id: lexical_score(query, revision.content or "") for entry, revision in lexical}
            lexical_ids = sorted((identity for identity, score in scores.items() if score > 0), key=lambda identity: (-scores[identity], identity))
            order = interleave_targets(semantic_ids, lexical_ids)
            omitted = dict.fromkeys(("not_ready", "source_unavailable", "scope_mismatch", "closure_limit", "budget", "read_limit", "candidate_limit"), 0)
            selected: list[dict[str, Any]] = []
            targets: list[dict[str, Any]] = []
            response: dict[str, Any] = {
                "targets": targets,
                "confirmation": {"inputs": inputs, "targets": selected, "model_binding": binding.to_dict()},
                "retrieval": {
                    "mode": request["retrieval_mode"],
                    "embedding_calls": int(request["retrieval_mode"] == "hybrid"),
                    "semantic_limit": SEMANTIC_LIMIT,
                    "lexical_scan_limit": LEXICAL_SCAN_LIMIT,
                    "registered_current_only": True,
                    "lexical_inventory_truncated": truncated_inventory,
                    "semantic_candidates_truncated": len(semantic) == SEMANTIC_LIMIT,
                    "inventory_complete": not truncated_inventory,
                    "query_sha256": content_hash(query),
                    "query_origin": "explicit" if request["query"] is not None else "whole_sources",
                    "query_truncated": False,
                },
                "budget": {"budget_tokens": request["budget_tokens"], "estimated_tokens": 0, "token_count_is_estimate": True},
                "truncated": truncated_inventory or len(semantic) == SEMANTIC_LIMIT,
                "omitted": omitted,
            }

            def measured() -> int:
                return self.count_tokens(json.dumps({**response, "confirmation": public_conditions(response["confirmation"])}, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

            if measured() + 64 > request["budget_tokens"]:
                raise RevisionValidationError("target_recommendation_budget_exceeded")
            plans: list[dict[str, Any]] = []
            omitted["candidate_limit"] = max(0, len(order) - MAX_EXAMINED)
            for identity in order[:MAX_EXAMINED]:
                entry, revision = pool[identity]
                try:
                    await self.conditions.snapshots._ready_entry(entry, revision)
                except RevisionConflict:
                    omitted["not_ready"] += 1
                    continue
                refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == identity, MemoryRevisionSource.revision == entry.current_revision).limit(33)))
                if not refs or len(refs) > 32 or len({ref.source_message_id for ref in refs} | set(request["source_ids"])) > 32:
                    omitted["closure_limit"] += 1
                    continue
                sources = list(
                    await self.db.scalars(
                        select(MemorySourceState).where(
                            MemorySourceState.memory_id == memory_id,
                            MemorySourceState.tenant_id == self.conditions.tenant_id,
                            MemorySourceState.source_message_id.in_([ref.source_message_id for ref in refs]),
                        )
                    )
                )
                by_id = {item.source_message_id: item for item in sources}
                if any(
                    ref.source_message_id not in by_id
                    or by_id[ref.source_message_id].state != "active"
                    or (ref.source_revision, ref.content_sha256) != (by_id[ref.source_message_id].source_revision, by_id[ref.source_message_id].content_sha256)
                    for ref in refs
                ):
                    omitted["source_unavailable"] += 1
                    continue
                if any((source.scope.get("user_id"), source.scope.get("agent_id")) != (request["user_id"], request["agent_id"]) for source in sources):
                    omitted["scope_mismatch"] += 1
                    continue
                frozen = {
                    "entry_id": identity,
                    "revision": entry.current_revision,
                    "message_id": entry.current_message_id,
                    "generation": entry.generation,
                    "content_sha256": revision.content_sha256,
                    "scope": message_scope(revision.message_snapshot, memory_id),
                    "source_versions": [
                        {
                            "source_id": source.source_message_id,
                            "source_revision": source.source_revision,
                            "source_generation": source.generation,
                            "content_sha256": source.content_sha256,
                            "scope": deepcopy(source.scope),
                        }
                        for source in sorted(sources, key=lambda source: source.source_message_id)
                    ],
                }
                target_body = revision.content
                target_binding = deepcopy(revision.message_snapshot.get("model_binding"))
                plans.append({"frozen": frozen, "content": target_body, "model_binding": target_binding})
            await release_sql(self.db)
            for index, plan in enumerate(plans):
                if len(targets) >= request["limit"]:
                    omitted["candidate_limit"] += len(plans) - index
                    break
                frozen, target_body, target_binding = plan["frozen"], plan["content"], plan["model_binding"]
                identity = frozen["entry_id"]
                try:
                    physical = await read(frozen["message_id"])
                    if (
                        physical is None
                        or not is_active(physical)
                        or physical.get("content") != target_body
                        or content_hash(target_body) != frozen["content_sha256"]
                        or message_scope(physical, memory_id) != frozen["scope"]
                        or vector_dimension(physical) != dimension
                        or target_binding != binding.to_dict()
                    ):
                        omitted["not_ready"] += 1
                        continue
                    frozen["projection_sha256"] = projection_digest(physical)
                    available = True
                    for source in frozen["source_versions"]:
                        raw = await read(source["source_id"])
                        if (
                            raw is None
                            or raw.get("message_type") != "raw"
                            or raw.get("source_id") not in (None, 0, "", "0")
                            or not is_active(raw)
                            or content_hash(raw.get("content", "")) != source["content_sha256"]
                            or message_scope(raw, memory_id) != source["scope"]
                        ):
                            available = False
                            break
                        source["projection_sha256"] = projection_digest(raw)
                    if not available:
                        omitted["source_unavailable"] += 1
                        continue
                except RevisionValidationError as error:
                    if error.code != "target_read_limit":
                        raise
                    omitted["read_limit"] += len(plans) - index
                    break
                reasons = []
                if identity in semantic_ids:
                    reasons.append({"kind": "semantic_rank", "rank": semantic_ids.index(identity) + 1})
                if scores.get(identity, 0) > 0:
                    reasons.append({"kind": "lexical_overlap", "score": round(scores[identity], 6)})
                public = public_conditions({"inputs": [], "targets": [frozen], "model_binding": binding.to_dict()})["targets"][0]
                targets.append({**public, "content": target_body, "reasons": reasons})
                selected.append(frozen)
                if measured() + 64 > request["budget_tokens"]:
                    targets.pop()
                    selected.pop()
                    omitted["budget"] += 1
            await self.conditions.validate_sql(memory_id, response["confirmation"], entry_ids=[item["entry_id"] for item in selected])
            response["truncated"] = response["truncated"] or any(omitted.values())
            response["confirmation"] = public_conditions(response["confirmation"])
            for _ in range(8):
                estimate = measured()
                if estimate == response["budget"]["estimated_tokens"]:
                    break
                response["budget"]["estimated_tokens"] = estimate
            if measured() > request["budget_tokens"]:
                raise RevisionValidationError("target_recommendation_budget_exceeded")
            return response
        finally:
            await release_sql(self.db)
