"""Real PostgreSQL/MinIO authorization and download compensation."""

import asyncio
import copy
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from minio import Minio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from agent.component import browser as module
from agent.component.browser import Browser, BrowserParam
from api.db.db_models import File, TenantLLM
from api.db.services.file_service import FileService, RuntimeAttachmentError
from common import resources
from common.config_utils import CONFIGS
from core.utils.minio_conn import MultiRAGMinio
from tests.support.database import scratch_database


def test_browser_file_scope_and_compensation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = Path(os.getenv("MULTIRAG_BROWSER_EVIDENCE", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    tenant, owner, foreign = (uuid4().hex for _ in range(3))
    bucket = f"browser-g-{uuid4().hex}"
    record: dict[str, Any] = {"bucket": bucket, "tenant": tenant, "owner": owner, "foreign": foreign, "created": False, "absent": False}
    manifest = evidence / f"{bucket}.json"

    def save() -> None:
        manifest.write_text(json.dumps(record, indent=2))

    cfg = CONFIGS["minio"]
    client = Minio(cfg["host"], access_key=cfg["user"], secret_key=str(cfg["password"]), secure=False)
    storage = copy.copy(MultiRAGMinio())
    storage.conn, storage.bucket, storage.prefix_path = client, bucket, ""
    monkeypatch.setitem(resources._state, "storage", storage)
    save()  # Register exact bucket before creating it.
    try:
        client.make_bucket(bucket)
        record["created"] = True
        save()
        with scratch_database(evidence) as engine:
            record["database"] = engine.url.database
            save()
            with Session(engine) as db:
                db.add_all(
                    [
                        File(id="own", tenant_id=tenant, created_by=owner, parent_id="folder", name="own.txt", location="own", type="doc", size=3),
                        File(id="foreign", tenant_id=tenant, created_by=foreign, parent_id="folder", name="secret.txt", location="foreign", type="doc", size=6),
                        TenantLLM(tenant_id=tenant, llm_factory="OpenAI", llm_name="controlled-browser", mdl_type="chat", api_key="test-only", api_base="http://127.0.0.1:1/v1", status="1"),
                    ]
                )
                db.commit()
            storage.put("folder", "own", b"own")
            storage.put("folder", "foreign", b"secret")

            async def verify() -> None:
                async_engine = create_async_engine(engine.url)
                sessions = async_sessionmaker(async_engine, expire_on_commit=False)

                @asynccontextmanager
                async def connect() -> AsyncIterator[AsyncSession]:
                    async with sessions() as db:
                        yield db

                monkeypatch.setattr(module, "async_db_connection", connect)
                obj = object.__new__(Browser)
                obj._param = BrowserParam()
                obj._param.llm_id = "controlled-browser@OpenAI"
                obj._id = "Browser:integration"
                obj._canvas = SimpleNamespace(artifact_owner_id=owner, get_tenant_id=lambda: tenant)
                try:
                    model = await obj._build_browser_llm()
                    assert model.model == "controlled-browser" and model.temperature == 0
                    assert model.add_schema_to_system_prompt and model.dont_force_structured_output
                    obj._canvas.get_tenant_id = lambda: foreign
                    with pytest.raises(LookupError):
                        await obj._build_browser_llm()
                    obj._canvas.get_tenant_id = lambda: tenant
                    obj._param.upload_sources = ["own"]
                    prepared = await obj._prepare_upload_files(str(tmp_path))
                    assert Path(prepared[0]["local_path"]).read_bytes() == b"own"
                    # Restore server-written runtime authority; ignore forged names/owners.
                    runtime_id = uuid4().hex
                    descriptor = {
                        "id": runtime_id,
                        "name": "runtime.txt",
                        "size": 7,
                        "mime_type": "text/plain",
                        "extension": "txt",
                        "created_by": owner,
                        "created_at": time.time(),
                        "preview_url": None,
                    }
                    FileService._put_runtime_object(owner, runtime_id, b"runtime")
                    FileService._put_runtime_object(owner, FileService._runtime_descriptor_key(runtime_id), json.dumps(descriptor).encode())
                    obj._param.upload_sources = [{"id": runtime_id, "name": "forged.txt", "created_by": foreign}]
                    runtime = await obj._prepare_upload_files(str(tmp_path))
                    assert runtime[0]["name"] == "runtime.txt" and Path(runtime[0]["local_path"]).read_bytes() == b"runtime"
                    obj._canvas.artifact_owner_id = foreign
                    with pytest.raises(RuntimeAttachmentError):
                        await obj._prepare_upload_files(str(tmp_path))
                    obj._canvas.artifact_owner_id = owner
                    storage.rm(f"{owner}-downloads", runtime_id)
                    with pytest.raises(RuntimeAttachmentError):
                        await obj._prepare_upload_files(str(tmp_path))
                    obj._param.upload_sources = [{"file_id": "own", "name": "forged.txt"}]
                    assert (await obj._prepare_upload_files(str(tmp_path)))[0]["name"] == "own.txt"
                    before = list(client.list_objects(bucket, recursive=True))
                    obj._param.upload_sources = ["foreign"]
                    with pytest.raises(PermissionError):
                        await obj._prepare_upload_files(str(tmp_path))
                    assert len(list(client.list_objects(bucket, recursive=True))) == len(before)
                    obj._canvas.artifact_owner_id = foreign
                    obj._param.upload_sources = ["own"]
                    with pytest.raises(PermissionError):
                        await obj._prepare_upload_files(str(tmp_path))
                    obj._canvas.artifact_owner_id = owner
                    downloads = tmp_path / "downloads"
                    downloads.mkdir()
                    (downloads / "report.txt").write_bytes(b"downloaded bytes")
                    files = await obj._save_downloads(str(downloads))
                    with Session(engine) as db:
                        row = db.scalar(select(File).where(File.id == files[0]["file_id"]))
                        assert row.created_by == owner and row.tenant_id == tenant
                        assert storage.get_bytes(row.parent_id, row.location) == b"downloaded bytes"
                        original_count = db.scalar(select(func.count()).select_from(File))
                    original_keys = {item.object_name for item in client.list_objects(bucket, recursive=True)}

                    class FailingSession(AsyncSession):
                        async def commit(self) -> None:
                            await self.flush()
                            raise RuntimeError("injected SQL failure")

                    failing = async_sessionmaker(async_engine, class_=FailingSession, expire_on_commit=False)

                    @asynccontextmanager
                    async def fail_connect() -> AsyncIterator[AsyncSession]:
                        async with failing() as db:
                            yield db

                    monkeypatch.setattr(module, "async_db_connection", fail_connect)
                    with pytest.raises(RuntimeError, match="SQL failure"):
                        await obj._save_downloads(str(downloads))
                    with Session(engine) as db:
                        assert db.scalar(select(func.count()).select_from(File)) == original_count
                    assert {item.object_name for item in client.list_objects(bucket, recursive=True)} == original_keys

                    class LostCommitSession(AsyncSession):
                        async def commit(self) -> None:
                            await super().commit()
                            raise RuntimeError("lost commit reply")

                    lost = async_sessionmaker(async_engine, class_=LostCommitSession, expire_on_commit=False)

                    @asynccontextmanager
                    async def lost_connect() -> AsyncIterator[AsyncSession]:
                        async with lost() as db:
                            yield db

                    monkeypatch.setattr(module, "async_db_connection", lost_connect)
                    with pytest.raises(RuntimeError, match="lost commit reply"):
                        await obj._save_downloads(str(downloads))
                    with Session(engine) as db:
                        assert db.scalar(select(func.count()).select_from(File)) == original_count
                    assert {item.object_name for item in client.list_objects(bucket, recursive=True)} == original_keys
                    monkeypatch.setattr(module, "async_db_connection", connect)
                    original_put = storage.put

                    def failed_put(*args: Any) -> bool:
                        original_put(*args)
                        return False

                    monkeypatch.setattr(storage, "put", failed_put)
                    with pytest.raises(RuntimeError, match="readback"):
                        await obj._save_downloads(str(downloads))
                    with Session(engine) as db:
                        assert db.scalar(select(func.count()).select_from(File)) == original_count
                    assert {item.object_name for item in client.list_objects(bucket, recursive=True)} == original_keys
                    record["checks"] = [
                        "tenant model identity/zero/structured compatibility",
                        "own upload byte readback",
                        "foreign actor denial",
                        "SQL+storage download readback",
                        "SQL failure compensation",
                        "false storage receipt compensation",
                        "lost SQL commit acknowledgement compensation",
                        "trusted runtime metadata/foreign/revoked guards",
                        "Browser download file_id descriptor reuse",
                    ]
                finally:
                    await async_engine.dispose()

            asyncio.run(verify())
    finally:
        if client.bucket_exists(bucket):
            for item in client.list_objects(bucket, recursive=True):
                client.remove_object(bucket, item.object_name)
            assert not list(client.list_objects(bucket, recursive=True))
            client.remove_bucket(bucket)
        record["absent"] = not client.bucket_exists(bucket)
        save()
        assert record["absent"]


def test_browser_url_upload_real_http(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            hits.append(self.path)
            if self.path in {"/redirect", "/blocked"}:
                self.send_response(302)
                self.send_header("Location", "/file" if self.path == "/redirect" else "http://127.0.0.1/private")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%2e%2e%2fsecret.txt")
            self.end_headers()
            self.wfile.write(b"owned URL payload")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    evidence = Path(os.getenv("MULTIRAG_BROWSER_EVIDENCE", str(tmp_path)))
    evidence.mkdir(exist_ok=True, parents=True)
    manifest = evidence / "url-listener.json"
    record = {"port": server.server_port, "closed": False}
    manifest.write_text(json.dumps(record))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    obj = object.__new__(Browser)
    real_guard = module.assert_url_is_safe

    def owned_guard(url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        if parsed.hostname == "browser-owned.test" and parsed.port == server.server_port:
            return "browser-owned.test", "127.0.0.1"
        return real_guard(url)

    try:
        # Public guard remains active except for this one registered test listener.
        monkeypatch.setattr(module, "assert_url_is_safe", owned_guard)
        base = f"http://browser-owned.test:{server.server_port}"
        result = obj._prepare_upload_url_file(base + "/redirect", str(tmp_path))
        assert result["name"] == "secret.txt" and Path(result["local_path"]).read_bytes() == b"owned URL payload"
        assert Path(result["local_path"]).parent == tmp_path
        before = set(tmp_path.iterdir())
        with pytest.raises(ValueError, match="non-public"):
            obj._prepare_upload_url_file(base + "/blocked", str(tmp_path))
        assert set(tmp_path.iterdir()) == before and hits == ["/redirect", "/file", "/blocked"]
        monkeypatch.setenv("MULTIRAG_BROWSER_UPLOAD_URL_MAX_BYTES", "2")
        with pytest.raises(ValueError, match="size limit"):
            obj._prepare_upload_url_file(base + "/file", str(tmp_path))
        assert set(tmp_path.iterdir()) == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        record["closed"] = not thread.is_alive()
        manifest.write_text(json.dumps(record))
        assert record["closed"]
