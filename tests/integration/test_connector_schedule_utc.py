import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.db.db_models import Connector, get_engine_config
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.connector_schedule import assert_schedule_readback, assert_utc_persistence
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def test_postgres_exact_schedule_and_session_independence(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Deliberately misleading environment must not select another DB's syntax.
    monkeypatch.setenv("DB_TYPE", "mysql")
    engine = sa.create_engine(bootstrapped_engine.url, **get_engine_config({"name": "postgresql+psycopg", "max_connections": 1}))
    try:
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SHOW TIME ZONE")) == "UTC"
        persistence = assert_utc_persistence(engine)
        utc_result = assert_schedule_readback(engine)
        with engine.connect() as connection:
            connection.execute(sa.text("SET TIME ZONE 'Asia/Shanghai'"))
            connection.commit()
        non_utc_result = assert_schedule_readback(engine)
        evidence = Path(os.environ.get("CONNECTOR_SCHEDULE_EVIDENCE", str(tmp_path))) / f"{engine.url.database}.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(json.dumps({"database": engine.url.database, "persistence": persistence, "utc_session": utc_result, "non_utc_session": non_utc_result}, indent=2))
        evidence.chmod(0o600)
    finally:
        engine.dispose()


async def test_postgres_async_session_utc_defaults(bootstrapped_engine: Engine) -> None:
    from uuid import uuid4

    engine = create_async_engine(bootstrapped_engine.url, **get_engine_config({"name": "postgresql+psycopg"}))
    identifier = uuid4().hex
    factory = async_sessionmaker(engine, expire_on_commit=False)
    before = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with factory() as db:
            assert (await db.execute(sa.text("SHOW TIME ZONE"))).scalar() == "UTC"
            db.add(Connector(id=identifier, tenant_id=uuid4().hex, name="async UTC", source="imap", input_type="poll", config={}))
            await db.commit()
        after = datetime.now(UTC).replace(tzinfo=None)
        async with factory() as db:
            row = await db.get(Connector, identifier)
            assert row is not None
            assert before <= row.create_date <= after
            await db.delete(row)
            await db.commit()
        async with factory() as db:
            assert await db.get(Connector, identifier) is None
    finally:
        await engine.dispose()


def test_log_http_exposes_unambiguous_utc_dates(connector_sync_api: dict[str, Any]) -> None:
    from uuid import uuid4

    import requests
    from sqlalchemy.orm import Session

    from api.db.db_models import Connector2Kb, SyncLogs

    env = connector_sync_api
    identifier, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    instant = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
    with Session(env["engine"]) as db:
        db.add(Connector(id=identifier, tenant_id=env["owners"][0], name="UTC HTTP", source="imap", input_type="poll", config={}))
        db.add(Connector2Kb(id=link_id, connector_id=identifier, kb_id=env["kb"]))
        db.execute(sa.insert(SyncLogs).values(id=task_id, connector_id=identifier, kb_id=env["kb"], status="schedule", update_date=instant.replace(tzinfo=None), poll_range_start=instant))
        db.commit()
    try:
        response = requests.get(f"{env['base']}/api/v1/connectors/{identifier}/logs", headers={"Authorization": f"Bearer {env['jwt']}"}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0
        logs = response.json()["data"]["logs"]
        assert len(logs) == 1 and logs[0]["id"] == task_id
        for field in ("update_date", "poll_range_start"):
            parsed = datetime.fromisoformat(logs[0][field].replace("Z", "+00:00"))
            assert parsed == instant and parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
        with Session(env["engine"]) as db:
            assert db.get(SyncLogs, task_id).update_date == instant.replace(tzinfo=None)
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.id == task_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == identifier))
            db.commit()
