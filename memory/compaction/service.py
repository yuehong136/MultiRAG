"""One durable, drained model attempt per explicit context-artifact operation."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.compaction.domain import MAX_ARTIFACTS, SYSTEM, canonical, input_fingerprint, model_body, package_for, selection, summary_text
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, request_hash, validate_operation_id
from memory.source_provenance import source_receipts
from memory.writes.admission import WriterCoordinator, _admissions
from memory.writes.types import Authorize

SourceLoader = Callable[[str, list[str]], Awaitable[list[dict[str, Any]]]]
ModelIdentity = Callable[[str], Awaitable[dict[str, Any]]]
Summarizer = Callable[[str, str, int, dict[str, Any]], Awaitable[str]]


class CompactionService(WriterCoordinator):
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        actor_id: str,
        authorize: Authorize,
        load_sources: SourceLoader,
        model_identity: ModelIdentity,
        summarize: Summarizer,
        count_tokens: Callable[[str], int],
    ) -> None:
        super().__init__(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)
        self.load_sources, self.model_identity, self.summarize, self.count_tokens = load_sources, model_identity, summarize, count_tokens
        self._pending: dict[str, Any] = {}

    async def _record(self, memory_id: str, operation_id: str, *, lock: bool = False) -> MemoryCompactionArtifact | None:
        query = (
            select(MemoryCompactionArtifact)
            .where(MemoryCompactionArtifact.memory_id == memory_id, MemoryCompactionArtifact.tenant_id == self.tenant_id, MemoryCompactionArtifact.operation_id == operation_id)
            .execution_options(populate_existing=True)
        )
        return await self.db.scalar(query.with_for_update() if lock else query)

    async def _root_bindings(self, artifact: MemoryCompactionArtifact) -> list[dict[str, Any]]:
        return [
            deepcopy(row.binding) for row in await self.db.scalars(select(MemoryCompactionRoot).where(MemoryCompactionRoot.artifact_id == artifact.artifact_id).order_by(MemoryCompactionRoot.ordinal))
        ]

    async def _assert_roots(self, memory_id: str, bindings: list[dict[str, Any]]) -> None:
        bank = await self.db.scalar(
            select(ManagedMemoryState)
            .where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id)
            .execution_options(populate_existing=True)
            .with_for_update(read=True)
        )
        if bank is None or bank.state != "active":
            raise RevisionConflict("compaction_managed_memory_required")
        ids = [int(item["source_id"]) for item in bindings]
        states = {
            row.source_message_id: row
            for row in await self.db.scalars(
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id.in_(ids))
                .order_by(MemorySourceState.source_message_id)
                .execution_options(populate_existing=True)
                .with_for_update(read=True)
            )
        }
        receipts = await source_receipts(self.db, memory_id=memory_id, tenant_id=self.tenant_id, source_ids=ids)
        for binding in bindings:
            state, receipt = states.get(int(binding["source_id"])), receipts.get(int(binding["source_id"]))
            pin = receipt["provenance_sha256"] if receipt is not None else None
            if (
                state is None
                or state.tenant_id != self.tenant_id
                or state.state != "active"
                or (str(state.source_revision), str(state.generation), state.content_sha256, state.scope, pin)
                != (binding["source_revision"], binding["source_generation"], binding["content_sha256"], binding["scope"], binding["provenance_sha256"])
            ):
                raise RevisionConflict("compaction_source_changed")

    async def _configure_admission(self, row: MemoryWriterAdmission, fence: MemoryWriterFence, request: dict[str, Any]) -> None:
        count = await self.db.scalar(
            select(func.count()).select_from(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == row.memory_id, MemoryCompactionArtifact.tenant_id == self.tenant_id)
        )
        if count is None or count >= MAX_ARTIFACTS:
            raise RevisionConflict("compaction_artifact_inventory_limit")
        if any(
            other.operation_id != row.operation_id and (other.active_token is not None or other.state in {"active", "unknown"}) for other in await _admissions(self.db, row.memory_id, self.tenant_id)
        ):
            raise RevisionBusy("memory_writer_in_progress")
        roots = self._pending["roots"]
        await self._assert_roots(row.memory_id, [item["binding"] for item in roots])
        artifact = MemoryCompactionArtifact(
            artifact_id=uuid4().hex,
            memory_id=row.memory_id,
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            operation_id=row.operation_id,
            request_sha256=request_hash(request),
            request=deepcopy(request),
            state="running",
            input_manifest_sha256=input_fingerprint(roots),
            model_binding=deepcopy(self._pending["model_binding"]),
            usage=deepcopy(self._pending["usage"]),
        )
        self.db.add(artifact)
        await self.db.flush()
        for ordinal, item in enumerate(roots):
            binding = item["binding"]
            self.db.add(
                MemoryCompactionRoot(
                    artifact_id=artifact.artifact_id,
                    ordinal=ordinal,
                    memory_id=row.memory_id,
                    tenant_id=self.tenant_id,
                    source_id=int(binding["source_id"]),
                    source_revision=int(binding["source_revision"]),
                    source_generation=int(binding["source_generation"]),
                    content_sha256=binding["content_sha256"],
                    provenance_sha256=binding["provenance_sha256"],
                    binding=deepcopy(binding),
                )
            )
        row.active_token, row.state, row.attempt, row.started_at = uuid4().hex, "active", 1, datetime.now(UTC)
        row.request_metadata = {"artifact_id": artifact.artifact_id, "source_ids": [int(item["binding"]["source_id"]) for item in roots]}
        outbox = await self._outbox(row)
        outbox.receipt = {"active_attempts": [row.active_token]}
        operation = await self.db.get(MemoryMutationOperation, (row.memory_id, row.operation_id))
        assert operation is not None
        operation.status, operation.index_sync_status, operation.result = "running", "ready", {"artifact_id": artifact.artifact_id, "generated_only": True}

    async def generate(
        self, memory_id: str, *, operation_id: str, source_ids: list[str], prefix_count: int, input_budget_tokens: int = 16384, output_budget_tokens: int = 1024, budget_tokens: int = 4096
    ) -> dict[str, Any]:
        request = selection(operation_id, source_ids, prefix_count, input_budget_tokens, output_budget_tokens, budget_tokens)
        await self._idle()
        await self.authorize(self.db, memory_id)
        previous = await self._record(memory_id, operation_id)
        if previous is not None:
            if previous.actor_id != self.actor_id or previous.request_sha256 != request_hash(request):
                raise RevisionConflict("operation_request_mismatch")
            await self._idle()
            return await self.get(memory_id, operation_id)
        await self._idle()
        roots = await self.load_sources(memory_id, source_ids)
        body = model_body(roots, prefix_count)
        before = self.count_tokens(canonical(package_for(roots)))
        estimated_input = self.count_tokens(canonical({"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}], "max_tokens": output_budget_tokens}))
        if estimated_input > input_budget_tokens:
            raise RevisionValidationError("compaction_input_budget_exceeded")
        if self.count_tokens(canonical(package_for(roots, prefix_count=prefix_count, summary=""))) > budget_tokens:
            raise RevisionValidationError("compaction_retained_budget_exceeded")
        binding = await self.model_identity(memory_id)
        usage = {
            "estimated_before_tokens": before,
            "estimated_after_tokens": None,
            "budget_tokens": budget_tokens,
            "estimated_input_tokens": estimated_input,
            "input_budget_tokens": input_budget_tokens,
            "output_budget_tokens": output_budget_tokens,
            "token_count_is_estimate": True,
            "provider_tokens": None,
            "provider_billing_verified": False,
        }
        self._pending = {"roots": roots, "model_binding": binding, "usage": usage}
        admitted = await self.admit(memory_id, operation_id=operation_id, kind="context_compact", request=request, _include_created=True)
        if admitted["_created_here"] is not True:
            return await self.get(memory_id, operation_id)
        token = admitted["active_token"]
        assert isinstance(token, str)
        try:
            current = await self.load_sources(memory_id, source_ids)
            if input_fingerprint(current) != input_fingerprint(roots):
                raise RevisionConflict("compaction_source_changed")
            await self._idle()
            artifact = await self._record(memory_id, operation_id)
            if artifact is None or artifact.cancellation_requested or artifact.state != "running":
                raise RevisionConflict("compaction_canceled_or_stale")
            await self._idle()
            text = summary_text(await finish_owned_work(self.summarize(memory_id, body, output_budget_tokens, binding)))
            package = package_for(roots, prefix_count=prefix_count, summary=text)
            after = self.count_tokens(canonical(package))
            usage = {**usage, "estimated_after_tokens": after}
            if after >= before:
                raise RevisionValidationError("compaction_no_effect")
            if after > budget_tokens:
                raise RevisionValidationError("compaction_output_budget_exceeded")
            current = await self.load_sources(memory_id, source_ids)
            if input_fingerprint(current) != input_fingerprint(roots):
                raise RevisionConflict("compaction_source_changed")
            await self._seal(memory_id, operation_id, state="generated", package=package, usage=usage, bindings=[item["binding"] for item in roots])
        except asyncio.CancelledError:
            await finish_owned_work(self._seal(memory_id, operation_id, state="canceled", error_code="compaction_request_canceled"))
            raise
        except RevisionPersistenceUnknown:
            raise
        except RevisionConflict as error:
            await self._seal(memory_id, operation_id, state="stale", error_code=error.code)
        except RevisionValidationError as error:
            await self._seal(memory_id, operation_id, state="failed", error_code=error.code, usage=usage)
        except Exception:
            await self._seal(memory_id, operation_id, state="failed", error_code="compaction_generation_failed")
        finally:
            await finish_owned_work(self.finish(memory_id, operation_id, token=token))
        return await self.get(memory_id, operation_id)

    async def _seal(
        self,
        memory_id: str,
        operation_id: str,
        *,
        state: str,
        error_code: str | None = None,
        package: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        bindings: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            artifact = await self._record(memory_id, operation_id, lock=True)
            assert artifact is not None
            if artifact.cancellation_requested or artifact.state in {"stale", "canceled"}:
                return
            if bindings is not None:
                await self._assert_roots(memory_id, bindings)
            artifact.state, artifact.error_code, artifact.package, artifact.updated_at = state, error_code, deepcopy(package), datetime.now(UTC)
            if usage is not None:
                artifact.usage = deepcopy(usage)
            row = await self._row(memory_id, operation_id, lock=True)
            row.publication_state = "ready" if state == "generated" else state
            outbox = await self._outbox(row)
            outbox.state, outbox.error_code = "ready" if state == "generated" else "failed", error_code
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code, operation.updated_at = state, "ready", error_code, datetime.now(UTC)

    async def get(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            artifact = await self._record(memory_id, operation_id)
            if artifact is None:
                raise RevisionNotFound("compaction_operation_not_found")
            bindings = await self._root_bindings(artifact)
            snapshot = {
                key: deepcopy(getattr(artifact, key))
                for key in ("artifact_id", "memory_id", "operation_id", "state", "input_manifest_sha256", "model_binding", "usage", "package", "error_code", "redacted")
            }
            snapshot["created_at"] = artifact.created_at.isoformat()
            admission = await self._row(memory_id, operation_id)
            busy = admission.active_token is not None or admission.state in {"active", "unknown"}
            await self._idle()
            valid = False
            if snapshot["state"] == "generated" and not snapshot["redacted"]:
                try:
                    current = await self.load_sources(memory_id, [item["source_id"] for item in bindings])
                    if [item["binding"] for item in current] != bindings:
                        raise RevisionConflict("compaction_source_changed")
                    await self._assert_roots(memory_id, bindings)
                    valid = True
                except (RevisionConflict, RevisionNotFound):
                    valid = False
                await self.authorize(self.db, memory_id)
                fresh = await self._record(memory_id, operation_id)
                if fresh is None:
                    raise RevisionNotFound("compaction_operation_not_found")
                for key in ("state", "redacted", "error_code"):
                    snapshot[key] = deepcopy(getattr(fresh, key))
                valid = valid and fresh.state == "generated" and not fresh.cancellation_requested and not fresh.redacted
                if not valid and snapshot["state"] == "generated":
                    snapshot["state"] = "stale"
            admission = await self._row(memory_id, operation_id)
            busy = admission.active_token is not None or admission.state in {"active", "unknown"}
            if snapshot["state"] == "running" and not busy:
                snapshot["state"] = "persistence_unknown"
            if not valid:
                snapshot["package"] = None
            snapshot.update(sources=bindings, valid_for_current=valid, validity_checked=True, snapshot_only=True)
            return {
                "operation": {
                    "operation_id": operation_id,
                    "memory_id": memory_id,
                    "kind": "context_compact",
                    "status": snapshot["state"],
                    "artifact_id": snapshot["artifact_id"],
                    "error_code": snapshot["error_code"],
                    "writer_busy": busy,
                    "needs_operator_reconciliation": snapshot["state"] == "persistence_unknown" or (busy and snapshot["state"] in {"canceled", "stale"}),
                    "can_retry": False,
                },
                "artifact": snapshot,
            }
        finally:
            await self._idle()

    async def by_id(self, memory_id: str, artifact_id: str) -> dict[str, Any]:
        await self._idle()
        await self.authorize(self.db, memory_id)
        operation_id = await self.db.scalar(
            select(MemoryCompactionArtifact.operation_id).where(
                MemoryCompactionArtifact.memory_id == memory_id, MemoryCompactionArtifact.tenant_id == self.tenant_id, MemoryCompactionArtifact.artifact_id == artifact_id
            )
        )
        await self._idle()
        if operation_id is None:
            raise RevisionNotFound("compaction_artifact_not_found")
        return await self.get(memory_id, operation_id)

    async def cancel(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            artifact = await self._record(memory_id, operation_id, lock=True)
            if artifact is None or artifact.actor_id != self.actor_id:
                raise RevisionNotFound("compaction_operation_not_found")
            artifact.cancellation_requested, artifact.package, artifact.state, artifact.error_code = True, None, "canceled", "compaction_canceled"
            row = await self._row(memory_id, operation_id, lock=True)
            row.publication_state = "canceled"
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.error_code = "canceled", "compaction_canceled"
            outbox = await self._outbox(row)
            outbox.state, outbox.error_code = "canceled", "compaction_canceled"
        return await self.get(memory_id, operation_id)

    async def list_artifacts(self, memory_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise RevisionValidationError("invalid_compaction_page")
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            query = select(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == memory_id, MemoryCompactionArtifact.tenant_id == self.tenant_id)
            if cursor is not None:
                try:
                    value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                    if value["memory_id"] != memory_id or value["actor_id"] != self.actor_id or value["limit"] != limit:
                        raise ValueError
                    stamp, identity = datetime.fromisoformat(value["created_at"]), value["artifact_id"]
                except (ValueError, TypeError, KeyError) as error:
                    raise RevisionValidationError("invalid_compaction_cursor") from error
                query = query.where(or_(MemoryCompactionArtifact.created_at < stamp, and_(MemoryCompactionArtifact.created_at == stamp, MemoryCompactionArtifact.artifact_id < identity)))
            rows = list(await self.db.scalars(query.order_by(MemoryCompactionArtifact.created_at.desc(), MemoryCompactionArtifact.artifact_id.desc()).limit(limit + 1)))
            more, rows = len(rows) > limit, rows[:limit]
            next_cursor = None
            if more:
                tail = rows[-1]
                next_cursor = base64.urlsafe_b64encode(
                    canonical({"memory_id": memory_id, "actor_id": self.actor_id, "limit": limit, "created_at": tail.created_at.isoformat(), "artifact_id": tail.artifact_id}).encode()
                ).decode()
            return {
                "items": [
                    {
                        "artifact_id": row.artifact_id,
                        "memory_id": memory_id,
                        "operation_id": row.operation_id,
                        "state": row.state,
                        "error_code": row.error_code,
                        "created_at": row.created_at.isoformat(),
                        "valid_for_current": None,
                        "validity_checked": False,
                        "package": None,
                    }
                    for row in rows
                ],
                "limit": limit,
                "has_more": more,
                "next_cursor": next_cursor,
            }
        finally:
            await self._idle()
