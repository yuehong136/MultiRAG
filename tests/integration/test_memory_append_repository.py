"""Real SQL admission/publication decisions with controlled provider and vector IO."""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory, Task
from api.db.memory_revision_models import ManagedMemoryState, MemoryPublicationOutbox, MemorySourceState
from api.db.memory_writer_models import MEMORY_WRITER_MODELS, MemoryWriterAdmission, MemoryWriterFence
from memory.revisions.domain import RevisionBusy, RevisionCapacityError, RevisionConflict, RevisionStorageUnknown
from memory.revisions.service import RevisionService
from memory.revisions.sources import SourceService
from memory.writes.admission import acknowledge_legacy_quiescence, close_writer_admissions, writer_drain_snapshot
from memory.writes.legacy_state import LegacyStateService
from memory.writes.service import AppendService
from tests.support.memory_append_repository import ControlledStore, _service
from tests.support.memory_append_repository import environment as environment


async def _append(env: dict[str, Any], operation_id: str = "append", *, content: str = "raw source", valid_at: str = "2026-11-15 10:00:00", enqueue: bool = True) -> dict[str, Any]:
    async with env["factory"]() as db:
        return await _service(db, env).append_raw(
            "bank", operation_id=operation_id, content=content, scope={"user_id": "", "agent_id": "agent", "session_id": "session"}, valid_at=valid_at, enqueue_extraction=enqueue
        )


async def test_raw_and_extraction_replay_use_fixed_ids_and_approved_provider_results(environment: dict[str, Any]) -> None:
    env = environment
    first = await _append(env)
    repeated = await _append(env, valid_at="2026-11-15 12:00:00")
    assert first == repeated and env["embeddings"] == 1 and env["writes"] == 1 and len(env["queue"]) == 1
    task_id = first["extraction_task_id"]
    async with env["factory"]() as db:
        service = _service(db, env)
        result = await service.extract_source("bank", operation_id=task_id, task_id=task_id, source_id=first["raw_source_id"])
        assert await service.extract_source("bank", operation_id=task_id, task_id=task_id, source_id=first["raw_source_id"]) == result
    assert env["extractions"] == 1 and env["embeddings"] == 2 and env["writes"] == 2
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(Task)) == 1
        assert await reader.scalar(sa.select(Task.progress)) == 1.0
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        assert all(row.publication_state == "ready" and row.active_token is None for row in (await reader.scalars(sa.select(MemoryWriterAdmission))).all())


async def test_unknown_physical_write_reconciles_the_existing_fixed_plan(environment: dict[str, Any]) -> None:
    env = environment
    env["lose_write"] = True
    unknown = await _append(env, enqueue=False)
    assert unknown["status"] == "persistence_unknown" and len(env["rows"]) == 1
    assert next(iter(env["rows"].values()))["status"] is False
    async with env["factory"]() as db:
        ready = await _service(db, env).reconcile("bank", "append")
    assert ready["status"] == "ready" and ready["message_ids"] == unknown["message_ids"]
    assert env["embeddings"] == 1 and env["writes"] == 1


async def test_queue_can_consume_child_immediately_while_parent_notification_is_active(environment: dict[str, Any]) -> None:
    environment["immediate_queue"] = True
    receipt = await _append(environment)
    assert receipt["queue_state"] == "submitted" and environment["extractions"] == 1 and len(environment["rows"]) == 2


async def test_source_withdrawal_during_llm_cancels_plan_before_derived_embedding(environment: dict[str, Any]) -> None:
    env = environment
    raw = await _append(env)
    env["extract_started"], env["extract_release"] = asyncio.Event(), asyncio.Event()

    async def extract() -> dict[str, Any]:
        async with env["factory"]() as db:
            return await _service(db, env).extract_source("bank", operation_id=raw["extraction_task_id"], task_id=raw["extraction_task_id"], source_id=raw["raw_source_id"])

    task = asyncio.create_task(extract())
    try:
        await asyncio.wait_for(env["extract_started"].wait(), timeout=5)
        async with env["factory"]() as db:

            async def authorize(session: AsyncSession, memory_id: str) -> None:
                assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

            store = ControlledStore(env, db)

            async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
                return await store.read(message_id)

            source = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=authorize, message_reader=read)
            await source.withdraw_source("bank", raw["raw_source_id"], operation_id="withdraw", expected_source_revision=1, reason="source withdrawn")
        env["extract_release"].set()
        with pytest.raises(RevisionConflict):
            await task
    finally:
        env["extract_release"].set()
        await asyncio.gather(task, return_exceptions=True)
    assert env["embeddings"] == 1 and env["writes"] == 1
    async with env["factory"]() as reader:
        state = await reader.get(MemorySourceState, ("bank", raw["raw_source_id"]))
        assert state is not None and state.state == "forgotten"
        child = await reader.get(MemoryWriterAdmission, ("bank", raw["extraction_task_id"]))
        assert child is not None and child.publication_state == "canceled" and child.active_token is None and not child.message_ids


