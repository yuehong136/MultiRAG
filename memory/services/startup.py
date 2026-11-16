"""Request-local startup data: complete notes and human applicability, no cache."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, Literal

from sqlalchemy import select

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemorySourceState
from memory.revisions.domain import RevisionConflict, RevisionValidationError, content_hash, is_active, message_scope, positive_int, request_hash
from memory.services.context import ContextScope, build_source_excerpt
from memory.verification.domain import identifier
from memory.verification.service import VerificationService, _public_binding, _ReadLimit, _without_projection
from memory.verification.startup_reads import StartupEntryRead, load_startup_entries
from memory.writes.providers import release_sql

PHYSICAL_READ_LIMIT = 256
OMITTED_DETAIL_LIMIT = 16


def _text(items: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"Entry {item['entry_id']}\nContent:\n{item['content']}\nHuman applicability:\n{item['verification']['applicability']}" for item in items)


def assemble_startup_package(
    memory_id: str,
    scope: dict[str, str | None],
    items: list[dict[str, Any]],
    omissions: list[dict[str, str]],
    selection: dict[str, Any],
    *,
    budget_tokens: int,
    max_items: int,
    count_tokens: Callable[[str], int],
) -> dict[str, Any]:
    """Budget the whole domain envelope; never cut a body or applicability."""
    if type(budget_tokens) is not int or not 1024 <= budget_tokens <= 32768 or type(max_items) is not int or not 1 <= max_items <= 16:
        raise RevisionValidationError("invalid_startup_budget")
    missing = deepcopy(omissions)
    chosen: list[dict[str, Any]] = []

    def build(selected: list[dict[str, Any]], skipped: list[dict[str, str]], details: int) -> dict[str, Any]:
        body = _text(selected)
        counts = dict(sorted(Counter(item["reason"] for item in skipped).items()))
        picked = deepcopy(selection)
        picked["selection_complete"] = not picked["inventory_truncated"] and not picked["read_limited"] and counts.get("max_items", 0) == 0 and counts.get("budget", 0) == 0
        value: dict[str, Any] = {
            "memory_id": memory_id,
            "scope": deepcopy(scope),
            "items": deepcopy(selected),
            "startup_text": body,
            "package_sha256": request_hash({"memory_id": memory_id, "scope": scope, "items": selected, "startup_text": body}),
            "usage": {"budget_tokens": budget_tokens, "estimated_tokens": 0, "token_count_is_estimate": True, "includes": "entire_json_with_startup_text"},
            "selection": picked,
            "omitted": {"counts": counts, "entries": deepcopy(skipped[:details]), "details_truncated": len(skipped) > details},
            "snapshot_only": True,
            "mutates_memory": False,
            "model_calls": False,
            "data_only": True,
        }
        for _ in range(8):
            total = count_tokens(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
            if type(total) is not int or total < 0:
                raise RevisionValidationError("invalid_startup_token_estimate")
            if total == value["usage"]["estimated_tokens"]:
                return value
            value["usage"]["estimated_tokens"] = total
        raise RevisionValidationError("startup_token_estimate_unstable")

    for item in items:
        if len(chosen) == max_items:
            missing.append({"entry_id": item["entry_id"], "reason": "max_items"})
        elif build([*chosen, item], missing, 0)["usage"]["estimated_tokens"] <= budget_tokens:
            chosen.append(deepcopy(item))
        else:
            missing.append({"entry_id": item["entry_id"], "reason": "budget"})
    while True:
        for details in range(min(OMITTED_DETAIL_LIMIT, len(missing)), -1, -1):
            result = build(chosen, missing, details)
            if result["usage"]["estimated_tokens"] <= budget_tokens:
                return result
        if not chosen:
            raise RevisionValidationError("startup_metadata_budget_exceeded")
        removed = chosen.pop()
        missing.append({"entry_id": removed["entry_id"], "reason": "budget"})


def _in_scope(value: dict[str, str], expected: dict[str, str | None]) -> bool:
    return all(wanted is None or value.get(key) == wanted for key, wanted in expected.items())


class StartupService:
    def __init__(self, verification: VerificationService, *, count_tokens: Callable[[str], int]) -> None:
        self.verification, self.db, self.count_tokens = verification, verification.db, count_tokens

    @asynccontextmanager
    async def _physical_pass(self) -> AsyncIterator[None]:
        prepare = getattr(self.verification.storage, "prepared_read_pass", None)
        if callable(prepare):
            async with prepare():
                yield
        else:
            yield

    async def build(
        self,
        memory_id: str,
        *,
        user_id: str,
        agent_id: str,
        session_id: str | None = None,
        entry_ids: list[str] | None = None,
        budget_tokens: int = 4096,
        max_items: int = 8,
        inventory_limit: int = 64,
    ) -> dict[str, Any]:
        scope = {"user_id": user_id, "agent_id": agent_id, "session_id": session_id}
        if any(value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 256) for value in scope.values()) or user_id is None or agent_id is None:
            raise RevisionValidationError("invalid_startup_scope")
        if type(inventory_limit) is not int or not 1 <= inventory_limit <= 128:
            raise RevisionValidationError("invalid_startup_inventory_limit")
        if type(budget_tokens) is not int or not 1024 <= budget_tokens <= 32768 or type(max_items) is not int or not 1 <= max_items <= 16:
            raise RevisionValidationError("invalid_startup_budget")
        explicit = entry_ids is not None
        if explicit:
            if not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= 32 or len(set(entry_ids)) != len(entry_ids):
                raise RevisionValidationError("invalid_startup_entry_selection")
            for identity in entry_ids:
                identifier(identity)
        await self.verification._idle()
        try:
            await self.verification.authorize(self.db, memory_id)
            query = (
                select(MemoryEntry.entry_id)
                .join(MemoryEntryRevision, (MemoryEntryRevision.entry_id == MemoryEntry.entry_id) & (MemoryEntryRevision.revision == MemoryEntry.current_revision))
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.verification.tenant_id)
            )
            for key, wanted in scope.items():
                if wanted is not None:
                    query = query.where(MemoryEntryRevision.message_snapshot[key].astext == wanted)
            if entry_ids is not None:
                query = query.where(MemoryEntry.entry_id.in_(entry_ids))
            found = list(await self.db.scalars(query.order_by(MemoryEntry.entry_id).limit(33 if explicit else inventory_limit + 1)))
            truncated = not explicit and len(found) > inventory_limit
            selected = found if explicit else found[:inventory_limit]
        finally:
            await release_sql(self.db)
        omissions = [{"entry_id": identity, "reason": "scope_mismatch"} for identity in sorted(set(entry_ids or []) - set(found))]
        initial = await self._metadata_phase(memory_id, selected, stage="before")
        items, pins, reads = [], {}, [PHYSICAL_READ_LIMIT]
        limited = False
        work: list[str] = []
        reserved = 0
        for identity in selected:
            state = initial[identity]
            if state.error is not None:
                error = state.error
                if isinstance(error, RevisionValidationError):
                    if error.code != "verification_source_closure_limit":
                        raise error
                    reason = "source_closure_limit"
                else:
                    reason = "source_unavailable" if "source" in error.code else "not_ready" if "ready" in error.code else "entry_unavailable"
                omissions.append({"entry_id": identity, "reason": reason})
                continue
            if not all(_in_scope(source["scope"], scope) for source in state.binding["sources"]):
                omissions.append({"entry_id": identity, "reason": "source_scope_mismatch"})
                continue
            record = state.record
            if record is None or record["state"] != "active" or record["result"] != "human_confirmed":
                reason = "unverified" if record is None else "verification_" + record["state"] if record["state"] != "active" else record["result"]
                omissions.append({"entry_id": identity, "reason": reason})
                continue
            cost = 2 * (len(state.binding["sources"]) + 1)
            if reserved + cost > PHYSICAL_READ_LIMIT:
                omissions.append({"entry_id": identity, "reason": "physical_read_limit"})
                limited = True
                continue
            work.append(identity)
            reserved += cost
        first: dict[str, dict[str, Any]] = {}
        async with self._physical_pass():
            for identity in work:
                state = initial[identity]
                first[identity] = await self.verification._physical_binding(memory_id, state.binding, state.projection, physical_budget=reads)
        middle = await self._metadata_phase(memory_id, work, stage="after_first")
        physical: dict[str, dict[str, Any]] = {}
        async with self._physical_pass():
            for identity in work:
                state = middle[identity]
                if state.error is not None:
                    raise state.error
                if state.binding != initial[identity].binding:
                    raise RevisionConflict("verification_binding_changed")
                if state.record is None or state.record_redacted:
                    continue
                try:
                    physical[identity] = await self.verification._physical_binding(memory_id, state.binding, state.projection, physical_budget=reads)
                except _ReadLimit as error:
                    raise RevisionConflict("startup_inventory_changed") from error
                if physical[identity] != first[identity]:
                    raise RevisionConflict("verification_projection_changed")
        after = await self._metadata_phase(memory_id, work, stage="after_second")
        for identity in work:
            state, latest = middle[identity], after[identity]
            if latest.error is not None:
                raise latest.error
            if latest.binding != state.binding:
                raise RevisionConflict("verification_binding_changed")
            if latest.head != state.head or latest.record != state.record or latest.stored_binding != state.stored_binding or latest.record_redacted != state.record_redacted:
                raise RevisionConflict("startup_verification_changed")
            record = state.record
            if record is None or state.record_redacted or record["state"] != "active" or record["result"] != "human_confirmed":
                reason = (
                    "unverified" if record is None else "verification_redacted" if state.record_redacted else "verification_" + record["state"] if record["state"] != "active" else record["result"]
                )
                omissions.append({"entry_id": identity, "reason": reason})
                continue
            binding = physical[identity]
            if state.stored_binding != binding or request_hash(binding) != record["binding_sha256"]:
                omissions.append({"entry_id": identity, "reason": "verification_stale"})
                continue
            if not isinstance(record["applicability"], str) or not record["applicability"].strip():
                raise RevisionConflict("startup_verification_changed")
            if not _in_scope(binding["entry"]["scope"], scope) or not all(_in_scope(source["scope"], scope) for source in binding["sources"]):
                raise RevisionConflict("startup_inventory_changed")
            current = {key: deepcopy(record[key]) for key in ("verification_id", "result", "applicability", "method", "checked_at_is_user_reported", "actor_id", "binding_sha256")}
            current.update(head_version=str(state.head[0]), record_version=str(record["record_version"]))
            current.update({key: record[key].isoformat() if record[key] is not None else None for key in ("checked_at", "recorded_at")})
            binding = _public_binding(binding)
            item = {
                **binding["entry"],
                **{key: deepcopy(initial[identity].projection.get(key)) for key in ("message_type", "valid_at", "invalid_at")},
                "assessment": {key: initial[identity].assessment.get(key) for key in ("kind", "verification_status")},
                "content": state.content,
                "verification": {
                    **{
                        key: current[key]
                        for key in (
                            "verification_id",
                            "head_version",
                            "record_version",
                            "result",
                            "applicability",
                            "method",
                            "checked_at",
                            "checked_at_is_user_reported",
                            "recorded_at",
                            "actor_id",
                            "binding_sha256",
                        )
                    },
                    "locator": {"memory_id": memory_id, "verification_id": current["verification_id"]},
                    "snapshot_only": True,
                },
                "sources": [{**source, "locator": {"kind": "memory_message", "memory_id": memory_id, "message_id": source["source_id"]}} for source in binding["sources"]],
            }
            items.append(item)
            pins[identity] = {
                "entry_id": identity,
                "binding": binding,
                "head_version": str(state.head[0]),
                "current_record": current,
                "record_snapshot": state.record,
                "stored_binding": state.stored_binding,
            }
        selection = {
            "mode": "explicit" if explicit else "inventory",
            "order": "entry_id_asc",
            "inventory_limit": inventory_limit,
            "scanned_count": len(selected),
            "inventory_truncated": truncated,
            "physical_read_limit": PHYSICAL_READ_LIMIT,
            "physical_reads": PHYSICAL_READ_LIMIT - reads[0],
            "read_limited": limited,
        }
        result = assemble_startup_package(memory_id, scope, items, omissions, selection, budget_tokens=budget_tokens, max_items=max_items, count_tokens=self.count_tokens)
        await self._closing(memory_id, [pins[item["entry_id"]] for item in result["items"]])
        return result

    async def _closing(self, memory_id: str, snapshots: list[dict[str, Any]]) -> None:
        current = await self._metadata_phase(memory_id, [snapshot["entry_id"] for snapshot in snapshots], stage="closing")
        for snapshot in snapshots:
            state = current[snapshot["entry_id"]]
            if state.error is not None or _public_binding(state.binding) != _without_projection(snapshot["binding"]):
                raise RevisionConflict("startup_inventory_changed")
            if (
                state.head != (int(snapshot["head_version"]), snapshot["current_record"]["verification_id"])
                or state.record != snapshot["record_snapshot"]
                or state.stored_binding != snapshot["stored_binding"]
                or state.record_redacted
            ):
                raise RevisionConflict("startup_verification_changed")

    async def _metadata_phase(self, memory_id: str, entry_ids: list[str], *, stage: Literal["before", "after_first", "after_second", "closing"]) -> dict[str, StartupEntryRead]:
        await self.verification._idle()
        try:
            return await load_startup_entries(self.verification, memory_id, entry_ids, closing=stage == "closing")
        finally:
            await release_sql(self.db)

    async def source_excerpt(
        self,
        memory_id: str,
        source_id: int,
        *,
        expected_source_revision: int,
        expected_source_generation: int,
        content_sha256: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 512,
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8192:
            raise RevisionValidationError("invalid_startup_source_range")
        positive_int(source_id, "invalid_source_id")
        expected = {
            "source_revision": positive_int(expected_source_revision, "invalid_source_version"),
            "generation": positive_int(expected_source_generation, "invalid_source_version"),
            "content_sha256": content_sha256,
            "scope": {"user_id": user_id, "agent_id": agent_id, "session_id": session_id},
        }
        await self.verification._idle()
        try:
            await self.verification.authorize(self.db, memory_id)
            await self._source_condition(memory_id, source_id, expected)
        finally:
            await release_sql(self.db)
        source = await self.verification.storage.read(source_id)
        if (
            source is None
            or source.get("memory_id") != memory_id
            or source.get("message_id") != source_id
            or source.get("message_type") != "raw"
            or not is_active(source)
            or message_scope(source, memory_id) != expected["scope"]
            or not isinstance(source.get("content"), str)
            or content_hash(source["content"]) != content_sha256
        ):
            raise RevisionConflict("startup_source_changed")
        if offset > len(source["content"]):
            raise RevisionValidationError("invalid_startup_source_range")
        result = build_source_excerpt(
            source, scope=ContextScope(frozenset({memory_id}), user_id=user_id, agent_id=agent_id, session_id=session_id), offset=offset, limit=limit, content_sha256=content_sha256
        )
        if result["state"] != "available":
            raise RevisionConflict("startup_source_changed")
        await self.verification._idle()
        try:
            await self.db.scalar(select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == self.verification.tenant_id).with_for_update(read=True))
            await self.verification.authorize(self.db, memory_id)
            bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id).with_for_update(read=True))
            if bank is not None and (bank.state != "active" or bank.tenant_id != self.verification.tenant_id):
                raise RevisionConflict("startup_source_changed")
            await self._source_condition(memory_id, source_id, expected)
        finally:
            await release_sql(self.db)
        return {**result, "source_revision": str(expected_source_revision), "source_generation": str(expected_source_generation), "scope": expected["scope"], "offset_unit": "unicode_codepoint"}

    async def _source_condition(self, memory_id: str, source_id: int, expected: dict[str, Any]) -> None:
        row = await self.db.get(MemorySourceState, (memory_id, source_id), populate_existing=True)
        if row is None or row.tenant_id != self.verification.tenant_id or row.state != "active" or any(getattr(row, key) != value for key, value in expected.items()):
            raise RevisionConflict("startup_source_changed")
