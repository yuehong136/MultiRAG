"""Actual S3/MinIO sync with independent SQL, object and Milvus readback."""

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import boto3
import pytest
import sqlalchemy as sa
import xxhash
from botocore.config import Config
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, Knowledgebase, SyncLogs
from api.db.services.connector_service import SyncLogsService, connector_doc_id_candidates
from api.db.services.document_service import DocumentService
from common import settings
from common.config_utils import CONFIGS
from common.constants import TaskStatus
from common.data_source.blob_connector import BlobStorageConnector, _normalize_etag
from common.data_source.config import BlobType
from core.svr.sync_data_source import S3
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def s3_sync(connector_sync_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = connector_sync_api
    cfg = CONFIGS["minio"]
    secure = str(cfg.get("secure", False)).lower() in {"true", "1", "yes"}
    client = boto3.client(
        "s3",
        endpoint_url=("https://" if secure else "http://") + cfg["host"],
        aws_access_key_id=cfg["user"],
        aws_secret_access_key=cfg["password"],
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 0}),
    )
    bucket, connector = f"s3-fingerprint-{uuid4().hex}", uuid4().hex
    state: dict[str, Any] = {"source_bucket": bucket, "connector_id": connector, "gets": [], "runs": []}
    env["record"]["s3_fingerprint"] = state

    def count_get(params: dict[str, Any], **kwargs: Any) -> None:
        state["gets"].append(params["Key"])

    client.meta.events.register("before-parameter-build.s3.GetObject", count_get)

    def load_credentials(self: BlobStorageConnector, credentials: dict[str, Any]) -> None:
        self.s3_client = client
        self.batch_size = 1
        self.size_threshold = state.get("max_size", self.size_threshold)

    monkeypatch.setattr(BlobStorageConnector, "load_credentials", load_credentials)
    conf = {"bucket_name": bucket, "prefix": "docs", "sync_deleted_files": True, "credentials": {}}

    def cleanup_source() -> None:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                client.delete_object(Bucket=bucket, Key=obj["Key"])
        assert client.list_objects_v2(Bucket=bucket).get("KeyCount", 0) == 0
        client.delete_bucket(Bucket=bucket)
        assert bucket not in {item["Name"] for item in client.list_buckets()["Buckets"]}
        state["source_bucket_removed"] = True

    def cleanup_sql() -> None:
        with env["engine"].begin() as db:
            for model in [SyncLogs, Connector2Kb, Connector]:
                column = model.id if model is Connector else model.connector_id
                db.execute(sa.delete(model).where(column == connector))
        with env["engine"].connect() as db:
            for model in [SyncLogs, Connector2Kb, Connector]:
                column = model.id if model is Connector else model.connector_id
                assert db.scalar(sa.select(sa.func.count()).select_from(model).where(column == connector)) == 0
        state["connector_sql_removed"] = True

    with ExitStack() as cleanup:
        cleanup.callback(client.close)
        client.create_bucket(Bucket=bucket)
        cleanup.callback(cleanup_source)
        cleanup.callback(cleanup_sql)
        with Session(env["engine"]) as db:
            db.add(Connector(id=connector, tenant_id=env["owners"][0], name="S3 fingerprint scratch", source="s3", input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
            db.add(Connector2Kb(id=uuid4().hex, connector_id=connector, kb_id=env["kb"], auto_parse="0"))
            db.commit()
        yield {**env, "client": client, "source_bucket": bucket, "connector": connector, "driver": S3(conf), "state": state}


def put(env: dict[str, Any], key: str, body: bytes) -> None:
    env["client"].put_object(Bucket=env["source_bucket"], Key="docs/" + key, Body=body)


def rows(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    with env["engine"].connect() as db:
        return {row["id"]: dict(row) for row in db.execute(sa.select(Document.__table__).where(Document.kb_id == env["kb"])).mappings()}


async def run_sync(env: dict[str, Any], *, reindex: bool = False, success: bool = True) -> tuple[dict[str, dict[str, Any]], list[str]]:
    task_id = uuid4().hex
    start = datetime(2025, 1, 1, tzinfo=UTC)
    with Session(env["engine"]) as db:
        previous_count = db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == env["connector"]))
        db.add(SyncLogs(id=task_id, connector_id=env["connector"], kb_id=env["kb"], status=TaskStatus.SCHEDULE, poll_range_start=start))
        db.commit()
    env["state"]["gets"].clear()
    await env["driver"](
        {
            "id": task_id,
            "connector_id": env["connector"],
            "kb_id": env["kb"],
            "tenant_id": env["owners"][0],
            "poll_range_start": start,
            "reindex": "1" if reindex else "0",
            "auto_parse": False,
            "timeout_secs": 60,
        }
    )
    with env["engine"].connect() as db:
        log = dict(db.execute(sa.select(SyncLogs.__table__).where(SyncLogs.id == task_id)).mappings().one())
        assert log["status"] == (TaskStatus.DONE if success else TaskStatus.FAIL), log["full_exception_trace"]
        assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == env["connector"])) == previous_count + (2 if success else 1)
        if not success:
            assert log["poll_range_start"] == start
            assert log["docs_removed_from_index"] == 0 and log["error_msg"]
    current = rows(env)
    gets = list(env["state"]["gets"])
    env["state"]["runs"].append(
        {"task_id": task_id, "status": log["status"], "gets": gets, "versions": {key: row["content_hash"] for key, row in current.items()}, "removed": log["docs_removed_from_index"]}
    )
    return current, gets


