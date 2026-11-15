"""Real SQL background dispatch, fencing, recovery and process rediscovery."""

import asyncio
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db.db_models import Memory
from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.maintenance import execution
from memory.maintenance.jobs import JobService
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown
from memory.revisions.sources import SourceService
from memory.writes.operators import acknowledge_attempt_quiescence
from tests.integration.test_memory_maintenance_repository import create, service
from tests.integration.test_memory_maintenance_repository import environment as environment
from tests.support.resource_journal import ResourceJournal


def jobs_factory(env: dict[str, Any]) -> execution.JobsFactory:
    async def build(db: AsyncSession, memory_id: str, *, actor_id: str) -> JobService:
        assert memory_id == "bank"
        jobs = service(db, env, actor_id=actor_id)

        async def authorize(session: AsyncSession, identity: str) -> None:
            permissions = await session.scalar(sa.select(Memory.permissions).where(Memory.id == identity, Memory.tenant_id == "tenant"))
            if permissions != "team":
                raise RevisionNotFound("memory_not_found")

        if env.get("check_acl"):
            jobs.authorize = authorize
        return jobs

    return build


async def submitted(env: dict[str, Any]) -> dict[str, Any]:
    job = await create(env)
    async with env["factory"]() as db:
        return await execution.ExecutionService(db, jobs=service(db, env)).submit("bank", job["job_id"], operation_id="analyze")


async def test_submission_is_atomic_idempotent_and_does_not_run_a_model(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    assert job["state"] == "pending" and job["execution"]["state"] == "queued" and env["analyses"] == 0
    async with env["factory"]() as db:
        jobs = service(db, env)
        queue = execution.ExecutionService(db, jobs=jobs)
        assert await queue.submit("bank", job["job_id"], operation_id="analyze") == job
        assert (await jobs.list_jobs("bank"))["jobs"][0]["execution"] == job["execution"]
        assert (await jobs.operation("bank", "analyze"))["execution"] == job["execution"]
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await execution.ExecutionService(db, jobs=service(db, env, actor_id="other")).submit("bank", job["job_id"], operation_id="analyze")
        with pytest.raises(RevisionBusy, match="background_execution_required"):
            await jobs.run("bank", job["job_id"], operation_id="analyze")
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceExecution)) == 1
        admission = await reader.get(MemoryWriterAdmission, ("bank", "analyze"))
        assert admission.state == "queued" and admission.active_token is None
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 1
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0


async def test_a_new_worker_discovers_the_queue_and_only_generates_candidates_once(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="new-worker")
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="another-worker")
    async with env["factory"]() as db:
        jobs = service(db, env)
        ready = await jobs.get("bank", job["job_id"])
        assert ready["state"] == "ready" and ready["execution"]["state"] == "succeeded" and ready["execution"]["attempt"] == 1
        assert env["analyses"] == 1
        assert (await jobs.candidates("bank", job["job_id"]))["candidates"][0]["state"] == "pending"


async def test_two_workers_claim_one_dispatch_and_a_late_expired_owner_cannot_start(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)

    async def claim(worker: str) -> dict[str, Any] | None:
        async with env["factory"]() as db:
            return await execution.claim_next(db, worker_id=worker, claim_timeout_seconds=5)

    rows = await asyncio.gather(claim("one"), claim("two"))
    assert sum(item is not None for item in rows) == 1
    old = next(item for item in rows if item is not None)
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(MemoryMaintenanceExecution).values(heartbeat_at=datetime.now(UTC) - timedelta(seconds=10)))
    new = await claim("replacement")
    assert new is not None and new["claim_token"] != old["claim_token"]
    async with env["factory"]() as db:
        jobs = service(db, env)
        assert (await jobs.run("bank", job["job_id"], operation_id="analyze", execution_claim_token=old["claim_token"]))["state"] == "pending"
        assert env["analyses"] == 0
        assert (await jobs.run("bank", job["job_id"], operation_id="analyze", execution_claim_token=new["claim_token"]))["state"] == "ready"
        assert env["analyses"] == 1


