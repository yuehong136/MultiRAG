"""Actual source SQL, target SQL/object/vector readback and per-KB cursors."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service
from api.db.db_models import Connector, Connector2Kb, Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from api.db.services.doc_metadata_service import DocMetadataService
from common.constants import TaskStatus
from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.rdbms_connector import RDBMSConnector, rdbms_scope_signature
from core.svr.sync_data_source import MySQL, PostgreSQL
from tests.support.connector_deleted_sync import assert_deleted_sync
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.rdbms_source import rdbms_source as rdbms_source
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def source_connector(env: dict[str, Any], **changes: Any) -> RDBMSConnector:
    config = env["config"] | changes
    result = RDBMSConnector(
        db_type=env["backend"], **{key: config[key] for key in ("host", "port", "database", "query", "content_columns", "metadata_columns", "id_column", "timestamp_column", "batch_size")}
    )
    result.load_credentials(config["credentials"])
    return result


def test_actual_query_ranges_types_hashes_metadata_and_empty(rdbms_source: dict[str, Any]) -> None:
    env = rdbms_source
    result = source_connector(env)
    upper = result.get_max_cursor_value()
    assert upper == Decimal("2.000002")
    documents = [doc for batch in result.load_from_state() for doc in batch]
    assert [doc.id for doc in documents] == [doc.id for doc in collect_slim_document_snapshot(result)]
    assert len(documents) == 2 and documents[1].metadata["seq"] == "2.000002"
    assert [doc.id for batch in result.load_from_cursor_range(upper, upper) for doc in batch] == [documents[1].id]
    assert [doc.id for batch in result.load_from_cursor_range(Decimal("1.000001"), upper) for doc in batch] == [doc.id for doc in documents]
    # Existence has both rows even though the incremental range has only one.
    assert len(collect_slim_document_snapshot(result)) == 2
    text_cursor = source_connector(env, timestamp_column="category")
    assert len([doc for batch in text_cursor.load_from_cursor_range("Z", text_cursor.get_max_cursor_value()) for doc in batch]) == 2
    hash_result = source_connector(env, id_column=None, content_columns="")
    assert [doc.id for batch in hash_result.load_from_state() for doc in batch] == [doc.id for doc in collect_slim_document_snapshot(hash_result)]
    binary = source_connector(env, id_column=None, content_columns="payload", metadata_columns="payload", query=env["config"]["query"].replace("LIMIT 100", "LIMIT 1"))
    binary_docs = [doc for batch in binary.load_from_state() for doc in batch]
    assert [doc.id for doc in binary_docs] == [doc.id for doc in collect_slim_document_snapshot(binary)]
    assert b"base64" in binary_docs[0].blob and "base64" in binary_docs[0].metadata["payload"]
    with env["engine"].begin() as db:
        db.execute(env["table"].update().where(env["table"].c.id == 2).values(active=False))
    assert len(collect_slim_document_snapshot(result)) == 1
    with env["engine"].begin() as db:
        db.execute(env["table"].delete())
    assert result.get_max_cursor_value() is None and collect_slim_document_snapshot(result) == ()
    env["record"]["readback"] = {"typed_decimal": True, "inclusive_boundary": True, "full_query_inventory": True, "auto_content_hash_id": True, "successful_empty": True}


@pytest.mark.parametrize("mode", ["complete", "empty", "partial-read", "query-error", "ingestion-error", "disabled"])
async def test_real_deletion_and_failure_protection(connector_sync_api: dict[str, Any], rdbms_source: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    source = rdbms_source
    source_connector(source).validate_connector_settings()
    with source["engine"].begin() as db:
        db.execute(source["table"].delete().where(source["table"].c.id == 2))
        if mode == "empty":
            db.execute(source["table"].delete())
    conf = dict(source["config"])
    if mode == "query-error":
        conf["query"] = "SELECT * FROM missing_owned_table"
    if mode == "disabled":
        conf["sync_deleted_files"] = False
    if mode == "partial-read":
        original = RDBMSConnector._rows

        def interrupted(self: RDBMSConnector, start: Any = None, end: Any = None) -> Any:
            yield from original(self, start, end)
            if start is None and end is None:
                raise ConnectionError("controlled disconnect before full snapshot exhaustion")

        monkeypatch.setattr(RDBMSConnector, "_rows", interrupted)
    source_name = source["backend"]
    await assert_deleted_sync(
        connector_sync_api,
        monkeypatch,
        MySQL if source_name == "mysql" else PostgreSQL,
        source_name,
        {"retained": f"DatabaseType.{source_name.upper()}:{conf['database']}:1", "stale": f"DatabaseType.{source_name.upper()}:{conf['database']}:2"},
        conf,
        mode,
    )
    source["record"]["target_readback"] = {"mode": mode, "sql_objects_vectors_checked": True}


async def test_same_title_metadata_cursor_commit_and_replay(connector_sync_api: dict[str, Any], rdbms_source: dict[str, Any]) -> None:
    env, source = connector_sync_api, rdbms_source
    with source["engine"].begin() as db:
        db.execute(source["table"].update().values(body="same title"))
    identifier, link = uuid4().hex, uuid4().hex
    conf = source["config"]
    driver_type = MySQL if source["backend"] == "mysql" else PostgreSQL
    with Session(env["engine"]) as db:
        db.add(Connector(id=identifier, tenant_id=env["owners"][0], name="RDBMS acceptance", source=source["backend"], input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link, connector_id=identifier, kb_id=env["kb"]))
        db.commit()
    expected_ids = {
        connector_doc_id_candidates(env["kb"], identifier, f"DatabaseType.{source['backend'].upper()}:{conf['database']}:{number}")[-1]: category for number, category in [(1, "a"), (2, "b")]
    }
    try:
        retained_id = next(identifier for identifier, category in expected_ids.items() if category == "a")
        for iteration in range(4):
            if iteration >= 2:
                with source["engine"].begin() as db:
                    db.execute(source["table"].delete().where(source["table"].c.id == (2 if iteration == 2 else 1)))
            active_ids = expected_ids if iteration < 2 else {retained_id: "a"} if iteration == 2 else {}
            expected_cursor = Decimal("2.000002") if iteration < 2 else Decimal("1.000001") if iteration == 2 else None
            with Session(env["engine"]) as db:
                if iteration == 0:
                    current = SyncLogs(id=uuid4().hex, connector_id=identifier, kb_id=env["kb"], status=TaskStatus.SCHEDULE)
                    db.add(current)
                    db.commit()
                else:
                    current = db.scalar(sa.select(SyncLogs).where(SyncLogs.connector_id == identifier, SyncLogs.status == TaskStatus.SCHEDULE))
                task = {
                    "id": current.id,
                    "connector_id": identifier,
                    "kb_id": env["kb"],
                    "tenant_id": env["owners"][0],
                    "poll_range_start": current.poll_range_start,
                    "reindex": "0",
                    "auto_parse": False,
                    "timeout_secs": 30,
                }
                config = db.get(Connector, identifier).config
            await driver_type(config)(task)
            with Session(env["engine"]) as db:
                assert db.get(SyncLogs, task["id"]).status == TaskStatus.DONE
                saved = db.get(Connector, identifier).config["rdbms_sync_scopes"][env["kb"]]
                assert RDBMSConnector.deserialize_cursor_value(saved["cursor"]) == expected_cursor
                rows = list(db.scalars(sa.select(Document).where(Document.source_type == f"{source['backend']}/{identifier}")))
                assert {row.id for row in rows} == set(active_ids)
                assert db.get(Knowledgebase, env["kb"]).doc_num == len(active_ids)
                for row in rows:
                    metadata = DocMetadataService.get_document_metadata(db, row.id)
                    assert metadata["category"] == active_ids[row.id]
                    response = env["storage"].get_object(env["bucket"], f"{env['kb']}/{row.location}")
                    try:
                        assert response.read() == "【body】:\nsame title".encode()
                    finally:
                        response.close()
                        response.release_conn()
            assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(active_ids)
        source["record"]["same_title_metadata_and_cursor"] = True
        source["record"]["last_record_deleted_and_cursor_reset"] = True
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == identifier))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link))
            db.execute(sa.delete(Connector).where(Connector.id == identifier))
            db.commit()


def test_cursor_completion_rollback_parallel_kbs_and_config_save(bootstrapped_engine: sa.Engine) -> None:
    engine = bootstrapped_engine
    identifier = uuid4().hex
    conf = {"database": "controlled-source", "credentials": {"username": "reader"}, "timestamp_column": "seq"}
    signature = rdbms_scope_signature(conf, "postgresql")
    with Session(engine) as db:
        db.add(Connector(id=identifier, tenant_id=uuid4().hex, name="cursor state", source="postgresql", input_type="poll", config=conf))
        db.commit()

    def complete(kb: str, value: Decimal, fail: bool = False) -> None:
        with Session(engine) as db:
            task_id = uuid4().hex
            db.add(SyncLogs(id=task_id, connector_id=identifier, kb_id=kb, status=TaskStatus.RUNNING))
            db.commit()
            state = {"source": "postgresql", "scope": signature, "cursor": RDBMSConnector.serialize_cursor_value(value)}
            if fail:
                # Row changes are staged, then rolled back like failed completion.
                ConnectorService.stage_rdbms_sync_state(db, identifier, kb, state)
                db.rollback()
            else:
                SyncLogsService.complete_and_schedule_next(db, task_id, identifier, kb, datetime.now(UTC), rdbms_state=state)

    try:
        with ThreadPoolExecutor(2) as threads:
            list(threads.map(lambda pair: complete(*pair), [("a", Decimal("1.000001")), ("b", Decimal("2.000002"))]))
        complete("a", Decimal(999), fail=True)
        with Session(engine) as db:
            saved = db.get(Connector, identifier).config["rdbms_sync_scopes"]
            assert {kb: RDBMSConnector.deserialize_cursor_value(item["cursor"]) for kb, item in saved.items()} == {"a": Decimal("1.000001"), "b": Decimal("2.000002")}
            assert connector_api_service.update_connector(db, identifier, {"config": conf | {"custom": "keep", "rdbms_sync_scopes": {}}})[0]
        with Session(engine) as db:
            config = db.get(Connector, identifier).config
            assert config["rdbms_sync_scopes"] == saved and config["custom"] == "keep"
            with pytest.raises(RuntimeError, match="changed"):
                ConnectorService.stage_rdbms_sync_state(db, identifier, "a", {"source": "postgresql", "scope": "stale", "cursor": 500})
    finally:
        with Session(engine) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == identifier))
            db.execute(sa.delete(Connector).where(Connector.id == identifier))
            db.commit()


def test_discovery_and_permission_loss_fail_closed(rdbms_source: dict[str, Any]) -> None:
    source = rdbms_source
    result = source_connector(source, query="")
    assert len(collect_slim_document_snapshot(result)) == 2
    if source["backend"] == "postgresql":
        with source["engine"].begin() as connection:
            connection.exec_driver_sql(f'REVOKE SELECT ON "{source["table"].name}" FROM "{source["record"]["role"]}"')
        from psycopg2.errors import InsufficientPrivilege

        with pytest.raises(InsufficientPrivilege, match="permission denied"):
            collect_slim_document_snapshot(result)
        with source["engine"].connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE usename=:name"), {"name": source["record"]["role"]}) == 0
    source["record"]["discovery_inventory_checked"] = True
