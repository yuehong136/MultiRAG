"""Versioned discovery and checkpoint decisions against isolated real SQL."""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_incremental_models import MEMORY_INCREMENTAL_MODELS, MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery, MemoryMaintenanceFragmentCoverage
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance.checkpoints import IncrementalCheckpoints
from memory.maintenance.execution import ExecutionService, process_next
from memory.maintenance.incremental import DiscoveryService
from memory.revisions.domain import RevisionConflict, RevisionPersistenceUnknown, content_hash
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_maintenance_repository import service as explicit_jobs


def service(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> DiscoveryService:
    jobs = explicit_jobs(db, env, actor_id=actor_id)

    async def identity(memory_id: str) -> dict[str, str]:
        return {"strategy_sha256": env.get("strategy", "a" * 64), "model_reference": "controlled-chat", "config_reference": env.get("config", "config-hash")}

    checkpoints = IncrementalCheckpoints(db, tenant_id="tenant", identity=identity)
    jobs.validate_binding, jobs.checkpoint = checkpoints.validate, checkpoints.checkpoint

    async def ready(entry: MemoryEntry, revision: MemoryEntryRevision) -> None:
        if entry.state != "active" or revision.parent_revision is not None or revision.content is None:
            raise RevisionConflict("test_context_not_ready")

    return DiscoveryService(db, jobs=jobs, identity=identity, validate_entry=ready)


async def seed(env: dict[str, Any], count: int) -> None:
    async with env["factory"]() as db, db.begin():
        present = set(await db.scalars(sa.select(MemorySourceState.source_message_id)))
        db.add_all(
            [
                MemorySourceState(
                    memory_id="bank",
                    tenant_id="tenant",
                    source_message_id=value,
                    source_revision=1,
                    generation=1,
                    content_sha256=content_hash("source " + str(value)),
                    state="active",
                    scope=env["scope"],
                )
                for value in range(1, count + 1)
                if value not in present
            ]
        )


def request(**changes: Any) -> dict[str, Any]:
    return {"scope": {"user_id": None, "agent_id": None, "session_id": None}, "batch_size": 32, "max_sources": 128, **changes}


async def create(env: dict[str, Any], operation_id: str = "discover", **changes: Any) -> dict[str, Any]:
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **request(**changes))
        return await owner.create("bank", operation_id=operation_id, expected_preview_sha256=preview["preview_sha256"], **request(**changes))


async def consume(env: dict[str, Any]) -> None:
    async def jobs(db: AsyncSession, memory_id: str, *, actor_id: str) -> Any:
        return service(db, env, actor_id=actor_id).jobs

    for _ in range(300):
        if not await process_next(env["factory"], jobs, worker_id="incremental-sql-test"):
            return
    raise AssertionError("bounded SQL work did not drain")


async def test_preview_is_readonly_and_large_inventory_partitions_every_selected_primary(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, 70)
    async with env["factory"]() as db:
        preview = await service(db, env).preview("bank", **request(max_sources=65, batch_size=16))
        assert preview["counts"]["active"] == 70 and preview["counts"]["selected_primary"] == 65 and preview["has_more"]
        ids = [int(source["source_id"]) for batch in preview["batches"] for source in batch["primary_sources"]]
        assert ids == list(range(1, 66)) and len(set(ids)) == 65
        assert all(len(batch["primary_sources"]) + len(batch["context_sources"]) <= 16 for batch in preview["batches"])
        assert env["analyses"] == env["snapshots"] == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceDiscovery)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage)) == 0


async def test_large_batched_empty_results_cover_only_processed_window(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = []
    await seed(env, 70)
    discovery = await create(env, max_sources=65, batch_size=16)
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="submit-window", limit=8)
    await consume(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        assert (await owner.get("bank", discovery["discovery_id"]))["state"] == "settled"
        preview = await owner.preview("bank", **request(max_sources=65, batch_size=16))
        assert preview["counts"]["covered"] == 65 and preview["counts"]["eligible"] == 5
        assert [source["source_id"] for batch in preview["batches"] for source in batch["primary_sources"]] == [str(value) for value in range(66, 71)]
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.state == "analyzed")) == 65
    assert env["analyses"] == 5


