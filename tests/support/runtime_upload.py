"""Owned HTTP, database and object-store fixtures shared by integration suites."""

import asyncio
import copy
import errno
import hashlib
import json
import os
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Tenant, User, UserTenant, get_async_db
from common import resources, settings
from common.config_utils import CONFIGS
from tests.support.event_loop import UVICORN_LOOP
from tests.support.resource_journal import ResourceJournal
from tests.support.uvicorn_runtime import run_server


@pytest.fixture
def runtime_upload_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    # A previous database-only test may have imported api.apps with only a signer.
    # Explicitly initialize this fixture's real storage/search dependencies even
    # when that application module is already cached.
    if not all(name in resources._state for name in ("storage", "doc_store", "retriever", "kg_retriever")):
        from common.bootstrap import ensure_initialized

        ensure_initialized(force=True)
    from api.apps import app, manager

    owner_ids = [uuid4().hex, uuid4().hex]
    membership_ids = [uuid4().hex, uuid4().hex]
    api_key = f"upload-test-{uuid4().hex}"
    bucket = f"upload-test-{uuid4().hex}"
    record: dict[str, Any] = {"database": bootstrapped_engine.url.database, "owners": owner_ids, "api_token_owner": owner_ids[1], "api_token_name": "upload-test", "bucket": bucket}
    evidence = Path(os.environ.get("MULTIRAG_343BDA_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    record_path = evidence / (owner_ids[0] + ".json")
    journal = ResourceJournal("runtime-upload-api")
    sql_url = bootstrapped_engine.url
    object_config = CONFIGS["minio"]
    object_endpoint = {"host": object_config["host"], "secure": str(object_config.get("secure", False)).lower() in {"true", "1", "yes"}}
    seed = {
        "owners": owner_ids,
        "memberships": membership_ids,
        "api_token_owner": owner_ids[1],
        "api_token_name": "upload-test",
        "api_token_sha256": hashlib.sha256(api_key.encode()).hexdigest(),
        "bucket": bucket,
    }
    journal.event(
        "reserve-runtime",
        database=record["database"],
        database_endpoint={"host": sql_url.host, "port": sql_url.port, "role": sql_url.username},
        object_endpoint=object_endpoint,
        fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        seed_sha256=hashlib.sha256(json.dumps(seed, sort_keys=True).encode()).hexdigest(),
        owners=owner_ids,
        memberships=membership_ids,
        api_token_owner=owner_ids[1],
        api_token_name="upload-test",
        bucket=bucket,
        record_path=str(record_path),
    )

    def save_record() -> None:
        record_path.write_text(json.dumps(record))
        record_path.chmod(0o600)

    def remove_rows() -> None:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(APIToken).where(APIToken.token == api_key))
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id.in_(owner_ids)))
            db.execute(sa.delete(Tenant).where(Tenant.id.in_(owner_ids)))
            db.execute(sa.delete(User).where(User.id.in_(owner_ids)))
            db.commit()
        with bootstrapped_engine.connect() as db:
            remaining = {
                model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(owner_ids)))
                for model, column in [(APIToken, APIToken.tenant_id), (UserTenant, UserTenant.user_id), (Tenant, Tenant.id), (User, User.id)]
            }
        record["base_remaining"] = remaining
        journal.event("sql-cleanup-readback", remaining=remaining)
        assert not any(remaining.values()), remaining

    save_record()
    with ExitStack() as cleanup:
        cleanup.callback(save_record)
        cleanup.callback(remove_rows)
        with Session(bootstrapped_engine) as db:
            for user_id, membership_id in zip(owner_ids, membership_ids, strict=True):
                db.add(User(id=user_id, email=f"{user_id}@upload.test", nickname="Upload test", password="unused", access_token="active"))
                db.add(Tenant(id=user_id, name="Upload scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=membership_id, user_id=user_id, tenant_id=user_id, role="owner", invited_by=user_id))
            db.add(APIToken(tenant_id=owner_ids[1], token=api_key, name="upload-test"))
            db.commit()

        cfg = object_config
        storage_client = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})

        def remove_bucket() -> None:
            if storage_client.bucket_exists(bucket):
                objects = list(storage_client.list_objects(bucket, recursive=True))
                record["objects"] = [item.object_name for item in objects]
                for item in objects:
                    storage_client.remove_object(bucket, item.object_name)
                assert not list(storage_client.list_objects(bucket, recursive=True))
                storage_client.remove_bucket(bucket)
            assert not storage_client.bucket_exists(bucket)
            record["bucket_removed"] = True
            journal.event("object-cleanup-readback", bucket=bucket, absent=True)

        # Register first: an acknowledged server-side create followed by a
        # transport error must still clean the uniquely owned bucket.
        cleanup.callback(remove_bucket)
        storage_client.make_bucket(bucket)
        storage = copy.copy(settings.STORAGE_IMPL)
        storage.conn, storage.bucket, storage.prefix_path = storage_client, bucket, ""
        monkeypatch.setitem(resources._state, "storage", storage)
        async_engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)

        def dispose_engine() -> None:
            asyncio.run(async_engine.dispose())

        cleanup.callback(dispose_engine)
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        async def scratch_db() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        monkeypatch.setitem(app.dependency_overrides, get_async_db, scratch_db)
        listener = socket.socket()
        cleanup.callback(listener.close)
        for attempt in range(64):
            port = 49152 + secrets.randbelow(16384)
            journal.event("reserve-listener", host="127.0.0.1", port=port, pid=os.getpid(), attempt=attempt)
            with socket.socket() as probe:
                probe.settimeout(0.2)
                probe_errno = probe.connect_ex(("127.0.0.1", port))
            if probe_errno == 0:
                journal.event("borrowed-listener", host="127.0.0.1", port=port, action="preserve-and-reselect")
                continue
            if probe_errno != errno.ECONNREFUSED:
                journal.event("listener-probe-unavailable", host="127.0.0.1", port=port, probe_errno=probe_errno)
                continue
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
                journal.event("borrowed-listener-bind-race", host="127.0.0.1", port=port, action="preserve-and-reselect")
                continue
            record["port"] = port
            break
        else:
            raise RuntimeError("Could not bind a preregistered scratch HTTP endpoint")
        journal.event("listener-bound-before-listen", host="127.0.0.1", port=record["port"], pid=os.getpid())
        base = f"http://127.0.0.1:{record['port']}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", loop=UVICORN_LOOP))
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True)

        def stop_server() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive()
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", record["port"])) != 0
            record["listener_closed"] = True
            journal.event("listener-cleanup-readback", host="127.0.0.1", port=record["port"], thread_stopped=True, connection_refused=True)

        cleanup.callback(stop_server)
        save_record()
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "scratch HTTP API failed to start"
        yield {
            "base": base,
            "owners": owner_ids,
            "jwt": manager.create_access_token(data={"sub": f"{owner_ids[0]}@upload.test"}),
            "api_key": api_key,
            "storage": storage_client,
            "bucket": bucket,
            "engine": bootstrapped_engine,
            "storage_adapter": storage,
            "record": record,
            "record_path": record_path,
            "journal": journal,
        }


def read_object(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
