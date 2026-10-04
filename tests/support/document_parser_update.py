"""Owned listener and complete PostgreSQL/Milvus/MinIO/Redis PATCH readbacks."""

import copy
import json
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, File2Document, Knowledgebase, PipelineOperationLog, SourceRecoveryRecord, Task, UserCanvas
from api.db.services import document_image_lock as image_lock
from api.db.services import document_status_service as status_service
from api.db.services import document_task_service as task_service
from api.db.services.document_ingest_recovery import recovery_key
from api.db.services.document_source_recovery import source_recovery_key
from common import settings
from common.config_utils import CONFIGS
from core.utils.redis_conn import REDIS_CONN
from tests.support.database import scratch_database
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_read_service import _json, _snapshot
from tests.support.document_image_read_service import image_resources as image_resources

_EVIDENCE_LOCK = threading.RLock()


def save(path: Path, value: Any) -> None:
    with _EVIDENCE_LOCK:
        path.write_text(json.dumps(value, default=_json, indent=2))
        path.chmod(0o600)


@pytest.fixture(scope="module")
def parser_database(_require_services: Any, tmp_path_factory: Any) -> Iterator[sa.Engine]:
    evidence = Path(os.environ.get("MULTIRAG_C810_API_EVIDENCE_DIR", str(tmp_path_factory.mktemp("parser_http"))))
    with scratch_database(evidence) as engine:
        yield engine


@pytest.fixture
def bootstrapped_engine(parser_database: sa.Engine) -> sa.Engine:
    return parser_database


def definition() -> dict[str, Any]:
    names = ["File", "Parser", "TokenChunker", "Tokenizer"]
    return {
        "components": {
            name: {
                "obj": {"component_name": name, "params": {"setups": {"text&code": {"suffix": ["txt"], "output_format": "text"}}} if name == "Parser" else {}},
                "upstream": names[index - 1 : index] if index else [],
                "downstream": names[index + 1 : index + 2],
            }
            for index, name in enumerate(names)
        },
        "path": [],
    }