@pytest.mark.parametrize("managed", [True, False])
async def test_fifo_evicts_complete_source_group_without_implicitly_managing_bank(environment: dict[str, Any], managed: bool) -> None:
    env = environment
    if not managed:
        async with env["factory"]() as db, db.begin():
            await db.execute(sa.delete(ManagedMemoryState))
    old = await _append(env, "old", content="old source " * 20, enqueue=False)
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=4300, forgetting_policy="FIFO"))
    new = await _append(env, "new", content="new source", valid_at="2026-11-15 12:00:00", enqueue=False)
    assert new["status"] == "ready" and [item["source_id"] for item in new["evicted_sources"]] == [old["raw_source_id"]]
    assert old["raw_source_id"] not in env["rows"] and new["raw_source_id"] in env["rows"]
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(ManagedMemoryState)) == int(managed)
        fence = await reader.get(MemoryWriterFence, "bank")
        assert fence is not None and fence.reserved_bytes == 0


async def test_closing_fence_cancels_queued_task_without_guessing_its_progress(environment: dict[str, Any]) -> None:
    env = environment
    raw = await _append(env)
    async with env["factory"]() as db, db.begin():
        await db.scalar(sa.select(Memory.id).where(Memory.id == "bank").with_for_update())
        snapshot = await close_writer_admissions(db, memory_id="bank", tenant_id="tenant", operation_id="delete")
        assert snapshot["drained"] and not snapshot["untracked_task_ids"]
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="writer_closed"):
            await _service(db, env).extract_source("bank", operation_id=raw["extraction_task_id"], task_id=raw["extraction_task_id"], source_id=raw["raw_source_id"])
        observed = await writer_drain_snapshot(db, memory_id="bank", tenant_id="tenant")
        assert observed["drained"] and not observed["active_operations"]


async def test_approved_plan_sql_commit_lost_ack_keeps_vectors_ids_and_one_task(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    async with env["factory"]() as db:
        original = db.commit
        lost = False

        async def commit() -> None:
            nonlocal lost
            payload = (
                await db.scalar(sa.select(MemoryPublicationOutbox.payload).where(MemoryPublicationOutbox.memory_id == "bank", MemoryPublicationOutbox.operation_id == "append")) if not lost else None
            )
            lose_ack = not lost and payload is not None and payload.get("approved") is True
            await original()
            if lose_ack:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        service = _service(db, env)
        unknown = await service.append_raw("bank", operation_id="append", content="raw source", scope={"user_id": "", "agent_id": "agent", "session_id": "session"}, valid_at="2026-11-15 10:00:00")
        assert lost and unknown["status"] == "persistence_unknown" and unknown["message_ids"]
        assert env["embeddings"] == 1 and env["writes"] == 0
        ready = await service.reconcile("bank", "append")
        assert ready["status"] == "ready" and ready["message_ids"] == unknown["message_ids"]
        assert ready["extraction_task_id"] == unknown["extraction_task_id"] and env["embeddings"] == 1
        monkeypatch.setattr(db, "commit", original)
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(Task)) == 1
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission)) == 2


