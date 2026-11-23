"""Real-SQL human verification, concurrency fences and migration behavior."""

import importlib.util
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_verification_models import MEMORY_VERIFICATION_MODELS, MemoryVerificationHead, MemoryVerificationOperation, MemoryVerificationRecord
from memory.maintenance.lifecycle import invalidate_for_bank, invalidate_for_entry, invalidate_for_source
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.verification.service import VerificationService
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_maintenance_repository import service as existing_jobs

ENTRY = "a" * 32


async def seed(env: dict[str, Any], *, entry_id: str = ENTRY, message_id: int = 100) -> None:
    env.setdefault("physical", {})
    env.setdefault("physical_reads", 0)
    raw = {
        "id": "bank_10",
        "memory_id": "bank",
        "message_id": 10,
        "source_id": 0,
        "message_type": "raw",
        "content": "source 10",
        "content_embed": [1.0, 0.0],
        "status": True,
        "valid_at": None,
        "invalid_at": None,
        "forget_at": None,
        **env["scope"],
    }
    message = {**raw, "id": f"bank_{message_id}", "message_id": message_id, "source_id": 10, "message_type": "semantic", "content": "人工待核对的项目事实；保留范围条件。"}
    env["physical"].update({10: raw, message_id: message})
    async with env["factory"]() as db, db.begin():
        db.add(
            MemoryEntry(
                entry_id=entry_id, memory_id="bank", tenant_id="tenant", legacy_message_id=message_id, current_revision=1, next_revision=2, current_message_id=message_id, generation=1, state="active"
            )
        )
        await db.flush()
        db.add(
            MemoryEntryRevision(
                entry_id=entry_id,
                revision=1,
                memory_id="bank",
                tenant_id="tenant",
                message_id=message_id,
                parent_revision=None,
                origin="legacy_adoption",
                content=message["content"],
                content_sha256=content_hash(message["content"]),
                assessment={"kind": "unknown", "verification_status": "unverified"},
                message_snapshot=deepcopy(message),
            )
        )
        await db.flush()
        db.add(
            MemoryRevisionSource(
                entry_id=entry_id,
                revision=1,
                memory_id="bank",
                source_message_id=10,
                source_revision=1,
                source_generation=1,
                content_sha256=content_hash("source 10"),
                relation="context",
                locator={"scope": env["scope"]},
            )
        )


def service(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> VerificationService:
    jobs = existing_jobs(db, env, actor_id=actor_id)

    class Storage:
        async def read(self, message_id: int) -> dict[str, Any] | None:
            assert not db.in_transaction(), "No SQL transaction across physical IO"
            env["physical_reads"] += 1
            if hook := env.get("on_read"):
                await hook(message_id)
            return deepcopy(env["physical"].get(message_id))

    store = cast(ManagedMessageStore, Storage())
    snapshots = MaintenanceSnapshots(db, tenant_id="tenant", storage=store, count_tokens=len)
    return VerificationService(db, tenant_id="tenant", actor_id=actor_id, authorize=jobs.authorize, storage=store, ready_entry=snapshots._ready_entry)


def command(snapshot: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "expected_head_version": snapshot["head_version"],
        "expected_binding_sha256": snapshot["binding_sha256"],
        "result": "human_confirmed",
        "method": "source_comparison",
        "applicability": "范围内人工核对。",
        "notes": "人工说明可能含私密原文。",
        "evidence": [{"description": "对照原始依据。", "reference": "private-reference"}],
        "checked_at": None,
        **changes,
    }


async def record(env: dict[str, Any], *, operation_id: str = "check", entry_id: str = ENTRY, **changes: Any) -> dict[str, Any]:
    async with env["factory"]() as db:
        owner = service(db, env)
        snapshot = await owner.snapshot("bank", entry_id)
        return await owner.record("bank", entry_id, operation_id=operation_id, **command(snapshot, **changes))


async def test_human_record_is_independent_and_replay_never_creates_a_second_event(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        snapshot = await owner.snapshot("bank", ENTRY)
        assert snapshot["head_version"] == "0" and snapshot["current_record"] is None
        first = await owner.record("bank", ENTRY, operation_id="fixed-check", **command(snapshot))
        assert first["record"]["effective_status"] == "human_confirmed" and first["record"]["valid_for_current"]
        replay = await owner.record("bank", ENTRY, operation_id="fixed-check", **command(snapshot))
        assert replay["verification_id"] == first["verification_id"]
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await owner.record("bank", ENTRY, operation_id="fixed-check", **command(snapshot, notes="different"))
        revision = await db.get(MemoryEntryRevision, (ENTRY, 1))
        assert revision.assessment == {"kind": "unknown", "verification_status": "unverified"}
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryVerificationRecord)) == 1
        assert env["analyses"] == env["snapshots"] == 0


