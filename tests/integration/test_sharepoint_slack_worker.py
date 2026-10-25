"""Real sync worker, SQL and object writes with controlled external SDK reads."""

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from slack_sdk import WebClient
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, SyncLogs, Task
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from common.constants import FileSource, TaskStatus
from common.data_source import sharepoint_connector
from common.data_source.models import BasicExpertInfo
from core.svr import sync_data_source
from tests.support.connector_sdk_fakes import SITE_URL, GraphHTTP, SlackHTTP
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.runtime_upload import read_object


@pytest.mark.parametrize(
    "source,outcome",
    [(source, outcome) for source in ["sharepoint", "slack"] for outcome in ["complete", "permission", "later-page", "content", "cancel", "timeout", "ingestion"]]
    + [("slack", "mention-scope"), ("slack", "mention-ratelimit")],
)
async def test_worker_storage_failure_and_cancellation(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str, outcome: str) -> None:
    env = rest_documents
    owner, kb_id = env["owners"][0], env["datasets"][0]
    connector_id, task_id = uuid4().hex, uuid4().hex
    start = datetime(2026, 1, 1, tzinfo=UTC)
    http: GraphHTTP | SlackHTTP
    if source == "sharepoint":
        http = GraphHTTP()
        # Patch only Graph HTTPS traffic; local API/storage HTTP remains real.
        original_get = sharepoint_connector.requests.get
        monkeypatch.setattr(sharepoint_connector.requests, "get", lambda url, **kwargs: http(url, **kwargs) if url.startswith("https://graph.microsoft.com/") else original_get(url, **kwargs))

        class App:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def acquire_token_for_client(self, **kwargs: Any) -> dict[str, Any]:
                return {"access_token": "controlled-token", "token_type": "Bearer", "expires_in": 3600}

        monkeypatch.setattr(sharepoint_connector.msal, "ConfidentialClientApplication", App)
        conf: dict[str, Any] = {"batch_size": 1, "credentials": {"tenant_id": "tenant", "client_id": "client", "client_secret": "controlled-secret", "site_url": SITE_URL}, "sync_deleted_files": True}
        driver_type = sync_data_source.SharePoint
        expected_ids = {"drive-A:same", "drive-A:nested", "drive-B:same"}
        http.mode = {"permission": "permission", "later-page": "permission", "content": "download"}.get(outcome, "complete")
    else:
        http = SlackHTTP()
        monkeypatch.setattr(WebClient, "_sync_send", lambda client, api_url, req_args: http(client, api_url, req_args))
        conf = {"batch_size": 1, "credentials": {"slack_bot_token": "controlled-token"}, "sync_deleted_files": True}
        driver_type = sync_data_source.Slack
        expected_ids = {"C1__1767225600.0", "C1__1767225601.0"}
        http.mode = {"permission": "permission", "later-page": "later-page", "content": "thread-error", "mention-scope": "mention-scope", "mention-ratelimit": "mention-ratelimit"}.get(
            outcome, "complete"
        )
    journal = env["journal"]
    journal.event("controlled-sdk", source=source, mode=http.mode)
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="C controlled SDK", source=source, input_type="poll", config=conf, refresh_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=start))
        db.commit()
    injected = False
    driver = driver_type(conf)
    if outcome.startswith("mention-"):
        initialize = driver._initialize_connector

        async def cached_sender(task: dict[str, Any]) -> None:
            await initialize(task)
            driver.connector.user_cache["U1"] = BasicExpertInfo(display_name="Cached Alice")

        monkeypatch.setattr(driver, "_initialize_connector", cached_sender)

    def after_request(_request: str) -> None:
        nonlocal injected
        if injected or outcome not in {"cancel", "timeout"}:
            return
        injected = True
        if outcome == "cancel":
            with Session(env["engine"]) as db:
                ConnectorService.cancel_tasks(db, connector_id)
        else:
            driver.connector._check_active.deadline = 0

    http.after_request = after_request
    if outcome == "ingestion":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion failure"], []))
    await driver(
        {
            "id": task_id,
            "connector_id": connector_id,
            "tenant_id": owner,
            "kb_id": kb_id,
            "poll_range_start": start,
            "reindex": "1" if outcome == "complete" else "0",
            "auto_parse": False,
            "timeout_secs": 30,
        }
    )
    with Session(env["engine"]) as db:
        log = db.get(SyncLogs, task_id)
        assert log is not None
        success = outcome == "complete"
        assert log.status == (TaskStatus.DONE if success else TaskStatus.CANCEL if outcome == "cancel" else TaskStatus.FAIL), log.full_exception_trace
        following = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == connector_id, SyncLogs.id != task_id)))
        if success:
            assert log.poll_range_end == datetime(2026, 1, 2, tzinfo=UTC)
            assert len(following) == 1 and following[0].status == TaskStatus.SCHEDULE
            assert following[0].poll_range_start == log.poll_range_end
        else:
            # Per-batch progress may exist; final success/checkpoint never commits.
            assert log.poll_range_start == start
            assert not following
            assert log.error_msg if outcome != "cancel" else True
        assert log.docs_removed_from_index == 0
        rows = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
        if success:
            assert {row.id for row in rows} == {connector_doc_id_candidates(kb_id, connector_id, identifier)[-1] for identifier in expected_ids}
            assert all(row.source_type == source + "/" + connector_id for row in rows)
        if outcome in {"permission", "cancel", "timeout", "content", "ingestion", "mention-scope", "mention-ratelimit"} and not (source == "sharepoint" and outcome == "permission"):
            assert rows == []
        # No parser tasks or queue work are created by auto_parse=False.
        assert db.scalar(sa.select(sa.func.count()).select_from(Task)) == 0
        assert FileSource(source) not in sync_data_source.DELETED_FILE_SYNC_SOURCES
        object_bodies = {row.id: read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in rows}
        assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(rows)
        if success and source == "sharepoint":
            assert set(object_bodies.values()) == {b"controlled file bytes"}
        if success and source == "slack":
            assert set(object_bodies.values()) == {"古い親\n\n新回复".encode(), b"standalone"}
        journal.event(
            "worker-readback",
            source=source,
            outcome=outcome,
            task=task_id,
            status=log.status,
            documents=[row.id for row in rows],
            object_sha256={key: hashlib.sha256(value).hexdigest() for key, value in object_bodies.items()},
            following=[row.id for row in following],
            requests=http.calls,
            deleted=log.docs_removed_from_index,
        )

    if outcome == "complete":
        if source == "sharepoint":
            original_file = http.file
            http.file = lambda identifier, name: {**original_file(identifier, name), "lastModifiedDateTime": "2026-01-03T00:00:00Z"}
            http.body = b"updated file bytes"
        else:
            http.reply_ts, http.reply_text = "1767398400.0", "updated reply"
        for phase in ("incremental", "rebuild"):
            with Session(env["engine"]) as db:
                pending = SyncLogsService.get_latest_task(db, connector_id, kb_id)
                assert pending is not None and pending.status == TaskStatus.SCHEDULE
                next_id, next_start = pending.id, pending.poll_range_start
            before_calls = len(http.calls)
            await driver_type(conf)(
                {
                    "id": next_id,
                    "connector_id": connector_id,
                    "tenant_id": owner,
                    "kb_id": kb_id,
                    "poll_range_start": next_start,
                    "reindex": "1" if phase == "rebuild" else "0",
                    "auto_parse": False,
                    "timeout_secs": 30,
                }
            )
            with Session(env["engine"]) as db:
                completed = db.get(SyncLogs, next_id)
                assert completed is not None and completed.status == TaskStatus.DONE, completed.full_exception_trace
                assert completed.poll_range_end == datetime(2026, 1, 3, tzinfo=UTC)
                updated = list(db.scalars(sa.select(Document).where(Document.kb_id == kb_id)))
                assert {row.id for row in updated} == {connector_doc_id_candidates(kb_id, connector_id, identifier)[-1] for identifier in expected_ids}
                bodies = {read_object(env["storage"], env["bucket"], f"{kb_id}/{row.location}") for row in updated}
                expected_bodies = {b"updated file bytes"} if source == "sharepoint" else {"古い親\n\nupdated reply".encode(), b"standalone"}
                assert bodies == expected_bodies
                assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(updated)
                journal.event("worker-phase-readback", source=source, phase=phase, status=completed.status, documents=[row.id for row in updated], requests=http.calls[before_calls:])


