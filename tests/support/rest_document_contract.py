"""Per-case owned HTTP/SQL/object resources with an append-only identity journal."""

import asyncio
import copy
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import uvicorn
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db import db_models
from api.db.db_models import APIToken, Base, Knowledgebase, Tenant, User, UserTenant, get_async_db
from common import resources, settings
from common.config_utils import CONFIGS
from tests.support.database import _pg_url
from tests.support.resource_journal import ResourceJournal
from tests.support.runtime_upload import read_object
from tests.support.uvicorn_runtime import run_server


@pytest.fixture
def rest_documents(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, postgres_service: None) -> Iterator[dict[str, Any]]:
    journal = ResourceJournal(request.node.nodeid)
    database = "multirag_test_" + journal.run_id[:16]
    bucket = "rest-doc-" + journal.run_id
    owners = [uuid4().hex for _ in range(2)]
    datasets = [uuid4().hex for _ in range(2)]
    name = "rest_contract_" + journal.run_id
    queue = "rest-doc:" + journal.run_id + ":queue"
    port = 20000 + int(journal.run_id[:4], 16) % 30000
    journal.event("reserve", database=database, bucket=bucket, owners=owners, datasets=datasets, dataset_name=name, queue=queue, port=port)
    admin = sa.create_engine(_pg_url(CONFIGS["postgresql"]["dbname"]), isolation_level="AUTOCOMMIT")
    with ExitStack() as cleanup:
        cleanup.callback(admin.dispose)

        def drop_database() -> None:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            with admin.connect() as conn:
                remaining = conn.scalar(sa.text("SELECT count(*) FROM pg_database WHERE datname=:name"), {"name": database})
            journal.event("database-cleanup", database=database, remaining=remaining)
            assert remaining == 0

        cleanup.callback(drop_database)
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{database}"'))
        engine = sa.create_engine(_pg_url(database))
        cleanup.callback(engine.dispose)
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE SCHEMA usr_ai"))

        def register_insert(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            if context.isinsert:
                identifiers = [{key: value for key, value in row.items() if key == "id" or key.endswith("_id")} for row in context.compiled_parameters]
                journal.event("reserve-sql-insert", database=database, table=context.compiled.statement.table.name, identifiers=identifiers)

        sa.event.listen(engine, "before_cursor_execute", register_insert)
        cleanup.callback(sa.event.remove, engine, "before_cursor_execute", register_insert)
        Base.metadata.create_all(engine)
        # Real service resources are borrowed; only our bucket/collection/queue are owned.
        from common.bootstrap import ensure_initialized

        if not all(key in resources._state for key in ("storage", "doc_store", "retriever", "kg_retriever")):
            ensure_initialized(force=True)
        from api import apps as app_module
        from api.apps import app, manager
        from core.nlp import search
        from core.utils.redis_conn import REDIS_CONN

        collections = [search.index_name_one(owner, name) for owner in owners]
        journal.event("reserve-indexes", collections=collections)
        store = settings.docStoreConn
        assert store.db_type() == "milvus"
        assert all(not store.has_collection(collection) for collection in collections)
        assert not REDIS_CONN.REDIS.exists(queue)

        def drop_indexes_queue() -> None:
            for collection in collections:
                if store.has_collection(collection):
                    store.delete_idx(collection, "")
            REDIS_CONN.REDIS.delete(queue)
            remaining = {collection: store.has_collection(collection) for collection in collections}
            queue_exists = bool(REDIS_CONN.REDIS.exists(queue))
            journal.event("index-queue-cleanup", collections=remaining, queue=queue, queue_exists=queue_exists)
            assert not any(remaining.values()) and not queue_exists

        cleanup.callback(drop_indexes_queue)
        monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
        cfg = CONFIGS["minio"]
        storage = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})

        def drop_bucket() -> None:
            if storage.bucket_exists(bucket):
                keys = [item.object_name for item in storage.list_objects(bucket, recursive=True)]
                journal.event("object-cleanup-start", bucket=bucket, keys=keys)
                for key in keys:
                    storage.remove_object(bucket, key)
                assert not list(storage.list_objects(bucket, recursive=True))
                storage.remove_bucket(bucket)
            absent = not storage.bucket_exists(bucket)
            journal.event("bucket-cleanup", bucket=bucket, absent=absent)
            assert absent

        cleanup.callback(drop_bucket)
        storage.make_bucket(bucket)
        adapter = copy.copy(settings.STORAGE_IMPL)
        adapter.conn, adapter.bucket, adapter.prefix_path = storage, bucket, ""
        original_put = adapter.put

        def put(namespace: str, key: str, binary: bytes) -> Any:
            journal.event("reserve-object", bucket=bucket, key=f"{namespace}/{key}")
            return original_put(namespace, key, binary)

        monkeypatch.setattr(adapter, "put", put)
        monkeypatch.setitem(resources._state, "storage", adapter)

        def before_flush(db: Session, context: Any, instances: Any) -> None:
            rows = [{"table": row.__tablename__, "id": getattr(row, "id", None)} for row in db.new]
            if rows:
                journal.event("reserve-sql", database=database, rows=rows)

        sa.event.listen(Session, "before_flush", before_flush)
        cleanup.callback(sa.event.remove, Session, "before_flush", before_flush)
        credentials = []
        with Session(engine) as db:
            for owner, dataset in zip(owners, datasets, strict=True):
                token = "rest-doc-secret-" + uuid4().hex
                credentials.append(token)
                db.add(User(id=owner, email=owner + "@rest-doc.test", nickname="REST contract", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name="REST contract", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, tenant_id=owner, user_id=owner, role="owner", invited_by=owner))
                db.add(APIToken(tenant_id=owner, token=token, name="rest-doc"))
                db.add(Knowledgebase(id=dataset, tenant_id=owner, created_by=owner, name=name, embd_id="scratch", parser_id="naive", parser_config={}))
            db.commit()
        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        async def scratch_db() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        @contextmanager
        def sync_db() -> Iterator[Session]:
            with Session(engine) as db:
                yield db

        from api.apps.services import document_api_service

        monkeypatch.setitem(app.dependency_overrides, get_async_db, scratch_db)
        monkeypatch.setattr(document_api_service, "db_connection", sync_db)
        monkeypatch.setattr(db_models, "SessionLocal", lambda: Session(engine))
        # LoginManager's legacy user loader holds its own imported factory alias.
        monkeypatch.setattr(app_module, "SessionLocal", lambda: Session(engine))
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", port))
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True)

        def stop() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            with socket.socket() as probe:
                closed = probe.connect_ex(("127.0.0.1", port)) != 0
            journal.event("listener-cleanup", port=port, thread_stopped=not thread.is_alive(), closed=closed)
            assert closed and not thread.is_alive()

        cleanup.callback(stop)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        env = {
            "base": f"http://127.0.0.1:{port}",
            "engine": engine,
            "datasets": datasets,
            "owners": owners,
            "keys": credentials,
            "jwt": [manager.create_access_token(data={"sub": owner + "@rest-doc.test"}) for owner in owners],
            "storage": storage,
            "bucket": bucket,
            "journal": journal,
            "queue": queue,
            "collections": collections,
        }
        try:
            yield env
        finally:
            journal.event("case-final-state", **snapshot(env))


def snapshot(env: dict[str, Any]) -> dict[str, Any]:
    """Read every SQL column and actual object bytes independently of the API."""
    with env["engine"].connect() as db:
        rows = {table.name: [dict(row) for row in db.execute(sa.select(table)).mappings()] for table in Base.metadata.sorted_tables if table.name != APIToken.__tablename__}
    return {"sql": rows, "objects": {item.object_name: read_object(env["storage"], env["bucket"], item.object_name) for item in env["storage"].list_objects(env["bucket"], recursive=True)}}


def call(env: dict[str, Any], method: str, suffix: str = "", *, credential: str | None = "owner", timeout: float = 30, **kwargs: Any) -> requests.Response:
    token = env["keys"][0] if credential == "owner" else credential
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return requests.request(method, env["base"] + "/api/v1" + suffix, headers=headers, timeout=timeout, **kwargs)