async def test_latest_dispute_only_and_revocation_cas_cannot_cover_new_head(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    disputed = await record(env, result="disputed")
    async with env["factory"]() as db:
        assert len((await service(db, env).todos("bank"))["items"]) == 1
    fresh = await record(env, operation_id="fresh-check")
    async with env["factory"]() as db:
        owner = service(db, env)
        assert (await owner.todos("bank"))["items"] == []
        with pytest.raises(RevisionConflict, match="verification_head_changed"):
            await owner.revoke("bank", disputed["verification_id"], operation_id="stale-revoke", expected_head_version="1", reason="old")
        revoked = await owner.revoke("bank", fresh["verification_id"], operation_id="revoke-current", expected_head_version="2", reason="需要重核。")
        assert revoked["record"]["effective_status"] == "revoked"
        assert (await owner.revoke("bank", fresh["verification_id"], operation_id="revoke-current", expected_head_version="2", reason="需要重核。"))["verification_id"] == fresh["verification_id"]
        history = await owner.history("bank", ENTRY, limit=1)
        assert history["has_more"] and history["records"][0]["verification_id"] == fresh["verification_id"]
        second = await owner.history("bank", ENTRY, limit=1, cursor=history["next_cursor"])
        assert second["records"][0]["effective_status"] == "superseded"


@pytest.mark.parametrize("change", ["entry", "source", "projection"])
async def test_version_or_physical_change_invalidates_without_get_writes(environment: dict[str, Any], change: str) -> None:
    env = environment
    await seed(env)
    first = await record(env)
    async with env["factory"]() as db, db.begin():
        if change == "entry":
            row = await db.get(MemoryEntry, ENTRY)
            row.generation += 2
        elif change == "source":
            source = await db.get(MemorySourceState, ("bank", 10))
            source.generation += 2
    if change == "projection":
        env["physical"][100]["content_embed"] = [0.0, 1.0]
    async with env["factory"]() as db:
        owner = service(db, env)
        value = await owner.operation("bank", "check")
        assert value["verification_id"] == first["verification_id"]
        assert value["record"]["effective_status"] == "stale" and not value["record"]["valid_for_current"]
        assert len((await owner.todos("bank"))["items"]) == 1
        head = await db.get(MemoryVerificationHead, ("bank", ENTRY))
        assert head.head_version == 1
    renewed = await record(env, operation_id="renewed")
    assert renewed["record"]["effective_status"] == "human_confirmed"


@pytest.mark.parametrize("target", ["source", "entry", "bank"])
async def test_permanent_removal_redacts_all_historical_and_operation_text(environment: dict[str, Any], target: str) -> None:
    env = environment
    await seed(env)
    await record(env)
    await record(env, operation_id="second", result="disputed")
    async with env["factory"]() as db, db.begin():
        if target == "source":
            source = await db.get(MemorySourceState, ("bank", 10))
            source.state, source.generation = "forgotten", source.generation + 1
            await invalidate_for_source(db, memory_id="bank", tenant_id="tenant", source_id=10, permanent=True)
        elif target == "entry":
            entry = await db.get(MemoryEntry, ENTRY)
            entry.state = "withdrawn"
            await invalidate_for_entry(db, memory_id="bank", tenant_id="tenant", entry_id=ENTRY, permanent=True)
        else:
            await invalidate_for_bank(db, memory_id="bank", tenant_id="tenant")
    async with env["factory"]() as db:
        owner = service(db, env)
        for row in (await owner.history("bank", ENTRY))["records"]:
            assert row["effective_status"] == "redacted" and row["notes"] is None and row["applicability"] is None and row["evidence"] == []
        assert (await owner.operation("bank", "check"))["record"]["notes"] is None
        for row in await db.scalars(sa.select(MemoryVerificationRecord)):
            assert row.notes is row.applicability is row.revocation_reason is None and row.evidence == []


async def test_commit_ack_unknown_recovers_original_operation_without_repeating_record(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        snapshot = await owner.snapshot("bank", ENTRY)
        original_commit = db.commit

        async def lost_ack() -> None:
            await original_commit()
            raise OperationalError("COMMIT", None, RuntimeError("lost acknowledgement"))

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RevisionPersistenceUnknown) as raised:
            await owner.record("bank", ENTRY, operation_id="lost-ack", **command(snapshot))
        assert raised.value.operation_id == "lost-ack"
    async with env["factory"]() as db:
        owner = service(db, env)
        recovered = await owner.operation("bank", "lost-ack")
        assert recovered["record"]["valid_for_current"]
        assert (await owner.record("bank", ENTRY, operation_id="lost-ack", **command(snapshot)))["verification_id"] == recovered["verification_id"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryVerificationRecord)) == 1