@pytest.mark.parametrize("phase", ["ready", "notification"])
async def test_ready_or_notification_commit_lost_ack_recovers_without_repeating_effects(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    from api.db.memory_revision_models import MemoryMutationOperation

    env = environment
    async with env["factory"]() as db:
        original = db.commit
        lost = False

        async def commit() -> None:
            nonlocal lost
            if phase == "ready":
                marker = await db.scalar(sa.select(MemoryWriterAdmission.publication_state).where(MemoryWriterAdmission.memory_id == "bank", MemoryWriterAdmission.operation_id == "append")) == "ready"
            else:
                result = await db.scalar(sa.select(MemoryMutationOperation.result).where(MemoryMutationOperation.memory_id == "bank", MemoryMutationOperation.operation_id == "append"))
                marker = result is not None and result.get("queue_state") == "submitted"
            lose_ack = not lost and marker
            await original()
            if lose_ack:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        service = _service(db, env)
        unknown = await service.append_raw("bank", operation_id="append", content="raw source", scope={"user_id": "", "agent_id": "agent", "session_id": "session"}, valid_at="2026-11-15 10:00:00")
        assert lost and unknown["status"] == "persistence_unknown" and env["writes"] == 1
        ready = await service.reconcile("bank", "append")
        assert ready["status"] == "ready" and ready["index_sync_status"] == "ready" and ready["queue_state"] == "submitted"
        assert ready["message_ids"] == unknown["message_ids"] and env["embeddings"] == 1 and env["writes"] == 1 and len(env["queue"]) == 1
        monkeypatch.setattr(db, "commit", original)


async def test_failed_new_write_keeps_the_actual_fifo_eviction_receipt(environment: dict[str, Any]) -> None:
    env = environment
    old = await _append(env, "old", content="old source " * 20, enqueue=False)
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=4300, forgetting_policy="FIFO"))
    env["lose_write"] = True
    result = await _append(env, "new", content="new source", valid_at="2026-11-15 12:00:00", enqueue=False)
    assert result["status"] == "persistence_unknown"
    assert result["evicted_sources"] == [{"source_id": old["raw_source_id"], "operation_id": result["evicted_sources"][0]["operation_id"], "state": "ready"}]
    assert old["raw_source_id"] not in env["rows"]
    async with env["factory"]() as db:
        recovered = await _service(db, env).reconcile("bank", "new")
        assert recovered["status"] == "ready" and recovered["message_ids"] == result["message_ids"] and recovered["evicted_sources"] == result["evicted_sources"]


async def test_fifo_parent_commit_lost_ack_replays_the_original_child_without_source_body(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.memory_revision_models import MemoryMutationOperation

    env = environment
    old = await _append(env, "old", content="old source " * 20, enqueue=False)
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=4300, forgetting_policy="FIFO"))
    async with env["factory"]() as db:
        original, lost = db.commit, False

        async def commit() -> None:
            nonlocal lost
            result = await db.scalar(sa.select(MemoryMutationOperation.result).where(MemoryMutationOperation.memory_id == "bank", MemoryMutationOperation.operation_id == "new"))
            marker = not lost and result is not None and bool(result.get("evicted_sources"))
            await original()
            if marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        service = _service(db, env)
        unknown = await service.append_raw(
            "bank", operation_id="new", content="new source", scope={"user_id": "", "agent_id": "agent", "session_id": "session"}, valid_at="2026-11-15 12:00:00", enqueue_extraction=False
        )
        assert lost and unknown["status"] == "persistence_unknown" and old["raw_source_id"] not in env["rows"]
        child_ids = [item["operation_id"] for item in unknown["evicted_sources"]]
        ready = await service.reconcile("bank", "new")
        assert ready["status"] == "ready" and ready["evicted_sources"] == unknown["evicted_sources"]
        snapshot = await service.snapshot("bank", "new")
        assert [item["operation_id"] for item in snapshot["payload"]["eviction_plan"]] == child_ids
        assert all("content" not in item for item in snapshot["payload"]["eviction_plan"])
        assert env["embeddings"] == 2 and len(env["rows"]) == 1
        monkeypatch.setattr(db, "commit", original)


async def test_capacity_rejection_finishes_actual_attempt_and_does_not_block_next_writer(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=1000, forgetting_policy="NONE"))
    with pytest.raises(RevisionCapacityError, match="insufficient_append_capacity"):
        await _append(env, "too-large", enqueue=False)
    async with env["factory"]() as reader:
        row = await reader.get(MemoryWriterAdmission, ("bank", "too-large"))
        assert row is not None and row.active_token is None and row.state == "drained"
        result = await _service(reader, env).operation("bank", "too-large")
        assert result["status"] == "failed" and result["index_sync_status"] == "failed"
    assert not env["rows"]
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=10000))
    assert (await _append(env, "next", enqueue=False))["status"] == "ready"


