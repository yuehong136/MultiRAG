"""Coordinated, snapshot-checked legacy state mutations on unmanaged derived rows."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from common.asyncio_utils import finish_owned_work
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash, message_scope, positive_int, vector_dimension
from memory.writes.admission import WriterCoordinator
from memory.writes.service import verify_approved_message
from memory.writes.types import Authorize, WriteStorage


def _forgotten(value: Any) -> bool:
    return value not in (None, "", "-")


def _matches_state(message: dict[str, Any], *, status: bool, forget_at: Any) -> bool:
    expected_forget = None if not _forgotten(forget_at) else forget_at
    actual_forget = None if not _forgotten(message.get("forget_at")) else message["forget_at"]
    return message.get("status") in ((True, 1, "1") if status else (False, 0, "0")) and actual_forget == expected_forget


def _verify_snapshot(message: dict[str, Any], expected: dict[str, Any]) -> None:
    # Status/forget are compared separately against the original OR exact target.
    verify_approved_message({**message, "status": False, "forget_at": None}, {**expected, "status": False, "forget_at": None}, active=False)


class LegacyStateService(WriterCoordinator):
    def __init__(self, db: AsyncSession, *, tenant_id: str, actor_id: str, authorize: Authorize, storage: WriteStorage) -> None:
        super().__init__(db, tenant_id=tenant_id, actor_id=actor_id, authorize=authorize)
        self.storage = storage

    async def _memory_fence(self, memory_id: str) -> tuple[Any, MemoryWriterFence]:
        memory, fence = await super()._memory_fence(memory_id)
        if await self.db.scalar(select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == memory_id)) is not None:
            raise RevisionConflict("managed_memory_requires_source_service")
        return memory, fence

    async def _configure_admission(self, row: MemoryWriterAdmission, fence: MemoryWriterFence, request: dict[str, Any]) -> None:
        target = positive_int(request.get("message_id"), "invalid_message_id")
        await self._require_ready_producer(row.memory_id, target)
        heads = {**fence.receipt.get("legacy_state_heads", {}), str(target): row.operation_id}
        if len(heads) > 10000:
            raise RevisionConflict("legacy_state_inventory_incomplete")
        fence.receipt = {**fence.receipt, "legacy_state_heads": heads}
        row.request_metadata = {"target_message_id": target}

    async def _require_ready_producer(self, memory_id: str, message_id: int) -> None:
        rows = list((await self.db.scalars(select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == memory_id, MemoryWriterAdmission.tenant_id == self.tenant_id).limit(10001))).all())
        if len(rows) > 10000:
            raise RevisionConflict("legacy_state_inventory_incomplete")
        owners = [row for row in rows if message_id in row.message_ids]
        if len(owners) > 1:
            raise RevisionConflict("legacy_state_producer_ambiguous")
        if owners and owners[0].publication_state != "ready":
            raise RevisionConflict("producer_publication_not_ready")

    async def change(self, memory_id: str, message_id: int, *, operation_id: str, status: bool | None = None, forget: bool = False) -> dict[str, Any]:
        positive_int(message_id, "invalid_message_id")
        if (status is None) == (forget is False) or (status is not None and type(status) is not bool) or type(forget) is not bool:
            raise RevisionValidationError("one_physical_state_change_required")
        await self._idle()
        try:
            await self.authorize(self.db, memory_id)
        finally:
            await self.db.rollback()
        # Read-only preflight does not grant a writer. The transaction below
        # rechecks Memory/managed mode before any mutation can begin.
        observed = await finish_owned_work(self.storage.read(message_id))
        if observed is None:
            raise RevisionNotFound("message_not_found")
        if observed.get("message_type") == "raw":
            raise RevisionConflict("source_adoption_required")
        message_scope(observed, memory_id)
        anchor = observed.get("source_id")
        source_id = anchor if type(anchor) is int and 0 < anchor <= 2**63 - 1 else None
        await self.admit(
            memory_id,
            operation_id=operation_id,
            kind="legacy_state",
            request={"message_id": message_id, "status": status, "forget": forget},
            source_id=source_id,
            server_metadata={"forget_at": datetime.now(UTC).isoformat(timespec="seconds")},
        )
        return await self.reconcile(memory_id, operation_id)

    async def get_operation(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        operation = await self.operation(memory_id, operation_id)
        if operation["kind"] != "legacy_state":
            raise RevisionNotFound("legacy_state_operation_not_found")
        return operation

    async def _checkpoint(self, memory_id: str, operation_id: str, token: str) -> dict[str, Any]:
        await self._idle()
        try:
            _, fence = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id)
            if row.active_token != token or row.writer_generation != fence.generation or row.publication_state == "canceled":
                raise RevisionConflict("legacy_state_fence_changed")
            outbox = await self._outbox(row)
            target = outbox.payload["request"]["message_id"]
            await self._require_ready_producer(memory_id, target)
            if fence.receipt.get("legacy_state_heads", {}).get(str(target)) != operation_id:
                raise RevisionConflict("legacy_state_superseded")
            return await self._view(row)
        finally:
            await self.db.rollback()

    async def _approve(self, memory_id: str, operation_id: str, token: str) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        if snapshot["payload"].get("approved"):
            return
        request = snapshot["payload"]["request"]
        observed = await finish_owned_work(self.storage.read(request["message_id"]))
        if observed is None:
            raise RevisionNotFound("message_not_found")
        if observed.get("message_type") == "raw":
            raise RevisionConflict("source_adoption_required")
        message_scope(observed, memory_id)
        if _forgotten(observed.get("forget_at")):
            raise RevisionConflict("message_permanently_forgotten")
        vector_dimension(observed)
        observed = deepcopy(observed)
        if hasattr(observed["content_embed"], "tolist"):
            observed["content_embed"] = observed["content_embed"].tolist()
        async with self._write(operation_id):
            _, fence = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state == "canceled" or fence.receipt.get("legacy_state_heads", {}).get(str(request["message_id"])) != operation_id:
                raise RevisionConflict("legacy_state_fence_changed")
            outbox = await self._outbox(row)
            await self._require_ready_producer(memory_id, request["message_id"])
            target_status = False if request["forget"] else request["status"]
            target_forget = outbox.payload["server_metadata"]["forget_at"] if request["forget"] else observed.get("forget_at")
            outbox.payload = {**outbox.payload, "approved": True, "preimage": observed, "target_status": target_status, "target_forget_at": target_forget}

    async def _publish(self, memory_id: str, operation_id: str, token: str) -> None:
        snapshot = await self._checkpoint(memory_id, operation_id, token)
        payload = snapshot["payload"]
        identity = payload["request"]["message_id"]
        expected = payload["preimage"]
        actual = await finish_owned_work(self.storage.read(identity))
        if actual is None:
            raise RevisionConflict("legacy_state_target_missing")
        _verify_snapshot(actual, expected)
        target_status, target_forget = payload["target_status"], payload["target_forget_at"]
        if not _matches_state(actual, status=target_status, forget_at=target_forget):
            if not _matches_state(actual, status=expected.get("status") in (True, 1, "1"), forget_at=expected.get("forget_at")):
                raise RevisionConflict("legacy_state_target_changed")
            await self._checkpoint(memory_id, operation_id, token)
            if payload["request"]["forget"]:
                await finish_owned_work(self.storage.write({**expected, "status": False, "forget_at": target_forget}))
            else:
                await finish_owned_work(self.storage.set_status(identity, target_status))
        actual = await finish_owned_work(self.storage.read(identity))
        if actual is None:
            raise RevisionConflict("legacy_state_target_missing")
        _verify_snapshot(actual, expected)
        if not _matches_state(actual, status=target_status, forget_at=target_forget):
            raise RevisionConflict("legacy_state_readback_mismatch")
        async with self._write(operation_id):
            _, fence = await self._memory_fence(memory_id)
            row = await self._row(memory_id, operation_id, lock=True)
            if row.active_token != token or row.publication_state == "canceled" or fence.receipt.get("legacy_state_heads", {}).get(str(identity)) != operation_id:
                raise RevisionConflict("legacy_state_fence_changed")
            outbox = await self._outbox(row)
            await self._require_ready_producer(memory_id, identity)
            row.publication_state, outbox.state = "ready", "ready"
            outbox.payload = {key: value for key, value in outbox.payload.items() if key != "preimage"}
            outbox.receipt = {**outbox.receipt, "content_sha256": content_hash(expected["content"]), "dimension": vector_dimension(expected), "reservation_released": True}
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "ready", "ready", None
            operation.result = {
                "message_id": identity,
                "source_id": row.source_message_id,
                "message_status": target_status,
                "forget_at": target_forget,
                "stop_supply": "forgotten" if payload["request"]["forget"] else "active" if target_status else "inactive",
                "physical_cleanup_complete": False,
            }

    async def _error(self, memory_id: str, operation_id: str, code: str, *, unknown: bool) -> None:
        async with self._write(operation_id):
            row = await self._row(memory_id, operation_id, lock=True)
            if row.publication_state in {"ready", "canceled"}:
                return
            operation = await self.db.get(MemoryMutationOperation, (memory_id, operation_id))
            assert operation is not None
            operation.status, operation.index_sync_status, operation.error_code = "persistence_unknown" if unknown else "failed", "unknown" if unknown else "failed", code

    async def reconcile(self, memory_id: str, operation_id: str) -> dict[str, Any]:
        operation = await self.get_operation(memory_id, operation_id)
        if operation["index_sync_status"] == "ready":
            return operation
        claimed = await self.claim(memory_id, operation_id)
        token = claimed["active_token"]
        try:
            await self._approve(memory_id, operation_id, token)
            await self._publish(memory_id, operation_id, token)
            return await self.get_operation(memory_id, operation_id)
        except asyncio.CancelledError:
            await finish_owned_work(self._error(memory_id, operation_id, "legacy_state_interrupted", unknown=True))
            raise
        except Exception as error:
            known = isinstance(error, (RevisionConflict, RevisionValidationError, RevisionNotFound))
            code = error.code if isinstance(error, (RevisionConflict, RevisionValidationError, RevisionNotFound)) else "legacy_state_io_unconfirmed"
            await self._error(memory_id, operation_id, code, unknown=not known)
            if known:
                raise
            return await self.get_operation(memory_id, operation_id)
        finally:
            await finish_owned_work(self.finish(memory_id, operation_id, token=token))