async def test_preview_cas_rejects_changed_selection_but_create_replay_keeps_original_plan(environment: dict[str, Any]) -> None:
    env = environment
    async with env["factory"]() as db:
        preview = await service(db, env).preview("bank", **request())
    await seed(env, 3)
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="incremental_preview_changed"):
            await service(db, env).create("bank", operation_id="stale-preview", expected_preview_sha256=preview["preview_sha256"], **request())
    discovery = await create(env)
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemorySourceState, ("bank", 1))
        source.generation += 1
    async with env["factory"]() as db:
        owner = service(db, env)
        repeated = await owner.create("bank", operation_id="discover", expected_preview_sha256=discovery["preview_sha256"], **request())
        assert repeated["discovery_id"] == discovery["discovery_id"] and repeated["batches"] == discovery["batches"]


async def test_deferred_primary_is_blocked_not_covered_and_does_not_starve_later_input(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"], env["defer_second"] = [], True
    discovery = await create(env)
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="run", limit=1)
    await consume(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        view = await owner.get("bank", discovery["discovery_id"])
        assert view["state"] == "partial" and view["progress"]["blocked"] == 1
        preview = await owner.preview("bank", **request())
        assert preview["counts"]["covered"] == preview["counts"]["blocked"] == 1
    await seed(env, 1)
    async with env["factory"]() as db:
        preview = await service(db, env).preview("bank", **request(max_sources=1))
        assert preview["batches"][0]["primary_sources"][0]["source_id"] == "1"
        larger = await service(db, env).preview("bank", **request(input_budget_tokens=32768))
        assert larger["counts"]["eligible"] == 3 and larger["counts"]["covered"] == 0


async def test_generation_late_arrival_and_new_strategy_are_discovered_again(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = []
    discovery = await create(env)
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="run")
    await consume(env)
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemorySourceState, ("bank", 10))
        source.generation += 1
    await seed(env, 1)
    async with env["factory"]() as db:
        preview = await service(db, env).preview("bank", **request())
        assert preview["counts"]["covered"] == 1 and preview["counts"]["eligible"] == 2
    env["strategy"] = "b" * 64
    async with env["factory"]() as db:
        assert (await service(db, env).preview("bank", **request()))["counts"]["eligible"] == 3


async def test_parent_run_freezes_batch_set_and_does_not_implicitly_retry_failed_execution(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = []
    discovery = await create(env, batch_size=1)
    async with env["factory"]() as db:
        owner = service(db, env)
        first = await owner.run("bank", discovery["discovery_id"], operation_id="first", limit=1)
        assert first["progress"]["submitted"] == 1
    await consume(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        repeated = await owner.run("bank", discovery["discovery_id"], operation_id="first", limit=1)
        assert repeated["progress"]["analyzed"] == repeated["progress"]["planned"] == 1
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await owner.run("bank", discovery["discovery_id"], operation_id="first", limit=2)
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await owner.cancel("bank", discovery["discovery_id"], operation_id="first")
        await owner.run("bank", discovery["discovery_id"], operation_id="second", limit=1)
    await consume(env)
    assert env["analyses"] == 2


async def test_create_commit_ack_loss_keeps_fixed_plan_and_original_handle(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **request())
        real = db.commit

        async def lost_ack() -> None:
            await real()
            raise sa.exc.DBAPIError(None, None, RuntimeError("controlled commit acknowledgement loss"), False)

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RevisionPersistenceUnknown) as raised:
            await owner.create("bank", operation_id="fixed-create", expected_preview_sha256=preview["preview_sha256"], **request())
        assert raised.value.operation_id == "fixed-create"
    async with env["factory"]() as db:
        owner = service(db, env)
        original = (await owner.list_discoveries("bank", operation_id="fixed-create"))["discoveries"][0]
        repeated = await owner.create("bank", operation_id="fixed-create", expected_preview_sha256=preview["preview_sha256"], **request())
        assert repeated["discovery_id"] == original["discovery_id"] and repeated["batches"] == original["batches"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceBatch)) == 1


