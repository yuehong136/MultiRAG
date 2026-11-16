"""Opt-in full-span coverage against owned PostgreSQL and controlled model IO."""

import importlib.util
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_incremental_models import MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery, MemoryMaintenanceFragmentCoverage
from api.db.memory_maintenance_models import MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance.checkpoints import IncrementalCheckpoints
from memory.maintenance.execution import process_next
from memory.maintenance.incremental import DiscoveryService
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import RevisionConflict, RevisionPersistenceUnknown, content_hash
from memory.revisions.storage import ManagedMessageStore
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_maintenance_repository import service as explicit_jobs


def options(**changes: Any) -> dict[str, Any]:
    return {
        "scope": {"user_id": None, "agent_id": None, "session_id": None},
        "batch_size": 32,
        "max_sources": 128,
        "input_budget_tokens": 4096,
        "segmentation": {"mode": "segmented", "fragment_token_limit": 256, "max_parts_per_source": 32},
        **changes,
    }


async def seed(env: dict[str, Any], *, count: int = 1, text: str | None = None) -> None:
    env["bodies"] = {}
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.delete(MemorySourceState))
        for index in range(count):
            identity = 2**53 + index + 11
            body = text or "\n\n".join(f"第{line}条来源{index}：" + "有条件地保留原始证据，不将助手承诺当作验证。" * 7 for line in range(12)) + "\n末尾事实：审批后才发布。"
            env["bodies"][identity] = body
            db.add(
                MemorySourceState(
                    memory_id="bank", tenant_id="tenant", source_message_id=identity, source_revision=1, generation=1, content_sha256=content_hash(body), state="active", scope=env["scope"]
                )
            )


def service(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> DiscoveryService:
    jobs = explicit_jobs(db, env, actor_id=actor_id)

    class Storage:
        async def read(self, message_id: int) -> dict[str, Any] | None:
            assert not db.in_transaction(), "SQL must be released before storage IO"
            if message_id in env.get("projections", {}):
                return deepcopy(env["projections"][message_id])
            body = env["bodies"].get(message_id)
            return (
                {"memory_id": "bank", "message_id": message_id, "source_id": 0, "message_type": "raw", "content": body, "status": True, "forget_at": None, "valid_at": None, **env["scope"]}
                if body is not None
                else None
            )

    snapshots = MaintenanceSnapshots(db, tenant_id="tenant", storage=cast(ManagedMessageStore, Storage()), count_tokens=jobs.count_tokens)
    jobs.snapshotter, jobs.segment_snapshotter, jobs.validate_inputs = snapshots.snapshot, snapshots.segment_snapshot, snapshots.validate

    async def analyzer(memory_id: str, manifest: dict[str, Any], policy: str, output_budget: int) -> Any:
        assert not db.in_transaction()
        env["analyses"] += 1
        fragments = [item["fragment"] for item in manifest["inputs"] if item.get("fragment")]
        env.setdefault("seen_fragments", []).extend(deepcopy(fragments))
        if any(item["part_index"] == env.get("fail_part") for item in fragments):
            raise ValueError("controlled malformed output")
        return {"proposals": [], "model_reference": "controlled-chat", "config_reference": "config-hash", "usage": {"input_tokens": 100, "output_tokens": 1}}

    async def identity(memory_id: str) -> dict[str, str]:
        return {"strategy_sha256": "a" * 64, "model_reference": "controlled-chat", "config_reference": "config-hash"}

    jobs.analyzer = analyzer
    checkpoints = IncrementalCheckpoints(db, tenant_id="tenant", identity=identity)
    jobs.validate_binding, jobs.checkpoint = checkpoints.validate, checkpoints.checkpoint
    return DiscoveryService(db, jobs=jobs, identity=identity, validate_entry=snapshots._ready_entry, segment_planner=snapshots.plan_segments)


async def create(env: dict[str, Any], operation: str = "discover", **changes: Any) -> dict[str, Any]:
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **options(**changes))
        return await owner.create("bank", operation_id=operation, expected_preview_sha256=preview["preview_sha256"], **options(**changes))


