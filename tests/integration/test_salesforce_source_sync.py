"""Salesforce HTTP boundary with real HTTP API, PostgreSQL and MinIO persistence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, DocumentMetadata, SyncLogs
from common.config_utils import CONFIGS
from common.constants import ConnectorTaskType, TaskStatus
from core.svr.sync_data_source import Salesforce
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.resource_journal import ResourceJournal
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.salesforce_transport import SalesforceTransport, configuration, install_transport


@pytest.fixture
def salesforce_env(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    journal = ResourceJournal(request.node.nodeid)
    scratch = bootstrapped_engine.url
    endpoint = (scratch.host, scratch.port, scratch.database, scratch.username)
    journal.event(
        "runtime-endpoints",
        postgresql={"host": scratch.host, "port": scratch.port, "database": scratch.database, "role": scratch.username},
        minio=CONFIGS["minio"]["host"],
        redis=CONFIGS["redis"]["host"],
    )

    def reserve_insert(connection: sa.Connection, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        target = connection.engine.url
        if (target.host, target.port, target.database, target.username) == endpoint and context.isinsert:
            for values in context.compiled_parameters:
                identities = {key: value for key, value in values.items() if key in {"id", "tenant_id", "kb_id", "connector_id", "document_id", "file_id"}}
                journal.event("reserve-sql-row", database=connection.engine.url.database, table=str(context.compiled.statement.table), identities=identities)

    # HTTP uses a separate AsyncEngine. A class hook scoped by exact scratch
    # endpoint captures both that engine and the worker's synchronous sessions.
    sa.event.listen(sa.Engine, "before_cursor_execute", reserve_insert)
    try:
        # Hooks are installed before these shared fixtures create their rows.
        env = request.getfixturevalue("connector_sync_api")
        journal.event("reserve-collection-before-use", collection=env["collection"], kb=env["kb"], action="preserve absence; cleanup-only when no parse is requested")
        adapter = env["storage_adapter"]
        put = adapter.put

        def tracked_put(bucket: str, key: str, body: bytes) -> Any:
            physical_key = f"{bucket}/{key}"
            journal.event("reserve-object", endpoint=CONFIGS["minio"]["host"], bucket=env["bucket"], key=physical_key, sha256=hashlib.sha256(body).hexdigest())
            return put(bucket, key, body)

        monkeypatch.setattr(adapter, "put", tracked_put)
        transport = SalesforceTransport()
        state = {"transport": transport}
        journal.event(
            "reserve-controlled-salesforce-transport",
            origin="https://owned.my.salesforce.com",
            object_ids={key: [row["Id"] for row in rows] for key, rows in transport.records.items()},
            input_sha256=hashlib.sha256(json.dumps(transport.records, sort_keys=True).encode()).hexdigest(),
        )
        install_transport(monkeypatch, transport)
        config = configuration(sync_deleted_files=False)
        headers = {"Authorization": "Bearer " + env["jwt"]}
        response = requests.post(
            env["base"] + "/api/v1/connectors", headers=headers, json={"name": "Salesforce controlled source", "source": "salesforce", "config": config, "refresh_freq": 0, "prune_freq": 0}, timeout=15
        ).json()
        assert response["retcode"] == 0, response
        connector_id = response["data"]["id"]
        assert not transport.requests, "save must have zero vendor IO"
        with Session(env["engine"]) as db:
            assert db.get(Connector, connector_id).config == config
            db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=env["kb"], auto_parse="0"))
            db.commit()
        try:
            yield {**env, "state": state, "config": config, "connector": connector_id, "headers": headers, "case_journal": journal}
        finally:
            with Session(env["engine"]) as db:
                extra_ids = list(db.scalars(sa.select(Connector.id).where(Connector.tenant_id == env["owners"][0], Connector.source == "salesforce")))
                for model, condition in [
                    (SyncLogs, SyncLogs.connector_id == connector_id),
                    (Connector2Kb, Connector2Kb.connector_id == connector_id),
                    (Connector, Connector.id.in_(extra_ids)),
                    (DocumentMetadata, DocumentMetadata.kb_id == env["kb"]),
                ]:
                    db.execute(sa.delete(model).where(condition))
                db.commit()
            with env["engine"].connect() as db:
                remaining = {
                    model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition))
                    for model, condition in [
                        (SyncLogs, SyncLogs.connector_id == connector_id),
                        (Connector2Kb, Connector2Kb.connector_id == connector_id),
                        (Connector, Connector.id.in_(extra_ids)),
                        (DocumentMetadata, DocumentMetadata.kb_id == env["kb"]),
                    ]
                }
            journal.event("salesforce-sql-cleanup-readback", remaining=remaining, transports_closed=(not transport.requests or transport.closed))
            assert not any(remaining.values()) and (not transport.requests or transport.closed)
    finally:
        sa.event.remove(sa.Engine, "before_cursor_execute", reserve_insert)


def _rows(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as reader:
        return {row["id"]: dict(row) for row in reader.execute(sa.select(Document.__table__).where(Document.kb_id == env["kb"])).mappings()}


async def _run(env: dict[str, Any], *, success: bool = True) -> dict[str, dict[str, Any]]:
    task_id = uuid4().hex
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with Session(env["engine"]) as db:
        db.add(SyncLogs(id=task_id, connector_id=env["connector"], kb_id=env["kb"], status=TaskStatus.SCHEDULE, task_type=ConnectorTaskType.SYNC, poll_range_start=start))
        db.commit()
    await Salesforce(env["config"])(
        {"id": task_id, "connector_id": env["connector"], "kb_id": env["kb"], "tenant_id": env["owners"][0], "poll_range_start": start, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
    )
    with env["engine"].connect() as reader:
        row = reader.execute(sa.select(SyncLogs.__table__).where(SyncLogs.id == task_id)).mappings().one()
        assert row["status"] == (TaskStatus.DONE if success else TaskStatus.FAIL), row["full_exception_trace"]
        assert row["docs_removed_from_index"] == 0
        if not success:
            assert row["poll_range_start"] == start
        env["case_journal"].event("worker-readback", task=task_id, status=row["status"], checkpoint=str(row["poll_range_start"]), removed=row["docs_removed_from_index"])
    assert env["state"]["transport"].closed or not env["state"]["transport"].requests
    return _rows(env)


async def test_salesforce_worker_real_sql_storage_and_same_timestamp_replay(salesforce_env: dict[str, Any]) -> None:
    env = salesforce_env
    rows = await _run(env)
    assert len(rows) == 2
    for row in rows.values():
        assert row["source_type"] == "salesforce/" + env["connector"]
        assert row["update_time"] is not None
        blob = read_object(env["storage"], env["bucket"], env["kb"] + "/" + row["location"])
        assert blob.startswith(b"Salesforce Account\n") and b"SystemModstamp: 2026-01-01T00:00:00Z" in blob
    assert set(await _run(env)) == set(rows)
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == env["kb"])) == 2


async def test_salesforce_partial_page_failure_retains_start_and_retry(salesforce_env: dict[str, Any]) -> None:
    env = salesforce_env
    env["state"]["transport"].mode = "later-page"
    partial = await _run(env, success=False)
    assert len(partial) == 1
    env["state"]["transport"].mode = "normal"
    # Resume is the existing explicit control; the failing poll start is kept.
    response = requests.patch(env["base"] + "/api/v1/connectors/" + env["connector"], headers=env["headers"], json={"status": "5", "reschedule": True}, timeout=15).json()
    assert response["retcode"] == 0
    assert len(await _run(env)) == 2


async def test_salesforce_empty_scope_and_invalid_update_have_no_vendor_or_document_effects(salesforce_env: dict[str, Any]) -> None:
    env = salesforce_env
    original = env["config"]
    response = requests.patch(env["base"] + "/api/v1/connectors/" + env["connector"], headers=env["headers"], json={"config": {**original, "sync_deleted_files": True}}, timeout=15).json()
    assert response["retcode"] != 0
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(Connector.config).where(Connector.id == env["connector"])) == original
    env["config"] = {**original, "objects": []}
    assert await _run(env) == {}
    assert not env["state"]["transport"].requests
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == env["kb"])) == 0


@pytest.mark.external_consumer
def test_salesforce_real_web_api_client_and_independent_sql_readback(salesforce_env: dict[str, Any], tmp_path: Path) -> None:
    env = salesforce_env
    checkout = Path(os.environ["WEB_DATASET_CHECKOUT"])
    script = Path(os.environ["SALESFORCE_WEB_CONSUMER"])
    journal = env["case_journal"]
    journal.event("reserve-web-consumer", script=str(script), sha256=hashlib.sha256(script.read_bytes()).hexdigest(), checkout=str(checkout), base=env["base"], tenant=env["owners"][0])
    command = ["node", "--import", "tsx", str(script), str(checkout), env["base"]]
    result = subprocess.run(
        command, cwd=checkout, env={**os.environ, "TEST_TENANT_TOKEN": env["jwt"], "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")}, capture_output=True, text=True, timeout=60
    )
    (tmp_path / "web-consumer.stdout").write_text(result.stdout)
    (tmp_path / "web-consumer.stderr").write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    journal.event("web-consumer-result", raw_exit=result.returncode, stdout=result.stdout)
    with env["engine"].connect() as reader:
        rows = list(reader.execute(sa.select(Connector.__table__).where(Connector.tenant_id == env["owners"][0], Connector.name.like("after98-w-%"))).mappings())
    assert {row["name"] for row in rows} == {"after98-w-default", "after98-w-none", "after98-w-selected"}
    configs = {row["name"]: row["config"] for row in rows}
    assert "objects" not in configs["after98-w-default"]
    assert configs["after98-w-none"]["objects"] == []
    assert configs["after98-w-selected"]["objects"] == ["Account", "Contact"]
    assert not env["state"]["transport"].requests
    journal.event("web-consumer-sql-readback", ids=[row["id"] for row in rows], scopes={name: conf.get("objects", "omitted") for name, conf in configs.items()}, vendor_requests=0)