def assert_objects(env: dict[str, Any], current: dict[str, dict[str, Any]], expected: dict[str, bytes]) -> None:
    assert {row["name"] for row in current.values()} == set(expected)
    for row in current.values():
        assert read_object(env["storage"], env["bucket"], f"{env['kb']}/{row['location']}") == expected[row["name"]]
    assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == len(expected)


def index_rows(env: dict[str, Any], current: dict[str, dict[str, Any]]) -> None:
    store = settings.docStoreConn
    store.create_idx(env["collection"], env["kb"], 768)
    assert (
        store.insert(
            [{"id": uuid4().hex, "doc_id": identifier, "kb_id": env["kb"], "content_with_weight": row["name"], "vector": [0.1] * 768, "q_768_vec": [0.1] * 768} for identifier, row in current.items()],
            env["collection"],
            env["kb"],
        )
        == []
    )


def assert_index(env: dict[str, Any], expected: set[str]) -> None:
    found = settings.docStoreConn.query(env["collection"], filter=f'kb_id == "{env["kb"]}"', output_fields=["doc_id"], consistency_level="Strong")
    assert {item["doc_id"] for item in found} == expected


@pytest.mark.parametrize("legacy", [None, 0, 1], ids=["scoped", "external-id", "connector-id"])
async def test_etag_sync_skips_unchanged_fetches_only_changed_and_retains_namespace(s3_sync: dict[str, Any], legacy: int | None) -> None:
    env = s3_sync
    for key, body in [("static.txt", b"static"), ("changed.txt", b"original"), ("gone.txt", b"gone")]:
        put(env, key, body)
    old_id = None
    if legacy is not None:
        external_id = f"{BlobType.S3}:{env['source_bucket']}:docs/static.txt"
        old_id = connector_doc_id_candidates(env["kb"], env["connector"], external_id)[legacy]
        with Session(env["engine"]) as db:
            errors, identifiers = SyncLogsService.duplicate_and_parse(
                db, db.get(Knowledgebase, env["kb"]), [{"id": old_id, "semantic_identifier": "static.txt", "extension": ".txt", "blob": b"static"}], env["owners"][0], "s3/" + env["connector"], False
            )
            assert not errors and identifiers == [old_id]
    initial, gets = await run_sync(env)
    assert sorted(gets) == ["docs/changed.txt", "docs/gone.txt", "docs/static.txt"]
    assert_objects(env, initial, {"static.txt": b"static", "changed.txt": b"original", "gone.txt": b"gone"})
    if old_id is not None:
        assert old_id in initial
    for row in initial.values():
        etag = env["client"].head_object(Bucket=env["source_bucket"], Key="docs/" + row["name"])["ETag"]
        assert row["content_hash"] == _normalize_etag(etag)
    with Session(env["engine"]) as db:
        assert DocumentService.list_id_content_hash_map_by_kb_and_source_type(db, env["kb"], "s3/" + env["connector"], page_size=1) == {
            identifier: row["content_hash"] for identifier, row in initial.items()
        }
        assert DocumentService.list_id_content_hash_map_by_kb_and_source_type(db, env["kb"], "s3/another") == {}
        assert DocumentService.list_id_content_hash_map_by_kb_and_source_type(db, uuid4().hex, "s3/" + env["connector"]) == {}
    index_rows(env, initial)
    unchanged, gets = await run_sync(env)
    assert gets == [] and unchanged == initial
    assert_index(env, set(initial))
    put(env, "changed.txt", b"changed!")
    env["client"].delete_object(Bucket=env["source_bucket"], Key="docs/gone.txt")
    changed, gets = await run_sync(env)
    assert gets == ["docs/changed.txt"]
    assert_objects(env, changed, {"static.txt": b"static", "changed.txt": b"changed!"})
    assert_index(env, set(changed))
    with env["engine"].connect() as db:
        assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == env["kb"])) == 2
    reindexed, gets = await run_sync(env, reindex=True)
    assert sorted(gets) == ["docs/changed.txt", "docs/static.txt"]
    # A forced fetch updates the ingestion timestamp even for equal content.
    # All business fields and identities must remain unchanged.
    volatile = {"update_time", "update_date"}
    assert {identifier: {k: v for k, v in row.items() if k not in volatile} for identifier, row in reindexed.items()} == {
        identifier: {k: v for k, v in row.items() if k not in volatile} for identifier, row in changed.items()
    }
    assert_objects(env, reindexed, {"static.txt": b"static", "changed.txt": b"changed!"})
    assert_index(env, set(changed))


