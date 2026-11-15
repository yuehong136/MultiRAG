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
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, is_active, message_scope
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import release_sql


def analysis_payload(manifest: dict[str, Any], policy: str) -> dict[str, Any]:
    """The token estimate and model use exactly the same included evidence body."""
    return {
        "policy": policy,
        "inputs": [item for item in manifest["inputs"] if item["coverage"] in {"included", "analyzed"}],
        "entries": manifest["entries"],
    }


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

    async def snapshot(self, memory_id: str, source_ids: list[int], entry_ids: list[str], input_budget_tokens: int) -> dict[str, Any]:
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

        def estimate() -> int:
            return self.count_tokens(json.dumps(analysis_payload(manifest, "preferences"), ensure_ascii=False, separators=(",", ":"))) + 512

        if estimate() > input_budget_tokens:
            raise RevisionValidationError("dreaming_input_metadata_budget_exceeded")
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
            if estimate() > input_budget_tokens:
                item.update(snapshot={}, coverage="deferred", defer_reason="input_budget")
        if not any(item["coverage"] == "included" for item in inputs):
            raise RevisionValidationError("dreaming_no_input_fits_budget")
        manifest["estimated_input_tokens"] = estimate()
        return manifest

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
