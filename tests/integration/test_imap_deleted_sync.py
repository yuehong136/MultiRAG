"""Production IMAP synchronization with controlled protocol and real owned stores."""

import email
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service
from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import ConnectorService
from common.constants import TaskStatus
from common.data_source.imap_connector import EmailHeaders, ImapConnector, imap_scope_signature
from core.svr.sync_data_source import IMAP
from tests.support.connector_deleted_sync import assert_configuration_readback, assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.imap_source import ImapSource, message
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def configuration() -> dict[str, Any]:
    return {"imap_host": "imap.test", "imap_port": 993, "imap_mailbox": [], "poll_range": 30, "credentials": {"imap_username": "owner", "imap_password": "synthetic"}, "sync_deleted_files": True}


@pytest.mark.parametrize("kind", ["mail", "attachment"])
@pytest.mark.parametrize("mode", ["complete", "empty", "list-error", "second-mailbox-error", "read-error", "missing-read", "disabled"])
async def test_imap_real_deletion_resources(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str, kind: str) -> None:
    source = ImapSource(mode)
    monkeypatch.setattr(ImapConnector, "_get_mail_client", lambda self: source)
    ids = {name: f"<{name}@test>" + ("#att:报告.txt" if kind == "attachment" else "") for name in ["retained", "stale"]}
    conf = {**configuration(), "sync_deleted_files": mode != "disabled"}
    await assert_deleted_sync(connector_sync_api, monkeypatch, IMAP, "imap", ids, conf, mode)
    with Session(connector_sync_api["engine"]) as db:
        assert db.scalar(select(Connector.id).where(Connector.source == "imap")) is None
    assert source.logged_out > 0


async def test_generated_ids_deduplicate_real_ingestion_and_object_readback(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = connector_sync_api
    raw = message(None, date="Thu, 01 Jan 2026 12:00:00 +0000")
    source = ImapSource(messages={"1": raw})
    monkeypatch.setattr(ImapConnector, "_get_mail_client", lambda self: source)
    identifier, link = uuid4().hex, uuid4().hex
    conf = configuration()
    source_id = EmailHeaders.from_email_msg(email.message_from_bytes(raw)).id
    source_type = f"imap/{identifier}"
    with Session(env["engine"]) as db:
        db.add(Connector(id=identifier, tenant_id=env["owners"][0], name="IMAP generated", source="imap", config=conf, input_type="poll", status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link, connector_id=identifier, kb_id=env["kb"]))
        db.commit()
    try:
        observed: set[str] | None = None
        for _ in range(2):
            task_id = uuid4().hex
            original = datetime(2025, 12, 1, tzinfo=UTC)
            with Session(env["engine"]) as db:
                db.add(SyncLogs(id=task_id, connector_id=identifier, kb_id=env["kb"], status=TaskStatus.SCHEDULE, poll_range_start=original))
                db.commit()
            await IMAP(conf)(
                {"id": task_id, "connector_id": identifier, "kb_id": env["kb"], "tenant_id": env["owners"][0], "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
            )
            with Session(env["engine"]) as db:
                log = db.get(SyncLogs, task_id)
                assert log.status == TaskStatus.DONE, log.full_exception_trace
                rows = list(db.scalars(select(Document).where(Document.source_type == source_type)))
                assert len(rows) == 2
                assert {row.name for row in rows} == {"Mail subject.txt", "报告.txt"}
                current = {row.id for row in rows}
                assert observed is None or current == observed
                observed = current
                assert set(db.scalars(select(File2Document.document_id).where(File2Document.document_id.in_(current)))) == current
                assert db.get(Knowledgebase, env["kb"]).doc_num == 2
                for row in rows:
                    response = env["storage"].get_object(env["bucket"], f"{env['kb']}/{row.location}")
                    try:
                        assert response.read() == (b"Mail body" if row.name == "Mail subject.txt" else b"attachment body")
                    finally:
                        response.close()
                        response.release_conn()
            assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == 2
        assert source_id.startswith("generated:")
    finally:
        with Session(env["engine"]) as db:
            db.execute(delete(SyncLogs).where(SyncLogs.connector_id == identifier))
            db.execute(delete(Connector2Kb).where(Connector2Kb.id == link))
            db.execute(delete(Connector).where(Connector.id == identifier))
            db.commit()


def test_imap_configuration_readback(connector_sync_api: dict[str, Any]) -> None:
    assert_configuration_readback(connector_sync_api, "imap", configuration())


def test_scope_restart_retry_rebuild_config_save_and_parallel_kbs(bootstrapped_engine: Engine) -> None:
    identifier = uuid4().hex
    conf = configuration()
    signature = imap_scope_signature(conf)
    with Session(bootstrapped_engine) as db:
        db.add(Connector(id=identifier, tenant_id=uuid4().hex, source="imap", name="scope persistence", config=conf, input_type="poll", status=TaskStatus.SCHEDULE))
        db.commit()

    def persist(kb: str, task: str, start: float, *, reindex: bool = False, incremental: bool = False) -> float:
        with Session(bootstrapped_engine) as db:
            return ConnectorService.ensure_imap_sync_scope(db, identifier, kb, task, signature, start, reindex=reindex, incremental=incremental)

    try:
        assert persist("a", "initial", 1000.0) == 1000.0
        assert persist("a", "restart", 9999.0, incremental=True) == 1000.0
        with ThreadPoolExecutor(2) as threads:
            futures = [threads.submit(persist, kb, "initial", start) for kb, start in [("b", 2000.0), ("c", 3000.0)]]
            assert [future.result() for future in futures] == [2000.0, 3000.0]
        assert persist("a", "rebuild", 4000.0, reindex=True) == 4000.0
        assert persist("a", "rebuild", 5000.0, reindex=True) == 4000.0
        assert persist("old", "upgrade", 6000.0, incremental=True) == 0
        with Session(bootstrapped_engine) as db:
            stored = db.get(Connector, identifier).config["imap_sync_scopes"]
            assert {kb: item["start"] for kb, item in stored.items()} == {"a": 4000.0, "b": 2000.0, "c": 3000.0, "old": 0}
            # A stale client cannot erase or replace the worker's committed state.
            changed = {**conf, "imap_mailbox": ["Archive"], "custom": "preserved", "imap_sync_scopes": {}}
            assert connector_api_service.update_connector(db, identifier, {"config": changed})[0]
        with Session(bootstrapped_engine) as db:
            config = db.get(Connector, identifier).config
            assert config["imap_sync_scopes"] == stored and config["custom"] == "preserved"
            with pytest.raises(RuntimeError, match="rebuild"):
                ConnectorService.ensure_imap_sync_scope(db, identifier, "a", "next", imap_scope_signature(config), 7000.0, reindex=False, incremental=True)
        with Session(bootstrapped_engine) as db:
            assert ConnectorService.ensure_imap_sync_scope(db, identifier, "a", "new-rebuild", imap_scope_signature(config), 7000.0, reindex=True, incremental=False) == 7000.0
        with Session(bootstrapped_engine) as db:
            scopes = db.get(Connector, identifier).config["imap_sync_scopes"]
            assert scopes["a"]["start"] == 7000.0 and scopes["b"]["start"] == 2000.0
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(delete(Connector).where(Connector.id == identifier))
            db.commit()