async def test_expired_running_attempt_stays_blocked_until_exact_operator_attestation(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    async with env["factory"]() as db:
        claimed = await execution.claim_next(db, worker_id="crashed", claim_timeout_seconds=5)
        jobs = service(db, env)
        attempt = await jobs.claim("bank", "analyze")
        await jobs._start("bank", job["job_id"], "analyze", attempt["active_token"], 1, claimed["claim_token"])
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(MemoryMaintenanceExecution).values(heartbeat_at=datetime.now(UTC) - timedelta(seconds=20)))
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="restart", claim_timeout_seconds=5, heartbeat_seconds=1)
    async with env["factory"]() as db:
        jobs = service(db, env)
        assert (await jobs.get("bank", job["job_id"]))["execution"]["needs_operator_reconciliation"] is True
        await acknowledge_attempt_quiescence(
            db,
            memory_id="bank",
            tenant_id="tenant",
            operation_id="analyze",
            expected_generation=1,
            expected_attempt=1,
            expected_active_token=attempt["active_token"],
            evidence_reference="test://owned-pre-model-attempt-confirmed-idle",
        )
        # GET derives this flag from the canonical token, without requeuing.
        observed = await jobs.get("bank", job["job_id"])
        assert observed["execution"]["state"] == "needs_reconciliation" and observed["execution"]["needs_operator_reconciliation"] is False
        queued = await execution.ExecutionService(db, jobs=jobs).submit("bank", job["job_id"], operation_id="analyze")
        assert queued["execution"]["state"] == "queued" and env["analyses"] == 0
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="after-attestation")
    assert env["analyses"] == 1


async def test_cancel_before_dispatch_cancels_the_sql_admission_without_model_work(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    async with env["factory"]() as db:
        canceled = await service(db, env).cancel("bank", job["job_id"], operation_id="cancel")
        assert canceled["state"] == "canceled" and canceled["execution"]["state"] == "canceled"
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="after-cancel")
    assert env["analyses"] == 0


async def test_cancel_running_model_waits_for_the_actual_attempt_to_finish(environment: dict[str, Any]) -> None:
    env = environment
    env["started"], env["release"] = asyncio.Event(), asyncio.Event()
    job = await submitted(env)
    pending = asyncio.create_task(execution.process_next(env["factory"], jobs_factory(env), worker_id="slow", heartbeat_seconds=0.1))
    try:
        await asyncio.wait_for(env["started"].wait(), timeout=5)
        async with env["factory"]() as db:
            canceled = await service(db, env).cancel("bank", job["job_id"], operation_id="cancel")
            assert canceled["state"] == "cancel_requested" and canceled["execution"]["state"] == "running"
        env["release"].set()
        assert await pending
        async with env["factory"]() as db:
            view = await service(db, env).get("bank", job["job_id"])
            assert view["state"] == "canceled" and view["execution"]["state"] == "canceled"
            admission = await db.get(MemoryWriterAdmission, ("bank", "analyze"))
            assert admission.active_token is None
    finally:
        env["release"].set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_nonanchor_source_withdrawal_cancels_background_work_without_repersisting_text(environment: dict[str, Any]) -> None:
    env = environment
    env["started"], env["release"] = asyncio.Event(), asyncio.Event()
    job = await submitted(env)
    pending = asyncio.create_task(execution.process_next(env["factory"], jobs_factory(env), worker_id="source-withdrawal", heartbeat_seconds=0.1))
    try:
        await asyncio.wait_for(env["started"].wait(), timeout=5)
        async with env["factory"]() as db:

            async def read(memory_id: str, source_id: int) -> dict[str, Any]:
                assert not db.in_transaction()
                return {
                    "memory_id": memory_id,
                    "message_id": source_id,
                    "message_type": "raw",
                    "source_id": 0,
                    "content": "source " + str(source_id),
                    "status": True,
                    "forget_at": None,
                    **env["scope"],
                }

            sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=service(db, env).authorize, message_reader=read)
            await sources.withdraw_source("bank", 20, operation_id="withdraw", expected_source_revision=1, reason="withdraw nonanchor evidence")
        env["release"].set()
        assert await pending
        async with env["factory"]() as db:
            state = await service(db, env).get("bank", job["job_id"])
            assert state["state"] == "redacted" and state["execution"]["state"] == "canceled"
            assert (await db.get(MemoryMaintenanceJob, job["job_id"])).entry_snapshots == []
            assert all(item.snapshot == {} for item in await db.scalars(sa.select(MemoryMaintenanceInput)))
            assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0
    finally:
        env["release"].set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_worker_rechecks_current_acl_before_any_model_call(environment: dict[str, Any]) -> None:
    env = environment
    env["check_acl"] = True
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).values(permissions="team"))
    await submitted(env)
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(Memory).values(permissions="me"))
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="revoked")
    assert env["analyses"] == 0
    async with env["factory"]() as db:
        row = await db.scalar(sa.select(MemoryMaintenanceExecution))
        assert row.state == "failed" and row.error_code == "memory_not_found"


