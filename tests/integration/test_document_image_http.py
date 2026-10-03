"""Real authenticated HTTP reads and complete owned SQL/index/object/queue snapshots."""

import asyncio
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import urllib3
import uvicorn
from alembic import command
from minio import Minio, S3Error
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Base, Document, DocumentMetadata, File, File2Document, Task, Tenant, User, UserTenant, get_async_db, get_db
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from common import resources, settings
from core.utils.encrypted_storage import EncryptedStorageWrapper
from core.utils.redis_conn import REDIS_CONN
from tests.integration.conftest import _alembic_config, _pg_role_can_create_db, _pg_url
from tests.integration.test_document_image_read_service import _image, _json, _raw_object, _snapshot
from tests.integration.test_document_image_read_service import image_resources as image_resources


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, default=_json, indent=2))
    path.chmod(0o600)


@pytest.fixture(scope="module")
def image_http_database(_require_services: Any, tmp_path_factory: Any) -> Iterator[sa.Engine]:
    """Register the exact scratch database before creation, including failed runs."""
    evidence = Path(os.environ.get("MULTIRAG_C511_IMAGE_EVIDENCE_DIR", str(tmp_path_factory.mktemp("image_http"))))
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"multirag_test_{uuid4().hex[:12]}"
    manifest = {"database": name, "pid": os.getpid(), "created": False, "absent": False, "private_config": None, "containers": []}
    path = evidence / f"{name}.database.json"
    _save(path, manifest)
    from common.config_utils import CONFIGS

    admin_url = _pg_url(CONFIGS["postgresql"]["dbname"])
    assert _pg_role_can_create_db(admin_url), "HTTP verification requires isolated scratch database creation"
    admin = sa.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine = sa.create_engine(_pg_url(name))
    try:
        with admin.connect() as db:
            db.execute(sa.text(f'CREATE DATABASE "{name}"'))
        manifest["created"] = True
        _save(path, manifest)
        with engine.begin() as db:
            db.execute(sa.text("CREATE SCHEMA usr_ai"))
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            cfg = _alembic_config()
            cfg.attributes["connection"] = db
            command.stamp(cfg, "head")
        yield engine
    finally:
        engine.dispose()
        if manifest["created"]:
            with admin.connect() as db:
                db.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
                assert db.scalar(sa.text("SELECT count(*) FROM pg_database WHERE datname=:name"), {"name": name}) == 0
        manifest["absent"] = True
        _save(path, manifest)
        admin.dispose()


@pytest.fixture
def bootstrapped_engine(image_http_database: sa.Engine) -> sa.Engine:
    return image_http_database


