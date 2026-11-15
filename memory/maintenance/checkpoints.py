"""Optional job hooks: frozen analysis profiles and same-transaction coverage."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceInput, MemoryMaintenanceJob
from memory.maintenance.selection import source_identity
from memory.maintenance.types import AnalysisResult
from memory.revisions.domain import RevisionConflict, RevisionValidationError, request_hash

AnalysisIdentity = Callable[[str], Awaitable[dict[str, str]]]


class IncrementalCheckpoints:
    def __init__(self, db: AsyncSession, *, tenant_id: str, identity: AnalysisIdentity) -> None:
        self.db, self.tenant_id, self.identity = db, tenant_id, identity

    async def _binding(self, job: MemoryMaintenanceJob) -> tuple[MemoryMaintenanceBatch, MemoryMaintenanceDiscovery] | None:
        batch = await self.db.scalar(select(MemoryMaintenanceBatch).where(MemoryMaintenanceBatch.job_id == job.job_id).execution_options(populate_existing=True).with_for_update())
        if batch is None:
            return None
        discovery = await self.db.scalar(
            select(MemoryMaintenanceDiscovery).where(MemoryMaintenanceDiscovery.discovery_id == batch.discovery_id).execution_options(populate_existing=True).with_for_update()
        )
        if (
            discovery is None
            or (batch.memory_id, batch.tenant_id, discovery.memory_id, discovery.tenant_id, discovery.actor_id, batch.create_operation_id)
            != (job.memory_id, job.tenant_id, job.memory_id, self.tenant_id, job.actor_id, job.operation_id)
            or discovery.state in {"canceled", "stale"}
        ):
            raise RevisionConflict("incremental_job_binding_changed")
        return batch, discovery

    async def validate(self, job: MemoryMaintenanceJob, manifest: dict[str, Any]) -> None:
        binding = await self._binding(job)
        if binding is None:
            return
        batch, discovery = binding
        profile = discovery.profile
        current = await self.identity(job.memory_id)
        if any(profile.get(name) != current.get(name) for name in ("strategy_sha256", "model_reference", "config_reference")):
            raise RevisionConflict("incremental_analysis_profile_changed")
        if (job.policy, job.input_budget_tokens, job.output_budget_tokens) != (profile["policy"], profile["input_budget_tokens"], profile["output_budget_tokens"]):
            raise RevisionConflict("incremental_analysis_profile_changed")
        expected = {item["source_id"]: item for item in [*batch.primary_sources, *batch.context_sources]}
        actual = {item["source_id"]: item for item in manifest["inputs"]}
        if set(expected) != set(actual) or any(source_identity(expected[key]) != source_identity(actual[key]) or expected[key]["scope"] != actual[key]["scope"] for key in expected):
            raise RevisionConflict("incremental_source_version_changed")
        context_ids = {int(item["source_id"]) for item in profile["context_sources"]} & set(expected)
        if any(actual[key]["coverage"] not in {"included", "analyzed"} for key in context_ids):
            raise RevisionValidationError("incremental_context_deferred")
        expected_entries = {item["entry_id"]: item for item in profile["entries"] if item["entry_id"] in batch.entry_ids}
        actual_entries = {item["entry_id"]: item for item in manifest["entries"]}
        if set(expected_entries) != set(actual_entries):
            raise RevisionConflict("incremental_target_version_changed")
        fields = ("revision", "message_id", "generation", "content_sha256")
        if any(any(str(expected_entries[key][field]) != str(actual_entries[key][field]) for field in fields) for key in expected_entries):
            raise RevisionConflict("incremental_target_version_changed")
        if request_hash(profile) != discovery.profile_sha256:
            raise RevisionConflict("incremental_profile_binding_changed")
        primary = {item["source_id"]: item for item in batch.primary_sources}
        coverage = list(
            await self.db.scalars(select(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.batch_id == batch.batch_id).order_by(MemoryMaintenanceCoverage.source_id).with_for_update())
        )
        if {row.source_id for row in coverage} != set(primary) or any(
            row.job_id != job.job_id or row.profile_sha256 != discovery.profile_sha256 or source_identity(row.__dict__) != source_identity(primary[row.source_id]) for row in coverage
        ):
            raise RevisionConflict("incremental_checkpoint_owner_changed")

    async def checkpoint(self, job: MemoryMaintenanceJob, inputs: list[MemoryMaintenanceInput], result: AnalysisResult) -> None:
        """Called only after complete candidate validation, inside _save_analysis."""
        manifest = {
            "inputs": [{name: getattr(item, name) for name in ("source_id", "source_revision", "source_generation", "content_sha256", "scope", "coverage")} for item in inputs],
            "entries": job.entry_snapshots,
        }
        await self.validate(job, manifest)
        binding = await self._binding(job)
        if binding is None:
            return
        batch, discovery = binding
        if result["model_reference"] != discovery.profile["model_reference"] or result["config_reference"] != discovery.profile["config_reference"]:
            raise RevisionConflict("incremental_analysis_profile_changed")
        primary = {item["source_id"] for item in batch.primary_sources}
        rows = list(
            await self.db.scalars(select(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.batch_id == batch.batch_id).order_by(MemoryMaintenanceCoverage.source_id).with_for_update())
        )
        if {row.source_id for row in rows} != primary:
            raise RevisionConflict("incremental_checkpoint_inventory_changed")
        by_id = {item.source_id: item for item in inputs}
        for row in rows:
            item = by_id[row.source_id]
            if row.job_id != job.job_id or row.profile_sha256 != discovery.profile_sha256 or source_identity(row.__dict__) != source_identity(item.__dict__):
                raise RevisionConflict("incremental_checkpoint_version_changed")
            row.state = "analyzed" if item.coverage == "analyzed" else "blocked"
            row.error_code = None if row.state == "analyzed" else "input_budget"
            row.updated_at = datetime.now(UTC)
        blocked = any(row.state == "blocked" for row in rows)
        batch.state, batch.error_code, batch.updated_at = "blocked" if blocked else "analyzed", "input_budget" if blocked else None, datetime.now(UTC)
