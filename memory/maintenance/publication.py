"""Stage a reviewed batch, choose all SQL currents once, then settle each item."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.maintenance.domain import ASSESSMENT, assemble_candidates, identifier
from memory.maintenance.jobs import manifest_for_job
from memory.maintenance.types import InputValidator
from memory.revisions.domain import (
    ModelBinding,
    RevisionBusy,
    RevisionCapacityError,
    RevisionConflict,
    RevisionNotFound,
    RevisionPersistenceUnknown,
    RevisionValidationError,
    content_hash,
    is_active,
    message_scope,
    positive_int,
    request_hash,
    validate_operation_id,
    vector_dimension,
)
from memory.services.messages import MessageService
from memory.writes.admission import WriterCoordinator, release_writer_reservation
from memory.writes.service import verify_approved_message
from memory.writes.types import Authorize, BindingReader, Embedder, MessageIdAllocator


class PublicationStorage(Protocol):
    tenant_id: str
    memory_id: str

    async def read(self, message_id: int) -> dict[str, Any] | None: ...
    async def write(self, message: dict[str, Any]) -> None: ...
    async def set_status(self, message_id: int, status: bool) -> None: ...
    async def inventory(self) -> list[dict[str, Any]]: ...
    async def delete(self, message_ids: list[int]) -> None: ...


def publication_selections(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 16:
        raise RevisionValidationError("invalid_publication_candidates")
    result = []
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {"candidate_id", "expected_revision"}:
            raise RevisionValidationError("invalid_publication_candidate")
        result.append({"candidate_id": identifier(item["candidate_id"]), "expected_revision": positive_int(item["expected_revision"], "invalid_candidate_revision")})
    if len({item["candidate_id"] for item in result}) != len(result):
        raise RevisionValidationError("duplicate_publication_candidate")
    return sorted(result, key=lambda item: item["candidate_id"])


class PublicationService(WriterCoordinator):
    def __init__(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        actor_id: str,
        authorize: Authorize,
        model_binding: BindingReader,
        embedder: Embedder,
        storage: PublicationStorage,
        allocate_message_id: MessageIdAllocator,
        count_tokens: Callable[[str], int],
        validate_inputs: InputValidator,
    ) -> None:
        super().__init__(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)
        self.model_binding, self.embedder, self.storage = model_binding, embedder, storage
        self.allocate_message_id, self.count_tokens, self.validate_inputs = allocate_message_id, count_tokens, validate_inputs

    async def _bank(self, memory_id: str) -> ManagedMemoryState:
        bank = await self.db.scalar(
            select(ManagedMemoryState).where(ManagedMemoryState.memory_id == memory_id, ManagedMemoryState.tenant_id == self.tenant_id).execution_options(populate_existing=True).with_for_update()
        )
        if bank is None or bank.state != "active":
            raise RevisionConflict("dreaming_requires_managed_memory")
        return bank

    async def _job(self, memory_id: str, job_id: str, *, lock: bool = False) -> MemoryMaintenanceJob:
        query = (
            select(MemoryMaintenanceJob)
            .where(MemoryMaintenanceJob.memory_id == memory_id, MemoryMaintenanceJob.tenant_id == self.tenant_id, MemoryMaintenanceJob.job_id == job_id)
            .execution_options(populate_existing=True)
        )
        row = await self.db.scalar(query.with_for_update() if lock else query)
        if row is None:
            raise RevisionNotFound("maintenance_job_not_found")
        return row

    async def _candidates(self, memory_id: str, job_id: str, selected: list[dict[str, Any]], *, lock: bool = False) -> list[MemoryMaintenanceCandidate]:
        query = (
            select(MemoryMaintenanceCandidate)
            .where(
                MemoryMaintenanceCandidate.memory_id == memory_id,
                MemoryMaintenanceCandidate.tenant_id == self.tenant_id,
                MemoryMaintenanceCandidate.job_id == job_id,
                MemoryMaintenanceCandidate.candidate_id.in_([item["candidate_id"] for item in selected]),
            )
            .order_by(MemoryMaintenanceCandidate.candidate_id)
            .execution_options(populate_existing=True)
        )
        rows = list(await self.db.scalars(query.with_for_update() if lock else query))
        if len(rows) != len(selected):
            raise RevisionNotFound("maintenance_candidate_not_found")
        for row, expected in zip(rows, selected, strict=True):
            if row.candidate_id != expected["candidate_id"] or row.revision != expected["expected_revision"]:
                raise RevisionConflict("maintenance_candidate_revision_changed")
        return rows

    async def _selected_manifest(self, job: MemoryMaintenanceJob, candidates: list[MemoryMaintenanceCandidate]) -> dict[str, Any]:
        inputs = list(
            await self.db.scalars(
                select(MemoryMaintenanceInput)
                .where(MemoryMaintenanceInput.job_id == job.job_id, MemoryMaintenanceInput.memory_id == job.memory_id, MemoryMaintenanceInput.tenant_id == self.tenant_id)
                .order_by(MemoryMaintenanceInput.input_id)
            )
        )
        manifest = manifest_for_job(job, inputs)
        input_ids = {identity for item in candidates for identity in item.source_input_ids}
        targets = {item.target_entry_id for item in candidates if item.action == "patch"}
        manifest["inputs"] = [item for item in manifest["inputs"] if item["input_id"] in input_ids]
        manifest["entries"] = [item for item in manifest["entries"] if item["entry_id"] in targets]
        if len(manifest["inputs"]) != len(input_ids) or len(manifest["entries"]) != len(targets):
            raise RevisionConflict("publication_evidence_missing")
        return manifest

    def _verify_candidates(self, rows: list[MemoryMaintenanceCandidate], manifest: dict[str, Any], budget: int) -> None:
        proposals = []
        for row in rows:
            if row.assessment != ASSESSMENT or row.review_actor_id is None or row.reviewed_at is None or row.action not in {"add", "patch"}:
                raise RevisionConflict("publication_candidate_not_approved")
            proposal: dict[str, Any] = {"action": row.action, "source_input_ids": row.source_input_ids, "reason": row.reason}
            if row.action == "add":
                proposal.update(content=row.content, message_type=row.message_type)
            else:
                proposal.update(target_entry_id=row.target_entry_id, patch=row.patch)
            proposals.append(proposal)
        assembled = assemble_candidates(proposals, manifest, self.count_tokens, budget)
        for row, expected in zip(rows, assembled, strict=True):
            for field in ("content", "base_content", "base_content_sha256", "patch", "diff", "sources", "expected_revision"):
                if getattr(row, field) != expected[field]:
                    raise RevisionConflict("publication_candidate_content_changed")

    async def _configure_admission(self, row: MemoryWriterAdmission, fence: MemoryWriterFence, request: dict[str, Any]) -> None:
        bank = await self._bank(row.memory_id)
        job = await self._job(row.memory_id, request["job_id"])
        candidates = await self._candidates(row.memory_id, job.job_id, request["candidates"])
        manifest = await self._selected_manifest(job, candidates)
        await self.validate_inputs(self.db, row.memory_id, manifest)
        job = await self._job(row.memory_id, job.job_id, lock=True)
        candidates = await self._candidates(row.memory_id, job.job_id, request["candidates"], lock=True)
        if job.state != "ready" or any(item.state != "approved" or item.publication_operation_id is not None for item in candidates):
            raise RevisionConflict("publication_candidate_not_approved")
        self._verify_candidates(candidates, manifest, job.output_budget_tokens)
        items, targets = [], set()
        for candidate in candidates:
            target = None
            target_snapshot = None
            if candidate.action == "patch":
                if candidate.target_entry_id in targets:
                    raise RevisionConflict("publication_duplicate_target")
                targets.add(candidate.target_entry_id)
                target = await self.db.get(MemoryEntry, candidate.target_entry_id, populate_existing=True)
                if target is None:
                    raise RevisionNotFound("entry_not_found")
                current = await self.db.get(MemoryEntryRevision, (target.entry_id, target.current_revision), populate_existing=True)
                if current is None:
                    raise RevisionConflict("publication_target_changed")
                target_snapshot = {
                    key: deepcopy(current.message_snapshot.get(key)) for key in ("message_type", "source_id", "user_id", "agent_id", "session_id", "valid_at", "invalid_at", "forget_at")
                }
                revision = target.next_revision
                target.next_revision += 1
            else:
                revision = 1
            entry_id = target.entry_id if target is not None else uuid4().hex
            child = "dream-item:" + request_hash({"memory_id": row.memory_id, "operation_id": row.operation_id, "candidate_id": candidate.candidate_id})
            sources = []
            for source in candidate.sources:
                original = next(item for item in manifest["inputs"] if item["input_id"] == source["input_id"])
                sources.append({**deepcopy(source), "scope": deepcopy(original["scope"]), "valid_at": original["valid_at"]})
            items.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "candidate_revision": candidate.revision,
                    "entry_id": entry_id,
                    "revision": revision,
                    "message_id": None,
                    "old_message_id": target.current_message_id if target is not None else None,
                    "expected_revision": candidate.expected_revision,
                    "entry_generation": target.generation if target is not None else 1,
                    "child_operation_id": child,
                    "child_outbox_id": uuid4().hex,
                    "sources": sources,
                    "target_snapshot": target_snapshot,
                    "message": {"content": candidate.content, "message_type": candidate.message_type},
                }
            )
            candidate.state, candidate.publication_operation_id = "publishing", row.operation_id
        for item in manifest["inputs"]:
            item["snapshot"] = {}
        outbox = await self._outbox(row)
        outbox.bank_generation = bank.generation
        outbox.payload = {
            "action": "dream_publish",
            "job_id": job.job_id,
            "job_generation": job.generation,
            "manifest": manifest,
            "selections": deepcopy(request["candidates"]),
            "items": items,
            "approved": False,
            "published": False,
            "reserved_bytes": 0,
            "reservation_owner": "managed_bank",
        }
        row.source_message_id = min(item["source_id"] for item in manifest["inputs"])
        operation = await self.db.get(MemoryMutationOperation, (row.memory_id, row.operation_id))
        assert operation is not None
        operation.result = {"job_id": job.job_id, "candidate_ids": [item.candidate_id for item in candidates], "entries": []}

    async def publish(self, memory_id: str, job_id: str, *, operation_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if self.storage.tenant_id != self.tenant_id or self.storage.memory_id != memory_id:
            raise RevisionValidationError("publication_storage_binding_mismatch")
        identifier(job_id)
        await self.admit(memory_id, operation_id=operation_id, kind="dream_publish", request={"job_id": job_id, "candidates": publication_selections(candidates)})
        return await self.reconcile(memory_id, operation_id)

    async def get_operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        validate_operation_id(operation_id)
        result = await self.operation(memory_id, operation_id)
        if result["kind"] != "dream_publish":
            raise RevisionNotFound("publication_operation_not_found")
        result["retryable"] = bool(result["retryable"] or any(item.get("state") in {"pending", "unknown", "sync_pending"} for item in result.get("item_states", [])))
        return result

    async def _locked(
        self, memory_id: str, operation_id: str, token: str, *, before_publish: bool = True
    ) -> tuple[ManagedMemoryState, MemoryWriterAdmission, MemoryPublicationOutbox, MemoryMutationOperation]:
        _, fence = await self._memory_fence(memory_id)
        bank = await self._bank(memory_id)
        admission = await self._row(memory_id, operation_id)
        observed = await self.db.scalar(
            select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id).execution_options(populate_existing=True)
        )
        if observed is None or observed.payload.get("action") != "dream_publish":
            raise RevisionConflict("invalid_publication_operation")
        payload = observed.payload
        if before_publish and not payload.get("published"):
            await self.validate_inputs(self.db, memory_id, payload["manifest"])
            job = await self._job(memory_id, payload["job_id"], lock=True)
            rows = await self._candidates(memory_id, job.job_id, payload["selections"], lock=True)
            if job.state != "ready" or job.generation != payload["job_generation"] or any(item.state != "publishing" or item.publication_operation_id != operation_id for item in rows):
                raise RevisionConflict("publication_candidate_changed")
        admission = await self._row(memory_id, operation_id, lock=True)
        outbox = await self._outbox(admission)
        if admission.active_token != token or admission.writer_generation != fence.generation or admission.publication_state == "canceled" or outbox.bank_generation != bank.generation:
            raise RevisionConflict("publication_fence_changed")
        operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id), populate_existing=True)
        if operation is None:
            raise RevisionNotFound("operation_not_found")
        return bank, admission, outbox, operation

    async def _checkpoint(self, memory_id: str, operation_id: str, token: str) -> dict[str, Any]:
        async with self._write(operation_id):
            _, _, outbox, _ = await self._locked(memory_id, operation_id, token)
            return deepcopy(outbox.payload)

    async def _verify_physical_sources(self, memory_id: str, manifest: dict[str, Any]) -> None:
        for source in manifest["inputs"]:
            raw = await finish_owned_work(self.storage.read(source["source_id"]))
            if (
                raw is None
                or raw.get("message_id") != source["source_id"]
                or raw.get("message_type") != "raw"
                or isinstance(raw.get("source_id"), bool)
                or raw.get("source_id") not in (None, 0, "", "0")
                or not is_active(raw)
                or not isinstance(raw.get("content"), str)
                or content_hash(raw["content"]) != source["content_sha256"]
                or message_scope(raw, memory_id) != source["scope"]
            ):
                raise RevisionConflict("publication_source_projection_changed")

    async def _prepare(self, memory_id: str, operation_id: str, token: str) -> None:
        payload = await self._checkpoint(memory_id, operation_id, token)
        if payload["approved"] or payload["published"]:
            return
        await self._verify_physical_sources(memory_id, payload["manifest"])
        inventory = await finish_owned_work(self.storage.inventory())
        minimum = max([await self._minimum_message_id(memory_id), *(positive_int(item["message_id"], "invalid_physical_message_id") for item in inventory)])
        items = deepcopy(payload["items"])
        for item in items:
            identity = await finish_owned_work(self.allocate_message_id(minimum_message_id=minimum))
            if positive_int(identity, "invalid_allocated_message_id") <= minimum:
                raise RevisionConflict("publication_message_id_collision")
            minimum = identity
            anchor = min(item["sources"], key=lambda value: value["source_id"])
            message_type = item["message"]["message_type"]
            metadata = {"message_type": message_type, "source_id": anchor["source_id"], **anchor["scope"], "valid_at": anchor["valid_at"], "invalid_at": None, "forget_at": None}
            if item["expected_revision"] is not None:
                target = next(target for target in payload["manifest"]["entries"] if target["entry_id"] == item["entry_id"])
                actual = await finish_owned_work(self.storage.read(target["message_id"]))
                frozen = item["target_snapshot"]
                if (
                    actual is None
                    or actual.get("message_id") != target["message_id"]
                    or not is_active(actual)
                    or not isinstance(actual.get("content"), str)
                    or content_hash(actual["content"]) != target["content_sha256"]
                    or message_scope(actual, memory_id) != target["scope"]
                    or any(actual.get(key) != value for key, value in frozen.items() if key not in {"valid_at", "invalid_at", "forget_at"})
                    or any(
                        (None if actual.get(key) in (None, "", "-") else actual[key]) != (None if frozen[key] in (None, "", "-") else frozen[key]) for key in ("valid_at", "invalid_at", "forget_at")
                    )
                    or frozen["source_id"] not in {ref["source_id"] for ref in item["sources"]}
                ):
                    raise RevisionConflict("publication_target_projection_changed")
                metadata = deepcopy(frozen)
            if metadata["message_type"] not in {"semantic", "episodic", "procedural"} or not isinstance(metadata["valid_at"], str):
                raise RevisionValidationError("invalid_publication_message")
            item["message_id"] = identity
            item["message"] = {
                "id": f"{memory_id}_{identity}",
                "memory_id": memory_id,
                "message_id": identity,
                **metadata,
                "content": item["message"]["content"],
                "status": False,
            }
        embedded = await finish_owned_work(self.embedder(memory_id, [item["message"] for item in items], None))
        vectors, binding = embedded["vectors"], embedded["model_binding"]
        if len(vectors) != len(items) or not isinstance(binding, ModelBinding):
            raise RevisionValidationError("invalid_publication_embedding")
        for item, vector in zip(items, vectors, strict=True):
            item["message"]["content_embed"] = vector
            if vector_dimension(item["message"]) != binding.dimension:
                raise RevisionValidationError("invalid_publication_embedding")
        async with self._write(operation_id):
            _, admission, outbox, operation = await self._locked(memory_id, operation_id, token)
            if await self.model_binding(self.db, memory_id, binding.dimension) != binding:
                raise RevisionConflict("publication_model_binding_changed")
            outbox.payload = {**outbox.payload, "items": items, "approved": True, "model_binding": binding.to_dict()}
            admission.message_ids = [item["message_id"] for item in items]
            operation.result = {
                **operation.result,
                "entries": [{"candidate_id": item["candidate_id"], "entry_id": item["entry_id"], "new_revision": item["revision"], "message_id": item["message_id"]} for item in items],
            }

    async def _minimum_message_id(self, memory_id: str) -> int:
        """A reset counter must not reuse physically removed SQL identities."""
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
            source = await self.db.scalar(select(func.max(MemorySourceState.source_message_id)).where(MemorySourceState.memory_id == memory_id))
            revision = await self.db.scalar(select(func.max(MemoryEntryRevision.message_id)).where(MemoryEntryRevision.memory_id == memory_id))
            manifests = list(
                await self.db.scalars(select(MemoryWriterAdmission.message_ids).where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == self.tenant_id).limit(10001))
            )
            if len(manifests) > 10000:
                raise RevisionConflict("writer_inventory_incomplete")
            return max([1, source or 1, revision or 1, *(positive_int(identity, "invalid_writer_message_id") for ids in manifests for identity in ids)])
        finally:
            await self.db.rollback()

    async def _reserve(self, memory_id: str, operation_id: str, token: str, capacity: int) -> None:
        async with self._write(operation_id):
            bank, _, outbox, _ = await self._locked(memory_id, operation_id, token)
            if outbox.payload.get("reservation_prepared") or outbox.payload["published"]:
                return
            generation, payload = bank.capacity_generation, deepcopy(outbox.payload)
        inventory = await finish_owned_work(self.storage.inventory())
        observed = sum(MessageService.calculate_message_size(item) for item in inventory)
        present = {item["message_id"] for item in inventory}
        required = sum(MessageService.calculate_message_size(item["message"]) + 4096 for item in payload["items"] if item["message_id"] not in present)
        async with self._write(operation_id):
            bank, _, outbox, _ = await self._locked(memory_id, operation_id, token)
            if bank.capacity_generation != generation:
                raise RevisionConflict("capacity_measurement_changed")
            if observed + bank.reserved_bytes + required > capacity:
                raise RevisionCapacityError("insufficient_publication_capacity")
            bank.reserved_bytes += required
            outbox.payload = {**outbox.payload, "reserved_bytes": required, "reservation_prepared": True}

    async def _stage(self, memory_id: str, operation_id: str, token: str) -> None:
        payload = await self._checkpoint(memory_id, operation_id, token)
        if payload["published"]:
            return
        for item in payload["items"]:
            await self._checkpoint(memory_id, operation_id, token)
            actual = await finish_owned_work(self.storage.read(item["message_id"]))
            if actual is None:
                await finish_owned_work(self.storage.write(item["message"]))
                actual = await finish_owned_work(self.storage.read(item["message_id"]))
            if actual is None:
                raise RevisionConflict("publication_stage_missing")
            verify_approved_message(actual, item["message"], active=False)
        await self._verify_physical_sources(memory_id, payload["manifest"])

    async def _choose_currents(self, memory_id: str, operation_id: str, token: str) -> None:
        async with self._write(operation_id):
            bank, admission, outbox, operation = await self._locked(memory_id, operation_id, token)
            if outbox.payload["published"]:
                return
            payload = deepcopy(outbox.payload)
            binding = ModelBinding(**payload["model_binding"])
            if await self.model_binding(self.db, memory_id, binding.dimension) != binding:
                raise RevisionConflict("publication_model_binding_changed")
            for item in payload["items"]:
                candidate = await self.db.get(MemoryMaintenanceCandidate, item["candidate_id"], populate_existing=True)
                if candidate is None or candidate.revision != item["candidate_revision"] or candidate.state != "publishing":
                    raise RevisionConflict("publication_candidate_changed")
                if item["expected_revision"] is None:
                    if await self.db.get(MemoryEntry, item["entry_id"]) is not None:
                        raise RevisionConflict("publication_entry_collision")
                    entry = MemoryEntry(
                        entry_id=item["entry_id"],
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        legacy_message_id=item["message_id"],
                        current_revision=1,
                        current_message_id=item["message_id"],
                        next_revision=2,
                        generation=1,
                        state="active",
                    )
                    self.db.add(entry)
                    await self.db.flush()
                else:
                    existing = await self.db.get(MemoryEntry, item["entry_id"], populate_existing=True)
                    if existing is None or (existing.current_revision, existing.current_message_id, existing.generation, existing.state) != (
                        item["expected_revision"],
                        item["old_message_id"],
                        item["entry_generation"],
                        "active",
                    ):
                        raise RevisionConflict("publication_target_changed")
                    entry = existing
                    entry.current_revision, entry.current_message_id = item["revision"], item["message_id"]
                    entry.generation += 1
                    entry.updated_at = datetime.now(UTC)
                item["entry_generation"] = entry.generation
                message = item["message"]
                snapshot = {key: deepcopy(value) for key, value in message.items() if key not in {"content", "content_embed"}}
                snapshot.update(model_binding=binding.to_dict(), embedding_dimension=binding.dimension)
                self.db.add(
                    MemoryEntryRevision(
                        entry_id=entry.entry_id,
                        revision=item["revision"],
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        message_id=item["message_id"],
                        parent_revision=item["expected_revision"],
                        content=message["content"],
                        content_sha256=content_hash(message["content"]),
                        origin="consolidated",
                        assessment=dict(ASSESSMENT),
                        message_snapshot=snapshot,
                    )
                )
                await self.db.flush()
                for source in item["sources"]:
                    self.db.add(
                        MemoryRevisionSource(
                            entry_id=entry.entry_id,
                            revision=item["revision"],
                            memory_id=memory_id,
                            source_message_id=source["source_id"],
                            source_revision=source["source_revision"],
                            source_generation=source["source_generation"],
                            content_sha256=source["content_sha256"],
                            relation="context",
                            locator={
                                "kind": "memory_message",
                                "memory_id": memory_id,
                                "message_id": source["source_id"],
                                "scope": deepcopy(source["scope"]),
                                **({"fragment": deepcopy(source["fragment"])} if source.get("fragment") else {}),
                            },
                        )
                    )
                self.db.add(
                    MemoryMutationOperation(
                        memory_id=memory_id,
                        operation_id=item["child_operation_id"],
                        tenant_id=self.tenant_id,
                        actor_id=self.actor_id,
                        kind="dream_publish_item",
                        request_sha256=request_hash({"parent": operation_id, "candidate": item["candidate_id"]}),
                        status="published",
                        index_sync_status="pending",
                        entry_id=entry.entry_id,
                        new_revision=item["revision"],
                        result={"parent_operation_id": operation_id},
                    )
                )
                self.db.add(
                    MemoryPublicationOutbox(
                        id=item["child_outbox_id"],
                        memory_id=memory_id,
                        tenant_id=self.tenant_id,
                        operation_id=item["child_operation_id"],
                        entry_id=entry.entry_id,
                        revision=item["revision"],
                        message_id=item["message_id"],
                        expected_revision=item["expected_revision"],
                        bank_generation=bank.generation,
                        entry_generation=entry.generation,
                        state="sync_pending",
                        attempt=0,
                        payload={
                            "action": "replace",
                            "published": True,
                            "parent_operation_id": operation_id,
                            "parent_outbox_id": outbox.id,
                            "candidate_id": item["candidate_id"],
                            "message": deepcopy(message),
                            "old_message_id": item["old_message_id"],
                            "model_binding": binding.to_dict(),
                            "reserved_bytes": 0,
                        },
                        receipt={"reservation_released": True},
                    )
                )
                candidate.receipt = {**candidate.receipt, "published_entry_id": entry.entry_id, "published_revision": item["revision"], "message_id": item["message_id"]}
            payload.update(items=payload["items"], published=True)
            outbox.payload, outbox.state = payload, "sync_pending"
            admission.publication_state = "ready"
            operation.status, operation.index_sync_status, operation.error_code = "published", "pending", None

    async def _locked_item(self, memory_id: str, operation_id: str, token: str, item: dict[str, Any]) -> tuple[str, MemoryPublicationOutbox, MemoryPublicationOutbox]:
        await self._memory_fence(memory_id)
        source_state = "active"
        for ref in sorted(item["sources"], key=lambda source: source["source_id"]):
            source = await self.db.scalar(
                select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == ref["source_id"])
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                source is None
                or source.tenant_id != self.tenant_id
                or source.state in {"forgotten", "deleted"}
                or (source.source_revision, source.content_sha256, source.scope) != (ref["source_revision"], ref["content_sha256"], ref["scope"])
            ):
                source_state = "stale"
            elif source.state != "active" and source_state != "stale":
                source_state = "pending"
        entry = await self.db.scalar(
            select(MemoryEntry).where(MemoryEntry.entry_id == item["entry_id"], MemoryEntry.memory_id == memory_id).execution_options(populate_existing=True).with_for_update()
        )
        if entry is None or (entry.current_revision, entry.current_message_id) != (item["revision"], item["message_id"]) or entry.state == "withdrawn":
            source_state = "stale"
        elif entry.state != "active" and source_state != "stale":
            source_state = "pending"
        _, _, parent, _ = await self._locked(memory_id, operation_id, token, before_publish=False)
        current = next(value for value in parent.payload["items"] if value["candidate_id"] == item["candidate_id"])
        child = await self.db.get(MemoryPublicationOutbox, item["child_outbox_id"], with_for_update=True, populate_existing=True)
        if child is None:
            raise RevisionConflict("publication_child_missing")
        if child.state in {"redacted", "stale"} or not isinstance(current.get("message", {}).get("content"), str):
            return "stale", parent, child
        if source_state != "active":
            return source_state, parent, child
        return "ready" if child.state == "ready" else "active", parent, child

    async def _item_state(self, memory_id: str, operation_id: str, token: str, item: dict[str, Any]) -> str:
        """Fresh source and current fences after the one SQL publish decision."""
        async with self._write(operation_id):
            state, _, _ = await self._locked_item(memory_id, operation_id, token, item)
            return state

    async def _settle_item(self, memory_id: str, operation_id: str, token: str, item: dict[str, Any]) -> str:
        state = await self._item_state(memory_id, operation_id, token, item)
        if state != "active":
            return state
        await finish_owned_work(self.storage.set_status(item["message_id"], True))
        actual = await finish_owned_work(self.storage.read(item["message_id"]))
        if actual is None:
            raise RevisionConflict("publication_active_projection_missing")
        verify_approved_message(actual, item["message"], active=True)
        if item["old_message_id"] is not None:
            old = await finish_owned_work(self.storage.read(item["old_message_id"]))
            if old is not None:
                await finish_owned_work(self.storage.set_status(item["old_message_id"], False))
                old = await finish_owned_work(self.storage.read(item["old_message_id"]))
                if old is not None and is_active(old):
                    raise RevisionConflict("publication_old_projection_still_active")
        state = await self._item_state(memory_id, operation_id, token, item)
        if state != "active":
            return state
        async with self._write(operation_id):
            state, parent, child = await self._locked_item(memory_id, operation_id, token, item)
            if state != "active":
                return state
            child.state = "ready"
            child.receipt = {**child.receipt, "confirmed_at": datetime.now(UTC).isoformat()}
            child_operation = await self.db.get(MemoryMutationOperation, (memory_id, item["child_operation_id"]))
            assert child_operation is not None
            child_operation.status, child_operation.index_sync_status = "ready", "ready"
            candidate = await self.db.get(MemoryMaintenanceCandidate, item["candidate_id"])
            if candidate is not None and candidate.state == "publishing" and candidate.publication_operation_id == operation_id:
                candidate.state = "published"
            parent.receipt = {**parent.receipt, "ready_candidate_ids": list(dict.fromkeys([*parent.receipt.get("ready_candidate_ids", []), item["candidate_id"]]))}
        return "ready"

    async def _sync(self, memory_id: str, operation_id: str, token: str) -> None:
        payload = await self._checkpoint(memory_id, operation_id, token)
        states = []
        for item in payload["items"]:
            state = await self._settle_item(memory_id, operation_id, token, item)
            states.append({"candidate_id": item["candidate_id"], "state": state})
        async with self._write(operation_id):
            bank, _, outbox, operation = await self._locked(memory_id, operation_id, token, before_publish=False)
            operation.result = {**operation.result, "item_states": states}
            if any(item["state"] == "stale" for item in states):
                operation.status, operation.index_sync_status, operation.error_code = "stale", "unknown", "dream_publication_partial"
                outbox.state = "stale"
            elif any(item["state"] == "pending" for item in states):
                operation.status, operation.index_sync_status, operation.error_code = "published", "pending", "publication_source_or_entry_inactive"
            else:
                operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
                outbox.state = "ready"
            # All local IO has settled and all staged bytes are now physically
            # counted by strict inventory, including inactive withdrawn rows.
            await release_writer_reservation(self.db, memory_id=memory_id, tenant_id=self.tenant_id, outbox=outbox, bank=bank)

    async def _record_error(self, memory_id: str, operation_id: str, code: str) -> None:
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            admission = await self._row(memory_id, operation_id, lock=True)
            outbox = await self._outbox(admission)
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id), populate_existing=True)
            if operation is None or admission.publication_state == "canceled" or outbox.state == "redacted":
                return
            operation.status, operation.index_sync_status, operation.error_code = "persistence_unknown", "unknown", code
            outbox.state, outbox.error_code = "sync_pending" if outbox.payload.get("published") else "unknown", code

    async def _cancel_unpublished(self, memory_id: str, operation_id: str, token: str) -> bool:
        """A canceled job retires its stable stages under the actual attempt."""
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            bank = await self._bank(memory_id)
            admission = await self._row(memory_id, operation_id)
            observed = await self.db.scalar(
                select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == operation_id).execution_options(populate_existing=True)
            )
            if observed is None or observed.payload.get("published"):
                return False
            job = await self._job(memory_id, observed.payload["job_id"], lock=True)
            if job.state == "ready" and job.generation == observed.payload["job_generation"]:
                return False
            admission = await self._row(memory_id, operation_id, lock=True)
            outbox = await self._outbox(admission)
            if admission.active_token != token or outbox.bank_generation != bank.generation or admission.publication_state == "canceled":
                raise RevisionConflict("publication_fence_changed")
            payload = deepcopy(outbox.payload)
            outbox.receipt = {**outbox.receipt, "job_cleanup_requested": True}
        identities = []
        for item in payload["items"]:
            identity = item.get("message_id")
            if identity is None:
                continue
            actual = await finish_owned_work(self.storage.read(identity))
            if actual is not None:
                verify_approved_message(actual, item["message"], active=False)
                identities.append(identity)
        if identities:
            # An unknown delete is never replayed within this attempt. A later
            # explicit reconcile reads these same IDs before deciding to delete.
            await finish_owned_work(self.storage.delete(identities))
        for item in payload["items"]:
            if item.get("message_id") is not None and await finish_owned_work(self.storage.read(item["message_id"])) is not None:
                raise RevisionConflict("publication_stage_cleanup_unconfirmed")
        async with self._write(operation_id):
            await self._memory_fence(memory_id)
            bank = await self._bank(memory_id)
            admission = await self._row(memory_id, operation_id, lock=True)
            outbox = await self._outbox(admission)
            if admission.active_token != token or outbox.payload.get("published"):
                raise RevisionConflict("publication_fence_changed")
            await release_writer_reservation(self.db, memory_id=memory_id, tenant_id=self.tenant_id, outbox=outbox, bank=bank)
            outbox.receipt = {**outbox.receipt, "stage_cleanup_confirmed": True}
            cleaned = deepcopy(outbox.payload)
            cleaned["manifest"]["entries"] = []
            for item in cleaned["items"]:
                item["message"] = {key: value for key, value in item["message"].items() if key not in {"content", "content_embed"}}
            outbox.payload, outbox.state, outbox.error_code = cleaned, "canceled", "maintenance_job_canceled"
            admission.publication_state = "canceled"
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id), populate_existing=True)
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "canceled", "canceled", "maintenance_job_canceled"
        return True

    async def reconcile(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        current = await self.get_operation(memory_id, operation_id)
        if (current["status"], current["index_sync_status"]) in {("ready", "ready"), ("canceled", "canceled")}:
            return current
        if self.storage.memory_id != memory_id or self.storage.tenant_id != self.tenant_id:
            raise RevisionValidationError("publication_storage_binding_mismatch")
        claimed = await self.claim(memory_id, operation_id)
        token, outcome = claimed["active_token"], "drained"
        try:
            if await self._cancel_unpublished(memory_id, operation_id, token):
                pass
            elif not claimed["payload"].get("published"):
                await self._prepare(memory_id, operation_id, token)
                await self._reserve(memory_id, operation_id, token, claimed["memory_config"]["memory_size"])
                await self._stage(memory_id, operation_id, token)
                await self._choose_currents(memory_id, operation_id, token)
                await self._sync(memory_id, operation_id, token)
            else:
                await self._sync(memory_id, operation_id, token)
        except RevisionPersistenceUnknown:
            outcome = "unknown"
            raise
        except asyncio.CancelledError:
            outcome = "unknown"
            await finish_owned_work(self._record_error(memory_id, operation_id, "publication_interrupted"))
            raise
        except (RevisionNotFound, RevisionBusy):
            raise
        except Exception as error:
            outcome = "unknown"
            code = error.code if isinstance(error, (RevisionConflict, RevisionValidationError)) else "publication_io_unconfirmed"
            cleaned = False
            if code == "publication_candidate_changed":
                try:
                    cleaned = await self._cancel_unpublished(memory_id, operation_id, token)
                except RevisionPersistenceUnknown:
                    raise
                except Exception:
                    code = "publication_stage_cleanup_unconfirmed"
            if cleaned:
                outcome = "drained"
            else:
                await self._record_error(memory_id, operation_id, code)
        finally:
            await finish_owned_work(self.finish(memory_id, operation_id, token=token, outcome=outcome))
        return await self.get_operation(memory_id, operation_id)