@pytest.mark.parametrize("failure", ["list", "get"])
async def test_incomplete_sync_fails_without_pruning_or_advancing(s3_sync: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = s3_sync
    for key in ["retained.txt", "stale.txt"]:
        put(env, key, key.encode())
    initial, _ = await run_sync(env)
    index_rows(env, initial)
    env["client"].delete_object(Bucket=env["source_bucket"], Key="docs/stale.txt")
    put(env, "retained.txt", b"changed")
    if failure == "get":

        def deny(params: dict[str, Any], **kwargs: Any) -> None:
            raise PermissionError("controlled GetObject failure")

        env["client"].meta.events.register("before-parameter-build.s3.GetObject", deny)
    else:
        original = env["client"].get_paginator

        class BrokenPaginator:
            def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
                yield next(iter(original("list_objects_v2").paginate(**kwargs)))
                raise PermissionError("controlled later-page failure")

        monkeypatch.setattr(env["client"], "get_paginator", lambda name: BrokenPaginator())
    current, _ = await run_sync(env, success=False)
    assert current == initial
    assert_objects(env, current, {"retained.txt": b"retained.txt", "stale.txt": b"stale.txt"})
    assert_index(env, set(initial))
    if failure == "list":
        monkeypatch.setattr(env["client"], "get_paginator", original)


async def test_listing_get_race_persists_fetched_version_and_missing_etag_refetches(s3_sync: dict[str, Any]) -> None:
    env = s3_sync
    put(env, "race.txt", b"first")
    client = env["client"]
    changed = False

    def race(params: dict[str, Any], **kwargs: Any) -> None:
        nonlocal changed
        if not changed:
            changed = True
            put(env, "race.txt", b"second")

    client.meta.events.register("before-parameter-build.s3.GetObject", race)
    current, gets = await run_sync(env)
    assert gets == ["docs/race.txt"]
    assert_objects(env, current, {"race.txt": b"second"})
    assert next(iter(current.values()))["content_hash"] == _normalize_etag(client.head_object(Bucket=env["source_bucket"], Key="docs/race.txt")["ETag"])
    _, gets = await run_sync(env)
    assert gets == []

    def strip_etags(parsed: dict[str, Any], **kwargs: Any) -> None:
        for obj in parsed.get("Contents", []):
            obj.pop("ETag", None)

    client.meta.events.register("after-call.s3.ListObjectsV2", strip_etags)
    for _ in range(2):
        current, gets = await run_sync(env)
        assert gets == ["docs/race.txt"]
        assert_objects(env, current, {"race.txt": b"second"})

    def strip_response_etag(parsed: dict[str, Any], **kwargs: Any) -> None:
        parsed.pop("ETag", None)

    client.meta.events.register("after-call.s3.GetObject", strip_response_etag)
    current, gets = await run_sync(env)
    assert gets == ["docs/race.txt"]
    assert next(iter(current.values()))["content_hash"] == xxhash.xxh128(b"second").hexdigest()
    assert_objects(env, current, {"race.txt": b"second"})


async def test_size_exclusion_retains_document_and_empty_inventory_removes_it(s3_sync: dict[str, Any]) -> None:
    env = s3_sync
    put(env, "limited.txt", b"tiny")
    initial, _ = await run_sync(env)
    index_rows(env, initial)
    env["state"]["max_size"] = 5
    put(env, "limited.txt", b"too large for configured limit")
    excluded, gets = await run_sync(env)
    assert gets == [] and excluded == initial
    assert_objects(env, excluded, {"limited.txt": b"tiny"})
    assert_index(env, set(initial))
    env["client"].delete_object(Bucket=env["source_bucket"], Key="docs/limited.txt")
    empty, gets = await run_sync(env)
    assert empty == {} and gets == []
    assert_objects(env, empty, {})
    assert_index(env, set())