@pytest.fixture
def parser_api(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    with ExitStack() as cleanup:
        env = image_http_api
        tracked_put = env["storage"].put

        def put(*args: Any, **kwargs: Any) -> Any:
            if "bucket" in kwargs:
                return tracked_put(kwargs.pop("bucket"), kwargs.pop("fnm"), kwargs.pop("binary"), **kwargs)
            return tracked_put(*args, **kwargs)

        monkeypatch.setattr(env["storage"], "put", put)
        ids = env["ids"]
        env["evidence"] = Path(os.environ.get("MULTIRAG_C810_API_EVIDENCE_DIR", str(env["evidence"])))
        env["evidence"].mkdir(parents=True, exist_ok=True, mode=0o700)
        env["parser_record"] = {
            "test_nodeid": request.node.nodeid,
            "events": [],
            "manifest": {
                "database": str(env["engine"].url.database),
                "documents": {},
                "files": [],
                "links": [],
                "tasks": [],
                "canvas": [],
                "redis_keys": [env["queue"]],
                "advisory_locks": [],
                "objects": [],
                "listener": env["manifest"]["listener"],
                "pid": os.getpid(),
                "private_config": None,
                "containers": [],
                "collections": env["collections"],
                "bucket": env["bucket"],
            },
        }
        record_path = env["evidence"] / f"{ids['kb']}.parser.json"
        env["parser_record_path"] = record_path
        manifest = env["parser_record"]["manifest"]
        for key in ["a", "b", "foreign"]:
            manifest["documents"][key] = uuid4().hex
        env["docs"] = manifest["documents"]
        for _ in range(3):
            manifest["files"].append(uuid4().hex)
            manifest["links"].append(uuid4().hex)
            manifest["tasks"].append(uuid4().hex)
        env["canvas"] = uuid4().hex
        manifest["canvas"].append(env["canvas"])
        for key in [*env["docs"].values()]:
            manifest["redis_keys"].append(recovery_key(key))
        manifest["redis_keys"].extend(task + "-cancel" for task in manifest["tasks"])
        manifest["redis_keys"].extend(image_lock.task_image_reservation_key(task) for task in manifest["tasks"])
        manifest["objects"] = [(ids["kb"], "a.txt"), (ids["kb"], "b.txt"), (ids["foreign"], "foreign.txt"), (ids["kb"], "history.png"), (ids["kb"], "shared.png"), (ids["foreign"], "foreign.png")]
        env["manifest"]["file_ids"] = manifest["files"]
        env["register"]()
        save(record_path, env["parser_record"])
        monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: env["queue"])

        def remove_rows() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.delete(SourceRecoveryRecord).where(SourceRecoveryRecord.document_id.in_(list(env["docs"].values()))))
                db.execute(sa.delete(PipelineOperationLog).where(PipelineOperationLog.document_id.in_(list(env["docs"].values()))))
                db.execute(sa.delete(UserCanvas).where(UserCanvas.id.in_(manifest["canvas"])))
                db.commit()
            with env["engine"].connect() as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(UserCanvas).where(UserCanvas.id.in_(manifest["canvas"]))) == 0
            env["parser_record"].setdefault("cleanup", {})["canvas_absent"] = True

        def remove_redis() -> None:
            REDIS_CONN.REDIS.delete(*manifest["redis_keys"])
            assert not any(REDIS_CONN.REDIS.exists(key) for key in manifest["redis_keys"])
            env["parser_record"].setdefault("cleanup", {})["redis_keys_absent"] = True

        cleanup.callback(save, record_path, env["parser_record"])
        cleanup.callback(remove_rows)
        cleanup.callback(remove_redis)
        with Session(env["engine"]) as db:
            db.add(UserCanvas(id=env["canvas"], user_id=ids["owner"], title="Owned dataflow", canvas_category="dataflow_canvas", permission="team", dsl=definition()))
            for index, key in enumerate(["a", "b", "foreign"]):
                dataset = ids["foreign"] if key == "foreign" else ids["kb"]
                owner = ids["other"] if key == "foreign" else ids["owner"]
                db.add(
                    Document(
                        id=env["docs"][key],
                        kb_id=dataset,
                        created_by=owner,
                        name=key + ".txt",
                        location=key + ".txt",
                        suffix="txt",
                        type="doc",
                        parser_id="naive",
                        parser_config={"pages": [[1, 1000000]], "raptor": {"use_raptor": False, "future": None}, "graphrag": {"use_graphrag": False}, "llm_id": "preserved", "unknown": [0]},
                        status="0" if key == "a" else "1",
                        run="3",
                        progress=1,
                        chunk_num=2 if key == "a" else 1,
                        token_num=0,
                        process_duration=7,
                    )
                )
                db.add(File(id=manifest["files"][index], parent_id=dataset, tenant_id=owner, created_by=owner, name=key + ".txt", location=key + ".txt", type="doc"))
                db.add(File2Document(id=manifest["links"][index], file_id=manifest["files"][index], document_id=env["docs"][key]))
                db.add(Task(id=manifest["tasks"][index], doc_id=env["docs"][key], progress=1, chunk_ids=""))
                env["storage"].put(dataset, key + ".txt", ("Original source " + key).encode())
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["kb"]).values(chunk_num=3, token_num=0))
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["foreign"]).values(chunk_num=1, token_num=0))
            db.commit()
        for key in ["history.png", "shared.png"]:
            env["storage"].put(ids["kb"], key, ("Exact image bytes " + key).encode())
        env["storage"].put(ids["foreign"], "foreign.png", b"Foreign image bytes")

        def create_collection(name: str) -> None:
            assert settings.docStoreConn.create_idx(env["collections"][name], ids[name], 768) is not False

        jobs = request.config.getoption("--fixture-jobs")
        if jobs == 1:
            for name in ["kb", "foreign"]:
                create_collection(name)
        else:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(create_collection, ["kb", "foreign"]))
        rows: list[dict[str, Any]] = []
        for key, identifier, mother, image in [
            ("a", "mother", "mother", "a.txt"),
            ("a", "child", "mother", "history.png"),
            ("a", "shared-child", "mother", "shared.png"),
            ("b", "other", "", "shared.png"),
            ("foreign", "foreign", "", "foreign.png"),
        ]:
            dataset = ids["foreign"] if key == "foreign" else ids["kb"]
            chunk_id = env["docs"][key] + identifier
            parent = env["docs"][key] + mother if mother else ""
            row = {
                "id": chunk_id,
                "pk": chunk_id,
                "doc_id": env["docs"][key],
                "kb_id": dataset,
                "content_with_weight": "Retained " + identifier,
                "docnm_kwd": key + ".txt",
                "img_id": dataset + "-" + image,
                "available_int": 0 if key == "a" else 1,
                "mom_id": parent,
                "vector": [0.2] * 768,
                "q_768_vec": [0.3] * 768,
                "create_timestamp_flt": 1790920000.0,
                "create_time": "2026-10-02 10:00:00",
            }
            rows.append(row)
            assert settings.docStoreConn.insert([row], env["collections"]["foreign" if key == "foreign" else "kb"], dataset) == []
        env["parser_record"]["source_rows"] = rows
        save(record_path, env["parser_record"])

        def register_tasks(db: Session, *args: Any) -> None:
            for row in db.new:
                if isinstance(row, Task) and row.doc_id in env["docs"].values() and row.id not in manifest["tasks"]:
                    manifest["tasks"].append(row.id)
                    manifest["redis_keys"].append(row.id + "-cancel")
                    manifest["redis_keys"].append(image_lock.task_image_reservation_key(row.id))
                    save(record_path, env["parser_record"])

        original_reserve = image_lock.reserve_task_image

        def reserve(task_id: str, key: tuple[str, str]) -> None:
            # Register failed attempts before their first reservation is created.
            redis_key = image_lock.task_image_reservation_key(task_id)
            if redis_key not in manifest["redis_keys"]:
                manifest["redis_keys"].append(redis_key)
                save(record_path, env["parser_record"])
            original_reserve(task_id, key)

        for module in [image_lock, status_service, task_service]:
            monkeypatch.setattr(module, "reserve_task_image", reserve)

        original_prepare = status_service.prepare_source_recovery

        def prepare(bind: Any, document_id: str, task_id: str, **data: Any) -> Any:
            key = source_recovery_key(document_id, task_id)
            if key not in manifest["redis_keys"]:
                manifest["redis_keys"].append(key)
                save(record_path, env["parser_record"])
            return original_prepare(bind, document_id, task_id, **data)

        monkeypatch.setattr(status_service, "prepare_source_recovery", prepare)

        def register_lock(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
            if "advisory_xact_lock" in statement:
                manifest["advisory_locks"].append(parameters["key"])
                save(record_path, env["parser_record"])

        sa.event.listen(Session, "before_flush", register_tasks)
        cleanup.callback(sa.event.remove, Session, "before_flush", register_tasks)
        sa.event.listen(env["engine"], "before_cursor_execute", register_lock)
        cleanup.callback(sa.event.remove, env["engine"], "before_cursor_execute", register_lock)
        env["parser_record"]["initial_stores"] = snapshot(env)
        save(record_path, env["parser_record"])

        def snapshot_before_cleanup() -> None:
            env["parser_record"]["before_cleanup"] = snapshot(env)
            save(record_path, env["parser_record"])

        cleanup.callback(snapshot_before_cleanup)
        yield env


def snapshot(env: dict[str, Any]) -> dict[str, Any]:
    result = _snapshot(env)
    with env["engine"].connect() as db:
        for model in [UserCanvas, PipelineOperationLog, SourceRecoveryRecord]:
            result["sql"][model.__tablename__] = [dict(row) for row in db.execute(sa.select(model.__table__).order_by(model.id)).mappings()]
    result["redis"], result["redis_states"] = {}, {}
    script = """
local kind = redis.call('TYPE', KEYS[1]).ok
local dump = redis.call('DUMP', KEYS[1])
local hex = ''
if dump then hex = string.gsub(dump, '.', function(c) return string.format('%02x', string.byte(c)) end) end
return {kind, hex, redis.call('PTTL', KEYS[1]), kind == 'string' and redis.call('GET', KEYS[1]) or false}
"""
    for key in env["parser_record"]["manifest"]["redis_keys"]:
        if key == env["queue"]:
            continue
        kind, dump, ttl, value = REDIS_CONN.REDIS.eval(script, 1, key)
        kind = kind.decode() if isinstance(kind, bytes) else kind
        assert isinstance(kind, str) and isinstance(ttl, int)
        result["redis_states"][key] = {"type": kind, "dump_hex": dump.decode() if isinstance(dump, bytes) else dump, "ttl": ttl, "absent": kind == "none"}
        if kind == "string":
            assert value is not None
            result["redis"][key] = {"value": value.decode() if isinstance(value, bytes) else value, "ttl": ttl}
    result["image_reservations"] = {
        key: {"members": sorted(REDIS_CONN.REDIS.smembers(key)), "ttl": REDIS_CONN.REDIS.pttl(key)} for key in env["parser_record"]["manifest"]["redis_keys"] if key.startswith("document-task-images:")
    }
    return result


def full_readback(env: dict[str, Any]) -> dict[str, Any]:
    """Independent physical SQL/native/object/Redis reads of the owned scratch."""
    from minio import Minio
    from pymilvus import MilvusClient
    from redis import Redis
    from sqlalchemy.pool import NullPool

    from tests.support.document_image_read_service import _raw_object

    result = snapshot(env)
    reader = sa.create_engine(env["engine"].url, poolclass=NullPool)
    try:
        with reader.connect() as db:
            inspector = sa.inspect(db)
            tables = db.execute(sa.text("SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY schemaname, tablename")).all()
            result["physical_sql"] = {}
            for schema, table in tables:
                quoted = db.dialect.identifier_preparer.quote_schema(schema) + "." + db.dialect.identifier_preparer.quote(table)
                result["physical_sql"][quoted] = {
                    "columns": [{**column, "type": str(column["type"])} for column in inspector.get_columns(table, schema=schema)],
                    "primary_key": inspector.get_pk_constraint(table, schema=schema),
                    "foreign_keys": inspector.get_foreign_keys(table, schema=schema),
                    "indexes": inspector.get_indexes(table, schema=schema),
                    "rows": [dict(row) for row in db.execute(sa.text(f"SELECT t.*, xmin::text AS _xmin FROM {quoted} t ORDER BY to_jsonb(t)::text")).mappings()],
                }
    finally:
        reader.dispose()
    cfg = CONFIGS["milvus"]
    native = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    try:
        result["native_index_stats"] = {name: native.get_collection_stats(collection) if native.has_collection(collection) else None for name, collection in env["collections"].items()}
    finally:
        native.close()
    cfg = CONFIGS["minio"]
    storage = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    result["native_objects"] = {obj.object_name: _raw_object(storage, env["bucket"], obj.object_name) for obj in storage.list_objects(env["bucket"], recursive=True)}
    redis = Redis(**{**REDIS_CONN.REDIS.connection_pool.connection_kwargs, "decode_responses": False})
    try:
        raw = {}
        for key in env["parser_record"]["manifest"]["redis_keys"]:
            kind = redis.type(key).decode()
            item: dict[str, Any] = {"type": kind, "dump": redis.dump(key), "pttl": redis.pttl(key)}
            if kind == "string":
                item["value"] = redis.get(key)
            elif kind == "set":
                item["members"] = sorted(redis.smembers(key))
            elif kind == "stream":
                item.update(entries=redis.xrange(key), stream=redis.xinfo_stream(key), groups=redis.xinfo_groups(key))
                item["consumers"] = {group["name"].decode(): redis.xinfo_consumers(key, group["name"]) for group in item["groups"]}
                item["pending"] = {group["name"].decode(): redis.xpending_range(key, group["name"], "-", "+", 100) for group in item["groups"]}
            elif kind != "none":
                raise AssertionError(f"Uncaptured Redis type {kind}")
            raw[key] = item
        result["native_redis"] = raw
    finally:
        redis.close()

    def json_keys(value: Any) -> Any:
        if isinstance(value, dict):
            return {(key.decode() if isinstance(key, bytes) else key): json_keys(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_keys(item) for item in value]
        return value

    return json_keys(result)


def assert_full_read_only(before: dict[str, Any], after: dict[str, Any], elapsed: float) -> None:
    left, right = copy.deepcopy(before), copy.deepcopy(after)
    for key, item in left["native_redis"].items():
        other = right["native_redis"][key]
        if item["pttl"] >= 0:
            assert 0 <= item["pttl"] - other["pttl"] <= elapsed * 1000 + 1000
            other["pttl"] = item["pttl"]
        for category, fields in [("consumers", ["idle", "inactive"]), ("pending", ["time_since_delivered"])]:
            for group, rows in item.get(category, {}).items():
                for a, b in zip(rows, other[category][group], strict=True):
                    for field in fields:
                        if field in a:
                            assert 0 <= b[field] - a[field] <= elapsed * 1000 + 1000
                            b[field] = a[field]
    assert left == right


def assert_nonparser_physical_allowlist(env: dict[str, Any], before: dict[str, Any], after: dict[str, Any], payload: dict[str, Any]) -> None:
    """Only this Document's explicit config/time/version and own KB version may move."""
    left, right = copy.deepcopy(before["physical_sql"]), copy.deepcopy(after["physical_sql"])
    for name, table in left.items():
        for a, b in zip(table["rows"], right[name]["rows"], strict=True):
            if a.get("id") == env["docs"]["a"] and name.split(".")[-1].strip('"') == Document.__tablename__:
                fields = {"update_time", "update_date", "_xmin"}
                if "parser_config" in payload:
                    fields.add("parser_config")
                for field in fields:
                    b[field] = a[field]
            elif a.get("id") == env["ids"]["kb"] and name.split(".")[-1].strip('"') == Knowledgebase.__tablename__:
                b["_xmin"] = a["_xmin"]
    assert left == right


def assert_durable_journal(env: dict[str, Any]) -> None:
    captured = snapshot(env)
    key = recovery_key(env["docs"]["a"])
    assert captured["redis"][key]["value"]
    assert captured["redis_states"][key]["type"] == "string" and captured["redis_states"][key]["dump_hex"]
    material = json.loads(captured["redis"][key]["value"])["payload"]
    assert material["document_id"] == env["docs"]["a"] and material["nonce"]
    env["parser_record"].setdefault("durable_journal_readbacks", []).append(captured)
    save(env["parser_record_path"], env["parser_record"])


def patch(env: dict[str, Any], payload: Any, *, role: str | None = "owner", key: str = "a", status: int = 200, dataset: str | None = None) -> dict[str, Any]:
    path = f"/api/v1/datasets/{dataset or env['ids']['kb']}/documents/{env['docs'][key]}"
    before_full = full_readback(env)
    response = requests.patch(env["base"] + path, json=payload, headers={"Authorization": "Bearer " + env["tokens"][role]} if role else {}, timeout=30, allow_redirects=False)
    body = response.json()
    read_dataset = env["ids"]["foreign" if key == "foreign" else "kb"]
    read_path = f"/api/v1/datasets/{read_dataset}/documents"
    readback = requests.get(env["base"] + read_path, params={"id": env["docs"][key]}, headers={"Authorization": "Bearer " + env["tokens"]["other" if key == "foreign" else "owner"]}, timeout=30)
    read_body = readback.json()
    env["parser_record"]["events"].append(
        {
            "path": path,
            "method": "PATCH",
            "headers": dict(response.headers),
            "raw": response.content,
            "full_stores_before": before_full,
            "full_stores_after": full_readback(env),
            "payload": payload,
            "role": role,
            "status": response.status_code,
            "body": body,
            "request_id": response.headers.get("X-Request-ID"),
            "authenticated_list_readback": {"path": read_path, "document_id": env["docs"][key], "status": readback.status_code, "body": read_body},
            "stores": snapshot(env),
        }
    )
    save(env["parser_record_path"], env["parser_record"])
    assert response.status_code == status, body
    if status == 200:
        assert body["code"] == 0 and body["data"]["id"] == env["docs"][key]
        assert set(body) == {"code", "message", "data"} and body["message"] == "success"
        assert readback.status_code == 200 and read_body["code"] == 0 and len(read_body["data"]["docs"]) == 1
        listed = read_body["data"]["docs"][0]
        for field in ["id", "dataset_id", "name", "chunk_method", "pipeline_id", "run", "status", "progress", "chunk_count", "token_count"]:
            assert listed[field] == body["data"][field], field
    else:
        assert type(body["code"]) is str and body["retcode"] != 0
        assert body["details"] == body["data"] and body["detail"] == body["message"] == body["retmsg"]
        assert body["request_id"] == response.headers["X-Request-ID"] and len(body["request_id"]) == 32
    return body
