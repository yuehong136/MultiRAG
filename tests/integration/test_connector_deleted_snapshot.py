"""Controlled source listings, actual scratch SQL/Milvus/MinIO/Redis cleanup."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs, Task
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import FileSource, TaskStatus
from common.data_source.models import NotionSearchResponse, SlimDocument
from common.data_source.notion_connector import NotionConnector
from core.svr import sync_data_source
from core.utils.redis_conn import REDIS_CONN
from tests.integration.test_document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("source_key", [FileSource.S3, FileSource.AIRTABLE, FileSource.GOOGLE_DRIVE, FileSource.BITBUCKET, FileSource.GMAIL])
@pytest.mark.parametrize("missing_index", [False, True])
async def test_complete_snapshot_prunes_only_owned_source_and_empty_prunes_last_file(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, missing_index: bool, source_key: str) -> None:
    env = parse_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    source = f"{source_key}/{connector_id}"
    retained_id = connector_doc_id_candidates(kb_id, connector_id, "retained")[0]
    removed_id = connector_doc_id_candidates(kb_id, connector_id, "removed")[-1]
    neighbor_id, running_id = uuid4().hex, uuid4().hex
    key = f"{running_id}-cancel"
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="snapshot scratch", source=source_key, input_type="poll", config={"sync_deleted_files": True}, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, from_beginning="0"))
        db.commit()
        kb = db.get(Knowledgebase, kb_id)
        assert kb is not None
        errors, ids = SyncLogsService.duplicate_and_parse(
            db,
            kb,
            [{"id": identifier, "semantic_identifier": name, "extension": ".txt", "blob": name.encode()} for identifier, name in [(retained_id, "retained"), (removed_id, "removed")]],
            owner,
            source,
            auto_parse=False,
        )
        assert not errors and set(ids) == {retained_id, removed_id}
        db.add(Document(id=neighbor_id, kb_id=kb_id, created_by=owner, source_type="local", name="neighbor.txt", type="doc", parser_id="naive"))
        db.add(Task(id=running_id, doc_id=removed_id, task_type="Parse"))
        db.commit()
        locations = {doc.id: doc.location for doc in db.scalars(sa.select(Document).where(Document.id.in_([retained_id, removed_id])))}
        assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == 2

    before_objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
    assert before_objects
    if not missing_index:
        settings.docStoreConn.create_idx(env["collection"], kb_id, 768)
        assert (
            settings.docStoreConn.insert(
                [
                    {"id": uuid4().hex, "doc_id": identifier, "kb_id": kb_id, "content_with_weight": identifier, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768}
                    for identifier in [retained_id, removed_id]
                ],
                env["collection"],
                kb_id,
            )
            == []
        )

    class Source:
        failure = False
        empty = False
        cancel_on_list = False

        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            if self.cancel_on_list:
                with Session(env["engine"]) as db:
                    ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
            if not self.empty:
                yield [SlimDocument(id="retained")]
            if self.failure:
                raise PermissionError("later page denied")

    class Driver(sync_data_source.SyncBase):
        SOURCE_NAME = source_key

        async def _generate(self, task: dict[str, Any]) -> Iterator[list[Any]]:
            return iter(())

    driver = Driver({"sync_deleted_files": True})
    driver.connector = Source()
    current = {
        "id": task_id,
        "connector_id": connector_id,
        "kb_id": kb_id,
        "tenant_id": owner,
        "poll_range_start": datetime(2026, 1, 1, tzinfo=UTC),
        "reindex": "0",
        "auto_parse": False,
        "timeout_secs": 30,
    }
    env["record"]["parse"]["redis_keys"].append(key)
    try:
        # A later page failure cannot remove the stale candidate from SQL/store.
        driver.connector.failure = True
        with pytest.raises(PermissionError, match="later page"):
            await driver._run_task_logic(current)
        with Session(env["engine"]) as db:
            assert db.get(Document, removed_id) is not None
            assert db.get(Task, running_id) is not None
        assert not REDIS_CONN.REDIS.exists(key)
        assert {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)} == before_objects

        driver.connector.failure = False
        driver.connector.cancel_on_list = True
        await driver(current)
        with Session(env["engine"]) as db:
            assert db.get(Document, removed_id) is not None
            assert db.get(SyncLogs, task_id).status == TaskStatus.CANCEL
            assert db.get(Connector, connector_id).status == TaskStatus.CANCEL
            assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
            ConnectorService.resume(db, connector_id, TaskStatus.SCHEDULE)
        driver.connector.cancel_on_list = False
        await driver(current)
        with Session(env["engine"]) as db:
            assert db.get(Document, removed_id) is None
            assert db.get(Task, running_id) is None
            assert db.get(Document, retained_id) is not None
            assert db.get(Document, neighbor_id) is not None
            assert not list(db.scalars(sa.select(File2Document.id).where(File2Document.document_id == removed_id)))
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == TaskStatus.DONE and log.docs_removed_from_index == 1
            assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == 1
        assert REDIS_CONN.REDIS.get(key) in {b"x", "x"}
        remaining_objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
        assert not any(locations[removed_id] in obj for obj in remaining_objects)
        assert any(locations[retained_id] in obj for obj in remaining_objects)
        if not missing_index:
            rows = settings.docStoreConn.query(env["collection"], filter=f'doc_id == "{removed_id}"', output_fields=["doc_id"], consistency_level="Strong")
            assert rows == []
            assert settings.docStoreConn.query(env["collection"], filter=f'doc_id == "{retained_id}"', output_fields=["doc_id"], consistency_level="Strong")

        # The same completed metadata contract explicitly permits an empty scope.
        driver.connector.empty = True
        await driver._run_task_logic(current)
        with Session(env["engine"]) as db:
            assert db.get(Document, retained_id) is None
            assert db.get(Document, neighbor_id) is not None
            assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == 0
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.docs_removed_from_index == 2
        assert not any(locations[retained_id] in obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True))
    finally:
        REDIS_CONN.REDIS.delete(key)
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()


async def test_notion_workspace_search_cannot_authorize_deletion(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parse_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    document_id = connector_doc_id_candidates(kb_id, connector_id, "existing-page")[-1]
    original = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="Notion scratch", source=FileSource.NOTION, input_type="poll", config={"sync_deleted_files": True}, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=original))
        db.commit()
        kb = db.get(Knowledgebase, kb_id)
        assert kb is not None
        errors, ids = SyncLogsService.duplicate_and_parse(
            db,
            kb,
            [{"id": document_id, "semantic_identifier": "existing-page", "extension": ".txt", "blob": b"existing Notion page"}],
            owner,
            f"notion/{connector_id}",
            auto_parse=False,
        )
        assert not errors and ids == [document_id]

    class Driver(sync_data_source.SyncBase):
        SOURCE_NAME = FileSource.NOTION

        async def _generate(self, task: dict[str, Any]) -> Iterator[list[Any]]:
            return iter(())

    driver = Driver({"sync_deleted_files": True})
    driver.connector = NotionConnector()
    search_calls: list[dict[str, Any]] = []

    def empty_search(query: dict[str, Any]) -> NotionSearchResponse:
        search_calls.append(query)
        return NotionSearchResponse(results=[], next_cursor=None)

    monkeypatch.setattr(driver.connector, "_search_notion", empty_search)
    before_objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
    assert before_objects
    try:
        await driver({"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30})
        assert search_calls == []
        with Session(env["engine"]) as db:
            assert db.get(Document, document_id) is not None
            assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == 1
            assert db.scalar(sa.select(File2Document.id).where(File2Document.document_id == document_id)) is not None
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == TaskStatus.FAIL and log.poll_range_start == original
            assert "root_page_id" in log.error_msg
            assert db.get(Connector, connector_id).status == TaskStatus.FAIL
            assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
        assert {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)} == before_objects
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()


@pytest.mark.parametrize("source", ["s3", "jira", "gmail"])
def test_connector_configuration_http_save_and_independent_readback(parse_api: dict[str, Any], source: str) -> None:
    import requests

    env = parse_api
    base = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    config = {"sync_deleted_files": False, "custom_option": {"keep": True}, "credentials": {"jira_username": "scratch-user", "jira_password": "synthetic-test-value"}}
    response = requests.post(base, headers=headers, json={"name": "snapshot configuration", "source": source, "config": config}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0
    connector_id = response.json()["data"]["id"]
    try:
        for enabled in [True, False]:
            config = {**config, "sync_deleted_files": enabled}
            response = requests.patch(f"{base}/{connector_id}", headers=headers, json={"config": config}, timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] == 0
            readback = requests.get(f"{base}/{connector_id}", headers=headers, timeout=30)
            assert readback.status_code == 200 and readback.json()["retcode"] == 0
            assert readback.json()["data"]["config"] == config
            with Session(env["engine"]) as db:
                stored = db.get(Connector, connector_id)
                assert stored is not None and stored.config == config
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()
