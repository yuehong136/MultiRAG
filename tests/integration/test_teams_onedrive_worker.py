"""Actual worker/SQL/object/queue/HTTP boundaries; only vendor reads controlled."""

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
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.runtime_upload import read_object
from tests.support.teams_onedrive_fakes import CREDS, MicrosoftHTTP, install_graph


@pytest.mark.parametrize("source", ["teams", "onedrive"])
@pytest.mark.parametrize("outcome", ["complete", "empty", "permission", "later-page", "content", "cancel", "timeout", "ingestion"])
async def test_worker_storage_failures_and_checkpoints(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str, outcome: str) -> None:
    env = rest_documents
    owner, kb_id = env["owners"][0], env["datasets"][0]
    connector_id, task_id = uuid4().hex, uuid4().hex
    start = datetime(2026, 1, 1, tzinfo=UTC)
    http = MicrosoftHTTP(source)
    install_graph(monkeypatch, http)
    config: dict[str, Any] = {"batch_size": 1, "credentials": CREDS, "sync_deleted_files": True}
    if source == "onedrive":
        config["drive_ids"] = ["A", "B"]
    driver_type = sync_data_source.Teams if source == "teams" else sync_data_source.OneDrive
    http.mode = outcome if outcome in {"empty", "permission", "later-page", "content"} else "complete"
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="C controlled Graph", source=source, input_type="poll", config=config, refresh_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=start))
        db.commit()
    driver = driver_type(config)
    stopped = False

    def after(path: str) -> None:
        nonlocal stopped
        # Stop after the final content/reply response and before any write.
        selected = path.endswith("/content") if source == "onedrive" else path.endswith("/replies")
        if stopped or not selected or outcome not in {"cancel", "timeout"}:
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

    await driver(task(task_id, start, outcome == "complete"))
    with Session(env["engine"]) as db:
        log = db.get(SyncLogs, task_id)
        success = outcome in {"complete", "empty"}
        assert log.status == (TaskStatus.DONE if success else TaskStatus.CANCEL if outcome == "cancel" else TaskStatus.FAIL), log.full_exception_trace
        following = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == connector_id, SyncLogs.id != task_id)))
        rows = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
        expected_ids = {"t1__c1__m1", "t1__c1__m2"} if source == "teams" else {"A:same", "A:nested", "B:same"}
        if outcome == "complete":
            assert log.poll_range_end == datetime(2026, 1, 2, tzinfo=UTC)
            assert {row.id for row in rows} == {connector_doc_id_candidates(kb_id, connector_id, item)[-1] for item in expected_ids}
            assert len(following) == 1 and following[0].poll_range_start == log.poll_range_end
        elif outcome == "empty":
            assert not rows and log.poll_range_end == start
        else:
            assert log.poll_range_start == start and not following
            assert "controlled-secret-must-not-leak" not in (log.error_msg or "")
        if outcome in {"empty", "permission", "content", "cancel", "timeout", "ingestion"} or (source == "onedrive" and outcome == "later-page"):
            assert not rows
        assert log.docs_removed_from_index == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(Task)) == 0
        assert FileSource(source) not in sync_data_source.DELETED_FILE_SYNC_SOURCES
        bodies = {row.id: read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in rows}
        assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(rows)
        if source == "onedrive" and outcome == "complete":
            assert set(bodies.values()) == {http.body}
        if source == "teams" and outcome == "complete":
            assert any(b"old parent" in body and b"new reply &lt;script&gt;" in body for body in bodies.values())
        env["journal"].event(
            "worker-readback",
            source=source,
            outcome=outcome,
            task=task_id,
            status=log.status,
            documents=list(bodies),
            object_sha256={key: hashlib.sha256(body).hexdigest() for key, body in bodies.items()},
            requests=http.calls,
            following=[row.id for row in following],
        )
    if outcome == "complete":
        http.modified, http.reply, http.body = "2026-01-03T00:00:00Z", "updated reply", b"updated bytes"
        for phase in ("incremental", "rebuild"):
            with Session(env["engine"]) as db:
                pending = SyncLogsService.get_latest_task(db, connector_id, kb_id)
                next_id, next_start = pending.id, pending.poll_range_start
            await driver_type(config)(task(next_id, next_start, phase == "rebuild"))
            with Session(env["engine"]) as db:
                completed = db.get(SyncLogs, next_id)
                assert completed.status == TaskStatus.DONE, completed.full_exception_trace
                assert completed.poll_range_end == datetime(2026, 1, 3, tzinfo=UTC)
                rows = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
                assert len(rows) == len(expected_ids)
                bodies = [read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in rows]
                assert set(bodies) == {http.body} if source == "onedrive" else any(b"updated reply" in body for body in bodies)
                env["journal"].event("worker-phase-readback", source=source, phase=phase, task=next_id, status=completed.status, objects=[hashlib.sha256(body).hexdigest() for body in bodies])