async def test_allocator_floor_includes_sql_tombstones_after_physical_rows_are_gone(environment: dict[str, Any]) -> None:
    env = environment
    old_id = 2**53 + 101
    async with env["factory"]() as db, db.begin():
        db.add(
            MemorySourceState(
                memory_id="bank",
                source_message_id=old_id,
                tenant_id="tenant",
                source_revision=1,
                generation=2,
                state="forgotten",
                content_sha256="0" * 64,
                scope={"user_id": "", "agent_id": "agent", "session_id": "session"},
            )
        )
    result = await _append(env, enqueue=False)
    assert result["raw_source_id"] > old_id and len(env["rows"]) == 1
    async with env["factory"]() as reader:
        assert (await reader.get(MemorySourceState, ("bank", old_id))).state == "forgotten"


async def test_first_adoption_waits_for_an_admitted_unmanaged_writer(environment: dict[str, Any]) -> None:
    from memory.writes.admission import WriterCoordinator

    env = environment
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.delete(ManagedMemoryState))
    env["rows"][10] = {
        "memory_id": "bank",
        "message_id": 10,
        "id": "bank_10",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "",
        "agent_id": "agent",
        "session_id": "session",
        "content": "old source",
        "content_embed": [0.1, 0.2],
        "status": True,
        "forget_at": None,
    }
    env["rows"][11] = {**env["rows"][10], "message_id": 11, "id": "bank_11", "message_type": "semantic", "source_id": 10, "content": "old fact"}
    async with env["factory"]() as db:
        append = _service(db, env)
        writers = WriterCoordinator(db, tenant_id="tenant", actor_id="actor", authorize=append.authorize)
        await writers.admit("bank", operation_id="legacy", kind="legacy_status", request={"message_id": 10, "status": False})
        claimed = await writers.claim("bank", "legacy")
    async with env["factory"]() as db:
        append = _service(db, env)
        store = ControlledStore(env, db)

        async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
            return await store.read(message_id)

        async def measure(memory_id: str) -> int:
            return sum(AppendService._message_bytes(row) for row in await store.inventory())

        revisions = RevisionService(
            db,
            tenant_id="tenant",
            actor_id="actor",
            authorize=append.authorize,
            message_reader=read,
            allocate_message_id=append.allocate_message_id,
            count_tokens=len,
            model_binding=append.model_binding,
            measure_memory_bytes=measure,
        )
        with pytest.raises(RevisionBusy, match="drain_before_activation"):
            await revisions.adopt_entry("bank", operation_id="adopt", legacy_message_id=11)
    async with env["factory"]() as db:
        append = _service(db, env)
        await WriterCoordinator(db, tenant_id="tenant", actor_id="actor", authorize=append.authorize).finish("bank", "legacy", token=claimed["active_token"])
        store = ControlledStore(env, db)

        async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
            return await store.read(message_id)

        async def measure(memory_id: str) -> int:
            return sum(AppendService._message_bytes(row) for row in await store.inventory())

        revisions = RevisionService(
            db,
            tenant_id="tenant",
            actor_id="actor",
            authorize=append.authorize,
            message_reader=read,
            allocate_message_id=append.allocate_message_id,
            count_tokens=len,
            model_binding=append.model_binding,
            measure_memory_bytes=measure,
        )
        assert (await revisions.adopt_entry("bank", operation_id="adopt", legacy_message_id=11))["status"] == "ready"


async def _legacy_rows(env: dict[str, Any]) -> None:
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.delete(ManagedMemoryState))
    env["rows"][10] = {
        "memory_id": "bank",
        "message_id": 10,
        "id": "bank_10",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "",
        "agent_id": "agent",
        "session_id": "session",
        "content": "old source",
        "content_embed": [0.1, 0.2],
        "status": True,
        "forget_at": None,
        "valid_at": None,
        "invalid_at": None,
    }
    env["rows"][11] = {**env["rows"][10], "message_id": 11, "id": "bank_11", "message_type": "semantic", "source_id": 10, "content": "old fact"}


