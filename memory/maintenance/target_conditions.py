"""Stateless, explicit current/source/projection preconditions for chosen targets."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionValidationError, content_hash, is_active, message_scope, positive_int, request_hash, vector_dimension
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import release_sql
from memory.writes.types import Authorize, BindingReader

_SOURCE_FIELDS = {"source_id", "source_revision", "source_generation", "content_sha256", "scope", "projection_sha256"}
_TARGET_FIELDS = {"entry_id", "revision", "message_id", "generation", "content_sha256", "scope", "projection_sha256", "source_versions"}


def _hash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RevisionValidationError("invalid_target_preconditions")
    return value


def exact_scope(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"user_id", "agent_id", "session_id"} or any(not isinstance(item, str) or len(item.encode("utf-8")) > 256 for item in value.values()):
        raise RevisionValidationError("invalid_target_scope")
    return dict(value)


def _identity(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    return positive_int(value, "invalid_target_preconditions")


def source_condition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise RevisionValidationError("invalid_target_preconditions")
    return {
        **{name: _identity(value[name]) for name in ("source_id", "source_revision", "source_generation")},
        "content_sha256": _hash(value["content_sha256"]),
        "scope": exact_scope(value["scope"]),
        "projection_sha256": _hash(value["projection_sha256"]),
    }


def canonical_target_preconditions(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"inputs", "targets", "model_binding"}:
        raise RevisionValidationError("invalid_target_preconditions")
    if not isinstance(value["inputs"], list) or not 1 <= len(value["inputs"]) <= 8 or not isinstance(value["targets"], list) or not 1 <= len(value["targets"]) <= 8:
        raise RevisionValidationError("invalid_target_preconditions")
    inputs = [source_condition(item) for item in value["inputs"]]
    targets: list[dict[str, Any]] = []
    for item in value["targets"]:
        if not isinstance(item, dict) or set(item) != _TARGET_FIELDS or not isinstance(item["entry_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["entry_id"]):
            raise RevisionValidationError("invalid_target_preconditions")
        if not isinstance(item["source_versions"], list) or not 1 <= len(item["source_versions"]) <= 32:
            raise RevisionValidationError("target_closure_limit")
        sources = sorted([source_condition(source) for source in item["source_versions"]], key=lambda source: source["source_id"])
        if len({source["source_id"] for source in sources}) != len(sources):
            raise RevisionValidationError("invalid_target_preconditions")
        targets.append(
            {
                "entry_id": item["entry_id"],
                **{name: _identity(item[name]) for name in ("revision", "message_id", "generation")},
                "content_sha256": _hash(item["content_sha256"]),
                "scope": exact_scope(item["scope"]),
                "projection_sha256": _hash(item["projection_sha256"]),
                "source_versions": sources,
            }
        )
    if len({item["source_id"] for item in inputs}) != len(inputs) or len({item["entry_id"] for item in targets}) != len(targets):
        raise RevisionValidationError("invalid_target_preconditions")
    sources_by_id: dict[int, dict[str, Any]] = {}
    principal = (inputs[0]["scope"]["user_id"], inputs[0]["scope"]["agent_id"])
    for source in [*inputs, *(source for target in targets for source in target["source_versions"])]:
        if (source["scope"]["user_id"], source["scope"]["agent_id"]) != principal or (source["source_id"] in sources_by_id and source != sources_by_id[source["source_id"]]):
            raise RevisionValidationError("target_scope_mismatch")
        sources_by_id[source["source_id"]] = source
    if len(sources_by_id) > 32 or any((target["scope"]["user_id"], target["scope"]["agent_id"]) != principal for target in targets):
        raise RevisionValidationError("target_closure_limit")
    binding = value["model_binding"]
    if not isinstance(binding, dict) or set(binding) != {"tenant_model_id", "model_reference", "dimension", "config_reference"}:
        raise RevisionValidationError("invalid_target_preconditions")
    try:
        model = ModelBinding(**binding)
    except (TypeError, ValueError) as error:
        raise RevisionValidationError("invalid_target_preconditions") from error
    return {"inputs": sorted(inputs, key=lambda source: source["source_id"]), "targets": sorted(targets, key=lambda target: target["entry_id"]), "model_binding": model.to_dict()}


def public_conditions(value: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(value)
    for source in [*result["inputs"], *(source for target in result["targets"] for source in target["source_versions"])]:
        for key in ("source_id", "source_revision", "source_generation"):
            source[key] = str(source[key])
    for target in result["targets"]:
        for key in ("revision", "message_id", "generation"):
            target[key] = str(target[key])
    return result


def projection_digest(message: dict[str, Any]) -> str:
    dimension = vector_dimension(message)
    vector = message["content_embed"]
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    fields = ("memory_id", "message_id", "message_type", "source_id", "user_id", "agent_id", "session_id", "content", "valid_at", "invalid_at", "forget_at")
    return request_hash({**{name: message.get(name) for name in fields}, "status": is_active(message), "content_embed": [float(item) for item in vector], "dimension": dimension})


class TargetConditions:
    def __init__(self, db: AsyncSession, *, tenant_id: str, authorize: Authorize, storage: ManagedMessageStore, model_binding: BindingReader, snapshots: MaintenanceSnapshots) -> None:
        self.db, self.tenant_id, self.authorize, self.storage, self.model_binding, self.snapshots = db, tenant_id, authorize, storage, model_binding, snapshots

    async def validate_sql(self, memory_id: str, value: dict[str, Any], *, entry_ids: list[str], source_ids: list[int] | None = None, lock: bool = False) -> None:
        await self.authorize(self.db, memory_id)
        bank = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
        if bank != "active" or set(entry_ids) != {item["entry_id"] for item in value["targets"]}:
            raise RevisionConflict("target_preconditions_changed")
        model = ModelBinding(**value["model_binding"])
        if await self.model_binding(self.db, memory_id, model.dimension) != model:
            raise RevisionConflict("target_model_changed")
        sources = {item["source_id"]: item for item in [*value["inputs"], *(source for target in value["targets"] for source in target["source_versions"])]}
        if source_ids is not None and not set(sources) <= set(source_ids):
            raise RevisionConflict("target_input_not_selected")
        if source_ids is not None:
            source_query = (
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.tenant_id, MemorySourceState.source_message_id.in_(source_ids))
                .order_by(MemorySourceState.source_message_id)
                .execution_options(populate_existing=True)
            )
            all_sources = list(await self.db.scalars(source_query.with_for_update() if lock else source_query))
            principal = value["inputs"][0]["scope"]
            if len(all_sources) != len(set(source_ids)) or any(row.state != "active" or any(row.scope.get(name) != principal[name] for name in ("user_id", "agent_id")) for row in all_sources):
                raise RevisionConflict("target_scope_mismatch")
        for identity, expected in sorted(sources.items()):
            query = (
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.tenant_id == self.tenant_id, MemorySourceState.source_message_id == identity)
                .execution_options(populate_existing=True)
            )
            row = await self.db.scalar(query.with_for_update() if lock else query)
            if (
                row is None
                or row.state != "active"
                or (row.source_revision, row.generation, row.content_sha256, row.scope) != (expected["source_revision"], expected["source_generation"], expected["content_sha256"], expected["scope"])
            ):
                raise RevisionConflict("target_preconditions_changed")
        for expected in value["targets"]:
            entry_query = (
                select(MemoryEntry)
                .where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, MemoryEntry.entry_id == expected["entry_id"])
                .execution_options(populate_existing=True)
            )
            entry = await self.db.scalar(entry_query.with_for_update() if lock else entry_query)
            if entry is None or (entry.current_revision, entry.current_message_id, entry.generation, entry.state) != (expected["revision"], expected["message_id"], expected["generation"], "active"):
                raise RevisionConflict("target_preconditions_changed")
            revision = await self.db.get(MemoryEntryRevision, (entry.entry_id, entry.current_revision), populate_existing=True)
            if (
                revision is None
                or revision.content is None
                or revision.content_sha256 != expected["content_sha256"]
                or content_hash(revision.content) != expected["content_sha256"]
                or message_scope(revision.message_snapshot, memory_id) != expected["scope"]
            ):
                raise RevisionConflict("target_preconditions_changed")
            await self.snapshots._ready_entry(entry, revision)
            refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == entry.entry_id, MemoryRevisionSource.revision == entry.current_revision)))
            if {ref.source_message_id for ref in refs} != {source["source_id"] for source in expected["source_versions"]} or any(
                (ref.source_revision, ref.content_sha256) != (sources[ref.source_message_id]["source_revision"], sources[ref.source_message_id]["content_sha256"]) for ref in refs
            ):
                raise RevisionConflict("target_preconditions_changed")

    async def validate_physical(self, memory_id: str, value: dict[str, Any]) -> None:
        await release_sql(self.db)
        try:
            await self.validate_sql(memory_id, value, entry_ids=[item["entry_id"] for item in value["targets"]])
            await release_sql(self.db)
            sources = {item["source_id"]: item for item in [*value["inputs"], *(source for target in value["targets"] for source in target["source_versions"])]}
            for identity, expected in sources.items():
                raw = await self.storage.read(identity)
                if (
                    raw is None
                    or raw.get("message_type") != "raw"
                    or raw.get("source_id") not in (None, 0, "", "0")
                    or not is_active(raw)
                    or content_hash(raw.get("content", "")) != expected["content_sha256"]
                    or message_scope(raw, memory_id) != expected["scope"]
                    or projection_digest(raw) != expected["projection_sha256"]
                ):
                    raise RevisionConflict("target_projection_changed")
            for expected in value["targets"]:
                message = await self.storage.read(expected["message_id"])
                if (
                    message is None
                    or not is_active(message)
                    or content_hash(message.get("content", "")) != expected["content_sha256"]
                    or message_scope(message, memory_id) != expected["scope"]
                    or projection_digest(message) != expected["projection_sha256"]
                ):
                    raise RevisionConflict("target_projection_changed")
            await self.validate_sql(memory_id, value, entry_ids=[item["entry_id"] for item in value["targets"]])
        finally:
            await release_sql(self.db)