@pytest.mark.parametrize("applied", [False, True])
async def test_terminal_sql_failure_stops_without_repeating_completed_analysis(environment: dict[str, Any], applied: bool) -> None:
    env = environment
    job = await submitted(env)
    lost = False

    class Session(AsyncSession):
        async def commit(self) -> None:
            nonlocal lost
            state = await self.scalar(sa.select(MemoryMaintenanceExecution.state).where(MemoryMaintenanceExecution.job_id == job["job_id"]))
            if not lost and state == "succeeded":
                lost = True
                if applied:
                    await super().commit()
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("terminal acknowledgement lost"), connection_invalidated=True)
            await super().commit()

    factory = async_sessionmaker(env["factory"].kw["bind"], class_=Session, expire_on_commit=False)
    with pytest.raises(execution.ExecutionBookkeepingUnknown):
        await execution.process_next(factory, jobs_factory(env), worker_id="lost-terminal")
    assert lost and env["analyses"] == 1
    async with env["factory"]() as db, db.begin():
        await db.execute(sa.update(MemoryMaintenanceExecution).values(heartbeat_at=datetime.now(UTC) - timedelta(seconds=120)))
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="recovery")
    async with env["factory"]() as db:
        view = await service(db, env).get("bank", job["job_id"])
        assert view["execution"]["state"] == "succeeded" and env["analyses"] == 1


async def test_submit_commit_lost_ack_keeps_one_durable_queue_item(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    job = await create(env)
    async with env["factory"]() as db:
        original, lost = db.commit, False

        async def commit() -> None:
            nonlocal lost
            marker = not lost and await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceExecution)) == 1
            await original()
            if marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("submit acknowledgement lost"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", commit)
        queue = execution.ExecutionService(db, jobs=service(db, env))
        with pytest.raises(RevisionPersistenceUnknown):
            await queue.submit("bank", job["job_id"], operation_id="analyze")
        assert (await queue.submit("bank", job["job_id"], operation_id="analyze"))["execution"]["state"] == "queued"
        assert lost and env["analyses"] == 0
        monkeypatch.setattr(db, "commit", original)
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="after-submit")
    assert env["analyses"] == 1


async def test_dispatch_claim_commit_lost_ack_never_starts_unconfirmed_work(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    lost = False

    class Session(AsyncSession):
        async def commit(self) -> None:
            nonlocal lost
            state = await self.scalar(sa.select(MemoryMaintenanceExecution.state).where(MemoryMaintenanceExecution.job_id == job["job_id"]))
            marker = not lost and state == "claimed"
            await super().commit()
            if marker:
                lost = True
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("dispatch acknowledgement lost"), connection_invalidated=True)

    factory = async_sessionmaker(env["factory"].kw["bind"], class_=Session, expire_on_commit=False)
    with pytest.raises(RevisionPersistenceUnknown) as failure:
        await execution.process_next(factory, jobs_factory(env), worker_id="unknown-dispatch")
    assert failure.value.operation_id == "analyze" and lost and env["analyses"] == 0
    async with env["factory"]() as db, db.begin():
        admission = await db.get(MemoryWriterAdmission, ("bank", "analyze"))
        assert admission.active_token is None and admission.attempt == 0
        await db.execute(sa.update(MemoryMaintenanceExecution).values(heartbeat_at=datetime.now(UTC) - timedelta(seconds=120)))
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="confirmed-dispatch")
    assert env["analyses"] == 1


async def test_failed_analysis_requires_an_explicit_same_operation_retry(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = [{"action": "overwrite", "content": "forbidden"}]
    job = await submitted(env)
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="first")
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="no-blind-retry")
    assert env["analyses"] == 1
    env["proposals"] = None
    async with env["factory"]() as db:
        assert (await execution.ExecutionService(db, jobs=service(db, env)).submit("bank", job["job_id"], operation_id="analyze"))["execution"]["state"] == "queued"
    assert await execution.process_next(env["factory"], jobs_factory(env), worker_id="explicit-retry")
    assert env["analyses"] == 2