@pytest.mark.parametrize("source,last_request", [("sharepoint", "content"), ("slack", "users.info"), ("slack", "conversations.replies")])
@pytest.mark.parametrize("event", ["deadline", "cancel"])
async def test_last_sdk_response_cannot_publish_after_stop(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, source: str, last_request: str, event: str) -> None:
    env = rest_documents
    owner, kb_id = env["owners"][0], env["datasets"][0]
    connector_id, task_id = uuid4().hex, uuid4().hex
    start = datetime(2026, 1, 1, tzinfo=UTC)
    http: GraphHTTP | SlackHTTP
    if source == "sharepoint":
        http = GraphHTTP()
        original_get = sharepoint_connector.requests.get
        monkeypatch.setattr(sharepoint_connector.requests, "get", lambda url, **kwargs: http(url, **kwargs) if url.startswith("https://graph.microsoft.com/") else original_get(url, **kwargs))

        class App:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def acquire_token_for_client(self, **kwargs: Any) -> dict[str, Any]:
                return {"access_token": "controlled-token", "token_type": "Bearer", "expires_in": 3600}

        monkeypatch.setattr(sharepoint_connector.msal, "ConfidentialClientApplication", App)
        config: dict[str, Any] = {"batch_size": 1, "credentials": {"tenant_id": "tenant", "client_id": "client", "client_secret": "controlled-secret", "site_url": SITE_URL}}
        driver = sync_data_source.SharePoint(config)
    else:
        http = SlackHTTP()
        monkeypatch.setattr(WebClient, "_sync_send", lambda client, api_url, req_args: http(client, api_url, req_args))
        config = {"batch_size": 1, "credentials": {"slack_bot_token": "controlled-token"}}
        driver = sync_data_source.Slack(config)
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="C last request boundary", source=source, input_type="poll", config=config, refresh_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=start))
        db.commit()
    injected = False
    calls_at_stop = 0

    def stop_on_response(request: str) -> None:
        nonlocal injected, calls_at_stop
        selected = request.endswith("/content") if source == "sharepoint" else request == last_request
        if last_request == "conversations.replies":
            selected = selected and http.calls[-1]["params"].get("cursor") == "replies-2"
        if injected or not selected:
            return
        injected, calls_at_stop = True, len(http.calls)
        if event == "deadline":
            driver.connector._check_active.deadline = 0
        else:
            with Session(env["engine"]) as db:
                ConnectorService.cancel_tasks(db, connector_id)

    http.after_request = stop_on_response
    await driver({"id": task_id, "connector_id": connector_id, "tenant_id": owner, "kb_id": kb_id, "poll_range_start": start, "reindex": "0", "auto_parse": False, "timeout_secs": 30})
    assert injected and len(http.calls) == calls_at_stop
    with Session(env["engine"]) as db:
        row = db.get(SyncLogs, task_id)
        assert row.status == (TaskStatus.FAIL if event == "deadline" else TaskStatus.CANCEL), row.full_exception_trace
        assert row.poll_range_start == start and row.poll_range_end is None and row.new_docs_indexed == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == kb_id)) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
        assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
        env["journal"].event(
            "last-response-readback", source=source, last_request=last_request, stop=event, status=row.status, checkpoint=None, documents=0, objects=0, successor_tasks=0, requests=http.calls
        )
