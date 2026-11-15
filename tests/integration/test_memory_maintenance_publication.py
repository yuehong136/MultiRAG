"""One SQL batch decision with controlled storage and independently read SQL."""

from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance.domain import ASSESSMENT, assemble_candidates
from memory.maintenance.jobs import JobService
from memory.maintenance.lifecycle import invalidate_for_source
from memory.maintenance.publication import PublicationService
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, content_hash
from memory.writes.admission import invalidate_source_writers
from tests.support.database import scratch_database

BINDING = ModelBinding(None, "controlled", 2, "controlled-config")
SELECTIONS = [{"candidate_id": "add", "expected_revision": 2}, {"candidate_id": "patch", "expected_revision": 2}]


def message(identity: int, content: str, *, source: int = 0, session: str = "one") -> dict[str, Any]:
    return {
        "id": f"bank_{identity}",
        "memory_id": "bank",
        "message_id": identity,
        "message_type": "raw" if source == 0 else "semantic",
        "source_id": source,
        "user_id": "user",
        "agent_id": "agent",
        "session_id": session,
        "content": content,
        "content_embed": [1.0, 0.0],
        "status": True,
        "valid_at": "2026-11-15 00:00:00",
        "invalid_at": None,
        "forget_at": None,
    }


class Store:
    tenant_id = "tenant"
    memory_id = "bank"

    def __init__(self, db: AsyncSession, env: dict[str, Any]) -> None:
        self.db, self.env = db, env

    async def read(self, identity: int) -> dict[str, Any] | None:
        assert not self.db.in_transaction()
        return deepcopy(self.env["rows"].get(identity))

    async def inventory(self) -> list[dict[str, Any]]:
        assert not self.db.in_transaction()
        return deepcopy(list(self.env["rows"].values()))

    async def write(self, row: dict[str, Any]) -> None:
        assert not self.db.in_transaction() and row["status"] is False
        self.env["writes"].append(row["message_id"])
        self.env["rows"][row["message_id"]] = deepcopy(row)
        hook = self.env.pop("write_hook", None)
        if hook is not None:
            await hook()
        if self.env.get("fail_stage") == len(self.env["writes"]):
            self.env["fail_stage"] = None
            raise OSError("controlled lost storage ACK")

    async def set_status(self, identity: int, status: bool) -> None:
        assert not self.db.in_transaction()
        self.env["statuses"].append((identity, status))
        self.env["rows"][identity]["status"] = status
        hook = self.env.pop("status_hook", None)
        if hook is not None:
            try:
                await hook(identity, status)
            except Exception as error:
                self.env["hook_failure"] = repr(error)
                raise

    async def delete(self, ids: list[int]) -> None:
        assert not self.db.in_transaction()
        self.env.setdefault("deletes", []).append(ids)
        for identity in ids:
            self.env["rows"].pop(identity, None)
        if self.env.pop("fail_delete_ack", False):
            raise OSError("controlled lost delete ACK")


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    with scratch_database(tmp_path / "maintenance-publication") as engine:
        asynchronous = create_async_engine(engine.url)
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)
        rows = {1: message(1, "source one"), 2: message(2, "old fact. sibling stays.", source=1), 5: message(5, "source five", session="two")}
        inputs = [
            {
                "input_id": f"source-{identity}",
                "source_id": identity,
                "source_revision": 1,
                "source_generation": 1,
                "content_sha256": content_hash(rows[identity]["content"]),
                "scope": {key: rows[identity][key] for key in ("user_id", "agent_id", "session_id")},
                "valid_at": rows[identity]["valid_at"],
                "snapshot": {"content": rows[identity]["content"], "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
                "coverage": "analyzed",
                "defer_reason": None,
            }
            for identity in (1, 5)
        ]
        target = {
            "entry_id": "old",
            "revision": 1,
            "message_id": 2,
            "generation": 1,
            "content": rows[2]["content"],
            "content_sha256": content_hash(rows[2]["content"]),
            "scope": inputs[0]["scope"],
            "source_ids": [1],
            "message_type": "semantic",
        }
        manifest = {"inputs": inputs, "entries": [target], "estimated_input_tokens": 100}
        proposals = [
            {"action": "add", "content": "added hypothesis", "message_type": "semantic", "source_input_ids": ["source-5"], "reason": "cross-session evidence"},
            {"action": "patch", "target_entry_id": "old", "patch": {"old_text": "old fact", "new_text": "new fact"}, "source_input_ids": ["source-1"], "reason": "correct one exact span"},
        ]
        try:
            async with factory() as db, db.begin():
                db.add(Memory(id="bank", tenant_id="tenant", name="publication scratch", memory_size=1048576, embd_id="controlled", llm_id="controlled"))
                db.add(ManagedMemoryState(memory_id="bank", tenant_id="tenant", generation=1, capacity_generation=1, reserved_bytes=0, state="active"))
                for item in inputs:
                    db.add(
                        MemorySourceState(
                            memory_id="bank",
                            tenant_id="tenant",
                            source_message_id=item["source_id"],
                            source_revision=1,
                            generation=1,
                            content_sha256=item["content_sha256"],
                            state="active",
                            scope=item["scope"],
                        )
                    )
                db.add(MemoryEntry(entry_id="old", memory_id="bank", tenant_id="tenant", legacy_message_id=2, current_revision=1, current_message_id=2, next_revision=2, generation=1, state="active"))
                await db.flush()
                db.add(
                    MemoryEntryRevision(
                        entry_id="old",
                        revision=1,
                        memory_id="bank",
                        tenant_id="tenant",
                        message_id=2,
                        content=rows[2]["content"],
                        content_sha256=target["content_sha256"],
                        origin="legacy_adoption",
                        assessment={},
                        message_snapshot=rows[2],
                    )
                )
                db.add(
                    MemoryMaintenanceJob(
                        job_id="job",
                        memory_id="bank",
                        tenant_id="tenant",
                        actor_id="actor",
                        operation_id="create",
                        request_sha256="0" * 64,
                        state="ready",
                        generation=1,
                        source_ids=[1, 5],
                        entry_ids=["old"],
                        entry_snapshots=[target],
                        policy="general",
                        input_budget_tokens=8192,
                        output_budget_tokens=8192,
                        estimated_input_tokens=100,
                    )
                )
                await db.flush()
                db.add(
                    MemoryRevisionSource(
                        entry_id="old",
                        revision=1,
                        memory_id="bank",
                        source_message_id=1,
                        source_revision=1,
                        source_generation=1,
                        content_sha256=inputs[0]["content_sha256"],
                        relation="context",
                        locator={"scope": inputs[0]["scope"]},
                    )
                )
                for item in inputs:
                    db.add(MemoryMaintenanceInput(job_id="job", memory_id="bank", tenant_id="tenant", **item))
                for identity, candidate in zip(("add", "patch"), assemble_candidates(proposals, manifest, len, 8192), strict=True):
                    db.add(
                        MemoryMaintenanceCandidate(
                            candidate_id=identity,
                            job_id="job",
                            memory_id="bank",
                            tenant_id="tenant",
                            revision=2,
                            state="approved",
                            review_actor_id="actor",
                            reviewed_at=datetime.now(UTC),
                            review_operation_id="review-" + identity,
                            **candidate,
                        )
                    )
            yield {"factory": factory, "rows": rows, "writes": [], "statuses": [], "embeddings": 0, "counter": 0, "floors": []}
        finally:
            await asynchronous.dispose()


def service(db: AsyncSession, env: dict[str, Any]) -> PublicationService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        if await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "tenant")) is None:
            raise RevisionNotFound("memory_not_found")

    async def binding(session: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
        assert session.in_transaction() and dimension == 2
        return BINDING

    async def embed(memory_id: str, messages: list[dict[str, Any]], expected: ModelBinding | None) -> dict[str, Any]:
        assert not db.in_transaction()
        env["embeddings"] += 1
        return {"vectors": [[1.0, 0.0] for _ in messages], "model_binding": BINDING}

    async def allocate(*, minimum_message_id: int) -> int:
        assert not db.in_transaction()
        env["floors"].append(minimum_message_id)
        env["counter"] = max(env["counter"], minimum_message_id) + 1
        return env["counter"]

    store = Store(db, env)
    snapshots = MaintenanceSnapshots(db, tenant_id="tenant", storage=store, count_tokens=len)
    return PublicationService(
        db,
        tenant_id="tenant",
        actor_id="actor",
        authorize=authorize,
        model_binding=binding,
        embedder=embed,
        storage=store,
        allocate_message_id=allocate,
        count_tokens=len,
        validate_inputs=snapshots.validate,
    )


async def publish(env: dict[str, Any], *, selections: list[dict[str, Any]] | None = None, operation_id: str = "publish") -> dict[str, Any]:
    async with env["factory"]() as db:
        return await service(db, env).publish("bank", "job", operation_id=operation_id, candidates=SELECTIONS if selections is None else selections)


async def test_all_inactive_stages_precede_one_sql_choice_and_replay(environment: dict[str, Any]) -> None:
    env = environment
    ready = await publish(env)
    assert ready["status"] == ready["index_sync_status"] == "ready"
    assert env["embeddings"] == 1 and len(env["writes"]) == 2
    assert env["rows"][2]["status"] is False
    assert sorted(env["rows"][item["message_id"]]["content"] for item in ready["entries"]) == ["added hypothesis", "new fact. sibling stays."]
    async with env["factory"]() as reader:
        assert (await reader.get(MemoryEntry, "old")).current_revision == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry)) == 2
        revisions = list(await reader.scalars(sa.select(MemoryEntryRevision).where(MemoryEntryRevision.origin == "consolidated")))
        assert len(revisions) == 2 and all(item.assessment == ASSESSMENT for item in revisions)
        refs = list(await reader.scalars(sa.select(MemoryRevisionSource).where(MemoryRevisionSource.revision == 2)))
        assert refs[0].locator["scope"]["session_id"] == "one"
        bank = await reader.get(ManagedMemoryState, "bank")
        assert bank.reserved_bytes == 0 and bank.capacity_generation == 2
        admission = await reader.get(MemoryWriterAdmission, ("bank", "publish"))
        assert admission.active_token is None and admission.publication_state == "ready"
    assert await publish(env) == ready and len(env["writes"]) == 2 and env["embeddings"] == 1