async def test_explicit_failed_retries_are_bounded_by_actual_model_starts(environment: dict[str, Any]) -> None:
    env = environment
    env["proposals"] = [{"action": "overwrite", "content": "forbidden"}]
    job = await submitted(env)
    for attempt in range(3):
        if attempt:
            async with env["factory"]() as db:
                await execution.ExecutionService(db, jobs=service(db, env)).submit("bank", job["job_id"], operation_id="analyze")
        assert await execution.process_next(env["factory"], jobs_factory(env), worker_id=f"attempt-{attempt}")
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="execution_attempts_exhausted"):
            await execution.ExecutionService(db, jobs=service(db, env)).submit("bank", job["job_id"], operation_id="analyze")
    assert not await execution.process_next(env["factory"], jobs_factory(env), worker_id="no-fourth-model")
    assert env["analyses"] == 3


def _migration() -> Any:
    path = Path(__file__).parents[2] / "configs/alembic/versions/f6b8d0e2a4c6_add_memory_maintenance_executions.py"
    spec = importlib.util.spec_from_file_location("tested_execution_migration", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


async def test_execution_migration_revalidates_existing_schema_and_retains_queue(environment: dict[str, Any]) -> None:
    engine, migration = environment["engine"], _migration()
    with engine.begin() as connection:
        MemoryMaintenanceExecution.__table__.drop(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
    job = await submitted(environment)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="persistent memory execution"):
        migration.downgrade()
    async with environment["factory"]() as db:
        assert (await db.get(MemoryMaintenanceExecution, job["job_id"])).state == "queued"


async def test_execution_migration_refuses_an_incompatible_table_without_replacing_it(environment: dict[str, Any]) -> None:
    engine, migration = environment["engine"], _migration()
    with engine.begin() as connection:
        MemoryMaintenanceExecution.__table__.drop(connection)
        connection.execute(sa.text("CREATE TABLE usr_ai.t_ai_memory_maintenance_executions (job_id TEXT PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="incompatible columns"):
            migration.upgrade()
        assert {column["name"] for column in sa.inspect(connection).get_columns("t_ai_memory_maintenance_executions", schema="usr_ai")} == {"job_id"}


async def test_fresh_processes_discover_persisted_work_and_do_not_replay_success(environment: dict[str, Any]) -> None:
    env = environment
    job = await submitted(env)
    journal = ResourceJournal("memory_maintenance_worker_process")
    code = """import asyncio,json,os
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession,create_async_engine,async_sessionmaker
from tests.support.database import _pg_url
from tests.integration.test_memory_maintenance_repository import service
from memory.maintenance.execution import process_next
from memory.maintenance.jobs import JobService
async def main() -> None:
 engine=create_async_engine(_pg_url(os.environ["MEMORY_EXECUTION_SCRATCH_DATABASE"]))
 factory=async_sessionmaker(engine,expire_on_commit=False)
 state={"scope":{"user_id":"user","agent_id":"agent","session_id":"session"},"snapshots":0,"analyses":0,"defer_second":False,"proposals":None}
 async def jobs(db: AsyncSession,memory_id: str,*,actor_id: str) -> JobService: return service(db,state,actor_id=actor_id)
 try:
  handled=await process_next(factory,jobs,worker_id="independent-process")
  print(json.dumps({"handled":handled,"analyses":state["analyses"]}))
 finally: await engine.dispose()
asyncio.run(main())"""
    child_env = {**os.environ, "MEMORY_EXECUTION_SCRATCH_DATABASE": env["engine"].url.database}
    child_env.setdefault("MULTIRAG_RUN_ID", journal.run_id)
    gate_journal = os.environ.get("MULTIRAG_RUN_JOURNAL", str(journal.path))
    command = [sys.executable, "-B", "scripts/run_registered.py", "--child", gate_journal, sys.executable, "-B", "-c", code]

    async def child(sequence: int) -> dict[str, Any]:
        journal.event("reserve-process", identity=f"worker-{sequence}", command=command, database=env["engine"].url.database, inherited_process_group=True)
        process = await asyncio.create_subprocess_exec(*command, env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        journal.event("process-started", identity=f"worker-{sequence}", pid=process.pid)
        try:
            output, errors = await asyncio.wait_for(process.communicate(), timeout=20)
            assert process.returncode == 0, errors.decode()
            return json.loads(output.decode().splitlines()[-1])
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
            journal.event("process-cleanup-readback", identity=f"worker-{sequence}", pid=process.pid, absent=True)

    assert await child(1) == {"handled": True, "analyses": 1}
    assert await child(2) == {"handled": False, "analyses": 0}
    async with env["factory"]() as db:
        assert (await db.get(MemoryMaintenanceExecution, job["job_id"])).state == "succeeded"
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 1