async def test_unsettled_execution_or_actual_token_never_counts_as_completed(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = []
    discovery = await create(env)
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="run")
    await consume(env)
    async with env["factory"]() as db, db.begin():
        row = await db.get(MemoryWriterAdmission, ("bank", discovery["batches"][0]["analysis_operation_id"]))
        row.active_token = "undrained"
    async with env["factory"]() as db:
        owner = service(db, env)
        preview = await owner.preview("bank", **request())
        assert preview["counts"]["covered"] == 0 and preview["counts"]["inflight"] == 2
        assert (await owner.get("bank", discovery["discovery_id"]))["state"] == "running"


async def test_failed_first_child_does_not_starve_unstarted_children(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = [{"not": "a valid candidate"}]
    discovery = await create(env, batch_size=1)
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="first", limit=1)
    await consume(env)
    env["proposals"] = []
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="next", limit=1)
    await consume(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        repeated = await owner.run("bank", discovery["discovery_id"], operation_id="first", limit=1)
        assert repeated["progress"]["failed"] == repeated["progress"]["analyzed"] == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 2
    await consume(env)
    assert env["analyses"] == 2


async def test_rebound_coverage_rejects_old_child_before_another_model_call(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = [{"not": "valid"}]
    first = await create(env, operation_id="first")
    async with env["factory"]() as db:
        await service(db, env).run("bank", first["discovery_id"], operation_id="first-run")
    await consume(env)
    assert env["analyses"] == 1
    second = await create(env, operation_id="second")
    async with env["factory"]() as db:
        jobs = service(db, env).jobs
        await ExecutionService(db, jobs=jobs).submit("bank", first["batches"][0]["job_id"], operation_id=first["batches"][0]["analysis_operation_id"])
    await consume(env)
    assert env["analyses"] == 1
    async with env["factory"]() as db:
        rows = list(await db.scalars(sa.select(MemoryMaintenanceCoverage)))
        assert all(row.batch_id == second["batches"][0]["batch_id"] and row.state == "reserved" for row in rows)


async def test_concurrent_parent_replay_never_selects_another_batch(environment: dict[str, Any]) -> None:
    env = environment
    discovery = await create(env, batch_size=1)

    async def submit() -> dict[str, Any]:
        async with env["factory"]() as db:
            return await service(db, env).run("bank", discovery["discovery_id"], operation_id="same-run", limit=1)

    responses = await asyncio.gather(submit(), submit())
    assert all(row["progress"]["submitted"] == row["progress"]["planned"] == 1 for row in responses)
    async with env["factory"]() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceExecution)) == 1
    assert env["analyses"] == 0


