"""Real SQL contracts for independent connector task scheduling and migration."""

import importlib.util
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service
from api.db.db_models import Connector, Connector2Kb, Knowledgebase, SyncLogs, get_engine_config
from api.db.services.connector_service import ConnectorService, SyncLogsService, SyncTaskCancelled, _schedule_time_condition
from common.constants import ConnectorTaskType, TaskStatus
from tests.support.database import scratch_database


@pytest.fixture
def connector_control_engine(bootstrapped_engine: sa.Engine) -> Iterator[sa.Engine]:
    engine = sa.create_engine(bootstrapped_engine.url, **get_engine_config({"name": "postgresql+psycopg"}))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def scheduled(connector_control_engine: sa.Engine) -> Iterator[dict[str, Any]]:
    connector_id, other_connector = uuid4().hex, uuid4().hex
    kb_ids = [uuid4().hex, uuid4().hex]
    owner = uuid4().hex
    with Session(connector_control_engine) as db:
        for kb_id in kb_ids:
            db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name="task control", embd_id="controlled"))
            db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=other_connector, kb_id=kb_ids[0], auto_parse="0"))
        for identifier in (connector_id, other_connector):
            db.add(
                Connector(
                    id=identifier, tenant_id=owner, name="task control", source="s3", input_type="poll", config={"sync_deleted_files": True}, refresh_freq=5, prune_freq=17, status=TaskStatus.UNSTART
                )
            )
        db.commit()
    try:
        yield {"engine": connector_control_engine, "connector": connector_id, "other": other_connector, "kbs": kb_ids}
    finally:
        with Session(connector_control_engine) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id.in_([connector_id, other_connector])))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id.in_([connector_id, other_connector])))
            db.execute(sa.delete(Connector).where(Connector.id.in_([connector_id, other_connector])))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(kb_ids)))
            db.commit()


def test_existing_logs_migrate_to_sync_and_task_type_roundtrips(tmp_path: Path) -> None:
    evidence = Path(os.environ.get("CONNECTOR_TASK_EVIDENCE", str(tmp_path))) / "migration"
    with scratch_database(evidence) as engine:
        identifier = uuid4().hex
        with Session(engine) as db:
            db.add(SyncLogs(id=identifier, connector_id=uuid4().hex, kb_id=uuid4().hex, status=TaskStatus.DONE))
            db.commit()
        path = Path(__file__).resolve().parents[2] / "configs/alembic/versions/a7c9e1f3b5d7_connector_task_type.py"
        spec = importlib.util.spec_from_file_location("connector_task_migration", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with engine.begin() as connection:
            connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_sync_logs DROP COLUMN task_type"))
            with Operations.context(MigrationContext.configure(connection)):
                module.upgrade()
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT task_type FROM usr_ai.t_ai_sync_logs WHERE id=:id"), {"id": identifier}) == "sync"
            assert any(index["column_names"] == ["task_type"] for index in sa.inspect(connection).get_indexes("t_ai_sync_logs", schema="usr_ai"))
        with Session(engine) as db:
            row = db.get(SyncLogs, identifier)
            assert row is not None
            row.task_type = ConnectorTaskType.PRUNE
            db.commit()
        with Session(engine) as db:
            assert db.get(SyncLogs, identifier).to_dict()["task_type"] == "prune"
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
            assert "task_type" not in {column["name"] for column in sa.inspect(connection).get_columns("t_ai_sync_logs", schema="usr_ai")}


def test_resume_schedules_all_kbs_and_types_once_immediately(scheduled: dict[str, Any]) -> None:
    env = scheduled
    with Session(env["engine"]) as db:
        ConnectorService.schedule_tasks(db, env["connector"])
        ConnectorService.schedule_tasks(db, env["connector"])
    with Session(env["engine"]) as db:
        rows = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == env["connector"])))
        assert {(row.kb_id, row.task_type) for row in rows} == {(kb, kind) for kb in env["kbs"] for kind in ConnectorTaskType}
        assert len(rows) == 4
        assert all(row.status == TaskStatus.SCHEDULE for row in rows)
        sync, prune = SyncLogsService.list_due_sync_tasks(db), SyncLogsService.list_due_prune_tasks(db)
        assert len([row for row in sync if row["connector_id"] == env["connector"]]) == 2
        assert len([row for row in prune if row["connector_id"] == env["connector"]]) == 2
        # A second connector bound to the same KB must not duplicate/join its auto_parse.
        assert all(row["auto_parse"] == "1" for row in sync if row["connector_id"] == env["connector"])
        assert len(SyncLogsService.list_sync_tasks(db, env["connector"])[0]) == 4


