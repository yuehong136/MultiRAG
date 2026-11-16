"""Authorized, bounded evidence snapshots for candidate-only maintenance."""

from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance.segments import split_source, validate_fragment
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, is_active, message_scope
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import release_sql


def analysis_payload(manifest: dict[str, Any], policy: str) -> dict[str, Any]:
    """The token estimate and model use exactly the same included evidence body."""
    value = {
        "policy": policy,
        "inputs": [item for item in manifest["inputs"] if item["coverage"] in {"included", "analyzed"}],
        "entries": manifest["entries"],
    }
    if any(item.get("fragment") is not None for item in manifest["inputs"]):
        value["evidence_window"] = (
            "Fragment snapshots contain the original read span. Core spans alone count toward source coverage; adjacent read context may qualify or negate a statement. Do not infer unseen source content or treat partial evidence as a complete conversation."
        )
    return value


class MaintenanceSnapshots:
    def __init__(self, db: AsyncSession, *, tenant_id: str, storage: ManagedMessageStore, count_tokens: Callable[[str], int]) -> None:
        self.db, self.tenant_id, self.storage, self.count_tokens = db, tenant_id, storage, count_tokens

    async def _bank(self, memory_id: str) -> None:
        owner = await self.db.scalar(select(Memory.tenant_id).where(Memory.id == memory_id))
        if owner != self.tenant_id:
            raise RevisionNotFound("memory_not_found")
        state = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
        if state != "active":
            raise RevisionConflict("dreaming_requires_managed_memory")

    async def _ready_entry(self, entry: MemoryEntry, revision: MemoryEntryRevision) -> None:
        if entry.state != "active" or revision.content is None:
            raise RevisionConflict("dreaming_target_not_active")
        if revision.origin == "consolidated":
            admissions = list(
                await self.db.scalars(
                    select(MemoryWriterAdmission).where(
                        MemoryWriterAdmission.memory_id == entry.memory_id,
                        MemoryWriterAdmission.tenant_id == self.tenant_id,
                        MemoryWriterAdmission.kind == "dream_publish",
                        MemoryWriterAdmission.message_ids.contains([revision.message_id]),
                        MemoryWriterAdmission.publication_state == "ready",
                    )
                )
            )
            ready = await self.db.scalar(
                select(MemoryPublicationOutbox.id).where(
                    MemoryPublicationOutbox.entry_id == entry.entry_id, MemoryPublicationOutbox.revision == revision.revision, MemoryPublicationOutbox.state == "ready"
                )
            )
            if len(admissions) == 1 and ready is not None:
                return
            raise RevisionConflict("dreaming_target_not_ready")
        if revision.parent_revision is None:
            return
        ready = await self.db.scalar(
            select(MemoryPublicationOutbox.id).where(
                MemoryPublicationOutbox.entry_id == entry.entry_id, MemoryPublicationOutbox.revision == revision.revision, MemoryPublicationOutbox.state == "ready"
            )
        )
        if ready is None:
            raise RevisionConflict("dreaming_target_not_ready")

    async def _evidence(self, memory_id: str, source_ids: list[int], entry_ids: list[str]) -> dict[str, Any]:
        await release_sql(self.db)
        try:
            await self._bank(memory_id)
            source_rows = list(
                await self.db.scalars(
                    select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id.in_(source_ids)).order_by(MemorySourceState.source_message_id)
                )
            )
            if len(source_rows) != len(source_ids):
                raise RevisionConflict("source_adoption_required")
            inputs: list[dict[str, Any]] = []
            for row in source_rows:
                if row.tenant_id != self.tenant_id or row.state != "active":
                    raise RevisionConflict("dreaming_source_not_active")
                inputs.append(
                    {
                        "input_id": f"source-{row.source_message_id}",
                        "source_id": row.source_message_id,
                        "source_revision": row.source_revision,
                        "source_generation": row.generation,
                        "content_sha256": row.content_sha256,
                        "scope": deepcopy(row.scope),
                        "valid_at": None,
                        "snapshot": {},
                        "coverage": "deferred",
                        "defer_reason": "input_budget",
                    }
                )
            entries: list[dict[str, Any]] = []
            for identity in sorted(entry_ids):
                entry = await self.db.scalar(select(MemoryEntry).where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, MemoryEntry.entry_id == identity))
                if entry is None:
                    raise RevisionNotFound("entry_not_found")
                revision = await self.db.get(MemoryEntryRevision, (identity, entry.current_revision))
                if revision is None:
                    raise RevisionConflict("dreaming_target_not_ready")
                await self._ready_entry(entry, revision)
                refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == identity, MemoryRevisionSource.revision == entry.current_revision)))
                if not refs or any(ref.source_message_id not in source_ids for ref in refs):
                    raise RevisionConflict("dreaming_target_source_scope_incomplete")
                entries.append(
                    {
                        "entry_id": identity,
                        "revision": entry.current_revision,
                        "message_id": entry.current_message_id,
                        "generation": entry.generation,
                        "content": revision.content,
                        "content_sha256": revision.content_sha256,
                        "scope": message_scope(revision.message_snapshot, memory_id),
                        "message_type": revision.message_snapshot.get("message_type"),
                        "source_ids": sorted(ref.source_message_id for ref in refs),
                    }
                )
            manifest = {"inputs": inputs, "entries": entries, "estimated_input_tokens": 0}
        finally:
            await release_sql(self.db)
        # Verify physical evidence before any model sees it; never hold SQL over RPC.
        for target in entries:
            physical = await self.storage.read(target["message_id"])
            if physical is None or not is_active(physical) or physical.get("content") != target["content"] or message_scope(physical, memory_id) != target["scope"]:
                raise RevisionConflict("dreaming_target_projection_changed")

        for item in inputs:
            physical = await self.storage.read(item["source_id"])
            if (
                physical is None
                or physical.get("message_type") != "raw"
                or physical.get("message_id") != item["source_id"]
                or physical.get("source_id") not in (None, 0, "", "0")
                or not is_active(physical)
                or not isinstance(physical.get("content"), str)
                or content_hash(physical["content"]) != item["content_sha256"]
                or message_scope(physical, memory_id) != item["scope"]
            ):
                raise RevisionConflict("dreaming_source_projection_changed")
            item.update(
                valid_at=physical.get("valid_at"),
                coverage="included",
                defer_reason=None,
                snapshot={"content": physical["content"], "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
            )
        return manifest

    def _estimate(self, manifest: dict[str, Any]) -> int:
        payload = self.count_tokens(json.dumps(analysis_payload(manifest, "preferences"), ensure_ascii=False, separators=(",", ":"))) + 512
        serialized = self.count_tokens(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return max(payload, serialized)

    async def snapshot(self, memory_id: str, source_ids: list[int], entry_ids: list[str], input_budget_tokens: int) -> dict[str, Any]:
        manifest = await self._evidence(memory_id, source_ids, entry_ids)

        def estimate() -> int:
            return self.count_tokens(json.dumps(analysis_payload(manifest, "preferences"), ensure_ascii=False, separators=(",", ":"))) + 512

        full = deepcopy(manifest["inputs"])
        for item in manifest["inputs"]:
            item.update(snapshot={}, coverage="deferred", defer_reason="input_budget")
        if estimate() > input_budget_tokens:
            raise RevisionValidationError("dreaming_input_metadata_budget_exceeded")
        for item, original in zip(manifest["inputs"], full, strict=True):
            item.update(original)
            if estimate() > input_budget_tokens:
                item.update(snapshot={}, coverage="deferred", defer_reason="input_budget")
        if not any(item["coverage"] == "included" for item in manifest["inputs"]):
            raise RevisionValidationError("dreaming_no_input_fits_budget")
        manifest["estimated_input_tokens"] = estimate()
        return manifest

    async def segment_snapshot(self, memory_id: str, source_ids: list[int], entry_ids: list[str], input_budget_tokens: int, fragment: dict[str, Any]) -> dict[str, Any]:
        manifest = await self._evidence(memory_id, source_ids, entry_ids)
        target = next((item for item in manifest["inputs"] if item["source_id"] == fragment["source_id"]), None)
        if target is None:
            raise RevisionConflict("maintenance_fragment_source_mismatch")
        expected = {key: value for key, value in fragment.items() if key != "source_id"}
        original = target["snapshot"]["content"]
        body = original[expected["read_start"] : expected["read_end"]]
        validate_fragment(expected, content=body)
        from memory.maintenance.segments import fragment_metadata

        if expected != fragment_metadata(memory_id, target, original, start=expected["core_start"], end=expected["core_end"], index=expected["part_index"], count=expected["part_count"]):
            raise RevisionConflict("maintenance_fragment_identity_changed")
        target.update(input_id="part-" + expected["fragment_id"][:24], fragment=expected)
        target["snapshot"]["content"] = body
        manifest["estimated_input_tokens"] = self._estimate(manifest)
        if manifest["estimated_input_tokens"] > input_budget_tokens:
            raise RevisionValidationError("segment_context_budget")
        return manifest

    async def plan_segments(
        self, memory_id: str, sources: list[dict[str, Any]], contexts: list[dict[str, Any]], entries: list[dict[str, Any]], configuration: dict[str, Any], input_budget_tokens: int
    ) -> dict[str, Any]:
        """Read evidence outside SQL; persist only version-bound span metadata."""
        identities = sorted({item["source_id"] for item in [*sources, *contexts]})
        if not identities:
            return {"sources": [], "blocked_sources": []}
        complete = await self._evidence(memory_id, identities, [item["entry_id"] for item in entries])
        by_id = {item["source_id"]: item for item in complete["inputs"]}
        for observed in [*sources, *contexts]:
            actual = by_id[observed["source_id"]]
            if any(actual[name] != observed[name] for name in ("source_id", "source_revision", "source_generation", "content_sha256", "scope")):
                raise RevisionConflict("incremental_source_version_changed")
        planned: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        for source in sources:
            primary = by_id[source["source_id"]]
            principal = (source["scope"]["user_id"], source["scope"]["agent_id"])
            matching = [item for item in contexts if (item["scope"]["user_id"], item["scope"]["agent_id"]) == principal]
            targets = [item for item in complete["entries"] if (item["scope"]["user_id"], item["scope"]["agent_id"]) == principal]
            template: dict[str, Any] = {"inputs": [deepcopy(by_id[item["source_id"]]) for item in matching] + [deepcopy(primary)], "entries": targets, "estimated_input_tokens": 0}
            text = primary["snapshot"]["content"]

            def fits(fragment: dict[str, Any]) -> bool:
                selected = template["inputs"][-1]
                selected.update(input_id="part-" + fragment["fragment_id"][:24], fragment=fragment)
                selected["snapshot"]["content"] = text[fragment["read_start"] : fragment["read_end"]]
                return self._estimate(template) + 64 <= input_budget_tokens

            try:
                fragments = split_source(memory_id, source, text, configuration, count_tokens=self.count_tokens, fits=fits)
                planned.extend({**deepcopy(source), "fragment": fragment} for fragment in fragments)
            except RevisionValidationError as error:
                blocked.append({**deepcopy(source), "error_code": error.code})
        return {"sources": planned, "blocked_sources": blocked}

    async def validate(self, db: AsyncSession, memory_id: str, manifest: dict[str, Any]) -> None:
        """Caller holds Memory/fence/bank; acquire source then target locks in order."""
        if db is not self.db:
            raise RuntimeError("Maintenance validation requires its owning session")
        await self._bank(memory_id)
        inputs = sorted(manifest["inputs"], key=lambda item: item["source_id"])
        for item in inputs:
            row = await db.scalar(
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == item["source_id"])
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                row is None
                or row.tenant_id != self.tenant_id
                or row.state != "active"
                or (row.source_revision, row.generation, row.content_sha256, row.scope) != (item["source_revision"], item["source_generation"], item["content_sha256"], item["scope"])
            ):
                raise RevisionConflict("dreaming_source_changed")
        for target in sorted(manifest["entries"], key=lambda item: item["entry_id"]):
            entry = await db.scalar(
                select(MemoryEntry)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.entry_id == target["entry_id"], MemoryEntry.tenant_id == self.tenant_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if entry is None or (entry.current_revision, entry.current_message_id, entry.generation, entry.state) != (target["revision"], target["message_id"], target["generation"], "active"):
                raise RevisionConflict("dreaming_target_changed")
            revision = await db.get(MemoryEntryRevision, (entry.entry_id, entry.current_revision))
            if revision is None or revision.content != target["content"] or revision.content_sha256 != target["content_sha256"]:
                raise RevisionConflict("dreaming_target_changed")
            await self._ready_entry(entry, revision)