async def test_second_stage_ack_loss_keeps_every_old_current_then_reuses_plan(environment: dict[str, Any]) -> None:
    env = environment
    env["fail_stage"] = 2
    unknown = await publish(env)
    assert unknown["status"] == "persistence_unknown" and unknown["operation_id"] == "publish"
    ids = [item["message_id"] for item in unknown["entries"]]
    assert all(env["rows"][identity]["status"] is False for identity in ids)
    async with env["factory"]() as reader:
        assert (await reader.get(MemoryEntry, "old")).current_message_id == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry)) == 1
    ready = await publish(env)
    assert ready["status"] == "ready" and [item["message_id"] for item in ready["entries"]] == ids
    assert env["embeddings"] == 1 and len(env["writes"]) == 2


def lose_batch_commit(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    original = db.commit
    fired = False

    async def commit() -> None:
        nonlocal fired
        publish_choice = any(
            isinstance(item, MemoryPublicationOutbox) and item.payload.get("action") == "dream_publish" and item.payload.get("published") and item.state == "sync_pending"
            for item in db.identity_map.values()
        )
        await original()
        if publish_choice and not fired:
            fired = True
            raise DBAPIError("controlled commit ACK", {}, RuntimeError("lost ACK"), connection_invalidated=True)

    monkeypatch.setattr(db, "commit", commit)


async def test_actual_batch_commit_lost_ack_keeps_handle_and_reconcile_only_syncs(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    async with env["factory"]() as db:
        lose_batch_commit(db, monkeypatch)
        with pytest.raises(RevisionPersistenceUnknown) as failure:
            await service(db, env).publish("bank", "job", operation_id="publish", candidates=SELECTIONS)
        assert failure.value.operation_id == "publish"
    async with env["factory"]() as reader:
        assert (await reader.get(MemoryEntry, "old")).current_revision == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry)) == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox).where(MemoryPublicationOutbox.state == "sync_pending")) == 3
    ready = await publish(env)
    assert ready["status"] == "ready" and env["embeddings"] == 1 and len(env["writes"]) == 2