async def consume(env: dict[str, Any]) -> None:
    async def jobs(db: AsyncSession, memory_id: str, *, actor_id: str) -> Any:
        return service(db, env, actor_id=actor_id).jobs

    for _ in range(300):
        if not await process_next(env["factory"], jobs, worker_id="fragment-sql-test"):
            return
    raise AssertionError("bounded fragment jobs did not drain")


async def submit_all(env: dict[str, Any], discovery: dict[str, Any]) -> None:
    for index in range(40):
        async with env["factory"]() as db:
            view = await service(db, env).get("bank", discovery["discovery_id"])
            if not any(item["state"] in {"planned", "created", "unknown"} for item in view["batches"]):
                return
            await service(db, env).run("bank", discovery["discovery_id"], operation_id=f"run-{index}", limit=8)
        await consume(env)
    raise AssertionError("bounded discovery did not settle")


async def test_preview_reads_full_evidence_without_ledger_and_create_revalidates_source(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **options())
        assert preview["token_fit"] == "checked_at_preview" and len(preview["batches"]) > 2
        assert env["analyses"] == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceDiscovery)) == 0
        await db.rollback()
        async with db.begin():
            source = await db.scalar(sa.select(MemorySourceState))
            source.generation += 1
        with pytest.raises(RevisionConflict, match="incremental_preview_changed"):
            await owner.create("bank", operation_id="changed", expected_preview_sha256=preview["preview_sha256"], **options())


