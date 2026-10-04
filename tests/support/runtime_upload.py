"""Owned HTTP, database and object-store fixtures shared by integration suites."""

import asyncio
import copy
import json
import os
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


@pytest.fixture
def runtime_upload_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    from api.apps import app, manager

    owner_ids = [uuid4().hex, uuid4().hex]
    api_key = f"upload-test-{uuid4().hex}"
    bucket = f"upload-test-{uuid4().hex}"
    record: dict[str, Any] = {"database": bootstrapped_engine.url.database, "owners": owner_ids, "api_token_owner": owner_ids[1], "api_token_name": "upload-test", "bucket": bucket}
    evidence = Path(os.environ.get("MULTIRAG_343BDA_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    record_path = evidence / (owner_ids[0] + ".json")

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
        assert not any(remaining.values()), remaining

    save_record()
    with ExitStack() as cleanup:
        cleanup.callback(save_record)
        cleanup.callback(remove_rows)
        with Session(bootstrapped_engine) as db:
            for user_id in owner_ids:
                db.add(User(id=user_id, email=f"{user_id}@upload.test", nickname="Upload test", password="unused", access_token="active"))
                db.add(Tenant(id=user_id, name="Upload scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=user_id, tenant_id=user_id, role="owner", invited_by=user_id))
            db.add(APIToken(tenant_id=owner_ids[1], token=api_key, name="upload-test"))
            db.commit()

        cfg = CONFIGS["minio"]
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
        listener.bind(("127.0.0.1", 0))
        record["port"] = listener.getsockname()[1]
        base = f"http://127.0.0.1:{record['port']}"
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop_server() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive()
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", record["port"])) != 0
            record["listener_closed"] = True

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
        }


def read_object(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()