@pytest.mark.parametrize("source_id", [1, 5])
async def test_anchor_or_nonanchor_withdraw_after_sql_choice_preserves_unrelated_item(environment: dict[str, Any], source_id: int) -> None:
    env = environment

    async def withdraw(identity: int, status: bool) -> None:
        assert status
        async with env["factory"]() as other, other.begin():
            await other.scalar(sa.select(Memory).where(Memory.id == "bank").with_for_update())
            await other.scalar(sa.select(ManagedMemoryState).where(ManagedMemoryState.memory_id == "bank").with_for_update())
            source = await other.get(MemorySourceState, ("bank", source_id), with_for_update=True)
            source.state, source.generation = "forgotten", source.generation + 1
            closure = await invalidate_source_writers(other, memory_id="bank", tenant_id="tenant", source_id=source_id)
            assert closure["active_operation_ids"] == ["publish"] and len(closure["message_ids"]) == 1
            env["cleanup_ids"] = closure["message_ids"]

    env["status_hook"] = withdraw
    result = await publish(env)
    assert result["status"] == "stale" and result["index_sync_status"] == "unknown", (result, env.get("hook_failure"))
    states = {item["candidate_id"]: item["state"] for item in result["item_states"]}
    affected = "patch" if source_id == 1 else "add"
    assert states == {affected: "stale", ("add" if affected == "patch" else "patch"): "ready"}
    async with env["factory"]() as reader:
        admission = await reader.get(MemoryWriterAdmission, ("bank", "publish"))
        assert admission.publication_state == "ready" and admission.active_token is None
        parent = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == "publish"))
        assert parent.receipt["active_attempts"] == []
        assert parent.payload["manifest"]["entries"] == []
        withdrawn = next(item for item in parent.payload["items"] if item["candidate_id"] == affected)
        assert "content" not in withdrawn["message"] and "content_embed" not in withdrawn["message"]
        remaining = next(item for item in parent.payload["items"] if item["candidate_id"] != affected)
        assert isinstance(remaining["message"]["content"], str)
        assert (await reader.get(MemoryMaintenanceJob, "job")).state == "redacted"
    # Only after actual attempt finished may the withdrawal worker delete targets.
    for identity in env["cleanup_ids"]:
        del env["rows"][identity]
    assert env["rows"][remaining["message_id"]]["status"] is True


