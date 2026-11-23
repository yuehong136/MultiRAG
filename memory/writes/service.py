"""Fixed-plan memory appends with SQL admission and recoverable physical IO."""

from __future__ import annotations

import asyncio
import json
import math
import struct
import sys
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Task
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from common.asyncio_utils import finish_owned_work
from common.constants import MemoryType
from memory.compaction.history import retired_compaction_history
from memory.revisions.domain import (
    ModelBinding,
    RevisionCapacityError,
    RevisionConflict,
    RevisionStorageUnknown,
    RevisionValidationError,
    content_hash,
    is_active,
    message_scope,
    positive_int,
    request_hash,
    vector_dimension,
)
from memory.writes.admission import WriterCoordinator, release_writer_reservation
from memory.writes.types import Authorize, BindingReader, Embedder, EnsureIndex, ExtractionResult, Extractor, FifoEvictor, MessageIdAllocator, QueueSubmitter, WriteStorage

MAX_APPEND_ITEMS = 32
MAX_APPROVED_BYTES = 8 * 1024 * 1024


def _content(value: Any, count_tokens: Callable[[str], int]) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 65535:
        raise RevisionValidationError("invalid_append_content")
    count = count_tokens(value)
    if type(count) is not int or not 0 <= count <= 8192:
        raise RevisionValidationError("append_content_tokens_exceeded")
    return value


def _vector_bytes(vector: list[float]) -> bytes:
    try:
        return struct.pack(f"<{len(vector)}f", *vector)
    except (OverflowError, struct.error) as error:
        raise RevisionValidationError("invalid_append_vector") from error


def _time(value: Any, *, optional: bool = False) -> str | None:
    if optional and value in (None, "", "-"):
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise RevisionValidationError("invalid_append_time")
    try:
        datetime.fromisoformat(value)
    except ValueError as error:
        raise RevisionValidationError("invalid_append_time") from error
    return value


def _verify(actual: dict[str, Any], expected: dict[str, Any], *, active: bool) -> None:
    mid = expected["memory_id"]
    if message_scope(actual, mid) != message_scope(expected, mid) or positive_int(actual.get("message_id"), "invalid_message_id") != expected["message_id"]:
        raise RevisionConflict("append_projection_binding_mismatch")
    if actual.get("id") not in (None, f"{mid}_{expected['message_id']}"):
        raise RevisionConflict("append_projection_binding_mismatch")
    if actual.get("message_type") != expected["message_type"] or isinstance(actual.get("source_id"), bool) or str(actual.get("source_id") or 0) != str(expected["source_id"]):
        raise RevisionConflict("append_projection_source_mismatch")
    if not isinstance(actual.get("content"), str) or content_hash(actual["content"]) != content_hash(expected["content"]):
        raise RevisionConflict("append_projection_content_mismatch")
    if actual.get("status") not in ((True, 1, "1") if active else (False, 0, "0")) or actual.get("forget_at") not in (None, "", "-"):
        raise RevisionConflict("append_projection_state_mismatch")
    for name in ("valid_at", "invalid_at"):
        actual_time = None if actual.get(name) in (None, "", "-") else actual[name]
        expected_time = None if expected.get(name) in (None, "", "-") else expected[name]
        if actual_time != expected_time:
            raise RevisionConflict("append_projection_time_mismatch")
    if vector_dimension(actual) != len(expected["content_embed"]):
        raise RevisionConflict("append_projection_dimension_mismatch")
    values = actual["content_embed"]
    if hasattr(values, "tolist"):
        values = values.tolist()
    if _vector_bytes(values) != _vector_bytes(expected["content_embed"]):
        raise RevisionConflict("append_projection_vector_mismatch")


def verify_approved_message(actual: dict[str, Any], expected: dict[str, Any], *, active: bool) -> None:
    """Validate a tracked physical row against its server-approved immutable plan."""
    _verify(actual, expected, active=active)


