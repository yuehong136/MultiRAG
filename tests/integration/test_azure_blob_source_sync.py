"""Azure SDK boundary with real HTTP API, PostgreSQL and MinIO persistence."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from azure.storage.blob import ContainerClient
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, DocumentMetadata, SyncLogs
from api.db.services.connector_service import connector_doc_id_candidates
from common.config_utils import CONFIGS
from common.constants import ConnectorTaskType, TaskStatus
from common.data_source import azure_blob_connector as azure
from core.svr.sync_data_source import AzureBlob
from tests.support.azure_blob_source_transport import BlobSourceTransport, BlobVersion
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.resource_journal import ResourceJournal
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def azure_sync_env(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
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
        state: dict[str, Any] = {
            "objects": {"docs/a.txt": BlobVersion(b"first"), "docs/sub/a.txt": BlobVersion(b"second"), "outside.txt": BlobVersion(b"outside")},
            "transports": [],
            "fail_page": None,
            "fail_get": None,
        }

        def client(*args: Any, **kwargs: Any) -> ContainerClient:
            transport = BlobSourceTransport(state["objects"])
            transport.fail_list_page, transport.fail_get = state["fail_page"], state["fail_get"]
            state["transports"].append(transport)
            journal.event(
                "reserve-controlled-azure-transport",
                account="ownedaccount",
                container="container",
                keys=list(state["objects"]),
                input_sha256=hashlib.sha256(repr(state["objects"]).encode()).hexdigest(),
            )
            return ContainerClient(*args, **{**kwargs, "transport": transport, "retry_total": 0})

        monkeypatch.setattr(azure, "ContainerClient", client)
        config = {
            "auth_mode": "account_key",
            "prefix": "docs/",
            "batch_size": 1,
            "allow_images": False,
            "sync_deleted_files": False,
            "credentials": {"account_name": "ownedaccount", "account_key": "Y29udHJvbGxlZC1rZXk=", "container_name": "container"},
        }
        headers = {"Authorization": "Bearer " + env["jwt"]}
        response = requests.post(
            env["base"] + "/api/v1/connectors", headers=headers, json={"name": "Azure controlled source", "source": "azure_blob", "config": config, "refresh_freq": 0, "prune_freq": 0}, timeout=15
        ).json()
        assert response["retcode"] == 0, response
        connector_id = response["data"]["id"]
        assert not state["transports"], "save must have zero vendor IO"
        with Session(env["engine"]) as db:
            assert db.get(Connector, connector_id).config == config
            db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=env["kb"], auto_parse="0"))
            db.commit()
        try:
            yield {**env, "state": state, "config": config, "connector": connector_id, "headers": headers, "case_journal": journal}
        finally:
            with Session(env["engine"]) as db:
                for model, condition in [
                    (SyncLogs, SyncLogs.connector_id == connector_id),
                    (Connector2Kb, Connector2Kb.connector_id == connector_id),
                    (Connector, Connector.id == connector_id),
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
                        (Connector, Connector.id == connector_id),
                        (DocumentMetadata, DocumentMetadata.kb_id == env["kb"]),
                    ]
                }
            journal.event("azure-sql-cleanup-readback", remaining=remaining, transports_closed=all(item.closed for item in state["transports"]))
            assert not any(remaining.values()) and all(item.closed for item in state["transports"])
    finally:
        sa.event.remove(sa.Engine, "before_cursor_execute", reserve_insert)


def _rows(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as reader:
        return {row["id"]: dict(row) for row in reader.execute(sa.select(Document.__table__).where(Document.kb_id == env["kb"])).mappings()}


async def _run(env: dict[str, Any], *, success: bool = True, reindex: bool = False, prune: bool = False) -> dict[str, dict[str, Any]]:
    task_id = uuid4().hex
    start = datetime(2025, 1, 1, tzinfo=UTC)
    task_type = ConnectorTaskType.PRUNE if prune else ConnectorTaskType.SYNC
    with env["engine"].connect() as reader:
        failed = reader.scalar(sa.select(Connector.status).where(Connector.id == env["connector"])) == TaskStatus.FAIL
    if failed:
        resumed = requests.patch(env["base"] + "/api/v1/connectors/" + env["connector"], headers=env["headers"], json={"status": "5", "reschedule": True}, timeout=15).json()
        assert resumed["retcode"] == 0
    with Session(env["engine"]) as db:
        pending = db.scalar(sa.select(SyncLogs).where(SyncLogs.connector_id == env["connector"], SyncLogs.kb_id == env["kb"], SyncLogs.task_type == task_type, SyncLogs.status == TaskStatus.SCHEDULE))
        if pending is not None:
            task_id, start = pending.id, pending.poll_range_start
        else:
            db.add(SyncLogs(id=task_id, connector_id=env["connector"], kb_id=env["kb"], status=TaskStatus.SCHEDULE, task_type=task_type, poll_range_start=start))
            db.commit()
    before = len(env["state"]["transports"])
    await AzureBlob(env["config"])(
        {
            "id": task_id,
            "connector_id": env["connector"],
            "kb_id": env["kb"],
            "tenant_id": env["owners"][0],
            "poll_range_start": start,
            "reindex": "1" if reindex else "0",
            "auto_parse": False,
            "timeout_secs": 30,
            "task_type": ConnectorTaskType.PRUNE if prune else ConnectorTaskType.SYNC,
        }
    )
    with env["engine"].connect() as reader:
        row = reader.execute(sa.select(SyncLogs.__table__).where(SyncLogs.id == task_id)).mappings().one()
        expected = TaskStatus.CANCEL if prune else TaskStatus.DONE if success else TaskStatus.FAIL
        assert row["status"] == expected, row["full_exception_trace"]
        assert row["docs_removed_from_index"] == 0
        if not success:
            assert row["poll_range_start"] == start
        env["case_journal"].event("worker-readback", task=task_id, status=row["status"], checkpoint=str(row["poll_range_start"]), removed=row["docs_removed_from_index"])
    assert all(transport.closed for transport in env["state"]["transports"][before:])
    return _rows(env)


async def test_azure_sdk_worker_persists_identity_metadata_and_skips_unchanged_downloads(azure_sync_env: dict[str, Any]) -> None:
    env = azure_sync_env
    initial = await _run(env)
    expected_ids = {connector_doc_id_candidates(env["kb"], env["connector"], key)[-1] for key in ["docs/a.txt", "docs/sub/a.txt"]}
    assert set(initial) == expected_ids
    transport = env["state"]["transports"][-1]
    assert sum("restype" not in urlsplit(item.url).query for item in transport.requests) == 2
    for row in initial.values():
        assert read_object(env["storage"], env["bucket"], f"{env['kb']}/{row['location']}") == env["state"]["objects"][row["name"]].body
    with env["engine"].connect() as reader:
        metadata = list(reader.execute(sa.select(DocumentMetadata.__table__).where(DocumentMetadata.kb_id == env["kb"])).mappings())
        assert {row["id"] for row in metadata} == expected_ids
        assert all("container" in str(row) and "AccountKey" not in str(row) for row in metadata)
    unchanged = await _run(env)
    assert set(unchanged) == expected_ids
    assert all("restype" in urlsplit(item.url).query for item in env["state"]["transports"][-1].requests)
    env["state"]["objects"]["docs/a.txt"] = BlobVersion(b"updated", '"version-two"')
    changed = await _run(env)
    assert sum("restype" not in urlsplit(item.url).query for item in env["state"]["transports"][-1].requests) == 1
    assert {row["name"] for row in changed.values()} == {"docs/a.txt", "docs/sub/a.txt"}
    await _run(env, reindex=True)
    assert sum("restype" not in urlsplit(item.url).query for item in env["state"]["transports"][-1].requests) == 2


async def test_azure_failure_recovery_prune_and_http_edit_boundaries(azure_sync_env: dict[str, Any]) -> None:
    env = azure_sync_env
    initial = await _run(env)
    baseline = {key: row["content_hash"] for key, row in initial.items()}
    env["state"]["fail_page"] = 2
    assert {key: row["content_hash"] for key, row in (await _run(env, success=False)).items()} == baseline
    env["state"]["fail_page"] = None
    env["state"]["objects"]["docs/a.txt"] = BlobVersion(b"updated", '"version-two"')
    env["state"]["fail_get"] = "docs/a.txt"
    assert {key: row["content_hash"] for key, row in (await _run(env, success=False)).items()} == baseline
    env["state"]["fail_get"] = None
    recovered = await _run(env)
    assert {key: row["content_hash"] for key, row in recovered.items()} != baseline
    before_objects = {row["location"]: read_object(env["storage"], env["bucket"], f"{env['kb']}/{row['location']}") for row in recovered.values()}
    env["state"]["objects"]["docs/../outside/secret.txt"] = BlobVersion(b"forbidden")
    refused = await _run(env, success=False)
    assert {key: row["content_hash"] for key, row in refused.items()} == {key: row["content_hash"] for key, row in recovered.items()}
    assert {row["location"]: read_object(env["storage"], env["bucket"], f"{env['kb']}/{row['location']}") for row in refused.values()} == before_objects
    assert all("restype" in urlsplit(item.url).query for item in env["state"]["transports"][-1].requests)
    del env["state"]["objects"]["docs/../outside/secret.txt"]
    count = len(env["state"]["transports"])
    assert set(await _run(env, success=False, prune=True)) == set(initial)
    assert len(env["state"]["transports"]) == count
    url = env["base"] + "/api/v1/connectors/" + env["connector"]
    forbidden = requests.patch(url, headers={"Authorization": "Bearer " + env["api_key"]}, json={"config": {**env["config"], "prefix": "outside"}}, timeout=15).json()
    assert forbidden["retcode"] != 0
    invalid = requests.patch(url, headers=env["headers"], json={"config": {**env["config"], "sync_deleted_files": True}}, timeout=15).json()
    assert invalid["retcode"] != 0
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(Connector.config).where(Connector.id == env["connector"])) == env["config"]
    edited = {**env["config"], "prefix": "docs/sub/", "allow_images": True}
    response = requests.patch(url, headers=env["headers"], json={"config": edited}, timeout=15).json()
    assert response["retcode"] == 0 and response["data"]["config"] == edited
    with env["engine"].connect() as reader:
        assert reader.scalar(sa.select(Connector.config).where(Connector.id == env["connector"])) == edited
    assert len(env["state"]["transports"]) == count, "save/edit must issue zero Azure requests"
