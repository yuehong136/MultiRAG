"""API and worker adapters for the coordinated memory writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory, Task, async_db_connection
from api.db.memory_revision_models import ManagedMemoryState
from api.db.memory_writer_models import MemoryWriterAdmission
from api.identity.run_context import RunContext
from common import settings
from common.asyncio_utils import finish_owned_work
from common.time_utils import current_timestamp, timestamp_to_date
from common.token_utils import encoder
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, validate_operation_id
from memory.revisions.identifiers import MEMORY_COUNTER_KEY, allocate_message_id
from memory.revisions.sources import SourceService
from memory.revisions.storage import ManagedMessageStore
from memory.structured_sources import canonical_source, source_envelope
from memory.writes.providers import MemoryWriteProviders, release_sql
from memory.writes.service import AppendService

_COUNTER_KEY = MEMORY_COUNTER_KEY


class MemoryTaskPending(Exception):
    """Retain a stable operation for retry or explicit operator reconciliation."""

    def __init__(self, operation_id: str, code: str, *, acknowledge: bool = False) -> None:
        self.operation_id = operation_id
        self.code = code
        self.acknowledge = acknowledge
        super().__init__(code)


def supports_coordinated_writes() -> bool:
    """Inspect declared adapter capabilities without probing missing resource stubs."""
    connector = type(settings.msgStoreConn)
    methods = ("ensure_memory_index", "query_source_rows", "query_management_rows", "index_exists_strict", "search_managed")
    return all(callable(getattr(connector, name, None)) for name in methods)


def append_ready(operation: dict[str, Any]) -> bool:
    return operation.get("status") == "ready" and operation.get("index_sync_status") == "ready" and operation.get("queue_state") in {"submitted", "not_required"}


async def service_for(db: AsyncSession, memory_id: str, *, actor_id: str | None, tenant_id: str | None = None) -> AppendService:
    row = (await db.execute(select(Memory.id, Memory.tenant_id).where(Memory.id == memory_id))).one_or_none()
    if row is None or (tenant_id is not None and row.tenant_id != tenant_id):
        raise RevisionNotFound("memory_not_found")
    provider = MemoryWriteProviders(db, tenant_id=row.tenant_id, actor_id=actor_id if actor_id is not None else row.tenant_id)
    await provider.authorize(db, memory_id)
    await release_sql(db)
    store = ManagedMessageStore(row.tenant_id, memory_id)

    async def allocate(*, minimum_message_id: int) -> int:
        return await allocate_message_id(minimum_message_id=minimum_message_id, counter_key=_COUNTER_KEY)

    async def source_read(mid: str, message_id: int) -> dict[str, Any] | None:
        if mid != memory_id:
            raise RevisionNotFound("memory_not_found")
        return await store.read(message_id)

    async def evict(mid: str, source: dict[str, Any], operation_id: str) -> dict[str, Any]:
        if mid != memory_id:
            raise RevisionNotFound("memory_not_found")
        managed = await db.scalar(select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == mid, ManagedMemoryState.tenant_id == provider.tenant_id))
        await release_sql(db)
        if managed is not None:
            source_service = SourceService(db, tenant_id=provider.tenant_id, actor_id=provider.actor_id, authorize=provider.authorize, message_reader=source_read)
            return await source_service.evict_source(mid, source, operation_id=operation_id, storage=store)
        from memory.writes.retention import FifoRetentionService

        retention = FifoRetentionService(db, tenant_id=provider.tenant_id, actor_id=provider.actor_id, authorize=provider.authorize)
        return await retention.evict_source(mid, source, operation_id=operation_id, storage=store)

    async def submit(payload: dict[str, Any]) -> bool:
        return await finish_owned_work(asyncio.to_thread(REDIS_CONN.queue_product, settings.get_svr_queue_name(priority=0), message=payload))

    return AppendService(
        db,
        tenant_id=provider.tenant_id,
        actor_id=provider.actor_id,
        authorize=provider.authorize,
        allocate_message_id=allocate,
        count_tokens=lambda text: len(encoder.encode(text, disallowed_special=())),
        model_binding=provider.model_binding,
        storage=store,
        embedder=provider.embed,
        extractor=provider.extract,
        fifo_evict=evict,
        ensure_index=store.ensure_index,
        queue_submit=submit,
    )


async def append_message(db: AsyncSession, memory_id: str, message: dict[str, Any], *, operation_id: str, actor_id: str | None, tenant_id: str | None = None) -> dict[str, Any]:
    validate_operation_id(operation_id)
    scope = {name: message.get(name, "") for name in ("user_id", "agent_id", "session_id")}
    if any(not isinstance(value, str) or len(value.encode("utf-8")) > 256 for value in scope.values()):
        raise RevisionValidationError("invalid_append_identity")
    content = f"User Input: {message.get('user_input')}\nAgent Response: {message.get('agent_response')}"
    service = await service_for(db, memory_id, actor_id=actor_id, tenant_id=tenant_id)
    return await service.append_raw(memory_id, operation_id=operation_id, content=content, scope=scope, valid_at=timestamp_to_date(current_timestamp()))


async def append_structured_message(db: AsyncSession, memory_id: str, message: dict[str, Any], *, operation_id: str, actor_id: str | None, tenant_id: str | None = None) -> dict[str, Any]:
    """Use the source writer contract for an explicitly selected Message producer."""
    validate_operation_id(operation_id)
    segments: list[dict[str, str]] = []
    for role, field in (("user", "user_input"), ("assistant", "agent_response")):
        content = message.get(field, "")
        if not isinstance(content, str):
            raise RevisionValidationError("invalid_message_source_content")
        if content.strip():
            segments.append({"role": role, "content": content})
    envelope = source_envelope(segments, None)
    scope = {name: message.get(name, "") for name in ("user_id", "agent_id", "session_id")}
    if any(not isinstance(value, str) or len(value.encode("utf-8")) > 256 for value in scope.values()):
        raise RevisionValidationError("invalid_append_identity")
    service = await service_for(db, memory_id, actor_id=actor_id, tenant_id=tenant_id)
    instant = datetime.now(UTC)
    return await service.append_raw(
        memory_id,
        operation_id=operation_id,
        content=canonical_source(envelope),
        scope=scope,
        valid_at=timestamp_to_date(int(instant.timestamp() * 1000)),
        enqueue_extraction=False,
        structured_received_at=instant.isoformat(),
        reconcile_existing=False,
    )


async def queue_messages(
    db: AsyncSession,
    memory_ids: list[str],
    message: dict[str, Any],
    *,
    actor_id: str | None = None,
    operation_id: str | None = None,
    run_context: RunContext | None = None,
    operation_results: list[dict[str, Any]] | None = None,
    structured_memory: bool = False,
    is_canceled: Callable[[], bool] | None = None,
) -> tuple[bool, str]:
    if type(structured_memory) is not bool:
        raise RevisionValidationError("invalid_structured_memory")
    if structured_memory:
        if operation_id is None:
            raise RevisionValidationError("structured_memory_operation_required")
        if not isinstance(run_context, RunContext) or run_context.principal is None or not run_context.platform_user_id:
            raise PermissionError("Structured memory requires an authenticated run.")
        if actor_id is not None and actor_id != run_context.platform_user_id:
            raise PermissionError("Memory actor does not match the run.")
        actor_id = run_context.platform_user_id
        target = run_context.agent_id or (run_context.draft_target.agent_id if run_context.draft_target is not None else None)
        if target is not None and message.get("agent_id") != target:
            raise PermissionError("Memory agent does not match the run.")
        if not supports_coordinated_writes():
            raise RevisionValidationError("structured_memory_backend_unsupported")
        if is_canceled is not None and is_canceled():
            raise asyncio.CancelledError
    ids = list(dict.fromkeys(memory_ids))
    rows = list(await db.execute(select(Memory.id, Memory.tenant_id).where(Memory.id.in_(ids))))
    owners = {row.id: row.tenant_id for row in rows}
    trusted = dict(message)
    if run_context is not None and run_context.principal is not None:
        if set(owners) != set(ids) or any(tenant != run_context.tenant_id for tenant in owners.values()):
            raise PermissionError("Memory not available")
        trusted["user_id"] = run_context.platform_user_id
    # Authorization denial must precede every model, counter, storage and queue effect.
    if actor_id is not None:
        if set(owners) != set(ids):
            await release_sql(db)
            return False, "Memory not found."
        for mid in ids:
            await MemoryWriteProviders(db, tenant_id=owners[mid], actor_id=actor_id).authorize(db, mid)
    await release_sql(db)
    operation_id = operation_id or "append:" + uuid4().hex
    validate_operation_id(operation_id)
    failures: list[str] = []
    for mid in ids:
        if structured_memory and is_canceled is not None and is_canceled():
            raise asyncio.CancelledError
        if mid not in owners:
            failures.append(f"Memory '{mid}' not found.")
            continue
        try:
            append = append_structured_message if structured_memory else append_message
            result = await append(db, mid, trusted, operation_id=operation_id, actor_id=actor_id, tenant_id=owners[mid])
        except asyncio.CancelledError:
            if structured_memory and operation_results is not None:
                operation_results.append({"memory_id": mid, "operation_id": operation_id, "error_code": "memory_write_canceled", "requires_reconciliation": True})
            raise
        except (RevisionConflict, RevisionValidationError, RevisionNotFound, RevisionPersistenceUnknown) as error:
            result = {"operation_id": operation_id, "error_code": error.code, "requires_reconciliation": True}
        except Exception:
            result = {"operation_id": operation_id, "error_code": "memory_write_unconfirmed", "requires_reconciliation": True}
        if operation_results is not None:
            operation_results.append({"memory_id": mid, **result})
        if not append_ready(result):
            failures.append(f"Memory '{mid}' operation '{operation_id}' requires reconciliation.")
    return (False, " ".join(failures)) if failures else (True, "All add to task.")


async def update_task_result(db: AsyncSession, memory_id: str, task_id: str, operation: dict[str, Any], *, failure_code: str | None = None) -> None:
    await release_sql(db)
    ready = operation.get("status") == "ready" and operation.get("index_sync_status") == "ready"
    await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.doc_id == memory_id, Task.task_type == "memory")
        .values(
            progress=1.0 if ready else -1.0 if failure_code is not None else 0.0,
            progress_msg="Memory extraction completed." if ready else f"Memory operation {task_id} requires reconciliation: {failure_code or operation.get('error_code') or 'pending'}",
        )
    )
    await finish_owned_work(db.commit())


async def handle_coordinated_task(task: dict[str, Any], *, trusted_internal: bool = False) -> bool:
    """A SQL acknowledgement failure retains the same unacknowledged task."""
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise RevisionValidationError("invalid_extraction_task")
    try:
        return await _handle_coordinated_task(task, trusted_internal=trusted_internal)
    except (DBAPIError, RevisionPersistenceUnknown) as error:
        raise MemoryTaskPending(task_id, "memory_task_commit_unconfirmed") from error


async def _handle_coordinated_task(task: dict[str, Any], *, trusted_internal: bool = False) -> bool:
    """Return False only for legacy tasks that have no durable writer admission."""
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise RevisionValidationError("invalid_extraction_task")
    async with async_db_connection() as db:
        row = await db.scalar(select(MemoryWriterAdmission).where(MemoryWriterAdmission.task_id == task_id))
        if row is None:
            persisted = await db.get(Task, task_id)
            if persisted is None or persisted.task_type != "memory" or persisted.doc_id != task.get("memory_id") or persisted.digest != str(task.get("source_id")):
                raise RevisionConflict("extraction_task_binding_mismatch")
            if persisted.progress >= 1 or persisted.progress < 0:
                return True
            tenant_id = await db.scalar(select(Memory.tenant_id).where(Memory.id == persisted.doc_id))
            if tenant_id is None:
                return True
            if not trusted_internal and tenant_id != task.get("tenant_id"):
                raise RevisionConflict("extraction_task_binding_mismatch")
            source_id = task.get("source_id")
            if type(source_id) is not int or source_id <= 0:
                raise RevisionValidationError("invalid_source_id")
            await release_sql(db)
            try:
                dependents = await ManagedMessageStore(tenant_id, task["memory_id"]).source_dependents(source_id)
            except Exception as error:
                raise MemoryTaskPending(task_id, "legacy_source_inventory_unconfirmed") from error
            if any(identity != source_id for identity in dependents):
                await update_task_result(db, task["memory_id"], task_id, {}, failure_code="legacy_extraction_requires_reconciliation")
                raise MemoryTaskPending(task_id, "legacy_extraction_requires_reconciliation", acknowledge=True)
            service = await service_for(db, task["memory_id"], actor_id=tenant_id, tenant_id=tenant_id)
            await service.admit(task["memory_id"], operation_id=task_id, kind="extract", request={"source_id": source_id, "task_id": task_id}, task_id=task_id, source_id=source_id)
            row = await db.scalar(select(MemoryWriterAdmission).where(MemoryWriterAdmission.task_id == task_id))
        if (
            row is None
            or row.kind != "extract"
            or row.operation_id != task_id
            or row.memory_id != task.get("memory_id")
            or (not trusted_internal and row.tenant_id != task.get("tenant_id"))
            or row.source_message_id != task.get("source_id")
            or type(row.source_message_id) is not int
        ):
            raise RevisionConflict("extraction_task_binding_mismatch")
        memory_id, source_id, actor_id, canceled = row.memory_id, row.source_message_id, row.actor_id, row.publication_state == "canceled"
        await release_sql(db)
        if canceled:
            await update_task_result(db, memory_id, task_id, {}, failure_code="extraction_canceled")
            return True
        service = await service_for(db, memory_id, actor_id=actor_id)
        failure: str | None = None
        try:
            result = await service.extract_source(memory_id, operation_id=task_id, task_id=task_id, source_id=source_id)
        except (RevisionConflict, RevisionValidationError, RevisionNotFound, RevisionPersistenceUnknown) as error:
            failure = error.code
            result = await service.operation(memory_id, task_id)
        if result.get("status") == "ready" and result.get("index_sync_status") == "ready":
            await update_task_result(db, memory_id, task_id, result)
            return True
        snapshot = await service.snapshot(memory_id, task_id)
        canceled = snapshot["publication_state"] == "canceled"
        exhausted = snapshot.get("receipt", {}).get("automatic_attempts_exhausted") is True
        needs_operator = failure == "writer_needs_operator_reconciliation"
        source_unavailable = failure in {"extraction_source_not_active", "append_source_changed", "append_source_missing"}
        attempts = await db.scalar(select(MemoryWriterAdmission.attempt).where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.operation_id == task_id))
        exhausted = exhausted or (attempts is not None and attempts >= 3)
        await release_sql(db)
        terminal = canceled or exhausted or needs_operator or source_unavailable
        await update_task_result(db, memory_id, task_id, result, failure_code=(failure or result.get("error_code") or "pending") if terminal else None)
        if canceled:
            return True
        raise MemoryTaskPending(task_id, failure or result.get("error_code") or "memory_extraction_pending", acknowledge=terminal)


async def save_input(memory_id: str, message: dict[str, Any]) -> tuple[bool, str]:
    """The internal save helper follows the same RAW-first asynchronous pipeline."""
    operation_id = "append:" + uuid4().hex
    async with async_db_connection() as db:
        result = await append_message(db, memory_id, message, operation_id=operation_id, actor_id=None)
    return (True, "Memory saved; extraction is queued when configured.") if append_ready(result) else (False, f"Memory operation {operation_id} requires reconciliation.")
