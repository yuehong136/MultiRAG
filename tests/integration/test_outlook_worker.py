"""Actual Outlook worker with SQL/object/queue readback and controlled Graph."""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, SyncLogs, Task
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from common.constants import FileSource, TaskStatus
from core.svr import sync_data_source
from core.utils.redis_conn import REDIS_CONN
from tests.support.outlook_fakes import OutlookHTTP
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.runtime_upload import read_object
from tests.support.teams_onedrive_fakes import CREDS, install_graph


@pytest.mark.parametrize("outcome", ["complete", "empty", "permission", "later-page", "missing-delta", "malformed", "cancel", "timeout", "ingestion", "empty-scope"])
async def test_worker_storage_failures_and_checkpoints(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    env = rest_documents
    owner, kb_id = env["owners"][0], env["datasets"][0]
    connector_id, task_id = uuid4().hex, uuid4().hex
    start = datetime(2026, 1, 1, tzinfo=UTC)
    http = OutlookHTTP()
    auth = install_graph(monkeypatch, http)
    config = {"batch_size": 1, "user_ids": [] if outcome == "empty-scope" else ["A", "B"], "folder": "inbox", "credentials": CREDS, "sync_deleted_files": False}
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="Outlook controlled Graph", source="outlook", input_type="poll", config=config, refresh_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=start))
        db.commit()
    driver = sync_data_source.Outlook(config)
    http.mode = outcome if outcome in {"empty", "permission", "later-page", "missing-delta", "malformed"} else "complete"
    stopped = False

    def after(path: str) -> None:
        nonlocal stopped
        if stopped or len(http.calls) < 2 or outcome not in {"cancel", "timeout"}:
            return
        stopped = True
        if outcome == "cancel":
            with Session(env["engine"]) as db:
                ConnectorService.cancel_tasks(db, connector_id)
        else:
            driver._source_task_guard.deadline = 0

    http.after_request = after
    if outcome == "ingestion":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion failure"], []))

    def task(identifier: str, checkpoint: datetime | None, rebuild: bool = False) -> dict[str, Any]:
        return {
            "id": identifier,
            "connector_id": connector_id,
            "tenant_id": owner,
            "kb_id": kb_id,
            "poll_range_start": checkpoint,
            "reindex": "1" if rebuild else "0",
            "auto_parse": False,
            "timeout_secs": 30,
        }

    assert sync_data_source.func_factory[FileSource.OUTLOOK] is sync_data_source.Outlook
    await driver(task(task_id, start))
    expected_ids = {"A:same", "A:second", "B:same", "B:second"}
    with Session(env["engine"]) as db:
        log = db.get(SyncLogs, task_id)
        success = outcome in {"complete", "empty", "empty-scope"}
        assert log.status == (TaskStatus.DONE if success else TaskStatus.CANCEL if outcome == "cancel" else TaskStatus.FAIL), log.full_exception_trace
        following = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == connector_id, SyncLogs.id != task_id)))
        rows = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
        if outcome == "complete":
            assert log.poll_range_end == datetime(2026, 1, 2, tzinfo=UTC)
            assert {row.id for row in rows} == {connector_doc_id_candidates(kb_id, connector_id, item)[-1] for item in expected_ids}
            assert len(following) == 1 and following[0].poll_range_start == log.poll_range_end
        elif success:
            assert not rows and log.poll_range_end == start
        else:
            assert log.poll_range_start == start and not following and not rows
            assert "controlled-secret-must-not-leak" not in (log.error_msg or "")
        assert log.docs_removed_from_index == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(Task)) == 0
        assert not REDIS_CONN.REDIS.exists(env["queue"])
        assert FileSource.OUTLOOK not in sync_data_source.DELETED_FILE_SYNC_SOURCES
        bodies = {row.id: read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in rows}
        assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(rows)
        assert all(b"Actual mail body &amp; bytes" in body and b"Latest &lt;subject&gt;" in body for body in bodies.values())
        env["journal"].event(
            "outlook-worker-readback",
            outcome=outcome,
            task=task_id,
            status=log.status,
            documents=list(bodies),
            object_sha256={key: hashlib.sha256(body).hexdigest() for key, body in bodies.items()},
            requests=http.calls,
            following=[row.id for row in following],
        )
    if outcome == "empty-scope":
        assert not http.calls and auth["constructed"] == 0
    if outcome == "complete":
        for phase in ("identical-replay", "incremental", "rebuild", "empty-preserves-existing", "failure-preserves-existing"):
            http.mode = "empty" if phase == "empty-preserves-existing" else "later-page" if phase == "failure-preserves-existing" else "complete"
            if phase == "incremental":
                http.modified = "2026-01-03T00:00:00Z"
            with Session(env["engine"]) as db:
                pending = SyncLogsService.get_latest_task(db, connector_id, kb_id)
                next_id, next_start = pending.id, pending.poll_range_start
            await sync_data_source.Outlook(config)(task(next_id, next_start, phase in {"identical-replay", "rebuild"}))
            with Session(env["engine"]) as db:
                completed = db.get(SyncLogs, next_id)
                assert completed.status == (TaskStatus.FAIL if phase == "failure-preserves-existing" else TaskStatus.DONE), completed.full_exception_trace
                assert completed.docs_removed_from_index == 0
                rows = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
                assert len(rows) == len(expected_ids)
                if phase != "failure-preserves-existing":
                    assert completed.poll_range_end == datetime(2026, 1, 2 if phase == "identical-replay" else 3, tzinfo=UTC)
                current = {row.id: read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in rows}
                assert current == bodies  # Body is stable even when modification time changes.
                assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(expected_ids)
                assert not REDIS_CONN.REDIS.exists(env["queue"])
                env["journal"].event(
                    "outlook-worker-phase-readback",
                    phase=phase,
                    task=next_id,
                    status=completed.status,
                    documents=list(current),
                    objects=[hashlib.sha256(body).hexdigest() for body in current.values()],
                )