async def test_maximum_principal_plan_has_bounded_read_query_count(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, 256)
    async with env["factory"]() as db, db.begin():
        rows = list(await db.scalars(sa.select(MemorySourceState)))
        for row in rows:
            row.scope = {**env["scope"], "user_id": str(row.source_message_id)}
    discovery = await create(env, max_sources=256)
    assert len(discovery["batches"]) == 256
    async with env["factory"]() as db:
        statements: list[str] = []

        def count(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            statements.append(statement)

        engine = cast(Any, db.bind).sync_engine
        sa.event.listen(engine, "before_cursor_execute", count)
        try:
            assert len((await service(db, env).get("bank", discovery["discovery_id"]))["batches"]) == 256
        finally:
            sa.event.remove(engine, "before_cursor_execute", count)
        assert len(statements) <= 8


async def test_explicit_entry_context_repeats_without_duplicating_primary_coverage(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = []
    await seed(env, 70)
    identity, text = "e" * 32, "context note"
    env["entries"] = [
        {
            "entry_id": identity,
            "revision": 1,
            "message_id": 1000,
            "generation": 1,
            "content": text,
            "content_sha256": content_hash(text),
            "scope": env["scope"],
            "source_ids": [10, 20],
            "message_type": "semantic",
        }
    ]
    async with env["factory"]() as db, db.begin():
        db.add(MemoryEntry(entry_id=identity, memory_id="bank", tenant_id="tenant", legacy_message_id=1000, current_revision=1, current_message_id=1000, generation=1, state="active"))
        await db.flush()
        db.add(
            MemoryEntryRevision(
                entry_id=identity,
                revision=1,
                memory_id="bank",
                tenant_id="tenant",
                message_id=1000,
                content=text,
                content_sha256=content_hash(text),
                parent_revision=None,
                origin="manual",
                assessment={},
                message_snapshot={"memory_id": "bank", **env["scope"], "message_type": "semantic"},
            )
        )
        await db.flush()
        db.add_all(
            [
                MemoryRevisionSource(
                    entry_id=identity,
                    revision=1,
                    source_message_id=value,
                    memory_id="bank",
                    source_revision=1,
                    source_generation=1,
                    content_sha256=content_hash("source " + str(value)),
                    relation="context",
                    locator={},
                )
                for value in (10, 20)
            ]
        )
    discovery = await create(env, entry_ids=[identity], max_sources=65, batch_size=16)
    primary: list[int] = []
    for batch in discovery["batches"]:
        primary.extend(int(item["source_id"]) for item in batch["primary_sources"])
        all_ids = {item["source_id"] for item in [*batch["primary_sources"], *batch["context_sources"]]}
        assert len(all_ids) <= 16 and {"10", "20"} <= all_ids and batch["entry_ids"] == [identity]
    assert sorted(primary) == list(range(1, 66)) and len(primary) == len(set(primary))
    async with env["factory"]() as db:
        await service(db, env).run("bank", discovery["discovery_id"], operation_id="context-run", limit=8)
    await consume(env)
    async with env["factory"]() as db:
        assert (await service(db, env).get("bank", discovery["discovery_id"]))["state"] == "settled"
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.state == "analyzed")) == 65


async def test_parent_reservation_commit_ack_loss_recovers_the_same_batch(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    env["proposals"] = []
    discovery = await create(env, batch_size=1)
    async with env["factory"]() as db:
        real = db.commit

        async def lost_ack() -> None:
            await real()
            raise sa.exc.DBAPIError(None, None, RuntimeError("controlled parent reservation acknowledgement loss"), False)

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RevisionPersistenceUnknown) as raised:
            await service(db, env).run("bank", discovery["discovery_id"], operation_id="original-run", limit=1)
        assert raised.value.operation_id == "original-run"
    async with env["factory"]() as db:
        row = await db.get(MemoryMaintenanceDiscovery, discovery["discovery_id"])
        assert row is not None and row.receipt["commands"]["original-run"]["batch_ids"] == [discovery["batches"][0]["batch_id"]]
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 0
    async with env["factory"]() as db:
        repeated = await service(db, env).run("bank", discovery["discovery_id"], operation_id="original-run", limit=1)
        assert repeated["progress"]["submitted"] == repeated["progress"]["planned"] == 1
    await consume(env)
    assert env["analyses"] == 1


async def test_incremental_migration_is_idempotent_and_rejects_incompatible_table(environment: dict[str, Any]) -> None:
    path = Path("configs/alembic/versions/f8c0e2a4b6d8_add_memory_incremental_coverage.py")
    spec = importlib.util.spec_from_file_location("incremental_test_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = environment["engine"]
    with engine.begin() as connection:
        cast(sa.Table, MemoryMaintenanceFragmentCoverage.__table__).drop(connection)
        for model in reversed(MEMORY_INCREMENTAL_MODELS):
            cast(sa.Table, model.__table__).drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()
    with pytest.raises(RuntimeError, match="incompatible columns"), engine.begin() as connection:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_memory_maintenance_discoveries ADD COLUMN unexpected text"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
