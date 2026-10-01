"""Real HTTP, owner auth, MinIO bytes, Redis descriptor and attachment consumers."""

import asyncio
import base64
import copy
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import redis
import requests
import sqlalchemy as sa
import uvicorn
from minio import Minio, S3Error
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, File, Tenant, User, UserTenant, get_async_db
from api.db.services.dialog_service import split_file_attachments
from common import resources, settings
from common.config_utils import CONFIGS


@pytest.fixture
def runtime_upload_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api.apps import app, manager

    owner_ids = [uuid4().hex, uuid4().hex]
    api_key = f"upload-test-{uuid4().hex}"
    with Session(bootstrapped_engine) as db:
        for user_id in owner_ids:
            db.add(User(id=user_id, email=f"{user_id}@upload.test", nickname="Upload test", password="unused", access_token="active"))
            db.add(Tenant(id=user_id, name="Upload scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(UserTenant(id=uuid4().hex, user_id=user_id, tenant_id=user_id, role="owner", invited_by=user_id))
        db.add(APIToken(tenant_id=owner_ids[1], token=api_key, name="upload-test"))
        db.commit()

    cfg = CONFIGS["minio"]
    storage_client = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    bucket = f"upload-test-{uuid4().hex}"
    storage_client.make_bucket(bucket)
    # Exercise production MinIO key-mapping decorators, using only this bucket.
    storage = copy.copy(settings.STORAGE_IMPL)
    storage.conn, storage.bucket, storage.prefix_path = storage_client, bucket, ""
    monkeypatch.setitem(resources._state, "storage", storage)
    async_engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def scratch_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_async_db, scratch_db)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
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
        }
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        asyncio.run(async_engine.dispose())
        for item in storage_client.list_objects(bucket, recursive=True):
            storage_client.remove_object(bucket, item.object_name)
        assert not list(storage_client.list_objects(bucket, recursive=True))
        storage_client.remove_bucket(bucket)
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(APIToken).where(APIToken.token == api_key))
            db.execute(sa.delete(UserTenant).where(UserTenant.user_id.in_(owner_ids)))
            db.execute(sa.delete(Tenant).where(Tenant.id.in_(owner_ids)))
            db.execute(sa.delete(User).where(User.id.in_(owner_ids)))
            db.commit()
        assert not thread.is_alive()