def test_periods_use_separate_utc_cutoffs_and_prune_double_gate(scheduled: dict[str, Any]) -> None:
    env = scheduled
    instant = datetime(2026, 10, 24, tzinfo=UTC).replace(tzinfo=None)
    with Session(env["engine"]) as db:
        ConnectorService.schedule_tasks(db, env["connector"])
        for kind, period in ((ConnectorTaskType.SYNC, 5), (ConnectorTaskType.PRUNE, 17)):
            db.execute(sa.update(SyncLogs).where(SyncLogs.connector_id == env["connector"], SyncLogs.task_type == kind).values(update_date=instant - timedelta(minutes=period)))
            query = (
                sa.select(SyncLogs.id)
                .join(Connector, SyncLogs.connector_id == Connector.id)
                .where(SyncLogs.connector_id == env["connector"], SyncLogs.task_type == kind, _schedule_time_condition("postgresql", sa.literal(instant), kind))
            )
            assert not list(db.scalars(query))
            db.execute(sa.update(SyncLogs).where(SyncLogs.connector_id == env["connector"], SyncLogs.task_type == kind).values(update_date=instant - timedelta(minutes=period, microseconds=1)))
            assert len(list(db.scalars(query))) == 2
        db.execute(sa.update(Connector).where(Connector.id == env["connector"]).values(prune_freq=0))
        db.commit()
        assert not any(row["connector_id"] == env["connector"] for row in SyncLogsService.list_due_prune_tasks(db))
        db.execute(sa.update(Connector).where(Connector.id == env["connector"]).values(prune_freq=17, config={"sync_deleted_files": False}))
        db.commit()
        assert not any(row["connector_id"] == env["connector"] for row in SyncLogsService.list_due_prune_tasks(db))


def test_cancel_and_reschedule_preserve_checkpoint_and_cannot_reanimate_old_worker(scheduled: dict[str, Any]) -> None:
    env = scheduled
    checkpoint = datetime(2026, 10, 1, tzinfo=UTC)
    with Session(env["engine"]) as db:
        task = SyncLogsService.schedule(db, env["connector"], env["kbs"][0], checkpoint, reindex=True, run_immediately=True)
        assert task is not None
        identifier = task.id
        SyncLogsService.start(db, identifier, env["connector"])
        assert connector_api_service.update_connector(db, env["connector"], {"refresh_freq": 23, "reschedule": True})[0]
    with Session(env["engine"]) as db:
        with pytest.raises(SyncTaskCancelled):
            SyncLogsService.complete_and_schedule_next(db, identifier, env["connector"], env["kbs"][0], datetime.now(UTC))
        db.rollback()
        assert db.get(SyncLogs, identifier).status == TaskStatus.CANCEL
        active = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == env["connector"], SyncLogs.status == TaskStatus.SCHEDULE)))
        assert len(active) == 4
        retried = next(row for row in active if row.kb_id == env["kbs"][0] and row.task_type == ConnectorTaskType.SYNC)
        assert retried.poll_range_start == checkpoint and retried.from_beginning == "1"
        assert db.get(Connector, env["connector"]).refresh_freq == 23
        ConnectorService.cancel_tasks(db, env["connector"])
    with Session(env["engine"]) as db:
        assert not list(db.scalars(sa.select(SyncLogs.id).where(SyncLogs.connector_id == env["connector"], SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING]))))
        assert db.get(Connector, env["connector"]).status == TaskStatus.CANCEL


