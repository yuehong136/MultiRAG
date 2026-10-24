"""Controlled source listings, actual scratch SQL/Milvus/MinIO/Redis cleanup."""

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, File2Document, Knowledgebase, SyncLogs, Task
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import ConnectorTaskType, FileSource, TaskStatus
from common.data_source.models import Document as SourceDocument
from common.data_source.models import NotionSearchResponse, SlimDocument
from common.data_source.notion_connector import NotionConnector
from core.nlp import search
from core.svr import sync_data_source
from core.utils.redis_conn import REDIS_CONN
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("legacy_index", [None, 0, 1], ids=["new", "external-only", "connector-only"])
async def test_shared_external_id_isolated_across_kbs_and_connectors(connector_sync_api: dict[str, Any], legacy_index: int | None) -> None:
    """Run creation, duplicate updates and pruning against actual owned stores."""
    env = connector_sync_api
    owner, kb_id = env["owners"][0], env["kb"]
    other_kb, connector_id, other_connector = uuid4().hex, uuid4().hex, uuid4().hex
    other_name = f"connector_identity_{uuid4().hex}"
    collections = {kb_id: env["collection"], other_kb: search.index_name_one(owner, other_name)}
    scopes = {"original": (kb_id, connector_id), "same-connector": (other_kb, connector_id), "same-kb": (kb_id, other_connector)}
    tasks = {
        name: {"id": uuid4().hex, "connector_id": connector, "kb_id": kb, "tenant_id": owner, "poll_range_start": datetime(2026, 1, 1, tzinfo=UTC), "reindex": "0", "auto_parse": False}
        for name, (kb, connector) in scopes.items()
    }
    external_id = "s3:shared-bucket:shared-key.txt"
    manifest: dict[str, Any] = {"secondary_kb": other_kb, "secondary_collection": collections[other_kb], "connector_ids": [connector_id, other_connector], "legacy_index": legacy_index}
    env["record"]["connector_identity"] = manifest

    class Source:
        retained = True

        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            if self.retained:
                yield [SlimDocument(id=external_id)]

    class Driver(sync_data_source.SyncBase):
        SOURCE_NAME = FileSource.S3

        def __init__(self) -> None:
            super().__init__({"sync_deleted_files": True})
            self.connector = Source()
            self.body = b"shared original body"
            self.ingest = True

        async def _initialize_connector(self, task: dict[str, Any]) -> None:
            pass

        async def _generate(self, task: dict[str, Any]) -> Iterator[list[SourceDocument]]:
            doc = SourceDocument(
                id=external_id, source=self.SOURCE_NAME, semantic_identifier="shared-key", extension=".txt", blob=self.body, size_bytes=len(self.body), doc_updated_at=datetime(2026, 2, 1, tzinfo=UTC)
            )
            return iter([[doc]]) if self.ingest else iter(())

    def remove_owned_rows() -> None:
        with Session(env["engine"]) as db:
            other_doc_ids = sa.select(Document.id).where(Document.kb_id == other_kb)
            db.execute(sa.delete(File2Document).where(File2Document.document_id.in_(other_doc_ids)))
            db.execute(sa.delete(Document).where(Document.kb_id == other_kb))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == other_kb))
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id.in_([connector_id, other_connector])))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id.in_([connector_id, other_connector])))
            db.execute(sa.delete(Connector).where(Connector.id.in_([connector_id, other_connector])))
            db.commit()
        with env["engine"].connect() as db:
            assert not db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.kb_id == other_kb))
            assert not db.scalar(sa.select(sa.func.count()).select_from(Connector).where(Connector.id.in_([connector_id, other_connector])))
        manifest["secondary_sql_removed"] = True

    def remove_other_index() -> None:
        settings.docStoreConn.delete_idx(collections[other_kb], other_kb)
        assert not settings.docStoreConn.has_collection(collections[other_kb])
        manifest["secondary_collection_removed"] = True

    with ExitStack() as cleanup:
        cleanup.callback(remove_owned_rows)
        cleanup.callback(remove_other_index)
        with Session(env["engine"]) as db:
            db.add(Knowledgebase(id=other_kb, tenant_id=owner, created_by=owner, name=other_name, embd_id="scratch-embedding", parser_id="naive", parser_config={}))
            for connector in [connector_id, other_connector]:
                db.add(
                    Connector(
                        id=connector, tenant_id=owner, name="identity scratch", source=FileSource.S3, input_type="poll", config={"sync_deleted_files": True}, prune_freq=5, status=TaskStatus.SCHEDULE
                    )
                )
            for current in tasks.values():
                db.add(Connector2Kb(id=uuid4().hex, connector_id=current["connector_id"], kb_id=current["kb_id"]))
                db.add(SyncLogs(id=current["id"], connector_id=current["connector_id"], kb_id=current["kb_id"], status=TaskStatus.SCHEDULE))
            db.commit()
            legacy_id = connector_doc_id_candidates(kb_id, connector_id, external_id)[legacy_index] if legacy_index is not None else None
            if legacy_id is not None:
                errors, ids = SyncLogsService.duplicate_and_parse(
                    db, db.get(Knowledgebase, kb_id), [{"id": legacy_id, "semantic_identifier": "shared-key", "extension": ".txt", "blob": b"old body"}], owner, f"s3/{connector_id}", auto_parse=False
                )
                assert not errors and ids == [legacy_id]

        driver = Driver()
        for _ in range(2):
            for current in tasks.values():
                await driver._run_task_logic(current)

        # Independent SQL and object reads prove three rows, in-place history
        # reuse, no cross-connector legacy reuse, and no same-content duplicates.
        with Session(env["engine"]) as db:
            rows = list(db.scalars(sa.select(Document).where(Document.kb_id.in_([kb_id, other_kb]))))
            assert len(rows) == 3
            by_scope = {(row.kb_id, row.source_type): (row.id, row.location) for row in rows}
            identities = {name: by_scope[(kb, f"s3/{connector}")] for name, (kb, connector) in scopes.items()}
            assert len({identifier for identifier, _ in identities.values()}) == 3
            if legacy_id is not None:
                assert identities["original"][0] == legacy_id
            assert all(db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb)) == count for kb, count in [(kb_id, 2), (other_kb, 1)])
            assert all(db.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.document_id == identifier)) == 1 for identifier, _ in identities.values())
            assert all(db.get(SyncLogs, current["id"]).error_count == 0 for current in tasks.values())
        manifest["identities"] = identities
        for name, (kb, _) in scopes.items():
            assert read_object(env["storage"], env["bucket"], f"{kb}/{identities[name][1]}") == driver.body
        before_objects = {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)}
        assert len(before_objects) == 3
        for kb, collection in collections.items():
            settings.docStoreConn.create_idx(collection, kb, 768)
            assert (
                settings.docStoreConn.insert(
                    [
                        {"id": uuid4().hex, "doc_id": identities[name][0], "kb_id": kb, "content_with_weight": name, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768}
                        for name, (scoped_kb, _) in scopes.items()
                        if scoped_kb == kb
                    ],
                    collection,
                    kb,
                )
                == []
            )

        driver.body = b"updated original scope only"
        await driver._run_task_logic(tasks["original"])
        for name, (kb, _) in scopes.items():
            assert read_object(env["storage"], env["bucket"], f"{kb}/{identities[name][1]}") == (driver.body if name == "original" else b"shared original body")

        # A successful nonempty listing with no changed content retains the
        # historical/current row. Empty listing prunes only this KB+connector.
        driver.ingest = False
        with Session(env["engine"]) as db:
            prune = SyncLogsService.schedule(db, connector_id, kb_id, task_type=ConnectorTaskType.PRUNE)
            assert prune is not None
            tasks["original"].update(id=prune.id, task_type=ConnectorTaskType.PRUNE)
        await driver._run_task_logic(tasks["original"])
        with Session(env["engine"]) as db:
            assert db.get(Document, identities["original"][0]) is not None
        driver.connector.retained = False
        await driver._run_task_logic(tasks["original"])
        with Session(env["engine"]) as db:
            assert set(db.scalars(sa.select(Document.id).where(Document.kb_id.in_([kb_id, other_kb])))) == {identities[name][0] for name in ["same-connector", "same-kb"]}
            assert all(db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb)) == 1 for kb in collections)
            assert not db.scalar(sa.select(File2Document.id).where(File2Document.document_id == identities["original"][0]))
            assert db.get(SyncLogs, tasks["original"]["id"]).docs_removed_from_index == 1
            assert all(db.get(SyncLogs, tasks[name]["id"]).docs_removed_from_index == 0 for name in ["same-connector", "same-kb"])
        assert {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)} == before_objects - {f"{kb_id}/{identities['original'][1]}"}
        for kb, collection in collections.items():
            indexed = settings.docStoreConn.query(collection, filter=f'kb_id == "{kb}"', output_fields=["doc_id"], consistency_level="Strong")
            assert {row["doc_id"] for row in indexed} == {identities[name][0] for name, (scoped_kb, _) in scopes.items() if scoped_kb == kb and name != "original"}
        manifest["isolation_and_deletion_verified"] = True


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
        db.add(
            Connector(id=connector_id, tenant_id=owner, name="snapshot scratch", source=source_key, input_type="poll", config={"sync_deleted_files": True}, prune_freq=5, status=TaskStatus.SCHEDULE)
        )
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, task_type=ConnectorTaskType.PRUNE, status=TaskStatus.SCHEDULE, from_beginning="0"))
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
                    ConnectorService.cancel_tasks(db, connector_id)
            if not self.empty:
                yield [SlimDocument(id="retained")]
            if self.failure:
                raise PermissionError("later page denied")

    class Driver(sync_data_source.SyncBase):
        SOURCE_NAME = source_key

        async def _initialize_connector(self, task: dict[str, Any]) -> None:
            pass

        async def _generate(self, task: dict[str, Any]) -> Iterator[list[Any]]:
            return iter(())

    driver = Driver({"sync_deleted_files": True})
    driver.connector = Source()
    current = {
        "task_type": ConnectorTaskType.PRUNE,
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
            ConnectorService.schedule_tasks(db, connector_id)
            task_id = SyncLogsService.get_latest_task(db, connector_id, kb_id, ConnectorTaskType.PRUNE).id
            current["id"] = task_id
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
        db.add(
            Connector(
                id=connector_id, tenant_id=owner, name="Notion scratch", source=FileSource.NOTION, input_type="poll", config={"sync_deleted_files": True}, prune_freq=5, status=TaskStatus.SCHEDULE
            )
        )
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, task_type=ConnectorTaskType.PRUNE, status=TaskStatus.SCHEDULE, poll_range_start=original))
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

        async def _initialize_connector(self, task: dict[str, Any]) -> None:
            pass

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
        await driver(
            {
                "task_type": ConnectorTaskType.PRUNE,
                "id": task_id,
                "connector_id": connector_id,
                "kb_id": kb_id,
                "tenant_id": owner,
                "poll_range_start": original,
                "reindex": "0",
                "auto_parse": False,
                "timeout_secs": 30,
            }
        )
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