def _legacy_service(db: AsyncSession, env: dict[str, Any]) -> LegacyStateService:
    return LegacyStateService(db, tenant_id="tenant", actor_id="actor", authorize=_service(db, env).authorize, storage=ControlledStore(env, db))


async def test_legacy_raw_mutations_require_explicit_source_adoption_before_admission(environment: dict[str, Any]) -> None:
    env = environment
    await _legacy_rows(env)
    async with env["factory"]() as db:
        service = _legacy_service(db, env)
        for action in ({"status": False}, {"forget": True}):
            with pytest.raises(RevisionConflict, match="source_adoption_required"):
                await service.change("bank", 10, operation_id="raw-guard", **action)
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission)) == 0
        assert await db.get(MemoryWriterFence, "bank") is None
    assert env["rows"][10]["status"] is True and env["writes"] == 0


@pytest.mark.parametrize("phase", ["physical", "ready"])
async def test_legacy_state_unknown_replays_the_fixed_target_without_duplicate_effect(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    env = environment
    await _legacy_rows(env)
    async with env["factory"]() as db:
        service = _legacy_service(db, env)
        original_commit, original_status = db.commit, service.storage.set_status
        lost, calls = False, 0

        async def set_status(identity: int, status: bool) -> None:
            nonlocal lost, calls
            calls += 1
            await original_status(identity, status)
            if phase == "physical" and not lost:
                lost = True
                raise RevisionStorageUnknown("private backend detail")

        async def commit() -> None:
            nonlocal lost
            marker = await db.scalar(sa.select(MemoryWriterAdmission.publication_state).where(MemoryWriterAdmission.memory_id == "bank", MemoryWriterAdmission.operation_id == "legacy")) == "ready"
            await original_commit()
            if phase == "ready" and not lost and marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("private lost ack"), connection_invalidated=True)

        monkeypatch.setattr(service.storage, "set_status", set_status)
        monkeypatch.setattr(db, "commit", commit)
        first = await service.change("bank", 11, operation_id="legacy", status=False)
        assert lost and first["status"] in {"ready", "persistence_unknown"}
        ready = await service.reconcile("bank", "legacy")
        assert ready["status"] == "ready" and ready["message_status"] is False and ready["index_sync_status"] == "ready" and ready["stop_supply"] == "inactive"
        assert calls == 1 and env["rows"][11]["status"] is False
        assert (await service.change("bank", 11, operation_id="legacy", status=False)) == ready
        forgotten = await service.change("bank", 11, operation_id="forget", forget=True)
        assert forgotten["stop_supply"] == "forgotten" and forgotten["forget_at"] == env["rows"][11]["forget_at"]
        assert (await service.reconcile("bank", "forget"))["forget_at"] == forgotten["forget_at"]
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await service.change("bank", 11, operation_id="legacy", status=True)
        monkeypatch.setattr(db, "commit", original_commit)
    assert env["rows"][10]["status"] is True


async def test_later_legacy_state_command_fences_an_older_unknown_operation(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    await _legacy_rows(env)
    async with env["factory"]() as db:
        service = _legacy_service(db, env)
        original = service.storage.set_status

        async def lost_ack(identity: int, status: bool) -> None:
            await original(identity, status)
            raise RevisionStorageUnknown("lost acknowledgement")

        monkeypatch.setattr(service.storage, "set_status", lost_ack)
        assert (await service.change("bank", 11, operation_id="disable", status=False))["index_sync_status"] == "unknown"
        monkeypatch.setattr(service.storage, "set_status", original)
        assert (await service.change("bank", 11, operation_id="enable", status=True))["index_sync_status"] == "ready"
        with pytest.raises(RevisionConflict, match="legacy_state_superseded"):
            await service.reconcile("bank", "disable")
        assert env["rows"][11]["status"] is True


async def test_legacy_state_rejects_a_pending_producer_and_a_managed_cutover(environment: dict[str, Any]) -> None:
    from memory.writes.admission import WriterCoordinator

    env = environment
    await _legacy_rows(env)
    async with env["factory"]() as db:
        writers = WriterCoordinator(db, tenant_id="tenant", actor_id="actor", authorize=_service(db, env).authorize)
        await writers.admit("bank", operation_id="producer", kind="extract", request={})
    async with env["factory"]() as db, db.begin():
        producer = await db.get(MemoryWriterAdmission, ("bank", "producer"))
        producer.message_ids = [11]
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="producer_publication_not_ready"):
            await _legacy_service(db, env).change("bank", 11, operation_id="disable", status=False)
        assert await db.get(MemoryWriterAdmission, ("bank", "disable")) is None
    async with env["factory"]() as db, db.begin():
        db.add(ManagedMemoryState(memory_id="bank", tenant_id="tenant", generation=1, capacity_generation=1, state="active", reserved_bytes=0))
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="managed_memory_requires_source_service"):
            await _legacy_service(db, env).change("bank", 11, operation_id="disable", status=False)
    assert env["rows"][11]["status"] is True