async def test_published_batch_ignores_stale_job_and_source_generation_after_temporary_aba(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    async with env["factory"]() as db:
        lose_batch_commit(db, monkeypatch)
        with pytest.raises(RevisionPersistenceUnknown):
            await service(db, env).publish("bank", "job", operation_id="publish", candidates=SELECTIONS)
    async with env["factory"]() as other, other.begin():
        source = await other.get(MemorySourceState, ("bank", 5), with_for_update=True)
        source.state, source.generation = "inactive", 2
        await invalidate_for_source(other, memory_id="bank", tenant_id="tenant", source_id=5, permanent=False)
        source.state, source.generation = "active", 3
    ready = await publish(env)
    assert ready["status"] == "ready" and env["embeddings"] == 1 and len(env["writes"]) == 2


async def test_unrelated_old_target_drift_does_not_block_selected_add(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db, db.begin():
        entry = await db.get(MemoryEntry, "old")
        entry.generation += 1
    ready = await publish(env, selections=[SELECTIONS[0]])
    assert ready["status"] == "ready" and len(ready["entries"]) == 1


async def test_sql_tombstone_floor_prevents_reset_counter_identity_reuse(environment: dict[str, Any]) -> None:
    env = environment
    high = 2**53 + 100
    async with env["factory"]() as db, db.begin():
        db.add(MemorySourceState(memory_id="bank", tenant_id="tenant", source_message_id=high, source_revision=1, generation=1, content_sha256=content_hash("removed"), state="forgotten", scope={}))
    ready = await publish(env)
    assert ready["status"] == "ready" and env["floors"][0] == high
    assert all(item["message_id"] > high for item in ready["entries"])


async def test_changed_review_revision_is_rejected_before_io(environment: dict[str, Any]) -> None:
    env = environment
    with pytest.raises(RevisionConflict, match="maintenance_candidate_revision_changed"):
        await publish(env, selections=[{"candidate_id": "add", "expected_revision": 1}])
    assert env["writes"] == [] and env["embeddings"] == 0
    async with env["factory"]() as reader:
        assert await reader.get(MemoryWriterAdmission, ("bank", "publish")) is None


async def cancel_job(env: dict[str, Any]) -> None:
    async def unused(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Cancel must not analyze or read physical storage")

    async with env["factory"]() as db:
        owner = service(db, env)
        jobs = JobService(db, tenant_id="tenant", actor_id="actor", authorize=owner.authorize, snapshotter=unused, validate_inputs=owner.validate_inputs, analyzer=unused, count_tokens=len)
        await jobs.cancel("bank", "job", operation_id="cancel")


@pytest.mark.parametrize("delete_ack_lost", [False, True])
async def test_cancel_staged_batch_retires_fixed_ids_and_reservation_after_readback(environment: dict[str, Any], delete_ack_lost: bool) -> None:
    env = environment

    async def cancel_after_stage() -> None:
        await cancel_job(env)

    env["write_hook"], env["fail_delete_ack"] = cancel_after_stage, delete_ack_lost
    result = await publish(env)
    assert env["rows"][2]["status"] is True
    assert len(env["writes"]) == 1 and len(env["deletes"]) == 1
    async with env["factory"]() as reader:
        assert (await reader.get(MemoryEntry, "old")).current_message_id == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry)) == 1
        bank = await reader.get(ManagedMemoryState, "bank")
        parent = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == "publish"))
        assert (bank.reserved_bytes > 0) is delete_ack_lost
        assert parent.receipt.get("reservation_released", False) is not delete_ack_lost
        assert parent.receipt["active_attempts"] == []
        ids = [item["message_id"] for item in parent.payload["items"]]
        assert not any(identity in env["rows"] for identity in ids)
    if delete_ack_lost:
        assert result["status"] == "persistence_unknown" and result["operation_id"] == "publish"
        result = await publish(env)
    assert result["status"] == result["index_sync_status"] == "canceled"
    assert env["embeddings"] == 1 and len(env["writes"]) == len(env["deletes"]) == 1
    assert await publish(env) == result
    async with env["factory"]() as reader:
        assert (await reader.get(ManagedMemoryState, "bank")).reserved_bytes == 0