def test_two_dispatchers_claim_only_once_and_completion_keeps_task_type(scheduled: dict[str, Any]) -> None:
    env = scheduled
    with Session(env["engine"]) as db:
        task = SyncLogsService.schedule(db, env["connector"], env["kbs"][0], task_type=ConnectorTaskType.PRUNE, run_immediately=True)
        assert task is not None
        identifier = task.id

    def claim() -> bool:
        with Session(env["engine"]) as db:
            try:
                SyncLogsService.start(db, identifier, env["connector"])
                return True
            except SyncTaskCancelled:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claim(), range(2))) == [False, True]
    with Session(env["engine"]) as db:
        next_id = SyncLogsService.complete_and_schedule_next(db, identifier, env["connector"], env["kbs"][0], None)
    with Session(env["engine"]) as db:
        original, following = db.get(SyncLogs, identifier), db.get(SyncLogs, next_id)
        assert original.status == TaskStatus.DONE and original.poll_range_end is None
        assert following.task_type == ConnectorTaskType.PRUNE and following.status == TaskStatus.SCHEDULE
        assert abs((following.update_date - datetime.now(UTC).replace(tzinfo=None)).total_seconds()) < 5
        assert not any(row["id"] == next_id for row in SyncLogsService.list_due_prune_tasks(db))


def test_sibling_completion_preserves_failure_and_disable_cancels_only_prune(scheduled: dict[str, Any]) -> None:
    env = scheduled
    with Session(env["engine"]) as db:
        ConnectorService.schedule_tasks(db, env["connector"])
        sync = SyncLogsService.get_latest_task(db, env["connector"], env["kbs"][0], ConnectorTaskType.SYNC)
        prune = SyncLogsService.get_latest_task(db, env["connector"], env["kbs"][0], ConnectorTaskType.PRUNE)
        assert sync and prune
        sync_id, prune_id = sync.id, prune.id
        SyncLogsService.start(db, sync_id, env["connector"])
        SyncLogsService.start(db, prune_id, env["connector"])
        SyncLogsService.fail(db, sync_id, env["connector"], "controlled ingestion failure")
        SyncLogsService.complete_and_schedule_next(db, prune_id, env["connector"], env["kbs"][0], None)
    with Session(env["engine"]) as db:
        assert db.get(Connector, env["connector"]).status == TaskStatus.FAIL
        assert not any(row["connector_id"] == env["connector"] for row in SyncLogsService.list_due_sync_tasks(db))
        ConnectorService.schedule_tasks(db, env["connector"])
        assert connector_api_service.update_connector(db, env["connector"], {"config": {"sync_deleted_files": False}})[0]
    with Session(env["engine"]) as db:
        assert not list(
            db.scalars(
                sa.select(SyncLogs.id).where(SyncLogs.connector_id == env["connector"], SyncLogs.task_type == ConnectorTaskType.PRUNE, SyncLogs.status.in_([TaskStatus.SCHEDULE, TaskStatus.RUNNING]))
            )
        )
        assert len(list(db.scalars(sa.select(SyncLogs.id).where(SyncLogs.connector_id == env["connector"], SyncLogs.task_type == ConnectorTaskType.SYNC, SyncLogs.status == TaskStatus.SCHEDULE)))) == 2


def test_next_task_insert_failure_rolls_back_completion_and_checkpoint(scheduled: dict[str, Any]) -> None:
    env = scheduled
    before = datetime(2026, 10, 1, tzinfo=UTC)
    with Session(env["engine"]) as db:
        task = SyncLogsService.schedule(db, env["connector"], env["kbs"][0], before)
        assert task is not None
        identifier = task.id
        SyncLogsService.start(db, identifier, env["connector"])
    with Session(env["engine"]) as db:

        def fail_insert(session: Session, *args: Any) -> None:
            if any(isinstance(row, SyncLogs) for row in session.new):
                raise RuntimeError("controlled next task insert failure")

        sa.event.listen(db, "before_flush", fail_insert)
        with pytest.raises(RuntimeError, match="next task insert"):
            SyncLogsService.complete_and_schedule_next(db, identifier, env["connector"], env["kbs"][0], datetime.now(UTC))
        db.rollback()
    with Session(env["engine"]) as db:
        row = db.get(SyncLogs, identifier)
        assert row.status == TaskStatus.RUNNING and row.poll_range_start == before and row.poll_range_end is None
        assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == env["connector"])) == 1
