"""Explicit, bounded discovery of registered source versions for existing jobs."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance.checkpoints import AnalysisIdentity
from memory.maintenance.domain import canonical_request
from memory.maintenance.execution import ExecutionService, execution_view
from memory.maintenance.jobs import JobService
from memory.maintenance.selection import MAX_BATCH_SOURCES, MAX_DISCOVERY_SOURCES, MAX_SUBMIT_BATCHES, canonical_scope, partition_sources, public_source, source_identity, source_key
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError, message_scope, request_hash, validate_operation_id
from memory.writes.admission import WriterCoordinator

_BUDGET_ERRORS = {"dreaming_no_input_fits_budget", "dreaming_input_metadata_budget_exceeded", "maintenance_input_budget_exceeded", "incremental_context_deferred"}
logger = logging.getLogger(__name__)


def selection_request(
    *,
    scope: dict[str, Any],
    policy: str = "general",
    entry_ids: list[str] | None = None,
    batch_size: int = 32,
    max_sources: int = 128,
    input_budget_tokens: int = 16384,
    output_budget_tokens: int = 4096,
) -> dict[str, Any]:
    common = canonical_request([1], entry_ids or [], policy, input_budget_tokens, output_budget_tokens)
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH_SOURCES or type(max_sources) is not int or not 1 <= max_sources <= MAX_DISCOVERY_SOURCES:
        raise RevisionValidationError("invalid_incremental_limits")
    return {
        "scope": canonical_scope(scope),
        **{name: common[name] for name in ("policy", "entry_ids", "input_budget_tokens", "output_budget_tokens")},
        "batch_size": batch_size,
        "max_sources": max_sources,
    }


def _source(row: MemorySourceState) -> dict[str, Any]:
    return {"source_id": row.source_message_id, "source_revision": row.source_revision, "source_generation": row.generation, "content_sha256": row.content_sha256, "scope": deepcopy(row.scope)}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


class DiscoveryService(WriterCoordinator):
    def __init__(self, db: AsyncSession, *, jobs: JobService, identity: AnalysisIdentity, validate_entry: Callable[[MemoryEntry, MemoryEntryRevision], Awaitable[None]] | None = None) -> None:
        if jobs.db is not db:
            raise RuntimeError("Discovery and job services must share their session")
        super().__init__(db, tenant_id=jobs.tenant_id, actor_id=jobs.actor_id, authorize=jobs.authorize)
        self.jobs, self.identity, self.validate_entry = jobs, identity, validate_entry

    async def _read_bank(self, memory_id: str) -> None:
        await self.authorize(self.db, memory_id)
        state = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
        if state != "active":
            raise RevisionConflict("incremental_requires_active_managed_memory")

    async def _profile(self, memory_id: str, request: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        identity = await self.identity(memory_id)
        contexts: dict[int, dict[str, Any]] = {}
        entries = []
        for entry_id in request["entry_ids"]:
            entry = await self.db.scalar(
                select(MemoryEntry).where(MemoryEntry.memory_id == memory_id, MemoryEntry.tenant_id == self.tenant_id, MemoryEntry.entry_id == entry_id).execution_options(populate_existing=True)
            )
            if entry is None:
                raise RevisionNotFound("entry_not_found")
            revision = await self.db.get(MemoryEntryRevision, (entry.entry_id, entry.current_revision), populate_existing=True)
            if revision is None or self.validate_entry is None:
                raise RevisionConflict("incremental_context_not_ready")
            await self.validate_entry(entry, revision)
            refs = list(await self.db.scalars(select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == entry_id, MemoryRevisionSource.revision == entry.current_revision)))
            if not refs:
                raise RevisionConflict("incremental_context_not_ready")
            for ref in refs:
                source = await self.db.get(MemorySourceState, (memory_id, ref.source_message_id), populate_existing=True)
                if source is None or source.tenant_id != self.tenant_id or source.state != "active" or (source.source_revision, source.content_sha256) != (ref.source_revision, ref.content_sha256):
                    raise RevisionConflict("incremental_context_source_changed")
                if any(expected is not None and source.scope.get(name) != expected for name, expected in request["scope"].items()):
                    raise RevisionConflict("incremental_context_scope_mismatch")
                contexts[source.source_message_id] = _source(source)
            entries.append(
                {
                    "entry_id": entry_id,
                    "revision": entry.current_revision,
                    "message_id": entry.current_message_id,
                    "generation": entry.generation,
                    "content_sha256": revision.content_sha256,
                    "scope": message_scope(revision.message_snapshot, memory_id),
                }
            )
        profile = {
            "scope": deepcopy(request["scope"]),
            "policy": request["policy"],
            "input_budget_tokens": request["input_budget_tokens"],
            "output_budget_tokens": request["output_budget_tokens"],
            **identity,
            "entries": [{name: str(entry[name]) for name in ("entry_id", "revision", "message_id", "generation", "content_sha256")} for entry in entries],
            "context_sources": [public_source(contexts[key]) for key in sorted(contexts)],
        }
        return profile, list(contexts.values()), entries

    def _inventory(self, memory_id: str, profile_sha256: str, scope: dict[str, Any]) -> Any:
        source, coverage, job, execution, admission, batch = (
            MemorySourceState,
            MemoryMaintenanceCoverage,
            MemoryMaintenanceJob,
            MemoryMaintenanceExecution,
            MemoryWriterAdmission,
            MemoryMaintenanceBatch,
        )
        completed = and_(coverage.state == "analyzed", job.state == "ready", execution.state == "succeeded", admission.operation_id.is_not(None), admission.active_token.is_(None))
        live = or_(admission.active_token.is_not(None), execution.state.in_(["queued", "claimed", "running", "needs_reconciliation"]))
        terminal = or_(job.state.in_(["failed", "canceled", "stale", "redacted"]), batch.state.in_(["failed", "canceled", "stale"]))
        status = case(
            (completed, "covered"),
            (or_(coverage.state == "blocked", batch.state == "blocked"), "blocked"),
            (live, "inflight"),
            (terminal, "eligible"),
            (coverage.coverage_key.is_not(None), "inflight"),
            else_="eligible",
        )
        query = (
            select(
                source.source_message_id.label("source_id"), source.source_revision, source.generation.label("source_generation"), source.content_sha256, source.scope, status.label("coverage_state")
            )
            .outerjoin(
                coverage,
                and_(
                    coverage.memory_id == source.memory_id,
                    coverage.tenant_id == source.tenant_id,
                    coverage.profile_sha256 == profile_sha256,
                    coverage.source_id == source.source_message_id,
                    coverage.source_revision == source.source_revision,
                    coverage.source_generation == source.generation,
                    coverage.content_sha256 == source.content_sha256,
                ),
            )
            .outerjoin(batch, and_(batch.batch_id == coverage.batch_id, batch.memory_id == source.memory_id, batch.tenant_id == self.tenant_id))
            .outerjoin(job, and_(job.job_id == coverage.job_id, job.memory_id == source.memory_id, job.tenant_id == self.tenant_id))
            .outerjoin(execution, and_(execution.job_id == job.job_id, execution.memory_id == source.memory_id, execution.tenant_id == self.tenant_id))
            .outerjoin(admission, and_(admission.memory_id == source.memory_id, admission.tenant_id == self.tenant_id, admission.operation_id == job.analysis_operation_id))
            .where(source.memory_id == memory_id, source.tenant_id == self.tenant_id, source.state == "active")
        )
        for name, expected in scope.items():
            if expected is not None:
                query = query.where(source.scope[name].astext == expected)
        return query.subquery()

    async def _preview(self, memory_id: str, request: dict[str, Any]) -> dict[str, Any]:
        await self._read_bank(memory_id)
        profile, contexts, entries = await self._profile(memory_id, request)
        profile_sha256 = request_hash(profile)
        inventory = self._inventory(memory_id, profile_sha256, request["scope"])
        counts = {
            name: int(await self.db.scalar(select(func.count()).select_from(inventory).where(inventory.c.coverage_state == name)) or 0) for name in ("covered", "inflight", "blocked", "eligible")
        }
        counts["active"] = sum(counts.values())
        # Coverage/state filtering occurs before LIMIT, so covered or oversized
        # low IDs cannot hide later eligible input versions.
        selected = [
            dict(row) for row in (await self.db.execute(select(inventory).where(inventory.c.coverage_state == "eligible").order_by(inventory.c.source_id).limit(request["max_sources"] + 1))).mappings()
        ]
        more = len(selected) > request["max_sources"]
        sources = [{key: value for key, value in row.items() if key != "coverage_state"} for row in selected[: request["max_sources"]]]
        batches = partition_sources(sources, contexts, entries, batch_size=request["batch_size"])
        counts.update(selected_primary=len(sources), context=len(contexts))
        frozen = {"request": request, "profile": profile, "sources": sources, "batches": batches, "counts": counts, "has_more": more}
        return {**frozen, "profile_sha256": profile_sha256, "preview_sha256": request_hash(frozen)}

    @staticmethod
    def _public_preview(value: dict[str, Any]) -> dict[str, Any]:
        return {name: deepcopy(value[name]) for name in ("profile", "profile_sha256", "preview_sha256", "counts", "has_more")} | {
            "registered_active_only": True,
            "token_fit": "not_checked",
            "batches": [
                {
                    "ordinal": batch["ordinal"],
                    "primary_sources": [public_source(row) for row in batch["primary_sources"]],
                    "context_sources": [public_source(row) for row in batch["context_sources"]],
                    "entry_ids": list(batch["entry_ids"]),
                }
                for batch in value["batches"]
            ],
        }

    async def preview(self, memory_id: str, **selection: Any) -> dict[str, Any]:
        await self._idle()
        try:
            return self._public_preview(await self._preview(memory_id, selection_request(**selection)))
        finally:
            await self.db.rollback()

    async def _discovery(self, memory_id: str, discovery_id: str, *, lock: bool = False) -> MemoryMaintenanceDiscovery:
        query = (
            select(MemoryMaintenanceDiscovery)
            .where(MemoryMaintenanceDiscovery.discovery_id == discovery_id, MemoryMaintenanceDiscovery.memory_id == memory_id, MemoryMaintenanceDiscovery.tenant_id == self.tenant_id)
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise RevisionNotFound("maintenance_discovery_not_found")
        return row

    async def _batches(self, discovery: MemoryMaintenanceDiscovery) -> list[MemoryMaintenanceBatch]:
        return list(
            await self.db.scalars(
                select(MemoryMaintenanceBatch).where(MemoryMaintenanceBatch.discovery_id == discovery.discovery_id).order_by(MemoryMaintenanceBatch.ordinal).execution_options(populate_existing=True)
            )
        )

    async def _discovery_view(self, discovery: MemoryMaintenanceDiscovery) -> dict[str, Any]:
        batches = []
        progress = dict.fromkeys(("planned", "created", "submitted", "running", "analyzed", "failed", "canceled", "blocked", "pending"), 0)
        planned = await self._batches(discovery)
        job_ids = [row.job_id for row in planned]
        jobs = (
            {
                row.job_id: row
                for row in await self.db.scalars(
                    select(MemoryMaintenanceJob)
                    .where(MemoryMaintenanceJob.job_id.in_(job_ids), MemoryMaintenanceJob.memory_id == discovery.memory_id, MemoryMaintenanceJob.tenant_id == self.tenant_id)
                    .execution_options(populate_existing=True)
                )
            }
            if job_ids
            else {}
        )
        executions = (
            {
                row.job_id: row
                for row in await self.db.scalars(
                    select(MemoryMaintenanceExecution)
                    .where(MemoryMaintenanceExecution.job_id.in_(job_ids), MemoryMaintenanceExecution.memory_id == discovery.memory_id, MemoryMaintenanceExecution.tenant_id == self.tenant_id)
                    .execution_options(populate_existing=True)
                )
            }
            if job_ids
            else {}
        )
        operation_ids = [row.analysis_operation_id for row in jobs.values() if row.analysis_operation_id is not None]
        admissions = (
            {
                row.operation_id: row.active_token
                for row in await self.db.execute(
                    select(MemoryWriterAdmission.operation_id, MemoryWriterAdmission.active_token).where(
                        MemoryWriterAdmission.memory_id == discovery.memory_id, MemoryWriterAdmission.tenant_id == self.tenant_id, MemoryWriterAdmission.operation_id.in_(operation_ids)
                    )
                )
            }
            if operation_ids
            else {}
        )
        for row in planned:
            job = jobs.get(row.job_id)
            execution = executions.get(row.job_id)
            actual_active = execution is not None and (execution.operation_id not in admissions or admissions[execution.operation_id] is not None)
            execution_data = execution_view(execution, actual_attempt_active=actual_active) if execution is not None else None
            state = row.state
            if job is not None:
                if actual_active or (execution_data and execution_data["state"] in {"claimed", "running", "needs_reconciliation"}):
                    state = "running"
                elif state == "blocked":
                    pass
                elif job.state == "ready" and execution_data and execution_data["state"] == "succeeded":
                    state = "analyzed"
                elif job.state in {"failed", "canceled", "stale", "redacted"}:
                    state = "stale" if job.state == "redacted" else job.state
                elif execution_data and execution_data["state"] == "queued":
                    state = "submitted"
                elif state not in {"blocked", "stale", "canceled", "unknown"}:
                    state = "created"
            progress[state if state in progress else "pending"] += 1
            batches.append(
                {
                    "batch_id": row.batch_id,
                    "ordinal": row.ordinal,
                    "state": state,
                    "job_id": row.job_id,
                    "create_operation_id": row.create_operation_id,
                    "analysis_operation_id": row.analysis_operation_id,
                    "primary_sources": [public_source(item) for item in row.primary_sources],
                    "context_sources": [public_source(item) for item in row.context_sources],
                    "entry_ids": deepcopy(row.entry_ids),
                    "error_code": row.error_code or (job.error_code if job else None),
                    "job_state": job.state if job else None,
                    "execution": execution_data,
                }
            )
        if discovery.state in {"canceled", "stale"}:
            state = discovery.state
        elif not batches or progress["analyzed"] == len(batches):
            state = "settled"
        elif progress["running"]:
            state = "running"
        elif progress["blocked"] or progress["failed"] or progress["pending"] or any(item["state"] == "stale" for item in batches):
            state = "partial"
        elif progress["submitted"]:
            state = "submitted"
        else:
            state = "planned"
        return {
            "discovery_id": discovery.discovery_id,
            "operation_id": discovery.operation_id,
            "state": state,
            "profile_sha256": discovery.profile_sha256,
            "preview_sha256": discovery.preview_sha256,
            "profile": deepcopy(discovery.profile),
            "counts": deepcopy(discovery.counts),
            "has_more": discovery.has_more,
            "registered_active_only": True,
            "token_fit": "not_checked",
            "progress": progress,
            "batches": batches,
            "created_at": _iso(discovery.created_at),
            "updated_at": _iso(discovery.updated_at),
        }

    async def get(self, memory_id: str, discovery_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            await self._read_bank(memory_id)
            return await self._discovery_view(await self._discovery(memory_id, discovery_id))
        finally:
            await self.db.rollback()

    async def list_discoveries(self, memory_id: str, *, limit: int = 20, operation_id: str | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RevisionValidationError("invalid_incremental_list_limit")
        await self._idle()
        try:
            await self._read_bank(memory_id)
            query = select(MemoryMaintenanceDiscovery).where(MemoryMaintenanceDiscovery.memory_id == memory_id, MemoryMaintenanceDiscovery.tenant_id == self.tenant_id)
            if operation_id is not None:
                validate_operation_id(operation_id)
                query = query.where(MemoryMaintenanceDiscovery.operation_id == operation_id)
            rows = list(await self.db.scalars(query.order_by(MemoryMaintenanceDiscovery.created_at.desc(), MemoryMaintenanceDiscovery.discovery_id.desc()).limit(limit)))
            return {"discoveries": [await self._discovery_view(row) for row in rows]}
        finally:
            await self.db.rollback()

    async def create(self, memory_id: str, *, operation_id: str, expected_preview_sha256: str, **selection: Any) -> dict[str, Any]:
        validate_operation_id(operation_id)
        request = selection_request(**selection)
        digest = request_hash({"selection": request, "expected_preview_sha256": expected_preview_sha256})
        async with self._write(operation_id):
            await self.jobs._memory_fence(memory_id)
            previous = await self.db.scalar(
                select(MemoryMaintenanceDiscovery).where(MemoryMaintenanceDiscovery.memory_id == memory_id, MemoryMaintenanceDiscovery.operation_id == operation_id).with_for_update()
            )
            if previous is not None:
                if previous.request_sha256 != digest or previous.actor_id != self.actor_id or previous.tenant_id != self.tenant_id:
                    raise RevisionConflict("operation_request_mismatch")
                return await self._discovery_view(previous)
            preview = await self._preview(memory_id, request)
            if preview["preview_sha256"] != expected_preview_sha256:
                raise RevisionConflict("incremental_preview_changed")
            identities = sorted({item["source_id"] for item in preview["sources"]} | {int(item["source_id"]) for item in preview["profile"]["context_sources"]})
            if identities:
                list(
                    await self.db.scalars(
                        select(MemorySourceState)
                        .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id.in_(identities))
                        .order_by(MemorySourceState.source_message_id)
                        .with_for_update()
                    )
                )
            # Recompute after locks: SQL source versions cannot drift between
            # the reviewed selection and persisted child reservations.
            confirmed = await self._preview(memory_id, request)
            if confirmed["preview_sha256"] != expected_preview_sha256:
                raise RevisionConflict("incremental_preview_changed")
            discovery = MemoryMaintenanceDiscovery(
                discovery_id=uuid4().hex,
                memory_id=memory_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                operation_id=operation_id,
                request_sha256=digest,
                preview_sha256=expected_preview_sha256,
                profile_sha256=preview["profile_sha256"],
                request=request,
                profile=preview["profile"],
                counts=preview["counts"],
                has_more=preview["has_more"],
                state="planned",
                receipt={},
            )
            self.db.add(discovery)
            await self.db.flush()
            for planned in preview["batches"]:
                ordinal = planned["ordinal"]
                batch = MemoryMaintenanceBatch(
                    batch_id=uuid4().hex,
                    discovery_id=discovery.discovery_id,
                    memory_id=memory_id,
                    tenant_id=self.tenant_id,
                    ordinal=ordinal,
                    job_id=uuid4().hex,
                    create_operation_id=f"discover-{discovery.discovery_id}-{ordinal}-create",
                    analysis_operation_id=f"discover-{discovery.discovery_id}-{ordinal}-analyze",
                    primary_sources=planned["primary_sources"],
                    context_sources=planned["context_sources"],
                    entry_ids=planned["entry_ids"],
                    state="planned",
                )
                self.db.add(batch)
                await self.db.flush()
                for source in batch.primary_sources:
                    key = source_key(memory_id, discovery.profile_sha256, source)
                    coverage = await self.db.get(MemoryMaintenanceCoverage, key, with_for_update=True, populate_existing=True)
                    if coverage is None:
                        coverage = MemoryMaintenanceCoverage(
                            coverage_key=key,
                            memory_id=memory_id,
                            tenant_id=self.tenant_id,
                            profile_sha256=discovery.profile_sha256,
                            **source_identity(source),
                            batch_id=batch.batch_id,
                            job_id=batch.job_id,
                            state="reserved",
                        )
                        self.db.add(coverage)
                    else:
                        coverage.batch_id, coverage.job_id, coverage.state, coverage.error_code, coverage.updated_at = batch.batch_id, batch.job_id, "reserved", None, datetime.now(UTC)
            await self.db.flush()
            return await self._discovery_view(discovery)

    async def _mark(self, memory_id: str, discovery_id: str, batch_id: str, operation_id: str, state: str, error_code: str | None = None) -> None:
        async with self._write(operation_id):
            await self.jobs._memory_fence(memory_id)
            discovery = await self._discovery(memory_id, discovery_id, lock=True)
            batch = await self.db.get(MemoryMaintenanceBatch, batch_id, with_for_update=True, populate_existing=True)
            if batch is None or batch.discovery_id != discovery_id or discovery.actor_id != self.actor_id:
                raise RevisionConflict("incremental_batch_binding_changed")
            if batch.state not in {"analyzed", "blocked", "stale", "canceled"}:
                batch.state, batch.error_code, batch.updated_at = state, error_code, datetime.now(UTC)
            if state == "blocked":
                rows = list(await self.db.scalars(select(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.batch_id == batch_id).with_for_update()))
                for row in rows:
                    row.state, row.error_code, row.updated_at = "blocked", error_code, datetime.now(UTC)
            if state == "submitted":
                commands = deepcopy(discovery.receipt.get("commands", {}))
                command = commands.get(operation_id)
                if command is None or command.get("action") != "run" or batch_id not in command["batch_ids"]:
                    raise RevisionConflict("incremental_command_binding_changed")
                command["submitted_batch_ids"] = sorted(set(command.get("submitted_batch_ids", [])) | {batch_id})
                discovery.receipt = {**discovery.receipt, "commands": commands}
            discovery.updated_at = datetime.now(UTC)

    async def _reserve_command(self, memory_id: str, discovery_id: str, operation_id: str, *, action: str, limit: int | None = None) -> dict[str, Any]:
        digest = request_hash({"action": action, "limit": limit})
        async with self._write(operation_id):
            await self.jobs._memory_fence(memory_id)
            discovery = await self._discovery(memory_id, discovery_id, lock=True)
            if discovery.actor_id != self.actor_id:
                raise RevisionConflict("incremental_actor_mismatch")
            if operation_id == discovery.operation_id:
                raise RevisionConflict("operation_request_mismatch")
            commands = deepcopy(discovery.receipt.get("commands", {}))
            previous = commands.get(operation_id)
            if previous is not None:
                if previous.get("request_sha256") != digest or previous.get("actor_id") != self.actor_id:
                    raise RevisionConflict("operation_request_mismatch")
                command = previous
            else:
                if action == "run":
                    if discovery.state in {"canceled", "stale"}:
                        raise RevisionConflict("incremental_discovery_not_runnable")
                    profile, _, _ = await self._profile(memory_id, discovery.request)
                    if request_hash(profile) != discovery.profile_sha256:
                        raise RevisionConflict("incremental_analysis_profile_changed")
                if len(commands) >= (1024 if action == "run" else 1025):
                    raise RevisionValidationError("incremental_command_limit")
                view = await self._discovery_view(discovery)
                selected = [item for item in view["batches"] if action == "cancel" or item["state"] in {"planned", "created", "unknown"}]
                if action == "run":
                    selected = selected[:limit]
                command = {
                    "request_sha256": digest,
                    "actor_id": self.actor_id,
                    "action": action,
                    "limit": limit,
                    "batch_ids": [item["batch_id"] for item in selected],
                    "submitted_batch_ids": [],
                }
                commands[operation_id] = command
                discovery.receipt = {**discovery.receipt, "commands": commands}
            if action == "cancel":
                discovery.state, discovery.updated_at = "canceled", datetime.now(UTC)
            plans = []
            for batch in await self._batches(discovery):
                if batch.batch_id not in command["batch_ids"]:
                    continue
                if action == "cancel" and await self.db.get(MemoryMaintenanceJob, batch.job_id) is None:
                    batch.state = "canceled"
                plans.append(
                    {
                        "batch_id": batch.batch_id,
                        "job_id": batch.job_id,
                        "create_operation_id": batch.create_operation_id,
                        "analysis_operation_id": batch.analysis_operation_id,
                        "source_ids": sorted({source["source_id"] for source in [*batch.primary_sources, *batch.context_sources]}),
                        "entry_ids": list(batch.entry_ids),
                    }
                )
            return {"plans": plans, "request": deepcopy(discovery.request), "command": deepcopy(command), "runnable": discovery.state not in {"canceled", "stale"}}

    async def run(self, memory_id: str, discovery_id: str, *, operation_id: str, limit: int = 1) -> dict[str, Any]:
        validate_operation_id(operation_id)
        if type(limit) is not int or not 1 <= limit <= MAX_SUBMIT_BATCHES:
            raise RevisionValidationError("invalid_incremental_run_limit")
        reserved = await self._reserve_command(memory_id, discovery_id, operation_id, action="run", limit=limit)
        if not reserved["runnable"]:
            return await self.get(memory_id, discovery_id)
        request, command = reserved["request"], reserved["command"]
        for plan in reserved["plans"]:
            batch_id = plan["batch_id"]
            if batch_id in command["submitted_batch_ids"]:
                continue
            try:
                job = await self.jobs.create(
                    memory_id,
                    operation_id=plan["create_operation_id"],
                    job_id=plan["job_id"],
                    source_ids=plan["source_ids"],
                    entry_ids=plan["entry_ids"],
                    **{name: request[name] for name in ("policy", "input_budget_tokens", "output_budget_tokens")},
                )
                if job["job_id"] != plan["job_id"]:
                    raise RevisionConflict("incremental_job_binding_changed")
                await self._mark(memory_id, discovery_id, batch_id, operation_id, "created")
                execution = job["execution"]
                submitted = await ExecutionService(self.db, jobs=self.jobs).submit(memory_id, plan["job_id"], operation_id=plan["analysis_operation_id"]) if execution is None else job
                if submitted["analysis_operation_id"] != plan["analysis_operation_id"] or submitted["execution"] is None:
                    raise RevisionConflict("incremental_execution_unconfirmed")
                await self._mark(memory_id, discovery_id, batch_id, operation_id, "submitted")
            except RevisionPersistenceUnknown as error:
                try:
                    await self._mark(memory_id, discovery_id, batch_id, operation_id, "unknown", "incremental_child_commit_unknown")
                except Exception as mark_error:
                    # The child outcome was already unknown. A subsequent ACL,
                    # deletion or SQL failure must not replace its public handle.
                    logger.warning("Unable to record unknown incremental child outcome (%s)", type(mark_error).__name__)
                raise RevisionPersistenceUnknown(operation_id) from error
            except RevisionValidationError as error:
                if error.code not in _BUDGET_ERRORS:
                    raise
                await self._mark(memory_id, discovery_id, batch_id, operation_id, "blocked", error.code)
            except RevisionConflict as error:
                await self._mark(memory_id, discovery_id, batch_id, operation_id, "stale", error.code)
        return await self.get(memory_id, discovery_id)

    async def cancel(self, memory_id: str, discovery_id: str, *, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        reserved = await self._reserve_command(memory_id, discovery_id, operation_id, action="cancel")
        for plan in reserved["plans"]:
            await self._idle()
            exists = await self.db.scalar(select(MemoryMaintenanceJob.job_id).where(MemoryMaintenanceJob.job_id == plan["job_id"]))
            await self.db.rollback()
            if exists is not None:
                await self.jobs.cancel(memory_id, plan["job_id"], operation_id=f"cancel-{plan['batch_id']}")
        return await self.get(memory_id, discovery_id)