class AppendService(WriterCoordinator):
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        actor_id: str,
        authorize: Authorize,
        allocate_message_id: MessageIdAllocator,
        count_tokens: Callable[[str], int],
        model_binding: BindingReader,
        storage: WriteStorage,
        embedder: Embedder,
        extractor: Extractor,
        fifo_evict: FifoEvictor,
        ensure_index: EnsureIndex,
        queue_submit: QueueSubmitter,
    ) -> None:
        super().__init__(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)
        self.allocate_message_id, self.count_tokens, self.model_binding = allocate_message_id, count_tokens, model_binding
        self.storage, self.embedder, self.extractor = storage, embedder, extractor
        self.fifo_evict, self.ensure_index, self.queue_submit = fifo_evict, ensure_index, queue_submit

    async def append_raw(
        self,
        memory_id: str,
        *,
        operation_id: str,
        content: str,
        scope: dict[str, str],
        valid_at: str,
        enqueue_extraction: bool = True,
        structured_received_at: str | None = None,
        reconcile_existing: bool = True,
    ) -> dict[str, Any]:
        if type(reconcile_existing) is not bool:
            raise RevisionValidationError("invalid_append_replay_policy")
        _content(content, self.count_tokens)
        if set(scope) != {"user_id", "agent_id", "session_id"} or any(not isinstance(value, str) for value in scope.values()) or type(enqueue_extraction) is not bool:
            raise RevisionValidationError("invalid_append_scope")
        if not isinstance(valid_at, str):
            raise RevisionValidationError("invalid_append_receipt_time")
        try:
            datetime.fromisoformat(valid_at)
        except ValueError as error:
            raise RevisionValidationError("invalid_append_receipt_time") from error
        metadata = {"valid_at": valid_at}
        request = {"content": content, "scope": scope, "enqueue_extraction": enqueue_extraction}
        if structured_received_at is not None:
            from memory.structured_sources import SOURCE_FORMAT, declared_event_time, parse_registered_source

            parse_registered_source(content)
            received = declared_event_time(structured_received_at)
            if received is None or enqueue_extraction:
                raise RevisionValidationError("structured_source_requires_explicit_maintenance")
            metadata.update(source_format=SOURCE_FORMAT, received_at=received, envelope_sha256=content_hash(content), submitted_by=self.actor_id, submission_operation_id=operation_id)
            request["source_format"] = SOURCE_FORMAT
        if reconcile_existing:
            await self.admit(memory_id, operation_id=operation_id, kind="append_raw", request=request, server_metadata=metadata)
        else:
            admitted = await self.admit(memory_id, operation_id=operation_id, kind="append_raw", request=request, server_metadata=metadata, _include_created=True)
            if admitted["_created_here"] is not True:
                return await self.operation(memory_id, operation_id)
        return await self.reconcile(memory_id, operation_id)

    async def extract_source(self, memory_id: str, *, operation_id: str, task_id: str, source_id: int) -> dict[str, Any]:
        if operation_id != task_id:
            raise RevisionValidationError("extraction_operation_must_match_task")
        positive_int(source_id, "invalid_source_id")
        existing = await self.snapshot(memory_id, operation_id)
        if existing["task_id"] != task_id or existing["source_id"] != source_id or existing["kind"] != "extract":
            raise RevisionConflict("extraction_task_binding_mismatch")
        return await self.reconcile(memory_id, operation_id)

    async def _checkpoint(self, memory_id: str, operation_id: str, token: str) -> dict[str, Any]:
        await self._idle()
        try:
            _, fence = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id)
            if row.writer_generation != fence.generation or row.active_token != token or row.publication_state == "canceled":
                raise RevisionConflict("writer_publication_fence_changed")
            payload = (await self._outbox(row)).payload
            if payload.get("model_binding") is not None:
                binding = ModelBinding(**payload["model_binding"])
                if await self.model_binding(self.db, memory_id, binding.dimension) != binding:
                    raise RevisionConflict("model_binding_changed")
            raw_binding = payload.get("raw_source_binding")
            if raw_binding is not None:
                state = await self.db.scalar(
                    select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == raw_binding["source_id"]).with_for_update()
                )
                published = row.publication_state in {"published", "ready"}
                if (
                    state is None
                    or state.state != ("active" if published else "pending")
                    or state.source_revision != 1
                    or (not published and state.generation != raw_binding["generation"])
                    or state.content_sha256 != raw_binding["content_sha256"]
                    or state.scope != raw_binding["scope"]
                ):
                    raise RevisionConflict("append_source_changed")
            source = payload.get("source_binding")
            if source is not None:
                state = await self.db.scalar(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source["source_id"]).with_for_update())
                published = row.publication_state in {"published", "ready"}
                if state is not None and (
                    state.state != "active"
                    or state.source_revision != source["revision"]
                    or (not published and state.generation != source["generation"])
                    or state.content_sha256 != source["content_sha256"]
                    or state.scope != source["scope"]
                ):
                    raise RevisionConflict("append_source_changed")
                if source.get("managed") and state is None:
                    raise RevisionConflict("append_source_missing")
            return await self._view(row)
        finally:
            await self.db.rollback()

    async def _source(self, memory_id: str, operation_id: str, token: str) -> dict[str, Any]:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        source_id = positive_int(snapshot["source_id"], "invalid_source_id")
        raw = await finish_owned_work(self.storage.read(source_id))
        if (
            raw is None
            or raw.get("message_id") != source_id
            or raw.get("message_type") != "raw"
            or raw.get("source_id") not in (None, 0, "0", "")
            or isinstance(raw.get("source_id"), bool)
            or not is_active(raw)
        ):
            raise RevisionConflict("extraction_source_not_active")
        scope = message_scope(raw, memory_id)
        if not isinstance(raw.get("content"), str):
            raise RevisionConflict("invalid_source_content")
        async with self._write(operation_id):
            _, _ = await self._memory_fence(memory_id)
            state = await self.db.scalar(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == source_id).with_for_update())
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state == "canceled":
                raise RevisionConflict("writer_publication_fence_changed")
            task = await self.db.get(Task, row.task_id)
            if task is None or task.doc_id != memory_id or task.task_type != "memory" or task.digest != str(source_id):
                raise RevisionConflict("extraction_task_binding_mismatch")
            if state is None:
                state = MemorySourceState(
                    memory_id=memory_id,
                    source_message_id=source_id,
                    tenant_id=self.tenant_id,
                    source_revision=1,
                    generation=1,
                    content_sha256=content_hash(raw["content"]),
                    state="active",
                    scope=scope,
                )
                self.db.add(state)
                await self.db.flush()
            if state is not None and (state.state != "active" or state.content_sha256 != content_hash(raw["content"]) or state.scope != scope):
                raise RevisionConflict("append_source_changed")
            outbox = await self._outbox(row)
            binding = {
                "source_id": source_id,
                "revision": state.source_revision if state is not None else 1,
                "generation": state.generation if state is not None else 1,
                "content_sha256": content_hash(raw["content"]),
                "scope": scope,
                "managed": state is not None,
            }
            outbox.payload = {**outbox.payload, "source_binding": binding}
            row.request_metadata = {**row.request_metadata, "source_content_sha256": binding["content_sha256"]}
            from memory.source_provenance import source_receipts

            metadata = (await source_receipts(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_ids=[source_id])).get(source_id)
            if metadata is not None:
                raw = {**raw, "source_metadata": metadata}
        return raw

    async def _minimum_message_id(self, memory_id: str) -> int:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            source = await self.db.scalar(select(func.max(MemorySourceState.source_message_id)).where(MemorySourceState.memory_id == memory_id))
            revision = await self.db.scalar(select(func.max(MemoryEntryRevision.message_id)).where(MemoryEntryRevision.memory_id == memory_id))
            manifests = list(
                (
                    await self.db.scalars(
                        select(MemoryWriterAdmission.message_ids)
                        .where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == self.tenant_id, ~retired_compaction_history(MemoryWriterAdmission))
                        .limit(10001)
                    )
                ).all()
            )
            if len(manifests) > 10000:
                raise RevisionConflict("writer_inventory_incomplete")
            return max([1, source or 1, revision or 1, *(identity for identities in manifests for identity in identities)])
        finally:
            await self.db.rollback()

    async def _prepare(self, memory_id: str, operation_id: str, token: str, memory_config: dict[str, Any]) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        if snapshot["payload"].get("approved"):
            return
        request, metadata = snapshot["payload"]["request"], snapshot["payload"].get("server_metadata", {})
        extraction: ExtractionResult | None = None
        if snapshot["kind"] == "append_raw":
            templates = [
                {
                    "memory_id": memory_id,
                    "message_type": "raw",
                    "source_id": 0,
                    **request["scope"],
                    "content": request["content"],
                    "valid_at": metadata["valid_at"],
                    "invalid_at": None,
                    "forget_at": None,
                    "status": False,
                }
            ]
        elif snapshot["kind"] == "extract":
            raw = await self._source(memory_id, operation_id, token)
            extracted: ExtractionResult = await finish_owned_work(self.extractor(memory_id, raw))
            extraction = extracted
            items = extracted["items"]
            if not isinstance(items, list) or len(items) > MAX_APPEND_ITEMS:
                raise RevisionValidationError("extraction_batch_too_large")
            templates = []
            for item in items:
                if not isinstance(item, dict) or item.get("message_type") not in ("semantic", "episodic", "procedural"):
                    raise RevisionValidationError("invalid_extracted_memory_type")
                enabled = MemoryType[item["message_type"].upper()].value
                if type(memory_config["memory_type"]) is not int or not memory_config["memory_type"] & enabled:
                    raise RevisionValidationError("extracted_memory_type_not_enabled")
                templates.append(
                    {
                        "memory_id": memory_id,
                        "message_type": item["message_type"],
                        "source_id": raw["message_id"],
                        **message_scope(raw, memory_id),
                        "content": _content(item.get("content"), self.count_tokens),
                        "valid_at": _time(item.get("valid_at") or raw.get("valid_at")),
                        "invalid_at": _time(item.get("invalid_at"), optional=True),
                        "forget_at": None,
                        "status": False,
                    }
                )
        else:
            raise RevisionValidationError("invalid_append_operation")
        await self._checkpoint(memory_id, operation_id, token)
        embedded = await finish_owned_work(self.embedder(memory_id, templates, None)) if templates else None
        vectors = embedded["vectors"] if embedded is not None else []
        binding = embedded["model_binding"] if embedded is not None else None
        if len(vectors) != len(templates) or (binding is not None and not isinstance(binding, ModelBinding)):
            raise RevisionConflict("embedding_result_mismatch")
        inventory = await finish_owned_work(self.storage.inventory())
        minimum = max([await self._minimum_message_id(memory_id), *(positive_int(item.get("message_id"), "invalid_physical_message_id") for item in inventory)])
        messages = []
        for template, vector in zip(templates, vectors, strict=True):
            dimension = vector_dimension({"content_embed": vector})
            if binding is None or binding.dimension != dimension or any(not math.isfinite(value) for value in vector):
                raise RevisionConflict("embedding_result_mismatch")
            _vector_bytes(vector)
            identity = positive_int(await self.allocate_message_id(minimum_message_id=minimum), "invalid_message_id")
            if identity <= minimum or await finish_owned_work(self.storage.read(identity)) is not None:
                raise RevisionConflict("projection_message_id_collision")
            minimum = identity
            messages.append({**template, "message_id": identity, "id": f"{memory_id}_{identity}", "content_embed": vector})
        if len(json.dumps(messages, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")) > MAX_APPROVED_BYTES:
            raise RevisionValidationError("approved_append_plan_too_large")
        async with self._write(operation_id):
            memory, _ = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state == "canceled":
                raise RevisionConflict("writer_publication_fence_changed")
            outbox = await self._outbox(row)
            if outbox.payload.get("approved"):
                return
            if binding is not None and await self.model_binding(self.db, memory_id, binding.dimension) != binding:
                raise RevisionConflict("model_binding_changed")
            row.message_ids = [message["message_id"] for message in messages]
            if row.kind == "append_raw":
                row.source_message_id = row.message_ids[0]
            row.request_metadata = {**row.request_metadata, "content_sha256": request_hash({"contents": [item["content"] for item in messages]})}
            outbox.payload = {
                **outbox.payload,
                "approved": True,
                "messages": messages,
                "model_binding": binding.to_dict() if binding is not None else None,
                "chat_model": {"model_reference": extraction["model_reference"], "config_reference": extraction["config_reference"]} if extraction is not None else None,
            }
            if row.kind == "append_raw":
                source = await self.db.scalar(select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == row.source_message_id).with_for_update())
                if source is not None:
                    raise RevisionConflict("projection_message_id_collision")
                scope = message_scope(messages[0], memory_id)
                digest = content_hash(messages[0]["content"])
                self.db.add(
                    MemorySourceState(
                        memory_id=memory_id, source_message_id=row.source_message_id, tenant_id=self.tenant_id, source_revision=1, generation=1, content_sha256=digest, state="pending", scope=scope
                    )
                )
                outbox.payload = {**outbox.payload, "raw_source_binding": {"source_id": row.source_message_id, "generation": 1, "content_sha256": digest, "scope": scope}}
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.result = {"raw_source_id": row.source_message_id, "message_ids": list(row.message_ids), "extraction_task_id": None, "queue_state": "not_required", "evicted_sources": []}
            if row.kind == "append_raw" and request["enqueue_extraction"] and memory.memory_type != MemoryType.RAW.value:
                task_id = uuid4().hex
                source_id = row.source_message_id
                child_request = {"source_id": source_id, "task_id": task_id}
                child_hash = request_hash({"tenant_id": self.tenant_id, "actor_id": self.actor_id, "memory_id": memory_id, "kind": "extract", "request": child_request})
                self.db.add(Task(id=task_id, doc_id=memory_id, task_type="memory", digest=str(source_id), progress=0.0, begin_at=datetime.now()))
                self.db.add(
                    MemoryWriterAdmission(
                        memory_id=memory_id,
                        operation_id=task_id,
                        tenant_id=self.tenant_id,
                        actor_id=self.actor_id,
                        kind="extract",
                        request_sha256=child_hash,
                        writer_generation=row.writer_generation,
                        state="queued",
                        publication_state="pending",
                        message_ids=[],
                        task_id=task_id,
                        source_message_id=source_id,
                        request_metadata={},
                        receipt={},
                    )
                )
                self.db.add(
                    MemoryMutationOperation(
                        memory_id=memory_id,
                        operation_id=task_id,
                        tenant_id=self.tenant_id,
                        actor_id=self.actor_id,
                        kind="extract",
                        request_sha256=child_hash,
                        status="accepted",
                        index_sync_status="pending",
                        result={},
                    )
                )
                self.db.add(
                    MemoryPublicationOutbox(
                        id=uuid4().hex,
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        operation_id=task_id,
                        bank_generation=1,
                        state="pending",
                        attempt=0,
                        payload={"action": "extract", "request": child_request, "approved": False, "reserved_bytes": 0},
                        receipt={},
                    )
                )
                queue = {
                    "id": task_id,
                    "task_id": task_id,
                    "task_type": "memory",
                    "memory_id": memory_id,
                    "tenant_id": self.tenant_id,
                    "source_id": source_id,
                    "operation_id": task_id,
                    "actor_id": self.actor_id,
                    "message_dict": deepcopy(request["scope"]),
                }
                outbox.payload = {**outbox.payload, "queue_payload": queue}
                operation.result = {**operation.result, "extraction_task_id": task_id, "queue_state": "pending"}

    async def reconcile(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        current = await self.operation(memory_id, operation_id)
        if current["index_sync_status"] == "ready" and current.get("queue_state") in {"not_required", "submitted"}:
            return current
        claimed = await self.claim(memory_id, operation_id)
        token = claimed["active_token"]
        outcome = "drained"
        try:
            await self._prepare(memory_id, operation_id, token, claimed["memory_config"])
            await self._reserve(memory_id, operation_id, token, claimed["memory_config"])
            await self._project(memory_id, operation_id, token)
            await self._dispatch(memory_id, operation_id, token)
            await self._complete(memory_id, operation_id, token)
            return await self.operation(memory_id, operation_id)
        except asyncio.CancelledError:
            outcome = "unknown"
            await finish_owned_work(self._record_error(memory_id, operation_id, "writer_interrupted"))
            raise
        except Exception as error:
            outcome = "unknown"
            known = isinstance(error, (RevisionValidationError, RevisionConflict))
            await self._record_error(memory_id, operation_id, error.code if isinstance(error, (RevisionValidationError, RevisionConflict)) else "writer_io_unconfirmed", unknown=not known)
            if isinstance(error, (RevisionValidationError, RevisionCapacityError, RevisionConflict)):
                raise
            return await self.operation(memory_id, operation_id)
        finally:
            await finish_owned_work(self.finish(memory_id, operation_id, token=token, outcome=outcome))

    async def _capacity(self, memory_id: str, operation_id: str, token: str) -> dict[str, Any]:
        await self._idle()
        try:
            _, fence = await self._memory_fence(memory_id)
            bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id))
            row = await self._row(memory_id, operation_id)
            if row.active_token != token or row.publication_state == "canceled":
                raise RevisionConflict("writer_publication_fence_changed")
            return {
                "owner": "managed_bank" if bank is not None else "writer_fence",
                "generation": bank.capacity_generation if bank is not None else fence.capacity_generation,
                "reserved": bank.reserved_bytes if bank is not None else fence.reserved_bytes,
            }
        finally:
            await self.db.rollback()

    @staticmethod
    def _message_bytes(message: dict[str, Any]) -> int:
        values = message.get("content_embed")
        return sys.getsizeof(message.get("content") or "") + (len(values) * sys.getsizeof(0.0) if values is not None else 0)

    async def _fifo_blocked_sources(self, memory_id: str) -> set[int]:
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            rows = list(
                (
                    await self.db.execute(
                        select(
                            MemoryPublicationOutbox.entry_id,
                            MemoryPublicationOutbox.payload["source_id"],
                            MemoryPublicationOutbox.payload["message"]["source_id"],
                            MemoryPublicationOutbox.payload["source_binding"]["source_id"],
                        )
                        .where(MemoryPublicationOutbox.memory_id == memory_id, func.jsonb_array_length(MemoryPublicationOutbox.receipt["active_attempts"]) > 0)
                        .limit(10001)
                    )
                ).all()
            )
            if len(rows) > 10000:
                raise RevisionConflict("writer_inventory_incomplete")
            ids = {int(value) for row in rows for value in (row[1], row[2], row[3]) if type(value) is int and value > 0}
            entries = {row[0] for row in rows if row[0] is not None}
            if entries:
                ids.update((await self.db.scalars(select(MemoryRevisionSource.source_message_id).where(MemoryRevisionSource.memory_id == memory_id, MemoryRevisionSource.entry_id.in_(entries)))).all())
            return ids
        finally:
            await self.db.rollback()

    async def _reserve(self, memory_id: str, operation_id: str, token: str, memory_config: dict[str, Any]) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        payload = snapshot["payload"]
        if snapshot["publication_state"] in {"published", "ready"} or payload.get("reservation_prepared"):
            return
        epoch = await self._capacity(memory_id, operation_id, token)
        inventory = await finish_owned_work(self.storage.inventory())
        selected_ids = set(snapshot["message_ids"])
        present_ids = {message["message_id"] for message in inventory}
        required = sum(self._message_bytes(message) + 4096 for message in payload["messages"] if message["message_id"] not in present_ids)
        observed = sum(self._message_bytes(message) for message in inventory)
        needed = observed + epoch["reserved"] + required - memory_config["memory_size"]
        plan = payload.get("eviction_plan")
        if plan is None:
            plan = []
            if needed > 0:
                if memory_config["forgetting_policy"] != "FIFO":
                    raise RevisionCapacityError("insufficient_append_capacity")
                blocked_sources = await self._fifo_blocked_sources(memory_id)
                candidates = []
                for raw in inventory:
                    if raw.get("message_type") != "raw" or raw["message_id"] == snapshot["source_id"] or raw["message_id"] in selected_ids or raw["message_id"] in blocked_sources:
                        continue
                    try:
                        source_time = datetime.fromisoformat(raw["valid_at"])
                        message_scope(raw, memory_id)
                    except (ValueError, TypeError, KeyError, RevisionConflict):
                        continue
                    candidates.append((raw.get("forget_at") in (None, "", "-"), source_time, raw))
                if len({item[1].tzinfo is None for item in candidates}) > 1:
                    raise RevisionCapacityError("fifo_source_times_incomparable")
                for _, _, raw in sorted(candidates, key=lambda item: (item[0], item[1], item[2]["message_id"])):
                    source_id = raw["message_id"]
                    child = "fifo:" + request_hash({"memory_id": memory_id, "operation_id": operation_id, "source_id": source_id})
                    plan.append({"source_id": source_id, "operation_id": child, "content_sha256": content_hash(raw["content"]), "scope": message_scope(raw, memory_id), "valid_at": raw["valid_at"]})
                    needed -= sum(self._message_bytes(item) for item in inventory if item["message_id"] == source_id or str(item.get("source_id")) == str(source_id))
                    if needed <= 0:
                        break
                if needed > 0:
                    raise RevisionCapacityError("fifo_capacity_unavailable")
            async with self._write(operation_id):
                await self._memory_fence(memory_id)
                row = await self._row(memory_id, operation_id, lock=True)
                if row.active_token != token or row.publication_state == "canceled":
                    raise RevisionConflict("writer_publication_fence_changed")
                outbox = await self._outbox(row)
                outbox.payload = {**outbox.payload, "eviction_plan": plan}
        for victim in plan:
            await self._checkpoint(memory_id, operation_id, token)
            victim_raw = await finish_owned_work(self.storage.read(victim["source_id"]))
            if victim_raw is not None and (
                not isinstance(victim_raw.get("content"), str) or content_hash(victim_raw["content"]) != victim["content_sha256"] or message_scope(victim_raw, memory_id) != victim["scope"]
            ):
                raise RevisionConflict("fifo_source_changed")
            source = (
                victim_raw
                if victim_raw is not None
                else {
                    "memory_id": memory_id,
                    "message_id": victim["source_id"],
                    "message_type": "raw",
                    "source_id": 0,
                    "content_sha256": victim["content_sha256"],
                    **victim["scope"],
                    "valid_at": victim["valid_at"],
                }
            )
            result = await finish_owned_work(self.fifo_evict(memory_id, source, victim["operation_id"]))
            async with self._write(operation_id):
                await self._memory_fence(memory_id)
                row = await self._row(memory_id, operation_id, lock=True)
                operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
                assert operation is not None
                evictions = [item for item in operation.result.get("evicted_sources", []) if item["source_id"] != victim["source_id"]]
                operation.result = {
                    **operation.result,
                    "evicted_sources": [*evictions, {"source_id": victim["source_id"], "operation_id": victim["operation_id"], "state": result.get("status", "unknown")}],
                }
            if result.get("index_sync_status") != "ready":
                raise RevisionCapacityError("fifo_eviction_requires_reconciliation")
        if plan:
            epoch = await self._capacity(memory_id, operation_id, token)
            inventory = await finish_owned_work(self.storage.inventory())
            observed = sum(self._message_bytes(message) for message in inventory)
            present_ids = {message["message_id"] for message in inventory}
            required = sum(self._message_bytes(message) + 4096 for message in payload["messages"] if message["message_id"] not in present_ids)
        async with self._write(operation_id):
            memory, fence = await self._memory_fence(memory_id)
            bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id))
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state == "canceled":
                raise RevisionConflict("writer_publication_fence_changed")
            target = bank if epoch["owner"] == "managed_bank" else fence
            if target is None or target.capacity_generation != epoch["generation"]:
                raise RevisionConflict("capacity_measurement_changed")
            if observed + target.reserved_bytes + required > memory.memory_size:
                raise RevisionCapacityError("insufficient_append_capacity")
            target.reserved_bytes += required
            outbox = await self._outbox(row)
            outbox.payload = {**outbox.payload, "reserved_bytes": required, "reservation_owner": epoch["owner"], "reservation_prepared": True}

    async def _project(self, memory_id: str, operation_id: str, token: str) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        if snapshot["publication_state"] == "ready":
            return
        messages = snapshot["payload"]["messages"]
        if messages:
            await finish_owned_work(self.ensure_index(memory_id, len(messages[0]["content_embed"])))
        if snapshot["publication_state"] != "published":
            for expected in messages:
                await self._checkpoint(memory_id, operation_id, token)
                actual = await finish_owned_work(self.storage.read(expected["message_id"]))
                if actual is None:
                    await finish_owned_work(self.storage.write(expected))
                    actual = await finish_owned_work(self.storage.read(expected["message_id"]))
                if actual is None:
                    raise RevisionStorageUnknown("append_projection_not_visible")
                _verify(actual, expected, active=False)
            async with self._write(operation_id):
                await self._memory_fence(memory_id)
                row = await self._row(memory_id, operation_id, lock=True)
                if row.active_token != token or row.publication_state == "canceled":
                    raise RevisionConflict("writer_publication_fence_changed")
                outbox = await self._outbox(row)
                raw_binding = outbox.payload.get("raw_source_binding")
                if raw_binding is not None:
                    state = await self.db.scalar(
                        select(MemorySourceState).where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == raw_binding["source_id"]).with_for_update()
                    )
                    if state is None or state.state != "pending" or state.generation != raw_binding["generation"] or state.content_sha256 != raw_binding["content_sha256"]:
                        raise RevisionConflict("append_source_changed")
                    state.state, state.updated_at = "active", datetime.now(UTC)
                row.publication_state, outbox.state = "published", "sync_pending"
                outbox.payload = {**outbox.payload, "published": True}
                operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
                assert operation is not None
                operation.status, operation.index_sync_status = "published", "sync_pending"
        for expected in messages:
            await self._checkpoint(memory_id, operation_id, token)
            actual = await finish_owned_work(self.storage.read(expected["message_id"]))
            if actual is None:
                raise RevisionStorageUnknown("published_append_missing")
            if actual.get("status") not in (True, 1, "1"):
                await finish_owned_work(self.storage.set_status(expected["message_id"], True))
            actual = await finish_owned_work(self.storage.read(expected["message_id"]))
            if actual is None:
                raise RevisionStorageUnknown("published_append_missing")
            _verify(actual, expected, active=True)
        async with self._write(operation_id):
            _, fence = await self._memory_fence(memory_id)
            bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id))
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state != "published":
                raise RevisionConflict("writer_publication_fence_changed")
            outbox = await self._outbox(row)
            await release_writer_reservation(self.db, memory_id=memory_id, tenant_id=self.tenant_id, outbox=outbox, fence=fence, bank=bank)
            row.publication_state, outbox.state = "ready", "ready"
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
            if row.kind == "extract" and row.task_id is not None:
                task = await self.db.get(Task, row.task_id)
                if task is None or task.doc_id != memory_id or task.digest != str(row.source_message_id):
                    raise RevisionConflict("extraction_task_binding_mismatch")
                task.progress, task.progress_msg = 1.0, "Managed extraction published."

    async def _dispatch(self, memory_id: str, operation_id: str, token: str) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        queue = snapshot["payload"].get("queue_payload")
        if queue is None:
            return
        operation = await self.operation(memory_id, operation_id)
        if operation.get("queue_state") == "submitted":
            return
        try:
            submitted = await finish_owned_work(self.queue_submit(queue))
        except Exception:
            submitted = False
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state != "ready":
                raise RevisionConflict("writer_publication_fence_changed")
            operation_row = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation_row is not None
            operation_row.result = {**operation_row.result, "queue_state": "submitted" if submitted is True else "unknown"}
            operation_row.status = "ready" if submitted is True else "published"

    async def _complete(self, memory_id: str, operation_id: str, token: str) -> None:
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state != "ready":
                raise RevisionConflict("writer_publication_fence_changed")
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            complete = operation.result.get("queue_state") in {"not_required", "submitted"}
            operation.status, operation.index_sync_status, operation.error_code = "ready" if complete else "published", "ready", None if complete else "queue_notification_unconfirmed"

    async def _record_error(self, memory_id: str, operation_id: str, code: str, *, unknown: bool = True) -> None:
        async with self._write(operation_id):
            row = await self._row(memory_id, operation_id, lock=True)
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            if operation is None or row.publication_state == "canceled":
                return
            operation.status, operation.index_sync_status, operation.error_code = "persistence_unknown" if unknown else "failed", "unknown" if unknown else "failed", code
