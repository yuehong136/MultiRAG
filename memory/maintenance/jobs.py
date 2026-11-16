"""Short SQL decisions around explicitly invoked, review-only model analysis."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.maintenance.domain import ASSESSMENT, assemble_candidates, canonical_request, identifier, validate_manifest
from memory.maintenance.target_conditions import TargetConditions, canonical_target_preconditions, public_conditions
from memory.maintenance.types import AnalysisResult, Analyzer, Authorize, InputValidator, Snapshotter
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionValidationError, positive_int, request_hash, validate_operation_id
from memory.writes.admission import WriterCoordinator


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def manifest_for_job(job: MemoryMaintenanceJob, inputs: list[MemoryMaintenanceInput]) -> dict[str, Any]:
    return {
        "inputs": [
            {
                **({"fragment": deepcopy(item.fragment)} if item.fragment is not None else {}),
                **{
                    name: deepcopy(getattr(item, name))
                    for name in ("input_id", "source_id", "source_revision", "source_generation", "content_sha256", "scope", "valid_at", "snapshot", "coverage", "defer_reason")
                },
            }
            for item in inputs
        ],
        "entries": deepcopy(job.entry_snapshots),
        "estimated_input_tokens": job.estimated_input_tokens,
    }


def candidate_view(candidate: MemoryMaintenanceCandidate, *, available: bool = True) -> dict[str, Any]:
    redacted = not available or candidate.state in {"stale", "redacted"} or (candidate.action != "noop" and candidate.content is None)
    value = {
        name: deepcopy(getattr(candidate, name))
        for name in (
            "candidate_id",
            "revision",
            "state",
            "action",
            "target_entry_id",
            "expected_revision",
            "message_type",
            "content",
            "base_content",
            "patch",
            "diff",
            "reason",
            "sources",
            "review_actor_id",
            "publication_operation_id",
        )
    }
    value.update(assessment=dict(ASSESSMENT), reviewed_at=_iso(candidate.reviewed_at) if candidate.reviewed_at else None, redacted=redacted)
    value["diff"] = {"old_text": candidate.base_content or "", "new_text": candidate.content or ""} if candidate.action != "noop" and not redacted else None
    if redacted:
        value.update(dict.fromkeys(("content", "base_content", "patch", "diff", "reason")))
    return value


def job_view(job: MemoryMaintenanceJob, inputs: list[MemoryMaintenanceInput]) -> dict[str, Any]:
    value = {
        name: deepcopy(getattr(job, name))
        for name in (
            "job_id",
            "operation_id",
            "state",
            "generation",
            "policy",
            "input_budget_tokens",
            "output_budget_tokens",
            "estimated_input_tokens",
            "analysis_operation_id",
            "error_code",
            "model_reference",
            "usage",
        )
    }
    value.update(
        coverage={
            "selected": len(inputs),
            "included": sum(item.coverage in {"included", "analyzed"} for item in inputs),
            "analyzed": sum(item.coverage == "analyzed" for item in inputs),
            "deferred": sum(item.coverage == "deferred" for item in inputs),
        },
        inputs=[
            {
                **{name: deepcopy(getattr(item, name)) for name in ("input_id", "source_id", "source_revision", "source_generation", "content_sha256", "scope", "coverage", "defer_reason")},
                **({"fragment": deepcopy(item.fragment)} if item.fragment is not None else {}),
            }
            for item in inputs
        ],
        created_at=_iso(job.created_at),
        updated_at=_iso(job.updated_at),
    )
    if job.receipt.get("target_preconditions") is not None:
        value["target_preconditions"] = public_conditions(job.receipt["target_preconditions"])
    return value


class JobService(WriterCoordinator):
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        actor_id: str,
        authorize: Authorize,
        snapshotter: Snapshotter,
        validate_inputs: InputValidator,
        analyzer: Analyzer,
        count_tokens: Callable[[str], int],
        validate_binding: Callable[[MemoryMaintenanceJob, dict[str, Any]], Awaitable[None]] | None = None,
        checkpoint: Callable[[MemoryMaintenanceJob, list[MemoryMaintenanceInput], AnalysisResult], Awaitable[None]] | None = None,
        segment_snapshotter: Callable[[str, list[int], list[str], int, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        target_conditions: TargetConditions | None = None,
    ) -> None:
        super().__init__(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)
        self.snapshotter, self.validate_inputs, self.analyzer, self.count_tokens = snapshotter, validate_inputs, analyzer, count_tokens
        self.validate_binding, self.checkpoint = validate_binding, checkpoint
        self.segment_snapshotter = segment_snapshotter
        self.target_conditions = target_conditions

    async def _memory_fence(self, memory_id: str) -> tuple[Any, MemoryWriterFence]:
        memory, fence = await super()._memory_fence(memory_id)
        bank = await self.db.scalar(select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id).with_for_update())
        if bank is None or bank.state != "active":
            raise RevisionConflict("maintenance_requires_active_managed_memory")
        return memory, fence

    async def _job(self, memory_id: str, job_id: str, *, lock: bool = False) -> MemoryMaintenanceJob:
        statement = (
            select(MemoryMaintenanceJob)
            .where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.job_id == job_id, MemoryMaintenanceJob.tenant_id == self.tenant_id)
            .execution_options(populate_existing=True)
        )
        job = await self.db.scalar(statement.with_for_update() if lock else statement)
        if job is None:
            raise RevisionNotFound("maintenance_job_not_found")
        return job

    async def _inputs(self, job: MemoryMaintenanceJob) -> list[MemoryMaintenanceInput]:
        return list(
            (
                await self.db.scalars(
                    select(MemoryMaintenanceInput)
                    .where(MemoryMaintenanceInput.job_id == job.job_id, MemoryMaintenanceInput.memory_id == job.memory_id, MemoryMaintenanceInput.tenant_id == self.tenant_id)
                    .order_by(MemoryMaintenanceInput.source_id)
                )
            ).all()
        )

    async def _manifest(self, job: MemoryMaintenanceJob) -> dict[str, Any]:
        return manifest_for_job(job, await self._inputs(job))

    async def _job_view(self, job: MemoryMaintenanceJob, inputs: list[MemoryMaintenanceInput]) -> dict[str, Any]:
        from api.db.memory_execution_models import MemoryMaintenanceExecution
        from memory.maintenance.execution import execution_snapshot

        value = job_view(job, inputs)
        row = await self.db.scalar(
            select(MemoryMaintenanceExecution).where(
                MemoryMaintenanceExecution.job_id == job.job_id, MemoryMaintenanceExecution.memory_id == job.memory_id, MemoryMaintenanceExecution.tenant_id == self.tenant_id
            )
        )
        value["execution"] = await execution_snapshot(self.db, row) if row is not None else None
        return value

    async def _locked(self, memory_id: str, job_id: str, *, validate: bool = True) -> tuple[MemoryMaintenanceJob, list[MemoryMaintenanceInput]]:
        await self._memory_fence(memory_id)
        observed = await self._job(memory_id, job_id)
        inputs = await self._inputs(observed)
        if validate:
            await self.validate_inputs(self.db, memory_id, manifest_for_job(observed, inputs))
        return await self._job(memory_id, job_id, lock=True), inputs

    async def _read(self, memory_id: str, job_id: str) -> tuple[MemoryMaintenanceJob, list[MemoryMaintenanceInput], bool]:
        await self.authorize(self.db, memory_id)
        job = await self._job(memory_id, job_id)
        inputs = await self._inputs(job)
        active = job.state in {"pending", "running", "ready", "failed"}
        bank = await self.db.scalar(select(ManagedMemoryState.state).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id))
        fence = await self.db.scalar(select(MemoryWriterFence.state).where(MemoryWriterFence.memory_id == memory_id, MemoryWriterFence.tenant_id == self.tenant_id))
        if active and bank == "active" and fence == "open":
            try:
                manifest = manifest_for_job(job, inputs)
                # A published patch necessarily changes its original target.
                # Auditing its frozen diff still depends on live RAW evidence,
                # while target-current checks belong to new review/publication.
                manifest["entries"] = []
                await self.validate_inputs(self.db, memory_id, manifest)
            except (RevisionConflict, RevisionNotFound):
                active = False
        else:
            active = False
        return job, inputs, active

    async def create(
        self,
        memory_id: str,
        *,
        operation_id: str,
        source_ids: list[int],
        entry_ids: list[str],
        policy: str = "general",
        input_budget_tokens: int = 16384,
        output_budget_tokens: int = 4096,
        job_id: str | None = None,
        fragment: dict[str, Any] | None = None,
        target_preconditions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validate_operation_id(operation_id)
        request = canonical_request(source_ids, entry_ids, policy, input_budget_tokens, output_budget_tokens)
        expected_targets = canonical_target_preconditions(target_preconditions)
        if expected_targets is not None:
            request["target_preconditions"] = expected_targets
        if fragment is not None:
            request["fragment"] = deepcopy(fragment)
        digest = request_hash({"memory_id": memory_id, "tenant_id": self.tenant_id, "actor_id": self.actor_id, **request})
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            previous = await self.db.scalar(select(MemoryMaintenanceJob).where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.operation_id == operation_id))
            if previous is not None:
                if previous.tenant_id != self.tenant_id or previous.request_sha256 != digest:
                    raise RevisionConflict("operation_request_mismatch")
                return await self._job_view(previous, await self._inputs(previous))
        finally:
            await self.db.rollback()
        if expected_targets is not None:
            if self.target_conditions is None:
                raise RevisionValidationError("target_recommendations_unsupported")
            await self.target_conditions.validate_physical(memory_id, expected_targets)
        if fragment is not None:
            if self.segment_snapshotter is None:
                raise RevisionValidationError("maintenance_segmentation_unsupported")
            captured = await finish_owned_work(self.segment_snapshotter(memory_id, request["source_ids"], request["entry_ids"], input_budget_tokens, fragment))
        else:
            captured = await finish_owned_work(self.snapshotter(memory_id, request["source_ids"], request["entry_ids"], input_budget_tokens))
        manifest = validate_manifest(captured, request, self.count_tokens)
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            await self.validate_inputs(self.db, memory_id, manifest)
            if expected_targets is not None and self.target_conditions is not None:
                await self.target_conditions.validate_sql(memory_id, expected_targets, source_ids=request["source_ids"], entry_ids=request["entry_ids"], lock=True)
            previous = await self.db.scalar(select(MemoryMaintenanceJob).where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.operation_id == operation_id).with_for_update())
            if previous is not None:
                if previous.request_sha256 != digest or previous.tenant_id != self.tenant_id:
                    raise RevisionConflict("operation_request_mismatch")
                return await self._job_view(previous, await self._inputs(previous))
            job = MemoryMaintenanceJob(
                job_id=identifier(job_id) if job_id is not None else uuid4().hex,
                memory_id=memory_id,
                tenant_id=self.tenant_id,
                actor_id=self.actor_id,
                operation_id=operation_id,
                request_sha256=digest,
                state="pending",
                generation=1,
                source_ids=request["source_ids"],
                entry_ids=request["entry_ids"],
                entry_snapshots=manifest["entries"],
                policy=policy,
                input_budget_tokens=input_budget_tokens,
                output_budget_tokens=output_budget_tokens,
                estimated_input_tokens=manifest["estimated_input_tokens"],
                receipt={"target_preconditions": expected_targets} if expected_targets is not None else {},
            )
            if self.validate_binding is not None:
                await self.validate_binding(job, manifest)
            self.db.add(job)
            await self.db.flush()
            inputs = [MemoryMaintenanceInput(job_id=job.job_id, memory_id=memory_id, tenant_id=self.tenant_id, **item) for item in manifest["inputs"]]
            self.db.add_all(inputs)
            await self.db.flush()
            result = await self._job_view(job, inputs)
        return result

    async def get(self, memory_id: str, job_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            job, inputs, _ = await self._read(memory_id, identifier(job_id))
            return await self._job_view(job, inputs)
        finally:
            await self.db.rollback()

    async def list_jobs(self, memory_id: str, *, limit: int = 20) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RevisionValidationError("invalid_maintenance_page_size")
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            jobs = list(
                (
                    await self.db.scalars(
                        select(MemoryMaintenanceJob)
                        .where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == self.tenant_id)
                        .order_by(MemoryMaintenanceJob.created_at.desc(), MemoryMaintenanceJob.job_id.desc())
                        .limit(limit + 1)
                    )
                ).all()
            )
            return {"jobs": [await self._job_view(job, await self._inputs(job)) for job in jobs[:limit]], "limit": limit, "has_more": len(jobs) > limit}
        finally:
            await self.db.rollback()

    async def candidates(self, memory_id: str, job_id: str) -> dict[str, Any]:
        await self._idle()
        try:
            job, _, available = await self._read(memory_id, identifier(job_id))
            rows = list(
                (
                    await self.db.scalars(
                        select(MemoryMaintenanceCandidate)
                        .where(MemoryMaintenanceCandidate.job_id == job.job_id, MemoryMaintenanceCandidate.memory_id == memory_id, MemoryMaintenanceCandidate.tenant_id == self.tenant_id)
                        .order_by(MemoryMaintenanceCandidate.created_at, MemoryMaintenanceCandidate.candidate_id)
                        .limit(33)
                    )
                ).all()
            )
            if len(rows) > 32:
                raise RevisionConflict("maintenance_candidate_window_exceeded")
            return {"candidates": [candidate_view(row, available=available) for row in rows]}
        finally:
            await self.db.rollback()

    async def _configure_admission(self, row: MemoryWriterAdmission, fence: MemoryWriterFence, request: dict[str, Any]) -> None:
        job, _ = await self._locked(row.memory_id, request["job_id"])
        if job.generation != request["job_generation"] or job.state not in {"pending", "failed", "running"}:
            raise RevisionConflict("maintenance_job_changed")
        if job.analysis_operation_id is not None and job.analysis_operation_id != row.operation_id:
            raise RevisionConflict("maintenance_analysis_operation_mismatch")
        if row.operation_id in job.receipt.get("commands", {}) or row.operation_id == job.operation_id:
            raise RevisionConflict("operation_request_mismatch")
        job.analysis_operation_id = row.operation_id
        row.request_metadata = {"job_id": job.job_id, "source_ids": list(job.source_ids)}
        await self.db.flush()
        outbox = await self._outbox(row)
        if outbox.payload.get("server_metadata", {}).get("execution_mode") == "background":
            from memory.maintenance.execution import register_execution

            await register_execution(self.db, job, row)

    async def _start(self, memory_id: str, job_id: str, operation_id: str, token: str, generation: int, execution_claim_token: str | None = None) -> tuple[dict[str, Any], str, int]:
        from memory.maintenance.execution import require_execution_permit

        async with self._write(operation_id):
            job, inputs = await self._locked(memory_id, job_id)
            if self.validate_binding is not None:
                await self.validate_binding(job, manifest_for_job(job, inputs))
            await require_execution_permit(self.db, job, operation_id, execution_claim_token, token, start=True)
            row = await self._row(memory_id, operation_id, lock=True)
            if (
                job.generation != generation
                or job.analysis_operation_id != operation_id
                or row.active_token != token
                or row.publication_state == "canceled"
                or job.state not in {"pending", "failed", "running"}
            ):
                raise RevisionConflict("maintenance_analysis_fence_changed")
            if len(job.receipt.get("analysis_attempts", {})) >= 1000:
                raise RevisionConflict("maintenance_attempt_window_exceeded")
            job.state, job.error_code, job.updated_at = "running", None, datetime.now(UTC)
            return manifest_for_job(job, inputs), job.policy, job.output_budget_tokens

    @staticmethod
    def _analysis_metadata(result: AnalysisResult) -> None:
        if not isinstance(result, dict) or set(result) != {"proposals", "model_reference", "config_reference", "usage"}:
            raise RevisionValidationError("invalid_maintenance_analysis_result")
        if any(not isinstance(value, str) or not value or len(value) > 256 for value in (result["model_reference"], result["config_reference"])):
            raise RevisionValidationError("invalid_maintenance_model_reference")
        if (
            not isinstance(result["usage"], dict)
            or len(result["usage"]) > 32
            or any(
                not isinstance(key, str) or len(key) > 64 or type(value) not in (int, float, bool) or (type(value) is not bool and (not math.isfinite(value) or value < 0))
                for key, value in result["usage"].items()
            )
        ):
            raise RevisionValidationError("invalid_maintenance_usage")

    @staticmethod
    def _record_attempt(job: MemoryMaintenanceJob, operation_id: str, token: str, result: AnalysisResult | None, error_code: str | None) -> None:
        attempts = job.receipt.get("analysis_attempts", {})
        if token in attempts or len(attempts) >= 1000:
            return
        metadata = {
            "operation_id": operation_id,
            "error_code": error_code,
            "recorded_at": _iso(datetime.now(UTC)),
            "usage": deepcopy(result["usage"]) if result is not None else {},
            "usage_unavailable": result is None,
        }
        if result is not None:
            metadata.update(model_reference=result["model_reference"], config_reference=result["config_reference"])
        attempts = {**attempts, token: metadata}
        totals: dict[str, Any] = {"attempts_recorded": len(attempts), "provider_billing_verified": False, "usage_unavailable": any(item["usage_unavailable"] for item in attempts.values())}
        for item in attempts.values():
            for name, value in item["usage"].items():
                if name in {"attempts_recorded", "provider_billing_verified", "usage_unavailable"}:
                    continue
                if type(value) is bool:
                    totals[name] = bool(totals.get(name, False)) or value
                elif "budget" in name:
                    totals[name] = value
                else:
                    totals[name] = totals.get(name, 0) + value
        job.receipt = {**job.receipt, "analysis_attempts": attempts}
        job.usage = totals

    async def _save_analysis(
        self, memory_id: str, job_id: str, operation_id: str, token: str, generation: int, result: AnalysisResult, candidates: list[dict[str, Any]], execution_claim_token: str | None = None
    ) -> None:
        from memory.maintenance.execution import require_execution_permit

        async with self._write(operation_id):
            job, inputs = await self._locked(memory_id, job_id)
            await require_execution_permit(self.db, job, operation_id, execution_claim_token, token, start=False)
            row = await self._row(memory_id, operation_id, lock=True)
            if job.generation != generation or job.analysis_operation_id != operation_id or row.active_token != token or row.publication_state == "canceled" or job.state != "running":
                raise RevisionConflict("maintenance_analysis_fence_changed")
            if await self.db.scalar(select(MemoryMaintenanceCandidate.candidate_id).where(MemoryMaintenanceCandidate.job_id == job_id).limit(1)) is not None:
                raise RevisionConflict("maintenance_candidates_already_saved")
            candidate_ids = []
            for candidate in candidates:
                identity = uuid4().hex
                candidate_ids.append(identity)
                self.db.add(MemoryMaintenanceCandidate(candidate_id=identity, job_id=job_id, memory_id=memory_id, tenant_id=self.tenant_id, revision=1, state="pending", **candidate))
            for item in inputs:
                if item.coverage == "included":
                    item.coverage = "analyzed"
            if self.checkpoint is not None:
                await self.checkpoint(job, inputs, result)
            self._record_attempt(job, operation_id, token, result, None)
            job.state, job.model_reference, job.config_reference = "ready", result["model_reference"], result["config_reference"]
            job.error_code, job.updated_at = None, datetime.now(UTC)
            row.publication_state = "ready"
            outbox = await self._outbox(row)
            outbox.state, outbox.payload = "ready", {"action": "dream_analyze", "job_id": job_id, "approved": True, "reserved_bytes": 0}
            outbox.receipt = {**outbox.receipt, "candidate_ids": candidate_ids, "reservation_released": True}
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code, operation.result = "ready", "ready", None, {"job_id": job_id, "candidate_ids": candidate_ids}

    async def _analysis_error(self, memory_id: str, job_id: str, operation_id: str, code: str, *, token: str, result: AnalysisResult | None = None, execution_claim_token: str | None = None) -> None:
        async with self._write(operation_id):
            job = await self._job(memory_id, job_id, lock=True)
            if execution_claim_token is not None:
                from api.db.memory_execution_models import MemoryMaintenanceExecution

                execution = await self.db.scalar(select(MemoryMaintenanceExecution).where(MemoryMaintenanceExecution.job_id == job_id).execution_options(populate_existing=True).with_for_update())
                if execution is None or execution.claim_token != execution_claim_token:
                    return
            if job.state != "ready":
                self._record_attempt(job, operation_id, token, result, code)
            if job.state not in {"pending", "running", "failed"}:
                return
            if result is not None:
                job.model_reference, job.config_reference = result["model_reference"], result["config_reference"]
            job.state, job.error_code, job.updated_at = "failed", code, datetime.now(UTC)
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            if operation is not None:
                operation.status, operation.index_sync_status, operation.error_code = "failed", "failed", code

    async def _settle_cancel(self, memory_id: str, job_id: str, operation_id: str) -> None:
        async with self._write(operation_id):
            job = await self._job(memory_id, job_id, lock=True)
            if job.state == "cancel_requested":
                job.state, job.updated_at = "canceled", datetime.now(UTC)
                if job.analysis_operation_id is not None:
                    operation = await self.db.get(MemoryMutationOperation, (memory_id, job.analysis_operation_id))
                    if operation is not None and operation.status == "cancel_requested":
                        operation.status, operation.index_sync_status = "canceled", "canceled"

    async def run(self, memory_id: str, job_id: str, *, operation_id: str, execution_claim_token: str | None = None) -> dict[str, Any]:
        validate_operation_id(operation_id)
        view = await self.get(memory_id, job_id)
        if view["analysis_operation_id"] is not None and view["analysis_operation_id"] != operation_id:
            raise RevisionConflict("maintenance_analysis_operation_mismatch")
        if view["state"] == "ready":
            return view
        if view["execution"] is not None and execution_claim_token is None:
            raise RevisionBusy("maintenance_background_execution_required")
        if view["state"] not in {"pending", "failed", "running"}:
            raise RevisionConflict("maintenance_job_not_runnable")
        generation = view["generation"]
        await self.admit(memory_id, operation_id=operation_id, kind="dream_analyze", request={"job_id": job_id, "job_generation": generation})
        claimed = await self.claim(memory_id, operation_id)
        token = claimed["active_token"]
        metadata: AnalysisResult | None = None
        try:
            manifest, policy, budget = await self._start(memory_id, job_id, operation_id, token, generation, execution_claim_token)
            if any(item["coverage"] == "included" for item in manifest["inputs"]):
                result: AnalysisResult = await finish_owned_work(self.analyzer(memory_id, manifest, policy, budget))
            else:
                result = {"proposals": [], "model_reference": "not_applicable", "config_reference": "all_inputs_deferred", "usage": {}}
            self._analysis_metadata(result)
            metadata = result
            proposed = assemble_candidates(result["proposals"], manifest, self.count_tokens, budget)
            await self._save_analysis(memory_id, job_id, operation_id, token, generation, result, proposed, execution_claim_token)
        except asyncio.CancelledError:
            await finish_owned_work(
                self._analysis_error(memory_id, job_id, operation_id, "maintenance_analysis_interrupted", token=token, result=metadata, execution_claim_token=execution_claim_token)
            )
            raise
        except Exception as error:
            code = error.code if isinstance(error, (RevisionConflict, RevisionValidationError, RevisionNotFound)) else "maintenance_analysis_unconfirmed"
            await self._analysis_error(memory_id, job_id, operation_id, code, token=token, result=metadata, execution_claim_token=execution_claim_token)
        finally:
            try:
                await finish_owned_work(self._settle_cancel(memory_id, job_id, operation_id))
            finally:
                await finish_owned_work(self.finish(memory_id, operation_id, token=token))
        return await self.get(memory_id, job_id)

    @staticmethod
    def _command(job: MemoryMaintenanceJob, operation_id: str, digest: str, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        commands = job.receipt.get("commands", {})
        previous = commands.get(operation_id)
        if previous is not None:
            if previous["request_sha256"] != digest:
                raise RevisionConflict("operation_request_mismatch")
            return previous["result"]
        if operation_id in {job.operation_id, job.analysis_operation_id}:
            raise RevisionConflict("operation_request_mismatch")
        if result is not None:
            if len(commands) >= 1000:
                raise RevisionConflict("maintenance_command_window_exceeded")
            job.receipt = {**job.receipt, "commands": {**commands, operation_id: {"request_sha256": digest, "result": deepcopy(result)}}}
        return None

    async def _review_locked(self, memory_id: str, job_id: str, candidate_id: str, operation_id: str, digest: str) -> tuple[MemoryMaintenanceJob, MemoryMaintenanceCandidate]:
        await self._memory_fence(memory_id)
        observed = await self._job(memory_id, job_id)
        candidate_query = (
            select(MemoryMaintenanceCandidate)
            .where(
                MemoryMaintenanceCandidate.candidate_id == candidate_id,
                MemoryMaintenanceCandidate.job_id == job_id,
                MemoryMaintenanceCandidate.memory_id == memory_id,
                MemoryMaintenanceCandidate.tenant_id == self.tenant_id,
            )
            .execution_options(populate_existing=True)
        )
        candidate = await self.db.scalar(candidate_query)
        if candidate is None:
            raise RevisionNotFound("maintenance_candidate_not_found")
        inputs = [item for item in await self._inputs(observed) if item.input_id in candidate.source_input_ids]
        if len(inputs) != len(candidate.source_input_ids):
            raise RevisionConflict("maintenance_candidate_sources_changed")
        manifest = manifest_for_job(observed, inputs)
        replay = self._command(observed, operation_id, digest)
        manifest["entries"] = [entry for entry in manifest["entries"] if replay is None and entry["entry_id"] == candidate.target_entry_id]
        await self.validate_inputs(self.db, memory_id, manifest)
        job = await self._job(memory_id, job_id, lock=True)
        candidate = await self.db.scalar(candidate_query.with_for_update())
        assert candidate is not None
        return job, candidate

    async def review(self, memory_id: str, job_id: str, candidate_id: str, *, operation_id: str, expected_revision: int, decision: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        positive_int(expected_revision)
        identifier(candidate_id)
        if decision not in {"approve", "reject"}:
            raise RevisionValidationError("invalid_maintenance_review_decision")
        digest = request_hash({"actor_id": self.actor_id, "job_id": job_id, "candidate_id": candidate_id, "expected_revision": expected_revision, "decision": decision})
        async with self._write(operation_id):
            job, candidate = await self._review_locked(memory_id, job_id, candidate_id, operation_id, digest)
            previous = self._command(job, operation_id, digest)
            if previous is not None:
                value = candidate_view(candidate)
                if not value["redacted"]:
                    value.update(previous)
                return value
            if job.state != "ready" or candidate.state not in {"pending", "approved", "rejected"} or candidate.revision != expected_revision:
                raise RevisionConflict("maintenance_candidate_revision_changed")
            if candidate.action == "noop" and decision == "approve":
                raise RevisionValidationError("maintenance_noop_not_publishable")
            candidate.state, candidate.revision = "approved" if decision == "approve" else "rejected", candidate.revision + 1
            candidate.review_actor_id, candidate.reviewed_at, candidate.review_operation_id, candidate.updated_at = self.actor_id, datetime.now(UTC), operation_id, datetime.now(UTC)
            self._command(job, operation_id, digest, {"revision": candidate.revision, "state": candidate.state, "review_actor_id": self.actor_id, "reviewed_at": _iso(candidate.reviewed_at)})
            await self.db.flush()
            value = candidate_view(candidate)
        return value

    async def cancel(self, memory_id: str, job_id: str, *, operation_id: str) -> dict[str, Any]:
        from memory.maintenance.execution import cancel_execution

        validate_operation_id(operation_id)
        digest = request_hash({"actor_id": self.actor_id, "job_id": job_id, "action": "cancel"})
        async with self._write(operation_id):
            job, inputs = await self._locked(memory_id, job_id, validate=False)
            if self._command(job, operation_id, digest) is not None:
                return await self._job_view(job, inputs)
            candidates = list(
                await self.db.scalars(select(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.job_id == job_id).order_by(MemoryMaintenanceCandidate.candidate_id).with_for_update())
            )
            await cancel_execution(self.db, memory_id=memory_id, job_id=job_id, reason="maintenance_job_canceled")
            row = (
                await self.db.scalar(
                    select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.operation_id == job.analysis_operation_id).with_for_update()
                )
                if job.analysis_operation_id
                else None
            )
            active = row is not None and (row.active_token is not None or row.state in {"active", "unknown"})
            job.state, job.generation, job.updated_at = "cancel_requested" if active else "canceled", job.generation + 1, datetime.now(UTC)
            job.entry_snapshots = []
            for item in inputs:
                item.snapshot = {}
            for candidate in candidates:
                candidate.state, candidate.content, candidate.base_content, candidate.patch, candidate.diff, candidate.reason = "redacted", None, None, None, None, None
            if row is not None and row.publication_state != "ready":
                row.publication_state = "canceled"
                if not active:
                    row.state = "canceled"
                outbox = await self._outbox(row)
                outbox.state = "redacted"
                outbox.payload = {"action": "dream_analyze", "job_id": job_id, "reserved_bytes": 0}
                operation = await self.db.get(MemoryMutationOperation, (memory_id, row.operation_id))
                if operation is not None:
                    operation.status, operation.index_sync_status, operation.error_code = (
                        ("cancel_requested", "pending", "maintenance_job_canceled") if active else ("canceled", "canceled", "maintenance_job_canceled")
                    )
            self._command(job, operation_id, digest, {"state": job.state, "generation": job.generation})
            await self.db.flush()
            value = await self._job_view(job, inputs)
        return value

    async def operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        from api.db.memory_execution_models import MemoryMaintenanceExecution
        from memory.maintenance.execution import execution_snapshot

        value = await super().operation(memory_id, operation_id)
        try:
            row = await self.db.scalar(
                select(MemoryMaintenanceExecution).where(
                    MemoryMaintenanceExecution.memory_id == memory_id, MemoryMaintenanceExecution.operation_id == operation_id, MemoryMaintenanceExecution.tenant_id == self.tenant_id
                )
            )
            value["execution"] = await execution_snapshot(self.db, row) if row is not None else None
            return value
        finally:
            await self.db.rollback()