async def test_all_parts_and_actual_drain_are_required_before_source_coverage(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    discovery = await create(env)
    total = discovery["source_coverage"]["total_parts"]
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="first", limit=1)
    await consume(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        partial = await owner.get("bank", discovery["discovery_id"])
        assert partial["source_coverage"] == {"unit": "sources", "selected": 1, "covered": 0, "partial": 1, "blocked": 0, "total_parts": total, "analyzed_parts": 1}
        assert (await owner.preview("bank", **options()))["counts"]["covered"] == 0
    await submit_all(env, discovery)
    async with env["factory"]() as db:
        ready = await service(db, env).get("bank", discovery["discovery_id"])
        assert ready["state"] == "settled" and ready["source_coverage"]["covered"] == 1
        assert (await service(db, env).preview("bank", **options()))["counts"]["covered"] == 1
        await db.rollback()
        async with db.begin():
            admission = await db.scalar(sa.select(MemoryWriterAdmission).limit(1))
            admission.active_token = "still-running"
        assert (await service(db, env).get("bank", discovery["discovery_id"]))["source_coverage"]["covered"] == 0
        assert (await service(db, env).preview("bank", **options()))["counts"]["inflight"] == 1


async def test_failed_part_can_resume_without_repeating_completed_parts(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    env["fail_part"] = 1
    discovery = await create(env)
    await submit_all(env, discovery)
    first_calls = env["analyses"]
    async with env["factory"]() as db:
        partial = await service(db, env).get("bank", discovery["discovery_id"])
        assert partial["state"] == "partial" and partial["source_coverage"]["covered"] == 0
        old_failed = next(item for item in partial["batches"] if item["state"] == "failed")
    env.pop("fail_part")
    resumed = await create(env, operation="resume")
    assert len(resumed["batches"]) == 1 and resumed["batches"][0]["primary_sources"][0]["fragment"]["part_index"] == 1
    async with env["factory"]() as db:
        old = await db.get(MemoryMaintenanceJob, old_failed["job_id"])
        manifest = await service(db, env).jobs._manifest(old)
        validate = service(db, env).jobs.validate_binding
        assert validate is not None
        with pytest.raises(RevisionConflict, match="incremental_checkpoint_owner_changed"):
            await validate(old, manifest)
        await db.rollback()
    await submit_all(env, resumed)
    assert env["analyses"] == first_calls + 1
    async with env["factory"]() as db:
        assert (await service(db, env).get("bank", resumed["discovery_id"]))["source_coverage"]["covered"] == 1


async def test_cancel_and_new_generation_do_not_cover_unprocessed_parts(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    original = await create(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        await owner.run("bank", original["discovery_id"], operation_id="first", limit=1)
    await consume(env)
    async with env["factory"]() as db:
        canceled = await service(db, env).cancel("bank", original["discovery_id"], operation_id="cancel")
        assert canceled["state"] == "canceled" and canceled["source_coverage"]["covered"] == 0
        assert (await service(db, env).preview("bank", **options()))["counts"]["eligible"] == 1
        await db.rollback()
        async with db.begin():
            source = await db.scalar(sa.select(MemorySourceState))
            source.generation += 1
    renewed = await create(env, operation="new-version")
    assert renewed["source_coverage"]["analyzed_parts"] == 0
    assert {item["primary_sources"][0]["fragment"]["fragment_id"] for item in renewed["batches"]}.isdisjoint({item["primary_sources"][0]["fragment"]["fragment_id"] for item in original["batches"]})


async def test_parts_limit_blocks_whole_source_without_hiding_later_sources(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, count=2)
    configuration = {"mode": "segmented", "fragment_token_limit": 256, "max_parts_per_source": 1}
    blocked = await create(env, segmentation=configuration, max_sources=1)
    assert blocked["state"] == "partial" and blocked["source_coverage"]["covered"] == 0
    assert blocked["blocked_sources"][0]["error_code"] == "segment_parts_limit"
    async with env["factory"]() as db:
        next_window = await service(db, env).preview("bank", **options(segmentation=configuration, max_sources=1))
        assert int(next_window["blocked_sources"][0]["source_id"]) == sorted(env["bodies"])[1]
        retried = await service(db, env).preview("bank", **options())
        assert retried["counts"]["selected_primary"] == 2 and not retried["blocked_sources"]


async def test_fragment_migration_adds_old_columns_and_is_idempotent(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    old = await create(env, segmentation={"mode": "whole"})
    identity = next(iter(env["bodies"]))
    async with env["factory"]() as db:
        job = await service(db, env).jobs.create("bank", operation_id="old-whole-input", source_ids=[identity], entry_ids=[])
    path = Path("configs/alembic/versions/fa02c4e6b8d0_add_memory_fragment_coverage.py")
    spec = importlib.util.spec_from_file_location("fragment_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with environment["engine"].begin() as connection:
        cast(sa.Table, MemoryMaintenanceFragmentCoverage.__table__).drop(connection)
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_memory_maintenance_inputs DROP COLUMN fragment"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_memory_maintenance_coverage DROP COLUMN expected_fragments"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
        assert {item["name"] for item in sa.inspect(connection).get_columns("t_ai_memory_maintenance_inputs", schema="usr_ai")} >= {"fragment"}
    async with env["factory"]() as independent:
        original = await independent.get(MemoryMaintenanceJob, job["job_id"])
        evidence = await independent.scalar(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job["job_id"]))
        coverage = await independent.scalar(sa.select(MemoryMaintenanceCoverage))
        assert original.operation_id == "old-whole-input" and original.source_ids == [identity]
        assert evidence.fragment is None and evidence.snapshot["content"] == env["bodies"][identity]
        assert coverage.expected_fragments == 0
        assert (await service(independent, env).get("bank", old["discovery_id"]))["profile"].get("segmentation") is None


async def test_segment_plan_commit_lost_ack_recovers_same_inventory(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **options())
        commit = db.commit

        async def lost_ack() -> None:
            await commit()
            raise sa.exc.DBAPIError(None, None, RuntimeError("controlled fragment plan ACK loss"), False)

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await owner.create("bank", operation_id="fixed-plan", expected_preview_sha256=preview["preview_sha256"], **options())
        assert error.value.operation_id == "fixed-plan"
    async with env["factory"]() as independent:
        stored = await independent.scalar(sa.select(MemoryMaintenanceDiscovery))
        stored_id = stored.discovery_id
        ids = set(await independent.scalars(sa.select(MemoryMaintenanceFragmentCoverage.fragment_key)))
        replay = await service(independent, env).create("bank", operation_id="fixed-plan", expected_preview_sha256=preview["preview_sha256"], **options())
        assert replay["discovery_id"] == stored_id
        assert set(await independent.scalars(sa.select(MemoryMaintenanceFragmentCoverage.fragment_key))) == ids
        assert env["analyses"] == 0


async def test_total_plan_limit_does_not_permanently_block_later_source_window(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, count=7, text="甲" * 50000)
    configuration = {"mode": "segmented", "fragment_token_limit": 256, "max_parts_per_source": 64}
    plan = await create(env, segmentation=configuration)
    assert 128 < plan["counts"]["selected_fragments"] <= 256
    assert plan["has_more"] and plan["blocked_sources"]
    assert all(item["error_code"] == "segment_plan_limit" for item in plan["blocked_sources"])
    blocked_ids = {item["source_id"] for item in plan["blocked_sources"]}
    async with env["factory"]() as db:
        next_window = await service(db, env).preview("bank", **options(segmentation=configuration))
        assert next_window["counts"]["eligible"] == len(blocked_ids)
        assert {item["source_id"] for batch in next_window["batches"] for item in batch["primary_sources"]} == blocked_ids
        assert next_window["counts"]["covered"] == 0


async def test_explicit_whole_entry_context_repeats_without_becoming_new_primary_coverage(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    context, content, entry_id = "旧会话记录了项目决定及理由。", "项目决定需保留原始原因。", "e" * 32
    scope = {**env["scope"], "session_id": "older-session"}
    env["bodies"][10] = context
    env["projections"] = {1000: {"memory_id": "bank", "message_id": 1000, "source_id": 10, "message_type": "semantic", "content": content, "status": True, "forget_at": None, **scope}}
    # RAW context is read with its own historical session, not the primary's.
    env["projections"][10] = {"memory_id": "bank", "message_id": 10, "source_id": 0, "message_type": "raw", "content": context, "status": True, "forget_at": None, "valid_at": None, **scope}
    async with env["factory"]() as db, db.begin():
        db.add(MemorySourceState(memory_id="bank", tenant_id="tenant", source_message_id=10, source_revision=1, generation=1, content_sha256=content_hash(context), state="active", scope=scope))
        db.add(MemoryEntry(entry_id=entry_id, memory_id="bank", tenant_id="tenant", legacy_message_id=1000, current_revision=1, current_message_id=1000, generation=1, state="active"))
        await db.flush()
        db.add(
            MemoryEntryRevision(
                entry_id=entry_id,
                revision=1,
                memory_id="bank",
                tenant_id="tenant",
                message_id=1000,
                content=content,
                content_sha256=content_hash(content),
                parent_revision=None,
                origin="manual",
                assessment={},
                message_snapshot=env["projections"][1000],
            )
        )
        await db.flush()
        db.add(
            MemoryRevisionSource(
                entry_id=entry_id,
                revision=1,
                source_message_id=10,
                memory_id="bank",
                source_revision=1,
                source_generation=1,
                content_sha256=content_hash(context),
                relation="context",
                locator={"scope": scope},
            )
        )
    plan = await create(env, entry_ids=[entry_id])
    assert plan["counts"]["selected_primary"] == 1 and plan["counts"]["context"] == 1
    assert all(item["entry_ids"] == [entry_id] and item["context_sources"][0]["source_id"] == "10" and "fragment" not in item["context_sources"][0] for item in plan["batches"])
    await submit_all(env, plan)
    async with env["factory"]() as db:
        assert (await service(db, env).get("bank", plan["discovery_id"]))["source_coverage"]["covered"] == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.source_id == 10)) == 0
        inputs = list(await db.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.source_id == 10)))
        assert len(inputs) == len(plan["batches"]) and all(item.fragment is None and item.coverage == "analyzed" and item.snapshot["content"] == context and item.scope == scope for item in inputs)
