from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import DateTime
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from api.db.db_models import SyncLogs
from api.db.services.connector_service import SyncLogsService, _to_utc
from core.svr import sync_data_source


def test_sync_cursor_columns_use_native_timezone_aware_datetimes():
    for column_name in ("time_started", "poll_range_start", "poll_range_end"):
        column_type = SyncLogs.__table__.columns[column_name].type
        assert isinstance(column_type, DateTime)
        assert column_type.timezone is True


def test_sync_cursor_normalization_preserves_the_instant_in_utc():
    naive = datetime(2026, 8, 2, 12, 0)
    assert _to_utc(naive) == naive.replace(tzinfo=UTC)

    east_eight = datetime(2026, 8, 2, 20, 0, tzinfo=timezone(timedelta(hours=8)))
    assert _to_utc(east_eight) == datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def test_increase_docs_uses_one_atomic_monotonic_update(monkeypatch):
    db = Session()
    statements = []
    commits = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(rowcount=1)

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "commit", lambda: commits.append(True))

    updated = SyncLogsService.increase_docs(
        db,
        "task-1",
        datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        3,
        error_count=1,
    )

    assert updated == 1
    assert commits == [True]
    assert len(statements) == 1
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "UPDATE usr_ai.t_ai_sync_logs" in sql
    assert sql.count("CASE WHEN") == 2
    assert "new_docs_indexed=(usr_ai.t_ai_sync_logs.new_docs_indexed +" in sql
    assert "total_docs_indexed=(usr_ai.t_ai_sync_logs.total_docs_indexed +" in sql


@pytest.mark.asyncio
async def test_connector_task_failure_does_not_cancel_healthy_peer():
    healthy_completed = False

    async def fail():
        raise RuntimeError("connector failed")

    async def succeed():
        nonlocal healthy_completed
        healthy_completed = True

    tasks = [asyncio_task(fail()), asyncio_task(succeed())]
    await sync_data_source._gather_connector_tasks(tasks)

    assert healthy_completed is True


def asyncio_task(coroutine):
    import asyncio

    return asyncio.create_task(coroutine)


@pytest.mark.asyncio
async def test_completion_failure_is_persisted_without_escaping_worker(monkeypatch):
    calls = []

    @contextmanager
    def fake_db_connection():
        yield Session()

    class SuccessfulSync(sync_data_source.SyncBase):
        async def _run_task_logic(self, task: dict) -> datetime:
            return datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(sync_data_source, "db_connection", fake_db_connection)
    monkeypatch.setattr(
        SyncLogsService,
        "start",
        classmethod(lambda cls, db, task_id, connector_id: calls.append(("start", task_id))),
    )

    def fail_completion(cls, db, task_id, connector_id, kb_id, checkpoint):
        raise RuntimeError("checkpoint commit failed")

    monkeypatch.setattr(SyncLogsService, "complete_and_schedule_next", classmethod(fail_completion))
    monkeypatch.setattr(
        SyncLogsService,
        "fail",
        classmethod(lambda cls, db, task_id, connector_id, error_msg, full_exception_trace="": calls.append(("fail", task_id, error_msg))),
    )

    await SuccessfulSync({})(
        {
            "id": "task-1",
            "connector_id": "connector-1",
            "kb_id": "kb-1",
            "timeout_secs": 5,
        }
    )

    assert calls == [
        ("start", "task-1"),
        ("fail", "task-1", "checkpoint commit failed"),
    ]