async def test_job_cancel_after_actual_sql_commit_does_not_undo_published_batch(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    async with env["factory"]() as db:
        lose_batch_commit(db, monkeypatch)
        with pytest.raises(RevisionPersistenceUnknown):
            await service(db, env).publish("bank", "job", operation_id="publish", candidates=SELECTIONS)
    await cancel_job(env)
    ready = await publish(env)
    assert ready["status"] == "ready" and env["embeddings"] == 1 and len(env["writes"]) == 2
    async with env["factory"]() as reader:
        assert (await reader.get(MemoryEntry, "old")).current_revision == 2
        assert (await reader.get(MemoryMaintenanceCandidate, "patch")).state == "redacted"


async def test_patch_preserves_frozen_event_interval_parent_and_scope(environment: dict[str, Any]) -> None:
    env = environment
    semantic = {"message_type": "episodic", "source_id": 1, "session_id": "original-event", "valid_at": "2025-03-01 09:30:00", "invalid_at": "2025-03-01 11:30:00"}
    env["rows"][2].update(semantic)
    async with env["factory"]() as db, db.begin():
        revision = await db.get(MemoryEntryRevision, ("old", 1))
        revision.message_snapshot = {**revision.message_snapshot, **semantic}
        job = await db.get(MemoryMaintenanceJob, "job")
        snapshots = deepcopy(job.entry_snapshots)
        snapshots[0]["scope"]["session_id"] = "original-event"
        snapshots[0]["message_type"] = "episodic"
        job.entry_snapshots = snapshots
        candidate = await db.get(MemoryMaintenanceCandidate, "patch")
        candidate.message_type = "episodic"
    result = await publish(env, selections=[SELECTIONS[1]])
    assert result["status"] == "ready"
    updated = env["rows"][result["entries"][0]["message_id"]]
    assert {key: updated[key] for key in semantic} == semantic
    assert updated["content"] == "new fact. sibling stays."
    async with env["factory"]() as reader:
        relation = await reader.get(MemoryRevisionSource, ("old", 2, 1))
        assert relation.locator["scope"]["session_id"] == "one"


async def test_patch_accepts_storage_empty_time_encoding_without_changing_semantics(environment: dict[str, Any]) -> None:
    env = environment
    env["rows"][2].update(invalid_at="-", forget_at="-")
    result = await publish(env, selections=[SELECTIONS[1]])
    assert result["status"] == "ready"
    row = env["rows"][result["entries"][0]["message_id"]]
    assert row["invalid_at"] is None and row["forget_at"] is None


async def test_withdrawn_item_does_not_prevent_temporarily_inactive_sibling_from_later_settling(environment: dict[str, Any]) -> None:
    env = environment

    async def change_sources(identity: int, status: bool) -> None:
        async with env["factory"]() as other, other.begin():
            for source_id, state, permanent in ((1, "forgotten", True), (5, "inactive", False)):
                source = await other.get(MemorySourceState, ("bank", source_id), with_for_update=True)
                source.state, source.generation = state, source.generation + 1
                await invalidate_source_writers(other, memory_id="bank", tenant_id="tenant", source_id=source_id, permanent=permanent)

    env["status_hook"] = change_sources
    partial = await publish(env)
    assert partial["status"] == "stale" and partial["retryable"] is True
    assert {item["candidate_id"]: item["state"] for item in partial["item_states"]} == {"add": "pending", "patch": "stale"}
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemorySourceState, ("bank", 5), with_for_update=True)
        source.state, source.generation = "active", source.generation + 1
    settled = await publish(env)
    assert settled["status"] == "stale" and settled["index_sync_status"] == "unknown" and settled["retryable"] is False
    assert {item["candidate_id"]: item["state"] for item in settled["item_states"]} == {"add": "ready", "patch": "stale"}
    assert env["embeddings"] == 1 and len(env["writes"]) == 2
