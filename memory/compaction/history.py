"""SQL predicates for compaction attempts with durable drain and retirement proof."""

from sqlalchemy import and_, func, literal, select
from sqlalchemy.sql.elements import ColumnElement

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission

SETTLED_OUTBOX = ("ready", "failed", "canceled", "redacted", "stale")


def quiet_compaction_attempt() -> ColumnElement[bool]:
    return func.coalesce(
        and_(
            MemoryWriterAdmission.operation_id.is_not(None),
            MemoryWriterAdmission.active_token.is_(None),
            MemoryWriterAdmission.state.not_in(("active", "unknown")),
            MemoryPublicationOutbox.lease_token.is_(None),
            MemoryPublicationOutbox.lease_until.is_(None),
            MemoryPublicationOutbox.receipt["active_attempts"] == [],
        ),
        False,
    )


def drained_compaction_attempt() -> ColumnElement[bool]:
    writer, outbox, artifact = MemoryWriterAdmission, MemoryPublicationOutbox, MemoryCompactionArtifact
    return func.coalesce(
        and_(
            quiet_compaction_attempt(),
            writer.kind == "context_compact",
            writer.actor_id == artifact.actor_id,
            writer.state.in_(("drained", "canceled")),
            writer.finished_at.is_not(None),
            writer.attempt == 1,
            writer.task_id.is_(None),
            writer.source_message_id.is_(None),
            writer.message_ids == [],
            writer.publication_state.in_(SETTLED_OUTBOX),
            writer.receipt["outcome"].astext == "drained",
            func.jsonb_typeof(writer.receipt["finished_tokens"][0]) == "string",
            writer.receipt["finished_tokens"] == func.jsonb_build_array(writer.receipt["finished_tokens"][0]),
            writer.receipt["finished_tokens"] == outbox.receipt["finished_attempts"],
            outbox.state.in_(SETTLED_OUTBOX),
            outbox.entry_id.is_(None),
            outbox.revision.is_(None),
            outbox.message_id.is_(None),
            outbox.expected_revision.is_(None),
            outbox.entry_generation.is_(None),
            outbox.payload.contains({"action": "context_compact", "reserved_bytes": 0}),
            ~outbox.payload.has_key("reservation_owner"),
        ),
        False,
    )


def retired_compaction_history(anchor: type[MemoryWriterAdmission] | type[MemoryPublicationOutbox] | type[MemoryMutationOperation]) -> ColumnElement[bool]:
    """Exclude only scoped, sanitized receipts with no remaining writer or storage work."""
    writer, outbox, artifact = MemoryWriterAdmission, MemoryPublicationOutbox, MemoryCompactionArtifact
    clauses = [
        artifact.retired_at.is_not(None),
        artifact.state == "stale",
        artifact.error_code == "compaction_retired",
        artifact.request == {},
        ~select(1).where(MemoryCompactionRoot.artifact_id == artifact.artifact_id).correlate(artifact).exists(),
        artifact.memory_id == anchor.memory_id,
        artifact.tenant_id == anchor.tenant_id,
        artifact.operation_id == anchor.operation_id,
        drained_compaction_attempt(),
        writer.publication_state == "canceled",
        writer.request_metadata == func.jsonb_build_object("artifact_id", artifact.artifact_id, "retired", True),
        outbox.state == "redacted",
        outbox.error_code == "compaction_retired",
        outbox.payload == {"action": "context_compact", "reserved_bytes": 0},
    ]
    if anchor is not writer:
        clauses.extend((writer.memory_id == anchor.memory_id, writer.tenant_id == anchor.tenant_id, writer.operation_id == anchor.operation_id))
    if anchor is not outbox:
        clauses.extend((outbox.memory_id == anchor.memory_id, outbox.tenant_id == anchor.tenant_id, outbox.operation_id == anchor.operation_id))
    if anchor is MemoryMutationOperation:
        clauses.extend(
            (
                artifact.actor_id == anchor.actor_id,
                anchor.kind == "context_compact",
                anchor.status == "stale",
                anchor.error_code == "compaction_retired",
            )
        )
    return func.coalesce(select(literal(True)).where(*clauses).correlate(anchor).limit(1).scalar_subquery(), False)