async def test_operator_attestation_covers_only_the_locked_legacy_task_set(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db, db.begin():
        db.add_all([Task(id="legacy-one", doc_id="bank", task_type="memory", digest="100", progress=1.0), Task(id="legacy-two", doc_id="bank", task_type="memory", digest="101", progress=-1.0)])
    async with env["factory"]() as db, db.begin():
        await db.scalar(sa.select(Memory.id).where(Memory.id == "bank").with_for_update())
        before = await close_writer_admissions(db, memory_id="bank", tenant_id="tenant", operation_id="delete")
        assert not before["drained"] and before["untracked_task_ids"] == ["legacy-one", "legacy-two"]
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="generation_changed"):
            await acknowledge_legacy_quiescence(
                db,
                memory_id="bank",
                tenant_id="tenant",
                expected_generation=before["generation"] - 1,
                expected_untracked_task_ids_sha256=before["untracked_task_ids_sha256"],
                evidence_reference="test://owned-worker-stop",
            )
        with pytest.raises(RevisionConflict, match="task_set_changed"):
            await acknowledge_legacy_quiescence(
                db, memory_id="bank", tenant_id="tenant", expected_generation=before["generation"], expected_untracked_task_ids_sha256="0" * 64, evidence_reference="test://owned-worker-stop"
            )
        confirmed = await acknowledge_legacy_quiescence(
            db,
            memory_id="bank",
            tenant_id="tenant",
            expected_generation=before["generation"],
            expected_untracked_task_ids_sha256=before["untracked_task_ids_sha256"],
            evidence_reference="test://owned-worker-stop",
        )
        assert confirmed["drained"] and confirmed["legacy_quiescence_attested"]
        assert confirmed["legacy_quiescence_attestation"]["verification"] == "operator_attestation"
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Task).where(Task.id == "legacy-one").values(digest="changed-source"))
        db.add(Task(id="legacy-new", doc_id="bank", task_type="memory", digest="102", progress=1.0))
    async with env["factory"]() as reader:
        changed = await writer_drain_snapshot(reader, memory_id="bank", tenant_id="tenant")
        assert not changed["drained"] and changed["untracked_task_ids"] == ["legacy-new", "legacy-one"]


def _migration() -> Any:
    path = Path(__file__).parents[2] / "configs/alembic/versions/f2b4d6e8a0c2_add_memory_writer_coordination.py"
    spec = importlib.util.spec_from_file_location("tested_writer_migration", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


async def test_writer_migration_upgrades_existing_bank_and_refuses_ownership_loss(environment: dict[str, Any]) -> None:
    engine = environment["engine"]
    migration = _migration()
    with engine.begin() as connection:
        for model in reversed(MEMORY_WRITER_MODELS):
            model.__table__.drop(connection)
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_memory_managed_states DROP COLUMN delete_permissions, DROP COLUMN delete_operation_id"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            connection.execute(MemoryWriterFence.__table__.insert().values(memory_id="bank", tenant_id="tenant", generation=4, state="open", receipt={}))
            migration.upgrade()
    with engine.connect() as reader:
        assert reader.scalar(sa.select(MemoryWriterFence.generation)) == 4
        assert reader.scalar(sa.select(ManagedMemoryState.generation)) == 1
        columns = {row["name"] for row in sa.inspect(reader).get_columns(ManagedMemoryState.__tablename__, schema="usr_ai")}
        assert {"delete_permissions", "delete_operation_id"} <= columns
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="Cannot discard"):
            migration.downgrade()
