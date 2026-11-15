"""One SQL-backed supply policy shared by legacy and managed message consumers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memory.revisions.domain import RevisionConflict, content_hash


@dataclass(frozen=True)
class SourceReadState:
    state: str
    revision: int
    generation: int
    content_sha256: str
    scope: dict[str, Any]
    tenant_id: str | None = None


@dataclass(frozen=True)
class ProjectionReadState:
    entry_id: str
    revision: int
    current_revision: int
    current_message_id: int
    entry_state: str
    content_sha256: str
    ready: bool
    sources: tuple[tuple[int, int, int, str], ...] = ()


@dataclass(frozen=True)
class ManagedReadSnapshot:
    banks: dict[str, tuple[str, str]] = field(default_factory=dict)  # memory -> tenant, lifecycle
    sources: dict[tuple[str, int], SourceReadState] = field(default_factory=dict)
    projections: dict[tuple[str, int], ProjectionReadState] = field(default_factory=dict)
    writers: dict[tuple[str, int], tuple[str, str]] = field(default_factory=dict)


def _positive_id(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and int(value) > 0:
        return int(value)
    return None


def _load_policy(memory_ids: list[str], messages: list[dict[str, Any]] | None) -> ManagedReadSnapshot:
    """Read current durable membership on every call; SQL failures never mean unmanaged."""
    from sqlalchemy import select, tuple_

    from api.db.db_models import db_connection
    from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
    from api.db.memory_writer_models import MemoryWriterAdmission

    with db_connection() as db:
        banks = {row.memory_id: (row.tenant_id, row.state) for row in db.scalars(select(ManagedMemoryState).where(ManagedMemoryState.memory_id.in_(memory_ids)))}
        admissions = list(db.scalars(select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id.in_(memory_ids)).limit(10001))) if messages is None or messages else []
        if len(admissions) > 10000:
            raise RevisionConflict("writer_policy_window_exceeded")
        requested = None if messages is None else {(message.get("memory_id"), _positive_id(message.get("message_id"))) for message in messages}
        writers: dict[tuple[str, int], tuple[str, str]] = {}
        for admission in admissions:
            for identity in admission.message_ids:
                if type(identity) is not int or not 0 < identity <= 2**63 - 1:
                    raise RevisionConflict("invalid_writer_projection_identity")
                key = (admission.memory_id, identity)
                if requested is not None and key not in requested:
                    continue
                if key in writers:
                    raise RevisionConflict("writer_projection_identity_conflict")
                writers[key] = (admission.tenant_id, admission.publication_state)
                if len(writers) > 20000:
                    raise RevisionConflict("writer_projection_window_exceeded")
        ids = {value for message in (messages or []) for value in (_positive_id(message.get("message_id")), _positive_id(message.get("source_id"))) if value is not None}
        if len(ids) > 20000:
            raise RevisionConflict("managed_candidate_window_exceeded")
        query = select(MemoryEntryRevision, MemoryEntry).join(MemoryEntry, MemoryEntry.entry_id == MemoryEntryRevision.entry_id).where(MemoryEntryRevision.memory_id.in_(banks))
        if messages is not None:
            query = query.where(MemoryEntryRevision.message_id.in_(ids))
        revisions = list(db.execute(query.limit(20001))) if banks else []
        if len(revisions) > 20000:
            raise RevisionConflict("managed_policy_window_exceeded")
        pairs = {(revision.entry_id, revision.revision) for revision, _ in revisions}
        references: dict[tuple[str, int], list[tuple[int, int, int, str]]] = {}
        if pairs:
            refs = list(db.scalars(select(MemoryRevisionSource).where(tuple_(MemoryRevisionSource.entry_id, MemoryRevisionSource.revision).in_(pairs)).limit(20001)))
            if len(refs) > 20000:
                raise RevisionConflict("managed_source_window_exceeded")
            for row in refs:
                references.setdefault((row.entry_id, row.revision), []).append((row.source_message_id, row.source_revision, row.source_generation, row.content_sha256))
                ids.add(row.source_message_id)
        source_query = select(MemorySourceState).where(MemorySourceState.memory_id.in_(memory_ids))
        if messages is not None:
            source_query = source_query.where(MemorySourceState.source_message_id.in_(ids))
        source_rows = list(db.scalars(source_query.limit(20001)))
        if len(source_rows) > 20000:
            raise RevisionConflict("managed_source_window_exceeded")
        sources = {(row.memory_id, row.source_message_id): SourceReadState(row.state, row.source_revision, row.generation, row.content_sha256, dict(row.scope), row.tenant_id) for row in source_rows}
        ready = (
            {
                (row.entry_id, row.revision)
                for row in db.scalars(
                    select(MemoryPublicationOutbox).where(
                        MemoryPublicationOutbox.memory_id.in_(banks),
                        tuple_(MemoryPublicationOutbox.entry_id, MemoryPublicationOutbox.revision).in_(pairs),
                        MemoryPublicationOutbox.state == "ready",
                        MemoryPublicationOutbox.payload["action"].as_string() == "replace",
                    )
                )
            }
            if pairs
            else set()
        )
        projections = {
            (revision.memory_id, revision.message_id): ProjectionReadState(
                revision.entry_id,
                revision.revision,
                entry.current_revision,
                entry.current_message_id,
                entry.state,
                revision.content_sha256,
                ((revision.entry_id, revision.revision) in ready and writers.get((revision.memory_id, revision.message_id)) == (banks[revision.memory_id][0], "ready"))
                if revision.origin == "consolidated"
                else (revision.parent_revision is None or (revision.entry_id, revision.revision) in ready),
                tuple(references.get((revision.entry_id, revision.revision), ())),
            )
            for revision, entry in revisions
        }
        return ManagedReadSnapshot(banks, sources, projections, writers)


def assert_legacy_write_allowed(tenant_id: str, memory_id: str) -> None:
    if memory_id in _load_policy([memory_id], []).banks:
        raise RevisionConflict("managed_memory_requires_coordinated_write")


def filter_messages(tenant_ids: list[str], memory_ids: list[str], messages: list[dict[str, Any]], *, management_metadata: bool = False, management_body: bool = False) -> list[dict[str, Any]]:
    policy = _load_policy(list(dict.fromkeys(memory_ids)), messages)
    if not policy.banks and not policy.writers and not policy.sources:
        return messages
    allowed: list[dict[str, Any]] = []
    for message in messages:
        memory_id = message.get("memory_id")
        if not isinstance(memory_id, str) or memory_id not in memory_ids:
            continue
        identity = _positive_id(message.get("message_id"))
        writer = policy.writers.get((memory_id, identity)) if identity is not None else None
        if writer is not None and (writer[0] not in tenant_ids or writer[1] != "ready"):
            continue
        managed = memory_id in policy.banks
        if managed:
            tenant, state = policy.banks[memory_id]
            if tenant not in tenant_ids or state != "active" or identity is None:
                continue
        raw_source = policy.sources.get((memory_id, identity)) if message.get("message_type") == "raw" and identity is not None else None
        if management_metadata and message.get("message_type") == "raw" and (managed or raw_source is not None):
            if raw_source is not None and raw_source.tenant_id is not None and raw_source.tenant_id not in tenant_ids:
                continue
            metadata = {key: value for key, value in message.items() if key not in {"content", "content_embed", "extract"}}
            if raw_source is not None:
                metadata.update(status=raw_source.state == "active", managed_source_state=raw_source.state)
            allowed.append(metadata)
            continue
        projection = policy.projections.get((memory_id, identity)) if identity is not None else None
        if projection is not None:
            manageable_state = projection.entry_state == "active" or (management_body and projection.entry_state == "inactive")
            if not manageable_state or not projection.ready or projection.revision != projection.current_revision or identity != projection.current_message_id:
                continue
            if "content" in message and (not isinstance(message["content"], str) or content_hash(message["content"]) != projection.content_sha256):
                continue
            valid = True
            for source_id, revision, generation, digest in projection.sources:
                source = policy.sources.get((memory_id, source_id))
                if source is None or source.state != "active" or (source.revision, source.content_sha256) != (revision, digest):
                    valid = False
                    break
            if not valid:
                continue
            if not management_body and "status" in message and message["status"] not in (True, 1, "1"):
                continue
            if management_body and projection.entry_state == "inactive":
                message = {**message, "status": False}
        anchor_id = identity if message.get("message_type") == "raw" else _positive_id(message.get("source_id"))
        source = policy.sources.get((memory_id, anchor_id)) if anchor_id is not None else None
        if source is not None:
            if source.tenant_id is not None and source.tenant_id not in tenant_ids:
                continue
            if (source.state != "active" and not (management_body and message.get("message_type") == "raw" and source.state == "inactive")) or any(
                name in message and message[name] != source.scope.get(name) for name in ("user_id", "agent_id", "session_id")
            ):
                continue
            if message.get("message_type") == "raw" and "content" in message and (not isinstance(message["content"], str) or content_hash(message["content"]) != source.content_sha256):
                continue
            if managed and message.get("message_type") == "raw" and not management_body and "status" in message and message["status"] not in (True, 1, "1"):
                continue
        allowed.append(message)
    return allowed


def managed_query_exclusions(tenant_ids: list[str], memory_ids: list[str]) -> dict[str, dict[str, list[int]]]:
    """Compute durable eligibility exclusions before the backend selects top_n."""
    policy = _load_policy(list(dict.fromkeys(memory_ids)), None)
    result: dict[str, dict[str, list[int]]] = {}
    for memory_id, (tenant, state) in policy.banks.items():
        if tenant not in tenant_ids or state != "active":
            raise RevisionConflict("managed_memory_not_active")
        blocked_sources = {source_id for (bank, source_id), source in policy.sources.items() if bank == memory_id and source.state != "active"}
        blocked_messages = set(blocked_sources)
        for (bank, message_id), revision in policy.projections.items():
            if bank != memory_id:
                continue
            valid = revision.entry_state == "active" and revision.ready and revision.current_revision == revision.revision and revision.current_message_id == message_id
            for source_id, source_revision, _, digest in revision.sources:
                source = policy.sources.get((memory_id, source_id))
                valid = valid and source is not None and source.state == "active" and (source.revision, source.content_sha256) == (source_revision, digest)
            if not valid:
                blocked_messages.add(message_id)
        result[memory_id] = {"message_ids": sorted(blocked_messages), "source_ids": sorted(blocked_sources)}
    for (memory_id, message_id), (tenant, state) in policy.writers.items():
        if tenant not in tenant_ids:
            raise RevisionConflict("writer_memory_binding_mismatch")
        if state != "ready":
            excluded = result.setdefault(memory_id, {"message_ids": [], "source_ids": []})
            if message_id not in excluded["message_ids"]:
                excluded["message_ids"].append(message_id)
    for (memory_id, source_id), source in policy.sources.items():
        if memory_id in policy.banks or source.state == "active":
            continue
        if source.tenant_id is not None and source.tenant_id not in tenant_ids:
            raise RevisionConflict("source_memory_binding_mismatch")
        excluded = result.setdefault(memory_id, {"message_ids": [], "source_ids": []})
        if source_id not in excluded["message_ids"]:
            excluded["message_ids"].append(source_id)
        excluded["source_ids"].append(source_id)
    for exclusions in result.values():
        exclusions["message_ids"].sort()
        exclusions["source_ids"].sort()
    return result


def is_managed(tenant_id: str, memory_id: str) -> bool:
    policy = _load_policy([memory_id], [])
    if memory_id not in policy.banks:
        return False
    if policy.banks[memory_id][0] != tenant_id:
        raise RevisionConflict("managed_memory_binding_mismatch")
    return True


def requires_guarded_read(tenant_id: str, memory_id: str) -> bool:
    """Unmanaged writers and FIFO tombstones also need eligibility before paging."""
    policy = _load_policy([memory_id], None)
    if memory_id in policy.banks:
        if policy.banks[memory_id][0] != tenant_id:
            raise RevisionConflict("managed_memory_binding_mismatch")
        return True
    for (bank, _), (tenant, state) in policy.writers.items():
        if bank == memory_id:
            if tenant != tenant_id:
                raise RevisionConflict("writer_memory_binding_mismatch")
            if state != "ready":
                return True
    for (bank, _), source in policy.sources.items():
        if bank == memory_id and source.state != "active":
            if source.tenant_id is not None and source.tenant_id != tenant_id:
                raise RevisionConflict("source_memory_binding_mismatch")
            return True
    return False
