from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from api.db import InputType
from api.db.db_models import Connector, SyncLogs
from api.db.services.connector_service import SyncLogsService
from common.constants import FileSource, TaskStatus


def test_checkpoint_and_next_schedule_commit_as_one_timestamptz_flow(bootstrapped_engine):
    connector_id = "connector-checkpoint-integration"
    kb_id = "kb-checkpoint-integration"
    task_id = "sync-checkpoint-integration"
    first_update = datetime(2026, 8, 2, 2, 0, tzinfo=UTC)
    latest_update = first_update + timedelta(hours=1)

    with Session(bootstrapped_engine) as db:
        db.add(
            Connector(
                id=connector_id,
                tenant_id="tenant-checkpoint-integration",
                name="checkpoint-integration",
                source=FileSource.GITHUB,
                input_type=InputType.POLL,
                config={},
                refresh_freq=5,
                prune_freq=0,
                timeout_secs=60,
                status=TaskStatus.RUNNING,
            )
        )
        db.add(
            SyncLogs(
                id=task_id,
                connector_id=connector_id,
                kb_id=kb_id,
                status=TaskStatus.RUNNING,
                from_beginning="1",
            )
        )
        db.commit()

        assert SyncLogsService.increase_docs(db, task_id, latest_update, 3) == 1
        assert SyncLogsService.increase_docs(db, task_id, first_update, 2) == 1
        next_task_id = SyncLogsService.complete_and_schedule_next(
            db,
            task_id,
            connector_id,
            kb_id,
            latest_update,
        )

        current = db.get(SyncLogs, task_id)
        next_task = db.get(SyncLogs, next_task_id)
        connector_status = db.scalar(select(Connector.status).where(Connector.id == connector_id))

        assert current is not None
        assert current.status == TaskStatus.DONE
        assert current.new_docs_indexed == 5
        assert current.total_docs_indexed == 5
        assert current.poll_range_start == latest_update
        assert current.poll_range_end == latest_update

        assert next_task is not None
        assert next_task.status == TaskStatus.SCHEDULE
        assert next_task.poll_range_start == latest_update
        assert next_task.total_docs_indexed == 5
        assert connector_status == TaskStatus.SCHEDULE


def test_failed_sync_rewinds_batch_checkpoint_and_preserves_cancel(bootstrapped_engine: Engine) -> None:
    from uuid import uuid4

    original = datetime(2026, 1, 1, tzinfo=UTC)
    advanced = original + timedelta(days=1)
    connector_id, task_id = uuid4().hex, uuid4().hex
    with Session(bootstrapped_engine) as db:
        db.add(Connector(id=connector_id, tenant_id=uuid4().hex, name="retry checkpoint", source=FileSource.S3, input_type=InputType.POLL, config={}, status=TaskStatus.RUNNING))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=uuid4().hex, status=TaskStatus.RUNNING, poll_range_start=original))
        db.commit()
        SyncLogsService.increase_docs(db, task_id, advanced, 1)
        SyncLogsService.fail(db, task_id, connector_id, "later batch failed", poll_range_start=original)
        db.expire_all()
        current = db.get(SyncLogs, task_id)
        assert current is not None and current.status == TaskStatus.FAIL and current.poll_range_start == original
        current.status = TaskStatus.CANCEL
        connector = db.get(Connector, connector_id)
        assert connector is not None
        connector.status = TaskStatus.CANCEL
        db.commit()
        SyncLogsService.fail(db, task_id, connector_id, "late timeout", poll_range_start=None)
        db.expire_all()
        assert db.get(SyncLogs, task_id).status == TaskStatus.CANCEL
        assert db.get(SyncLogs, task_id).poll_range_start == original
        assert db.get(Connector, connector_id).status == TaskStatus.CANCEL
