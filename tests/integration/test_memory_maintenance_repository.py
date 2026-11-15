"""Maintenance SQL durability with controlled, transaction-free model work."""

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from api.db.memory_maintenance_models import MEMORY_MAINTENANCE_MODELS, MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, content_hash
from tests.support.memory_maintenance_repository import create, service
from tests.support.memory_maintenance_repository import environment as environment


async def test_create_replay_never_resnapshots_and_get_is_readonly(environment: dict[str, Any]) -> None:
    env = environment
    job = await create(env)
    assert env["snapshots"] == 1 and env["analyses"] == 0
    assert await create(env) == job and env["snapshots"] == 1
    async with env["factory"]() as db:
        owner = service(db, env)
        assert await owner.get("bank", job["job_id"]) == job
        assert (await owner.list_jobs("bank"))["jobs"] == [job]
        assert (await owner.candidates("bank", job["job_id"]))["candidates"] == []
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await owner.create("bank", operation_id="create", source_ids=[10], entry_ids=[])
        with pytest.raises(RevisionNotFound):
            await service(db, env, actor_id="denied").get("bank", job["job_id"])
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission)) == 0


async def test_analysis_and_review_are_isolated_idempotent_and_do_not_complete_deferred_inputs(environment: dict[str, Any]) -> None:
    env = environment
    env["defer_second"] = True
    job = await create(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        ready = await owner.run("bank", job["job_id"], operation_id="analyze")
        assert ready["state"] == "ready" and ready["coverage"] == {"selected": 2, "included": 1, "analyzed": 1, "deferred": 1}
        assert await owner.run("bank", job["job_id"], operation_id="analyze") == ready and env["analyses"] == 1
        candidate = (await owner.candidates("bank", job["job_id"]))["candidates"][0]
        assert candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified"
        approved = await owner.review("bank", job["job_id"], candidate["candidate_id"], operation_id="review", expected_revision=1, decision="approve")
        assert approved["state"] == "approved" and approved["revision"] == 2 and approved["assessment"]["kind"] == "hypothesis"
        assert await owner.review("bank", job["job_id"], candidate["candidate_id"], operation_id="review", expected_revision=1, decision="approve") == approved
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await owner.review("bank", job["job_id"], candidate["candidate_id"], operation_id="review", expected_revision=1, decision="reject")
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 1
        raw = await reader.get(MemoryMaintenanceInput, (job["job_id"], "input20"))
        assert raw.snapshot == {} and raw.coverage == "deferred"
        attempt = await reader.get(MemoryWriterAdmission, ("bank", "analyze"))
        assert attempt.active_token is None and attempt.publication_state == "ready" and attempt.message_ids == []


async def test_invalid_model_authority_saves_no_candidates_and_same_operation_can_retry(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = [{"action": "add", "content": "wrong authority", "message_type": "semantic", "source_input_ids": ["input10"], "reason": "claim", "assessment": {"kind": "verified"}}]
    job = await create(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        failed = await owner.run("bank", job["job_id"], operation_id="analyze")
        assert failed["state"] == "failed" and failed["coverage"]["analyzed"] == 0
        assert failed["usage"]["input_tokens"] == 400 and failed["usage"]["attempts_recorded"] == 1 and failed["usage"]["provider_billing_verified"] is False
        assert (await owner.candidates("bank", job["job_id"]))["candidates"] == []
        env["proposals"] = None
        ready = await owner.run("bank", job["job_id"], operation_id="analyze")
        assert ready["state"] == "ready" and ready["usage"]["input_tokens"] == 800 and ready["usage"]["attempts_recorded"] == 2
        assert env["analyses"] == 2


async def test_cancel_during_model_work_redacts_snapshots_and_waits_actual_drain(environment: dict[str, Any]) -> None:
    env = environment
    env["started"], env["release"] = asyncio.Event(), asyncio.Event()
    job = await create(env)

    async def analyze() -> dict[str, Any]:
        async with env["factory"]() as db:
            return await service(db, env).run("bank", job["job_id"], operation_id="analyze")

    pending = asyncio.create_task(analyze())
    try:
        await asyncio.wait_for(env["started"].wait(), timeout=5)
        async with env["factory"]() as db:
            canceled = await service(db, env).cancel("bank", job["job_id"], operation_id="cancel")
            assert canceled["state"] == "cancel_requested"
        async with env["factory"]() as reader:
            attempt = await reader.get(MemoryWriterAdmission, ("bank", "analyze"))
            assert attempt.active_token is not None and attempt.publication_state == "canceled"
        env["release"].set()
        assert (await pending)["state"] == "canceled"
        async with env["factory"]() as reader:
            assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0
            inputs = list(await reader.scalars(sa.select(MemoryMaintenanceInput)))
            assert all(item.snapshot == {} and item.coverage != "analyzed" for item in inputs)
            attempt = await reader.get(MemoryWriterAdmission, ("bank", "analyze"))
            assert attempt.active_token is None
    finally:
        env["release"].set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_source_generation_aba_during_model_work_cannot_save_a_late_candidate(environment: dict[str, Any]) -> None:
    env = environment
    env["started"], env["release"] = asyncio.Event(), asyncio.Event()
    job = await create(env)

    async def analyze() -> dict[str, Any]:
        async with env["factory"]() as db:
            return await service(db, env).run("bank", job["job_id"], operation_id="analyze")

    pending = asyncio.create_task(analyze())
    try:
        await asyncio.wait_for(env["started"].wait(), timeout=5)
        async with env["factory"]() as db, db.begin():
            await db.execute(sa.update(MemorySourceState).where(MemorySourceState.source_message_id == 20).values(generation=3))
        env["release"].set()
        result = await pending
        assert result["state"] == "failed" and result["error_code"] == "maintenance_source_changed"
        async with env["factory"]() as reader:
            assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0
            assert all(item.coverage == "included" for item in await reader.scalars(sa.select(MemoryMaintenanceInput)))
    finally:
        env["release"].set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_analysis_commit_lost_ack_keeps_exact_candidates_without_model_replay(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    job = await create(env)
    async with env["factory"]() as db:
        original, lost = db.commit, False

        async def commit() -> None:
            nonlocal lost
            marker = not lost and await db.scalar(sa.select(MemoryMaintenanceJob.state).where(MemoryMaintenanceJob.job_id == job["job_id"])) == "ready"
            await original()
            if marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        owner = service(db, env)
        ready = await owner.run("bank", job["job_id"], operation_id="analyze")
        assert lost and ready["state"] == "ready"
        assert ready["usage"]["attempts_recorded"] == 1 and ready["usage"]["input_tokens"] == 400
        candidates = await owner.candidates("bank", job["job_id"])
        assert await owner.run("bank", job["job_id"], operation_id="analyze") == ready
        assert await owner.candidates("bank", job["job_id"]) == candidates and env["analyses"] == 1
        monkeypatch.setattr(db, "commit", original)


async def test_unfinished_analysis_token_is_not_reclaimed_by_another_run(environment: dict[str, Any]) -> None:
    env = environment
    job = await create(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        await owner.admit("bank", operation_id="analyze", kind="dream_analyze", request={"job_id": job["job_id"], "job_generation": 1})
        claimed = await owner.claim("bank", "analyze")
        with pytest.raises(RevisionBusy, match="operator_reconciliation"):
            await owner.run("bank", job["job_id"], operation_id="analyze")
        assert env["analyses"] == 0
        await owner.finish("bank", "analyze", token=claimed["active_token"])


async def test_review_of_an_unrelated_candidate_survives_an_earlier_patch_publication(environment: dict[str, Any]) -> None:
    env = environment
    content = "Shanghai; loves tea"
    env["entries"] = [
        {
            "entry_id": "entry",
            "revision": 1,
            "generation": 1,
            "message_id": 11,
            "content": content,
            "content_sha256": content_hash(content),
            "scope": dict(env["scope"]),
            "source_ids": [10],
            "message_type": "semantic",
        }
    ]
    env["proposals"] = [
        {"action": "patch", "target_entry_id": "entry", "patch": {"old_text": "Shanghai", "new_text": "Beijing"}, "source_input_ids": ["input10"], "reason": "correction"},
        {"action": "add", "content": "a separate fact", "message_type": "semantic", "source_input_ids": ["input20"], "reason": "independent source"},
    ]
    async with env["factory"]() as db:
        owner = service(db, env)
        job = await owner.create("bank", operation_id="create", source_ids=[10, 20], entry_ids=["entry"])
        assert (await owner.run("bank", job["job_id"], operation_id="analyze"))["state"] == "ready"
        rows = (await owner.candidates("bank", job["job_id"]))["candidates"]
        patched, added = next(row for row in rows if row["action"] == "patch"), next(row for row in rows if row["action"] == "add")
        assert patched["diff"] == {"old_text": content, "new_text": "Beijing; loves tea"}
        approved = await owner.review("bank", job["job_id"], patched["candidate_id"], operation_id="approve-patch", expected_revision=1, decision="approve")
        assert approved["diff"] == patched["diff"]
    async with env["factory"]() as db, db.begin():
        candidate = await db.get(MemoryMaintenanceCandidate, patched["candidate_id"])
        candidate.state, candidate.publication_operation_id = "published", "published-operation"
    env["current_revisions"] = {"entry": 2}
    async with env["factory"]() as db:
        owner = service(db, env)
        audited = (await owner.candidates("bank", job["job_id"]))["candidates"]
        assert all(not row["redacted"] for row in audited)
        published = next(row for row in audited if row["candidate_id"] == patched["candidate_id"])
        assert published["state"] == "published" and published["base_content"] == content and published["content"] == "Beijing; loves tea"
        assert published["diff"] == {"old_text": content, "new_text": "Beijing; loves tea"}
        assert (await owner.review("bank", job["job_id"], added["candidate_id"], operation_id="approve-add", expected_revision=1, decision="approve"))["state"] == "approved"
        assert (await owner.review("bank", job["job_id"], patched["candidate_id"], operation_id="approve-patch", expected_revision=1, decision="approve"))["revision"] == 2


@pytest.mark.parametrize("phase", ["create", "review"])
async def test_create_or_review_commit_lost_ack_reuses_the_same_command(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    env = environment
    async with env["factory"]() as db:
        owner = service(db, env)
        if phase == "review":
            job = await owner.create("bank", operation_id="create", source_ids=[10, 20], entry_ids=[])
            await owner.run("bank", job["job_id"], operation_id="analyze")
            candidate = (await owner.candidates("bank", job["job_id"]))["candidates"][0]
        original, lost = db.commit, False

        async def commit() -> None:
            nonlocal lost
            marker = not lost
            await original()
            if marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        if phase == "create":
            with pytest.raises(RevisionPersistenceUnknown):
                await owner.create("bank", operation_id="create", source_ids=[10, 20], entry_ids=[])
            job = await owner.create("bank", operation_id="create", source_ids=[10, 20], entry_ids=[])
            assert job["state"] == "pending" and env["snapshots"] == 1
            assert len((await owner.list_jobs("bank"))["jobs"]) == 1
        else:
            with pytest.raises(RevisionPersistenceUnknown):
                await owner.review("bank", job["job_id"], candidate["candidate_id"], operation_id="review", expected_revision=1, decision="approve")
            approved = await owner.review("bank", job["job_id"], candidate["candidate_id"], operation_id="review", expected_revision=1, decision="approve")
            assert approved["revision"] == 2 and approved["state"] == "approved" and env["analyses"] == 1
        assert lost
        monkeypatch.setattr(db, "commit", original)


async def test_concurrent_reviews_with_the_same_revision_have_one_sql_winner(environment: dict[str, Any]) -> None:
    env = environment
    job = await create(env)
    async with env["factory"]() as db:
        owner = service(db, env)
        await owner.run("bank", job["job_id"], operation_id="analyze")
        candidate = (await owner.candidates("bank", job["job_id"]))["candidates"][0]

    async def review(operation_id: str, decision: str) -> dict[str, Any]:
        async with env["factory"]() as db:
            return await service(db, env).review("bank", job["job_id"], candidate["candidate_id"], operation_id=operation_id, expected_revision=1, decision=decision)

    outcomes = await asyncio.gather(review("approve", "approve"), review("reject", "reject"), return_exceptions=True)
    assert sum(isinstance(result, dict) and result["revision"] == 2 for result in outcomes) == 1
    assert sum(isinstance(result, RevisionConflict) for result in outcomes) == 1


def _migration() -> Any:
    path = Path(__file__).parents[2] / "configs/alembic/versions/f4b6d8e0a2c4_add_memory_maintenance_candidates.py"
    spec = importlib.util.spec_from_file_location("tested_maintenance_migration", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


async def test_migration_bootstraps_revalidates_and_refuses_nonempty_review_history(environment: dict[str, Any]) -> None:
    from api.db.memory_execution_models import MemoryMaintenanceExecution

    migration, engine = _migration(), environment["engine"]
    with engine.begin() as connection:
        MemoryMaintenanceExecution.__table__.drop(connection)
        for model in reversed(MEMORY_MAINTENANCE_MODELS):
            model.__table__.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        MemoryMaintenanceExecution.__table__.create(connection)
    job = await create(environment)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        with pytest.raises(RuntimeError, match="review history"):
            migration.downgrade()
    async with environment["factory"]() as reader:
        assert (await reader.get(MemoryMaintenanceJob, job["job_id"])).state == "pending"


async def test_migration_rejects_incompatible_existing_table_without_replacing_it(environment: dict[str, Any]) -> None:
    from api.db.memory_execution_models import MemoryMaintenanceExecution

    migration, engine = _migration(), environment["engine"]
    with engine.begin() as connection:
        MemoryMaintenanceExecution.__table__.drop(connection)
        for model in reversed(MEMORY_MAINTENANCE_MODELS):
            model.__table__.drop(connection)
        connection.execute(sa.text("CREATE TABLE usr_ai.t_ai_memory_maintenance_jobs (job_id TEXT PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="incompatible columns"):
            migration.upgrade()
        assert {column["name"] for column in sa.inspect(connection).get_columns("t_ai_memory_maintenance_jobs", schema="usr_ai")} == {"job_id"}