def read_object(client: Minio, bucket: str, key: str) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def test_http_runtime_upload_storage_and_real_consumers(runtime_upload_api: dict[str, Any]) -> None:
    from agent.canvas import Canvas

    env = runtime_upload_api
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    url = f"{env['base']}/api/v1/documents/upload"
    text = b"Runtime attachment from the REST endpoint. Owned by the first test user."
    response = requests.post(url, headers=headers, files={"file": ("single.txt", text, "text/plain")}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    single = response.json()["data"]
    assert isinstance(single, dict) and single["created_by"] == env["owners"][0]
    assert single["name"] == "single.txt" and single["mime_type"] == "text/plain" and single["size"] == len(text)
    assert single["extension"] == "txt" and single["preview_url"] is None and isinstance(single["created_at"], (float, int))
    assert read_object(env["storage"], env["bucket"], f"{single['created_by']}-downloads/{single['id']}") == text
    with pytest.raises(S3Error) as denied:
        read_object(env["storage"], env["bucket"], f"{env['owners'][1]}-downloads/{single['id']}")
    assert denied.value.code == "NoSuchKey"
    texts, images = split_file_attachments([single])
    assert text.decode() in "\n".join(texts) and images == []

    png = BytesIO()
    Image.new("RGB", (12, 8), "blue").save(png, format="PNG")
    image_bytes = png.getvalue()
    api_headers = {"Authorization": f"Bearer {env['api_key']}"}
    response = requests.post(url, headers=api_headers, files=[("file", ("second.txt", b"second owner's text", "text/plain")), ("file", ("image.png", image_bytes, "image/png"))], timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    multiple = response.json()["data"]
    assert isinstance(multiple, list) and len(multiple) == 2
    assert [item["name"] for item in multiple] == ["second.txt", "image.png"]
    for item, binary in zip(multiple, [b"second owner's text", image_bytes], strict=True):
        assert item["created_by"] == env["owners"][1] and item["size"] == len(binary)
        assert read_object(env["storage"], env["bucket"], f"{item['created_by']}-downloads/{item['id']}") == binary
    for path, field, auth, code_key, owner in [
        ("/v1/document/upload_info", "file", headers, "retcode", env["owners"][0]),
        ("/api/v1/files/upload_info", "files", api_headers, "code", env["owners"][1]),
    ]:
        body = requests.post(f"{env['base']}{path}", headers=auth, files={field: ("compat.txt", b"compatibility bytes", "text/plain")}, timeout=30).json()
        assert body[code_key] == 0 and isinstance(body["data"], dict)
        descriptor = body["data"]
        assert descriptor["created_by"] == owner and descriptor["size"] == len(b"compatibility bytes")
        assert read_object(env["storage"], env["bucket"], f"{owner}-downloads/{descriptor['id']}") == b"compatibility bytes"
    canvas = Canvas.__new__(Canvas)
    with ThreadPoolExecutor(max_workers=2) as pool:
        canvas._thread_pool = pool
        parsed = asyncio.run(canvas.get_files_async(multiple))
    assert "second owner's text" in parsed[0]
    assert parsed[1] == "data:image/png;base64," + base64.b64encode(image_bytes).decode()

    cfg = CONFIGS["redis"]
    host, port = cfg["host"].rsplit(":", 1)
    cache = redis.Redis(host=host, port=int(port), db=int(cfg.get("db", 1)), username=cfg.get("username") or None, password=cfg.get("password") or None)
    key = f"upload-test:{uuid4().hex}"
    try:
        assert cache.ping() and cache.set(key, json.dumps(multiple), ex=60)
        restored = json.loads(cache.get(key))
        assert restored == multiple
        texts, images = split_file_attachments(restored, raw=True)
        assert "second owner's text" in "\n".join(texts) and images == [image_bytes]
    finally:
        cache.delete(key)
        assert cache.get(key) is None
        cache.close()

    for auth in [{}, {"Authorization": "Bearer invalid-key"}]:
        response = requests.post(url, headers=auth, files={"file": ("denied.txt", b"denied")}, timeout=30)
        assert response.status_code == 401 and response.json()["code"] == 401
    for kwargs in [{}, {"files": {"file": ("", b"")}}, {"params": {"url": "http://127.0.0.1/"}}, {"params": {"url": "https://example.com"}, "files": {"file": ("mixed.txt", b"mixed")}}]:
        body = requests.post(url, headers=headers, timeout=30, **kwargs).json()
        assert body["code"] == 101 and "data" not in body

    adapter = env["storage_adapter"]
    existing = {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)}
    original_put = adapter.put
    count = 0

    def second_write_fails(bucket: str, key: str, binary: bytes) -> Any:
        nonlocal count
        count += 1
        if count == 3:
            # Real MinIO error, after the first write reached the real bucket.
            return env["storage"].put_object(f"missing-upload-test-{uuid4().hex}", key, BytesIO(binary), len(binary))
        return original_put(bucket, key, binary)

    adapter.put = second_write_fails
    try:
        response = requests.post(url, headers=headers, files=[("file", ("first.txt", b"compensate first")), ("file", ("second.txt", b"failure"))], timeout=30)
        assert response.json()["code"] == 100 and "data" not in response.json()
        assert {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)} == existing
    finally:
        adapter.put = original_put
    adapter.bucket = f"missing-upload-test-{uuid4().hex}"
    try:
        response = requests.post(url, headers=headers, files={"file": ("storage-failure.txt", b"not stored")}, timeout=30)
        assert response.status_code == 200 and response.json() == {"code": 100, "message": "Failed to upload document."}
    finally:
        adapter.bucket = env["bucket"]

    # Optional external crawl acceptance, explicitly requested by the operator.
    # The normal integration gate remains independent of Internet availability.
    crawl_url = os.environ.get("RUNTIME_UPLOAD_ACCEPTANCE_URL")
    if crawl_url:
        response = requests.post(url, headers=headers, params={"url": crawl_url}, timeout=120)
        body = response.json()
        assert response.status_code == 200 and body["code"] == 0, body
        descriptor = body["data"]
        assert isinstance(descriptor, dict) and descriptor["created_by"] == env["owners"][0]
        binary = read_object(env["storage"], env["bucket"], f"{descriptor['created_by']}-downloads/{descriptor['id']}")
        assert descriptor["size"] == len(binary) and binary
        texts, images = split_file_attachments([descriptor])
        assert images == [] and texts
        expected = os.environ.get("RUNTIME_UPLOAD_ACCEPTANCE_TEXT")
        if expected:
            assert expected in "\n".join(texts)
        print("real URL crawl: business code, descriptor owner/size, independent MinIO bytes and parsed attachment content passed")
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][1]).values(status="0"))
        db.commit()
    response = requests.post(url, headers=api_headers, files={"file": ("inactive.txt", b"denied")}, timeout=30)
    assert response.status_code == 401 and response.json()["code"] == 401
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.created_by.in_(env["owners"]))) == 0
        assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.created_by.in_(env["owners"]))) == 0
    assert len(list(env["storage"].list_objects(env["bucket"], recursive=True))) == (12 if crawl_url else 10)

    smoke = subprocess.run(["make", "smoke"], cwd=Path(__file__).resolve().parents[2], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    print("real HTTP: JWT single + API-key repeated-file; MinIO bytes/owner + chat/Canvas + Redis descriptor readback; make smoke passed; scratch cleanup required")
