"""Shared real SQL scheduling/readback assertions, using exact owned records."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, delete, func, insert, literal, select, update
from sqlalchemy.orm import Session

from api.db import InputType
from api.db.db_models import Connector, Connector2Kb, Knowledgebase, SyncLogs
from api.db.services.connector_service import SyncLogsService, _schedule_time_condition
from common.constants import TaskStatus


def assert_schedule_readback(engine: Engine) -> dict[str, Any]:
    """Exercise exact interval edges, due-list filters and UTC cursor output."""
    dialect = engine.dialect.name
    instant = datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    naive = instant.replace(tzinfo=None)
    kb_id, connector_id = uuid4().hex, uuid4().hex
    task_ids: dict[str, str] = {}
    connection_id = uuid4().hex
    with Session(engine) as db:
        db.add(Knowledgebase(id=kb_id, tenant_id=uuid4().hex, name="schedule acceptance", created_by="owned", embd_id="controlled"))
        db.add(Connector(id=connector_id, tenant_id=uuid4().hex, name="schedule acceptance", source="imap", input_type=InputType.POLL, config={}, refresh_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=connection_id, kb_id=kb_id, connector_id=connector_id))
        db.commit()
        for case, delta in {"before": -1, "equal": 0, "after": 1}.items():
            task_id = task_ids[case] = uuid4().hex
            # Core insert intentionally bypasses ORM timestamp generation for the
            # deterministic boundary fixture, not for the persistence assertions.
            db.execute(
                insert(SyncLogs).values(
                    id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, update_date=naive - timedelta(minutes=5) + timedelta(seconds=delta), poll_range_start=instant
                )
            )
        db.commit()
    try:
        with Session(engine) as db:
            condition = _schedule_time_condition(dialect, literal(naive))
            query = select(SyncLogs.id).join(Connector, SyncLogs.connector_id == Connector.id).where(SyncLogs.connector_id == connector_id, condition)
            assert set(db.scalars(query)) == {task_ids["before"]}
            db.execute(update(Connector).where(Connector.id == connector_id).values(refresh_freq=0))
            assert set(db.scalars(query)) == set(task_ids.values())
            db.execute(update(Connector).where(Connector.id == connector_id).values(refresh_freq=5))
            # Changing the session timezone cannot move already persisted UTC
            # wall-clock scheduling data. The comparison explicitly uses UTC.
            server_now = db.scalar(select(func.timezone("UTC", func.now()) if dialect == "postgresql" else func.utc_timestamp(6)))
            for case, delta in {"before": -2, "equal": 10, "after": 60}.items():
                db.execute(update(SyncLogs).where(SyncLogs.id == task_ids[case]).values(update_date=server_now - timedelta(minutes=5) + timedelta(seconds=delta)))
            db.commit()
        with Session(engine) as db:
            tasks = SyncLogsService.list_due_sync_tasks(db)
            owned = [task for task in tasks if task["connector_id"] == connector_id]
            assert [task["id"] for task in owned] == [task_ids["before"]]
            assert owned[0]["poll_range_start"] == instant and owned[0]["poll_range_start"].tzinfo is UTC
            assert owned[0]["update_date"].tzinfo is UTC
            db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.CANCEL))
            db.commit()
        with Session(engine) as db:
            assert not any(task["connector_id"] == connector_id for task in SyncLogsService.list_due_sync_tasks(db))
            # Explicit log inspection bypasses the due/status filter.
            assert len(SyncLogsService.list_sync_tasks(db, connector_id)[0]) == 3
            db.execute(update(Connector).where(Connector.id == connector_id).values(status=TaskStatus.SCHEDULE, input_type="event"))
            db.commit()
        with Session(engine) as db:
            assert not any(task["connector_id"] == connector_id for task in SyncLogsService.list_due_sync_tasks(db))
            db.execute(update(Connector).where(Connector.id == connector_id).values(input_type=InputType.POLL))
            db.execute(update(SyncLogs).where(SyncLogs.id == task_ids["before"]).values(status=TaskStatus.CANCEL))
            db.commit()
        with Session(engine) as db:
            assert not any(task["connector_id"] == connector_id for task in SyncLogsService.list_due_sync_tasks(db))
        return {
            "dialect": dialect,
            "exact_boundary": {"before": True, "equal": False, "after": False},
            "zero_interval": True,
            "due_list_readback": True,
            "cancelled_connector": True,
            "cancelled_task": True,
            "event_source": True,
            "utc_cursor": True,
        }
    finally:
        with Session(engine) as db:
            db.execute(delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(delete(Connector2Kb).where(Connector2Kb.id == connection_id))
            db.execute(delete(Connector).where(Connector.id == connector_id))
            db.execute(delete(Knowledgebase).where(Knowledgebase.id == kb_id))
            db.commit()
        with Session(engine) as db:
            assert db.scalar(select(func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 0


def assert_utc_persistence(engine: Engine) -> dict[str, Any]:
    """ORM defaults and sync-service timestamps survive a fresh SQL readback."""
    from datetime import timezone

    connector_id, kb_id = uuid4().hex, uuid4().hex
    supplied = datetime(2026, 1, 2, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    before = datetime.now(UTC).replace(tzinfo=None)
    try:
        with Session(engine) as db:
            db.add(Connector(id=connector_id, tenant_id=uuid4().hex, name="UTC persistence", source="imap", input_type=InputType.POLL, config={}))
            db.commit()
            task = SyncLogsService.schedule(db, connector_id, kb_id, supplied)
            assert task is not None
            task_id = task.id
            SyncLogsService.start(db, task_id, connector_id)
        after = datetime.now(UTC).replace(tzinfo=None)
        with Session(engine) as db:
            row = db.get(SyncLogs, task_id)
            assert row is not None
            assert before <= row.create_date <= after and before <= row.update_date <= after
            assert row.poll_range_start.replace(tzinfo=UTC) == supplied.astimezone(UTC)
            assert before <= row.time_started.replace(tzinfo=None) <= after
            assert row.status == TaskStatus.RUNNING
        return {"utc_wall_clock_defaults": True, "utc_service_update": True, "non_utc_input_cursor": True}
    finally:
        with Session(engine) as db:
            db.execute(delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(delete(Connector).where(Connector.id == connector_id))
            db.commit()