@pytest.fixture
def image_http_api(image_resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    import api.apps as apps
    from api.db import db_models

    env, ids = image_resources, image_resources["ids"]
    env["manifest"].update(physical_objects=[], documents={}, runtime_ids={}, api_token_owners=[ids["owner"], ids["other"]], sql_ids={}, private_config=None, containers=[])
    manifest_lock = threading.RLock()

    def register() -> None:
        with manifest_lock:
            _save(env["manifest_path"], env["manifest"])

    original_put = env["storage"].put

    def tracked_put(namespace: str, key: str, data: bytes, *args: Any, **kwargs: Any) -> Any:
        # Register exact runtime ID before the server's first physical write.
        physical = env["adapter"]._resolve_bucket_and_path(namespace, key)[1]
        with manifest_lock:
            if physical not in env["manifest"]["physical_objects"]:
                env["manifest"]["physical_objects"].append(physical)
            register()
        return original_put(namespace, key, data, *args, **kwargs)

    monkeypatch.setattr(env["storage"], "put", tracked_put)
    ids["disabled"] = uuid4().hex
    env["tokens"] = {role: apps.manager.create_access_token(data={"sub": f"{ids[role]}@image.test"}) for role in ["owner", "other", "normal", "admin", "invite", "inactive", "outsider", "disabled"]}
    env["tokens"]["api"] = "image-http-" + uuid4().hex
    env["tokens"]["expired"] = apps.manager.create_access_token(data={"sub": f"{ids['owner']}@image.test"}, expires=timedelta(seconds=-1))
    env["tokens"]["malformed"] = "untrusted.jwt.signature"
    env["tokens"]["unknown"] = "image-unknown-" + uuid4().hex
    personal_users = [ids[role] for role in ["normal", "admin", "invite", "inactive", "outsider", "disabled"]]
    personal_memberships = {user_id: uuid4().hex for user_id in personal_users}
    env["manifest"].update(personal_tenant_ids=personal_users, personal_membership_ids=list(personal_memberships.values()))
    register()
    with Session(env["engine"]) as db:
        db.add(User(id=ids["disabled"], email=f"{ids['disabled']}@image.test", nickname="Disabled", password="unused", access_token="active", is_active=False))
        for user_id in personal_users:
            db.add(Tenant(id=user_id, name="HTTP personal scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(UserTenant(id=personal_memberships[user_id], user_id=user_id, tenant_id=user_id, role="owner", invited_by=user_id))
        for role in ["api", "disabled", "expired", "malformed"]:
            db.add(APIToken(tenant_id=ids["other"] if role == "api" else ids["owner"], token=env["tokens"][role], name="image-http"))
        db.commit()
    async_engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    sync_sessions = sessionmaker(env["engine"])

    async def scratch_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    def scratch_sync_db() -> Iterator[Session]:
        with sync_sessions() as db:
            yield db

    monkeypatch.setitem(apps.app.dependency_overrides, get_async_db, scratch_db)
    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_sync_db)
    monkeypatch.setattr(apps, "SessionLocal", sync_sessions)
    monkeypatch.setattr(db_models, "SessionLocal", sync_sessions)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    env.update(base=f"http://127.0.0.1:{port}", async_engine=async_engine, register=register)
    env["manifest"].update(listener={"host": "127.0.0.1", "port": port, "thread": thread.name, "command": "pytest in-process uvicorn"}, pid=os.getpid())
    register()
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield env
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        asyncio.run(async_engine.dispose())
        with Session(env["engine"]) as db:
            db.execute(sa.delete(APIToken).where(APIToken.tenant_id.in_([ids["owner"], ids["other"]]), APIToken.name == "image-http"))
            db.execute(sa.delete(UserTenant).where(UserTenant.id.in_(list(personal_memberships.values()))))
            db.execute(sa.delete(Tenant).where(Tenant.id.in_(personal_users)))
            db.commit()
            assert db.scalar(sa.select(sa.func.count()).select_from(APIToken).where(APIToken.tenant_id.in_([ids["owner"], ids["other"]]), APIToken.name == "image-http")) == 0
        assert not thread.is_alive()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        env["manifest"].update(listener_closed=True, api_tokens_absent=True)
        register()


def _setup(env: dict[str, Any]) -> dict[str, Any]:
    ids, storage, manifest = env["ids"], env["storage"], env["manifest"]
    cases = {
        "a-b 空间%/image.jpg": (_image("PNG"), "image/png"),
        "jpeg": (_image("JPEG"), "image/jpeg"),
        "gif.png": (_image("GIF"), "image/gif"),
        "webp": (_image("WEBP"), "image/webp"),
        "bmp": (_image("BMP"), "image/bmp"),
    }
    cases.update(
        {
            key: (data, None)
            for key, data in [
                ("empty", b""),
                ("html.png", b"<html>private</html>"),
                ("svg.png", b"<svg/>"),
                ("unknown", b"unknown"),
                ("magic.png", b"\x89PNG\r\n\x1a\n"),
                ("truncated.jpg", _image("JPEG")[:-20]),
                ("truncated.bmp", _image("BMP")[:-8]),
            ]
        }
    )
    documents = manifest["documents"]
    with Session(env["engine"]) as db:
        for key, (data, _) in cases.items():
            doc_id = documents[key] = uuid4().hex
            env["register"]()
            db.add(
                Document(
                    id=doc_id,
                    kb_id=ids["kb"],
                    created_by=ids["owner"],
                    parser_id="naive",
                    parser_config={},
                    type="visual",
                    name=key,
                    thumbnail=key,
                    status="0",
                    chunk_num=2,
                    token_num=11,
                    meta_fields={"kept": True},
                )
            )
            storage.put(ids["kb"], key, data)
        for key, thumbnail in [
            ("inline", "data:image/webp;base64,abc"),
            ("none", None),
            ("emptythumbnail", ""),
            ("missing", "missing-object"),
            ("legacy", "legacy.png"),
            ("encrypted", "encrypted.png"),
            ("broken", "broken.png"),
        ]:
            documents[key] = uuid4().hex
            env["register"]()
            db.add(Document(id=documents[key], kb_id=ids["kb"], created_by=ids["owner"], parser_id="naive", type="visual", name=key, thumbnail=thumbnail))
        for kb in ["sibling", "foreign", "inactivekb"]:
            documents[kb] = uuid4().hex
            env["register"]()
            db.add(Document(id=documents[kb], kb_id=ids[kb], created_by=ids["other"] if kb == "foreign" else ids["owner"], parser_id="naive", type="visual", thumbnail="other.png", status="0"))
            storage.put(ids[kb], "other.png", _image("PNG"))
        folder_id, file_id, link_id, task_id = (uuid4().hex for _ in range(4))
        manifest.update(file_ids=[folder_id, file_id], sql_ids={"task": task_id, "relation": link_id})
        env["register"]()
        db.add(File(id=folder_id, parent_id=folder_id, tenant_id=ids["owner"], created_by=ids["owner"], name="Images", type="folder"))
        db.add(File(id=file_id, parent_id=folder_id, tenant_id=ids["owner"], created_by=ids["owner"], name="Preserved", type="visual", location="legacy.png", size=len(_image("PNG"))))
        db.add(File2Document(id=link_id, file_id=file_id, document_id=documents[next(iter(cases))]))
        db.add(DocumentMetadata(id=documents[next(iter(cases))], tenant_id=ids["owner"], kb_id=ids["kb"], meta_fields={"kept": "metadata"}))
        db.add(Task(id=task_id, doc_id=documents[next(iter(cases))], task_type="Parse", progress=0.25, digest="kept"))
        db.commit()
    storage.put(ids["kb"], "legacy.png", _image("PNG"))
    storage.put(ids["kb"], "unregistered.png", _image("PNG"))
    encrypted = EncryptedStorageWrapper(env["adapter"], key="scratch-http-image-key")
    for key in ["encrypted.png", "broken.png"]:
        manifest["physical_objects"].append(env["adapter"]._resolve_bucket_and_path(ids["kb"], key)[1])
    env["register"]()
    encrypted.put(ids["kb"], "encrypted.png", _image("PNG"))
    env["adapter"].put(ids["kb"], "broken.png", b"RAGF" + b"0" * 19)
    chunks = {}
    parent, child = uuid4().hex, uuid4().hex
    for kb in ["kb", "sibling", "foreign", "inactivekb"]:
        items = (
            [(parent, "", "chunk-key.png", documents[next(iter(cases))]), (child, parent, "child.png", documents[next(iter(cases))]), (uuid4().hex, "", "foreign-doc.png", documents["foreign"])]
            if kb == "kb"
            else [(uuid4().hex, "", "other.png", documents[kb])]
        )
        rows = [
            {
                "id": chunk,
                "pk": chunk,
                "doc_id": doc,
                "kb_id": ids[kb],
                "img_id": f"{ids[kb]}-{key}",
                "content_with_weight": "Preserved image registration",
                "available_int": 0,
                "mom_id": mother,
                "vector": [0.1] * 768,
                "q_768_vec": [0.1] * 768,
                "create_timestamp_flt": 1234.5,
            }
            for chunk, mother, key, doc in items
        ]
        manifest.setdefault("chunk_ids", {})[kb] = [row["id"] for row in rows]
        env["register"]()
        for _, _, key, _ in items:
            storage.put(ids[kb], key, _image("PNG"))
        assert settings.docStoreConn.insert(rows, env["collections"][kb], ids[kb]) == []
        chunks[kb] = rows
    assert REDIS_CONN.queue_product(env["queue"], {"id": task_id, "doc_id": documents[next(iter(cases))], "kept": True})
    runtimes = {}
    for role, token_role in [("owner", "owner"), ("other", "api")]:
        body = requests.post(
            env["base"] + "/api/v1/documents/upload", headers={"Authorization": "Bearer " + env["tokens"][token_role]}, files={"file": ("runtime.png", _image("PNG"), "image/png")}, timeout=30
        ).json()
        assert body["code"] == 0
        descriptor = runtimes[role] = body["data"]
        assert descriptor["created_by"] == ids[role] and descriptor["size"] == len(_image("PNG"))
        manifest["runtime_ids"][role] = descriptor["id"]
        env["register"]()
        physical = env["adapter"]._resolve_bucket_and_path(f"{ids[role]}-downloads", descriptor["id"])[1]
        assert _raw_object(env["client"], env["bucket"], physical) == _image("PNG")
        assert json.loads(_raw_object(env["client"], env["bucket"], physical + ".upload.json")) == descriptor
    for kind in ["tampered", "size", "empty", "missing-sidecar", "bad-json"]:
        file_id = uuid4().hex
        manifest["runtime_ids"][kind] = file_id
        env["register"]()
        data = b"" if kind == "empty" else _image("PNG")
        descriptor = {**runtimes["owner"], "id": file_id, "created_by": ids["other"] if kind == "tampered" else ids["owner"], "size": len(data) + (1 if kind == "size" else 0)}
        storage.put(f"{ids['owner']}-downloads", file_id, data)
        if kind != "missing-sidecar":
            storage.put(f"{ids['owner']}-downloads", FileService._runtime_descriptor_key(file_id), b"invalid-json" if kind == "bad-json" else json.dumps(descriptor).encode())
        runtimes[kind] = descriptor
    return {"cases": cases, "chunks": chunks, "runtimes": runtimes, "encrypted": encrypted}


def _retirement_snapshot(env: dict[str, Any]) -> dict[str, Any]:
    """Read every physical scratch SQL column/xmin and all owned native stores."""
    from minio import Minio
    from pymilvus import MilvusClient
    from redis import Redis

    from common.config_utils import CONFIGS

    with env["engine"].connect() as db:
        tables = db.execute(sa.text("SELECT schemaname, tablename FROM pg_tables WHERE schemaname NOT IN ('pg_catalog', 'information_schema') ORDER BY schemaname, tablename")).all()
        physical = {}
        inspector = sa.inspect(db)
        for schema, table in tables:
            quoted = db.dialect.identifier_preparer.quote_schema(schema) + "." + db.dialect.identifier_preparer.quote(table)
            rows = db.execute(sa.text(f"SELECT t.*, xmin::text AS _xmin FROM {quoted} t ORDER BY to_jsonb(t)::text")).mappings()
            physical[quoted] = {
                "columns": [{**column, "type": str(column["type"])} for column in inspector.get_columns(table, schema=schema)],
                "primary_key": inspector.get_pk_constraint(table, schema=schema),
                "foreign_keys": inspector.get_foreign_keys(table, schema=schema),
                "indexes": inspector.get_indexes(table, schema=schema),
                "rows": [dict(row) for row in rows],
            }
    cfg = CONFIGS["milvus"]
    index = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    try:
        indexes = {
            name: {
                "schema": index.describe_collection(collection),
                "count": index.query(collection, filter='pk != ""', output_fields=["count(*)"], consistency_level="Strong"),
                "stats": index.get_collection_stats(collection),
                "rows": sorted(index.query(collection, filter='pk != ""', output_fields=["*", "vector", "q_768_vec"], consistency_level="Strong"), key=lambda row: row["pk"]),
            }
            for name, collection in env["collections"].items()
        }
    finally:
        index.close()
    cfg = CONFIGS["minio"]
    storage = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    objects = {obj.object_name: _raw_object(storage, env["bucket"], obj.object_name) for obj in storage.list_objects(env["bucket"], recursive=True)}
    kwargs = {**REDIS_CONN.REDIS.connection_pool.connection_kwargs, "decode_responses": False}
    redis = Redis(**kwargs)
    try:
        key = env["queue"]
        queue: dict[str, Any] = {
            "type": redis.type(key),
            "dump": redis.dump(key),
            "pttl": redis.pttl(key),
            "entries": redis.xrange(key),
            "stream": redis.xinfo_stream(key),
            "groups": redis.xinfo_groups(key),
        }
        queue["consumers"] = {group["name"].decode(): redis.xinfo_consumers(key, group["name"]) for group in queue["groups"]}
        queue["pending"] = {group["name"].decode(): redis.xpending_range(key, group["name"], "-", "+", 100) for group in queue["groups"]}
    finally:
        redis.close()

    # JSON object keys must be strings; raw byte values remain complete base64.
    def json_keys(value: Any) -> Any:
        if isinstance(value, dict):
            return {(key.decode() if isinstance(key, bytes) else key): json_keys(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_keys(item) for item in value]
        return value

    return json_keys({"sql": physical, "index": indexes, "objects": objects, "redis": queue})


def _assert_retirement_unchanged(before: dict[str, Any], after: dict[str, Any], elapsed: float) -> None:
    """Only Redis's documented relative ages may increase with elapsed time."""
    import copy

    left, right = copy.deepcopy(before), copy.deepcopy(after)
    for category, fields in [("consumers", ["idle", "inactive"]), ("pending", ["time_since_delivered"])]:
        for group, rows in left["redis"][category].items():
            for a, b in zip(rows, right["redis"][category][group], strict=True):
                for field in fields:
                    if field in a:
                        assert 0 <= b[field] - a[field] <= elapsed * 1000 + 1000, (category, field, a[field], b[field], elapsed)
                        b[field] = a[field]
    assert left == right


def _retired_binary_matrix(env: dict[str, Any]) -> None:
    """Actual listener absence, with private-call guards and fresh native readback."""
    import sys
    from urllib.parse import unquote, urlsplit

    from api.db.services import document_image_service

    group, consumer = "oldbinary-retirement", "oldbinary-reader"
    env["manifest"]["retirement_stream_group"] = {"key": env["queue"], "group": group, "consumer": consumer}
    env["register"]()
    REDIS_CONN.REDIS.xgroup_create(env["queue"], group, id="0")
    REDIS_CONN.REDIS.xreadgroup(group, consumer, {env["queue"]: ">"}, count=1)
    docs, kb = env["manifest"]["documents"], env["ids"]["kb"]
    cases = {
        "credentials": [
            ("GET", f"/v1/document/image/{kb}-legacy.png", role, {})
            for role in ["owner", "admin", "normal", "invite", "inactive", "outsider", "other", "api", "disabled", "expired", "malformed", "unknown", None]
        ],
        "paths_and_payloads": [
            ("GET", path, "owner", {"params": {"owner": env["ids"]["other"], "doc_ids": docs["legacy"]}, "json": {"image_id": "private", "created_by": env["ids"]["other"]}})
            for path in [
                f"/v1/document/image/{kb}-a-b%20%E7%A9%BA%E9%97%B4%25%2Fimage.jpg",
                f"/v1/document/image/{kb}-chunk-key.png",
                "/v1/document/image/nohyphen",
                "/v1/document/image/",
                "/v1/document/image",
                "/v1/document/image//",
                "/v1/document/image/%2F",
                "/v1/document/image/%25",
                "/v1/document/image/%ZZ",
                "/v1/document/image/汉字",
                f"/v1/document/image/{kb}-legacy.png/",
            ]
        ],
        "methods": [(method, f"/v1/document/image/{kb}-legacy.png", "owner", {"json": {"image_id": "private"}}) for method in ["POST", "HEAD", "PATCH", "DELETE"]],
    }
    record: dict[str, Any] = {"boundary": "real JWT/API tokens; actual route removal; native readers outside guarded request boundaries; HEAD has empty wire body", "groups": {}, "private_calls": []}
    path = env["evidence"] / f"{kb}.oldbinary-retirement.json"
    _save(path, record)
    for label, requests_in_group in cases.items():
        observation: dict[str, Any] = {"before": _retirement_snapshot(env), "requests": []}
        record["groups"][label] = observation
        _save(path, record)
        start = time.monotonic()

        def forbid(*args: Any, **kwargs: Any) -> Any:
            record["private_calls"].append("private read/write")
            _save(path, record)
            raise AssertionError("retired binary route reached a private dependency")

        def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            record["private_calls"].append({"sql": statement})
            _save(path, record)
            raise AssertionError("retired binary route reached business SQL")

        with pytest.MonkeyPatch.context() as guard:
            targets = (
                [(env["storage"], name) for name in ["get", "get_bytes", "put", "rm"]]
                + [(env["adapter"], name) for name in ["get", "get_bytes", "put", "rm"]]
                + [(env["client"], name) for name in ["get_object", "put_object", "remove_object"]]
            )
            targets += [(settings.docStoreConn, name) for name in ["search", "insert", "update", "delete"]]
            targets += [(DocumentService, "get_thumbnails"), (REDIS_CONN, "queue_product")]
            targets += [(FileService, name) for name in ["parse", "parse_docs", "upload_info", "upload_infos", "resolve_runtime_uploads"]]
            targets += [(tempfile, name) for name in ["NamedTemporaryFile", "mkstemp", "mkdtemp"]]
            for module in [document_image_service, sys.modules["api.apps.restful_apis.document"]]:
                targets += [(module, name) for name in ["list_thumbnails", "read_dataset_image", "read_runtime_image"]]
            for target, attr in targets:
                guard.setattr(target, attr, forbid)
            engines = [env["engine"], env["async_engine"].sync_engine]
            for engine in engines:
                sa.event.listen(engine, "before_cursor_execute", sql_guard)
            try:
                for method, url, role, payload in requests_in_group:
                    headers = {"Authorization": "Bearer " + env["tokens"][role]} if role else {}
                    response = requests.request(method, env["base"] + url, headers=headers, allow_redirects=False, timeout=30, **payload)
                    actual_path = unquote(urlsplit(response.request.url).path)
                    item = {
                        "method": method,
                        "path": url,
                        "request_url": response.request.url,
                        "actual_path": actual_path,
                        "principal": role,
                        "payload": payload,
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "body": response.content,
                    }
                    observation["requests"].append(item)
                    _save(path, record)
                    assert response.status_code == 404 and "location" not in response.headers, item
                    assert response.headers["content-type"] == "application/json"
                    if method == "HEAD":
                        assert response.content == b""
                    else:
                        assert response.json() == {"code": 404, "message": "Not Found: " + actual_path, "data": None, "error": "Not Found"}, item
            finally:
                for engine in engines:
                    sa.event.remove(engine, "before_cursor_execute", sql_guard)
        observation["after"] = _retirement_snapshot(env)
        observation["elapsed_monotonic_seconds"] = time.monotonic() - start
        _save(path, record)
        _assert_retirement_unchanged(observation["before"], observation["after"], observation["elapsed_monotonic_seconds"])
        observation["unchanged_except_bounded_redis_relative_age"] = True
        _save(path, record)
    assert not record["private_calls"]
    record["verified_request_count"] = sum(len(value["requests"]) for value in record["groups"].values())
    _save(path, record)


def test_authenticated_image_http_full_storage_no_writes(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = image_http_api, image_http_api["ids"]
    setup = _setup(env)
    _retired_binary_matrix(env)
    record: dict[str, Any] = {
        "chunks": setup["chunks"],
        "runtimes": setup["runtimes"],
        "calls": [],
        "auth": "actual JWT and API token; no identity override",
        "fault_boundary": "denied/read/close/index controlled; closed MinIO loopback and ciphertext real",
    }
    missing_key = env["adapter"]._resolve_bucket_and_path(ids["kb"], "missing-object")[1]
    with pytest.raises(S3Error) as missing:
        env["client"].stat_object(env["bucket"], missing_key)
    assert missing.value.code == "NoSuchKey"
    record["physical_missing"] = {"bucket": env["bucket"], "key": missing_key, "storage_code": missing.value.code, "http_404_semantics": "availability mask, not physical proof"}
    path = env["evidence"] / f"{ids['kb']}.http-readback.json"
    before = record["before"] = _snapshot(env)
    _save(path, record)
    guards: list[str] = []
    sql_reads: list[str] = []

    def forbid(*args: Any, **kwargs: Any) -> Any:
        guards.append("write")
        raise AssertionError("image HTTP read attempted a write")

    def sql_guard(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        sql_reads.append(statement.lstrip().split()[0].upper())
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
            guards.append("SQL write")
            raise AssertionError("image HTTP attempted SQL DML")

    def get(
        url: str, role: str | None = "owner", params: Any = None, status: int = 200, code: int | None = None, binary: bytes | None = None, mime: str | None = None, feature: bool = True
    ) -> requests.Response:
        headers = {"Authorization": "Bearer " + env["tokens"][role]} if role else {}
        response = requests.get(env["base"] + url, headers=headers, params=params, timeout=40, allow_redirects=False)
        item = {"path": url, "principal": role, "status": response.status_code, "headers": dict(response.headers), "body": response.content}
        record["calls"].append(item)
        _save(path, record)
        assert response.status_code == status, item
        if feature:
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "content-disposition" not in response.headers
        assert int(response.headers["content-length"]) == len(response.content)
        if binary is not None:
            assert response.content == binary and response.headers["content-type"] == mime
        if code is not None:
            body = response.json()
            assert body["code"] == code and response.headers["content-type"] == "application/json"
            if status != 200 and feature:
                messages = {
                    400: "Invalid image request.",
                    401: "Unauthorized",
                    403: "Unauthorized",
                    404: "Image is unavailable.",
                    415: "Image data is invalid.",
                    422: "Invalid image request.",
                    500: "Image could not be read.",
                }
                assert body == {"code": code, "message": messages[status], "data": None}
        return response

    def image(key: str, role: str = "owner", kb: str = "kb", **kwargs: Any) -> requests.Response:
        return get(f"/api/v1/documents/images/{ids[kb]}-{quote(key, safe='')}", role, **kwargs)

    def deny_read(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rejection reached private object/index service")

    with monkeypatch.context() as guard:
        for target, attr in [
            (FileService, "parse"),
            (FileService, "parse_docs"),
            (FileService, "upload_info"),
            (FileService, "upload_infos"),
            (tempfile, "NamedTemporaryFile"),
            (tempfile, "mkstemp"),
            (tempfile, "mkdtemp"),
        ]:
            guard.setattr(target, attr, forbid)
        for target, attr in [
            (env["storage"], "put"),
            (env["storage"], "rm"),
            (env["adapter"], "put"),
            (settings.docStoreConn, "insert"),
            (settings.docStoreConn, "update"),
            (settings.docStoreConn, "delete"),
            (REDIS_CONN, "queue_product"),
        ]:
            guard.setattr(target, attr, forbid)
        sa.event.listen(env["async_engine"].sync_engine, "before_cursor_execute", sql_guard)
        sa.event.listen(env["engine"], "before_cursor_execute", sql_guard)
        try:
            with monkeypatch.context() as denied:
                denied.setattr(env["storage"], "get_bytes", deny_read)
                denied.setattr(settings.docStoreConn, "search", deny_read)
                for role in [None, "unknown", "expired", "malformed", "disabled"]:
                    for url in [
                        "/api/v1/thumbnails?doc_ids=" + env["manifest"]["documents"]["legacy"],
                        f"/api/v1/documents/images/{ids['kb']}-legacy.png",
                        f"/api/v1/documents/runtime/{setup['runtimes']['owner']['id']}/image",
                    ]:
                        get(url, role, status=401, code=401)
                with monkeypatch.context() as sdk_disabled:
                    sdk_disabled.setenv("DISABLE_SDK", "1")
                    image("other.png", "api", "foreign", status=401, code=401)
                for role in ["invite", "inactive", "outsider", "other"]:
                    image("legacy.png", role, status=404, code=102)
                for kb in ["foreign", "inactivekb"]:
                    image("other.png", kb=kb, status=404, code=102)
                denied.setattr(DocumentService, "get_thumbnails", deny_read)
                for role in [None, "owner", "malformed"]:
                    reads_before = len(sql_reads)
                    get("/v1/document/thumbnails", role, status=404, code=404, feature=False)
                    assert len(sql_reads) == reads_before
            docs = env["manifest"]["documents"]
            first = next(iter(setup["cases"]))
            params = [("doc_ids", docs[key]) for key in ["none", first, first, "foreign", "inactivekb", "inline", "emptythumbnail"]] + [("doc_ids", uuid4().hex)]
            mapping = get("/api/v1/thumbnails", params=params, code=0).json()
            expected = {docs["none"]: None, docs[first]: f"/api/v1/documents/images/{ids['kb']}-{quote(first, safe='')}", docs["inline"]: "data:image/webp;base64,abc", docs["emptythumbnail"]: ""}
            assert mapping == {"code": 0, "message": "success", "data": expected} and list(mapping["data"]) == list(expected)
            for role in ["invite", "inactive", "outsider", "other"]:
                assert get("/api/v1/thumbnails", role, params={"doc_ids": docs["legacy"]}, code=0).json()["data"] == {}
            get("/api/v1/thumbnails", status=422, code=101)
            get("/api/v1/thumbnails", params={"doc_ids": ""}, status=400, code=101)
            assert get("/api/v1/thumbnails", params=[("doc_ids", docs["legacy"])] * 100, code=0).json()["data"] == {docs["legacy"]: f"/api/v1/documents/images/{ids['kb']}-legacy.png"}
            get("/api/v1/thumbnails", params=[("doc_ids", docs["legacy"])] * 101, status=400, code=101)
            for role in ["owner", "normal", "admin"]:
                for key, (data, mime) in setup["cases"].items():
                    image(key, role, binary=data if mime else None, mime=mime, status=200 if mime else 415, code=None if mime else 102)
            image("other.png", "api", "foreign", binary=_image("PNG"), mime="image/png")
            for key in ["chunk-key.png", "child.png"]:
                image(key, binary=_image("PNG"), mime="image/png")
            for key in ["foreign-doc.png", "unregistered.png", "missing-object", "unknown-key"]:
                image(key, status=404, code=102)
            image("other.png", kb="sibling", binary=_image("PNG"), mime="image/png")
            image("legacy.png", kb="sibling", status=404, code=102)
            for bad in ["missinghyphen", "-key", ids["kb"] + "-"]:
                get("/api/v1/documents/images/" + bad, status=400, code=101)
            for method, url, kwargs in [
                ("get", f"/api/v1/datasets/{ids['kb']}/documents", {}),
                ("get", "/v1/document/list", {"params": {"kb_id": ids["kb"]}}),
                ("post", "/v1/document/list", {"params": {"id": ids["kb"]}, "json": {}}),
            ]:
                response = requests.request(method, env["base"] + url, headers={"Authorization": "Bearer " + env["tokens"]["owner"]}, timeout=30, **kwargs)
                assert response.status_code == 200 and response.json()["code"] == 0, response.content
                listed = response.json()["data"]["docs"]
                thumb = next(item["thumbnail"] for item in listed if item["id"] == docs[first])
                assert thumb == expected[docs[first]]
                record["calls"].append({"producer": url, "body": response.content, "thumbnail": thumb})
                get(thumb, binary=_image("PNG"), mime="image/png")
            for role, token_role in [("owner", "owner"), ("other", "api")]:
                get(f"/api/v1/documents/runtime/{setup['runtimes'][role]['id']}/image", token_role, binary=_image("PNG"), mime="image/png")
            other_url = f"/api/v1/documents/runtime/{setup['runtimes']['other']['id']}/image"
            get(other_url, params={"owner": ids["other"], "created_by": ids["other"], "preview_url": "http://untrusted.invalid"}, status=404, code=102)
            for kind in ["tampered", "size", "empty", "missing-sidecar", "bad-json"]:
                get(f"/api/v1/documents/runtime/{setup['runtimes'][kind]['id']}/image", status=415 if kind in {"size", "empty"} else 404, code=102)
            for bad in ["bad", "A" * 32, "a" * 31]:
                get(f"/api/v1/documents/runtime/{bad}/image", status=400, code=101)
            get(f"/api/v1/documents/runtime/{uuid4().hex}/image", status=404, code=102)
            with monkeypatch.context() as fault:
                fault.setattr(env["adapter"], "conn", Minio("127.0.0.1:1", access_key="scratch", secret_key="scratch-secret", secure=False, http_client=urllib3.PoolManager(retries=0, timeout=1)))
                image("legacy.png", status=500, code=500)
            for kind in ["denied", "read", "close"]:
                with monkeypatch.context() as fault:
                    original_get = env["client"].get_object

                    def fault_get(*args: Any, **kwargs: Any) -> Any:
                        if kind == "denied":
                            raise S3Error("AccessDenied", "private object", "private", "request", "host", None)
                        raw = original_get(*args, **kwargs)

                        def bad_read() -> bytes:
                            raise OSError("private read failure")

                        def bad_close() -> None:
                            raw.close()
                            raise OSError("private close failure")

                        return SimpleNamespace(read=bad_read if kind == "read" else raw.read, close=bad_close if kind == "close" else raw.close, release_conn=raw.release_conn)

                    fault.setattr(env["client"], "get_object", fault_get)
                    image("legacy.png", status=500, code=500)
            with monkeypatch.context() as fault:
                fault.setitem(resources._state, "storage", setup["encrypted"])
                image("encrypted.png", binary=_image("PNG"), mime="image/png")
                image("broken.png", status=500, code=500)
            with monkeypatch.context() as fault:

                def unexpected(*args: Any, **kwargs: Any) -> Any:
                    raise ValueError("private index transport")

                fault.setattr(settings.docStoreConn, "search", unexpected)
                image("chunk-key.png", status=500, code=500)
            paths = get("/openapi.json", feature=False).json()["paths"]
            assert "/v1/document/thumbnails" not in paths and not any(path.startswith("/v1/document/image") for path in paths)
            assert "/v1/document/run" not in paths and "/v1/document/upload_and_parse" not in paths and "/v1/document/change_status" not in paths
            assert "/v1/document/change_parser" in paths
            for canonical in ["/api/v1/thumbnails", "/api/v1/documents/images/{image_id}", "/api/v1/documents/runtime/{file_id}/image", "/api/v1/documents/ingest"]:
                assert canonical in paths
            record["openapi"] = paths
        finally:
            sa.event.remove(env["async_engine"].sync_engine, "before_cursor_execute", sql_guard)
            sa.event.remove(env["engine"], "before_cursor_execute", sql_guard)
    after = record["after"] = _snapshot(env)
    record.update(unchanged=before == after, write_attempts=guards)
    _save(path, record)
    assert not guards and record["unchanged"]
    runtime_ids = {item["id"] for item in setup["runtimes"].values()}
    assert not runtime_ids.intersection(row["id"] for row in before["sql"][Document.__tablename__])
    assert all(not runtime_ids.intersection(row["doc_id"] for row in index["rows"]) for index in before["index"].values())
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    path.with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