async def test_storage_failure_is_not_missing_and_acl_is_fresh(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    await record(env)

    async def fail(identity: int) -> None:
        raise RuntimeError("strict physical read failed")

    env["on_read"] = fail
    async with env["factory"]() as db:
        with pytest.raises(RuntimeError, match="strict physical read failed"):
            await service(db, env).operation("bank", "check")
        with pytest.raises(RevisionNotFound):
            await service(db, env, actor_id="denied").operation("bank", "check")


async def test_formal_migration_up_down_and_repeat_upgrade_preserve_existing_entry(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    path = Path(__file__).parents[2] / "configs/alembic/versions/fb13d5e7a9c1_add_memory_verification_records.py"
    spec = importlib.util.spec_from_file_location("verification_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with env["engine"].begin() as connection:
        prior = connection.execute(sa.select(MemoryEntry.__table__)).mappings().one()
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert all(not sa.inspect(connection).has_table(model.__tablename__, schema="usr_ai") for model in MEMORY_VERIFICATION_MODELS)
            migration.upgrade()
            migration.upgrade()
        assert dict(connection.execute(sa.select(MemoryEntry.__table__)).mappings().one()) == dict(prior)
    first = await record(env)
    with env["engine"].begin() as connection, Operations.context(MigrationContext.configure(connection)):
        with pytest.raises(RuntimeError, match="Cannot discard"):
            migration.downgrade()
    assert first["record"]["valid_for_current"]


@pytest.mark.parametrize("removed", ["source", "entry"])
async def test_snapshot_never_returns_old_body_after_nested_view_observes_removal(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, removed: str) -> None:
    env = environment
    await seed(env)
    await record(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        original_view = owner._view

        async def remove_before_view(memory_id: str, verification_id: str, **options: Any) -> dict[str, Any]:
            async with env["factory"]() as writer, writer.begin():
                if removed == "source":
                    source = await writer.get(MemorySourceState, ("bank", 10))
                    source.state, source.generation = "forgotten", source.generation + 1
                    await invalidate_for_source(writer, memory_id="bank", tenant_id="tenant", source_id=10, permanent=True)
                else:
                    entry = await writer.get(MemoryEntry, ENTRY)
                    entry.state, entry.generation = "withdrawn", entry.generation + 1
                    await invalidate_for_entry(writer, memory_id="bank", tenant_id="tenant", entry_id=ENTRY, permanent=True)
            return await original_view(memory_id, verification_id, **options)

        monkeypatch.setattr(owner, "_view", remove_before_view)
        with pytest.raises(RevisionConflict, match=r"verification_(source|entry)_unavailable"):
            await owner.snapshot("bank", ENTRY)
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryVerificationOperation)) == 1
        stored = await reader.scalar(sa.select(MemoryVerificationRecord))
        assert stored.state == "redacted" and stored.notes is None and stored.evidence == []


async def test_snapshot_head_change_during_nested_view_is_conflict_not_mixed_snapshot(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    await seed(env)
    await record(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        original_view = owner._view

        async def replace_before_view(memory_id: str, verification_id: str, **options: Any) -> dict[str, Any]:
            await record(env, operation_id="new-head", result="disputed")
            return await original_view(memory_id, verification_id, **options)

        monkeypatch.setattr(owner, "_view", replace_before_view)
        with pytest.raises(RevisionConflict, match="verification_head_changed"):
            await owner.snapshot("bank", ENTRY)
    async with env["factory"]() as reader:
        old = await service(reader, env).operation("bank", "check")
        assert old["record"]["head_version"] == "2" and not old["record"]["is_current"]
        assert old["record"]["effective_status"] == "superseded" and not old["record"]["review_required"]


@pytest.mark.parametrize("change", ["replace", "revoke"])
async def test_record_view_closing_refreshes_head_identity_and_version(environment: dict[str, Any], change: str) -> None:
    env = environment
    await seed(env)
    first = await record(env)

    async def change_during_read(message_id: int) -> None:
        env.pop("on_read")
        if change == "replace":
            await record(env, operation_id="new-head", result="needs_review")
        else:
            async with env["factory"]() as writer:
                await service(writer, env).revoke("bank", first["verification_id"], operation_id="revoke-mid-read", expected_head_version="1", reason="新发现需复核。")

    env["on_read"] = change_during_read
    async with env["factory"]() as db:
        view = (await service(db, env).operation("bank", "check"))["record"]
        assert view["head_version"] == "2"
        assert view["is_current"] is (change == "revoke")
        assert view["effective_status"] == ("revoked" if change == "revoke" else "superseded")
        assert view["review_required"] is (change == "revoke")
        assert not view["valid_for_current"] and not view["can_actions"]["revoke"]
        if change == "revoke":
            assert view["revoked_by"] == "actor"
            assert isinstance(view["revoked_at"], str) and view["revoked_at"]
            assert view["revocation_reason"] == "新发现需复核。"


async def test_todo_bounded_scan_returns_continuation_past_confirmed_prefix(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from memory.verification import service as implementation

    env = environment
    for index in range(3):
        identity = f"{index + 1:032x}"
        await seed(env, entry_id=identity, message_id=100 + index)
        await record(env, operation_id=f"check-{index}", entry_id=identity, result="needs_review" if index == 2 else "human_confirmed")
    monkeypatch.setattr(implementation, "TODO_SCAN_LIMIT", 2)
    async with env["factory"]() as db:
        owner = service(db, env)
        first = await owner.todos("bank", limit=1)
        assert first["items"] == [] and first["scan_limited"] and first["has_more"] and first["scanned_count"] == 2
        second = await owner.todos("bank", limit=1, cursor=first["next_cursor"])
        assert len(second["items"]) == 1 and second["items"][0]["entry_id"] == f"{3:032x}"
        assert not second["has_more"]
