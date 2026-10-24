"""Typed Web consumer over real routes, SQL and objects; browser actions controlled."""

import asyncio
import copy
import json
import os
import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from minio import Minio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from agent.component import browser as browser_module
from agent.component.browser import Browser
from api.db.db_models import API4Conversation, File, TenantLLM, UserCanvas
from common import resources
from common.config_utils import CONFIGS
from core.utils.minio_conn import MultiRAGMinio
from tests.support.agent_update_release import release_api as release_api


@pytest.mark.external_consumer
def test_browser_actual_web_save_upload_debug_run(release_api: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    evidence = Path(os.getenv("MULTIRAG_BROWSER_CONSUMER_EVIDENCE", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    bucket = f"browser-consumer-{uuid4().hex}"
    owner = env["owners"][0]
    record: dict[str, Any] = {"bucket": bucket, "owner": owner, "database": env["engine"].url.database, "base": env["base"], "actions": []}

    def save() -> None:
        (evidence / "resources.json").write_text(json.dumps(record, indent=2) + "\n")

    cfg = CONFIGS["minio"]
    client = Minio(cfg["host"], access_key=cfg["user"], secret_key=str(cfg["password"]), secure=False)
    storage = copy.copy(MultiRAGMinio())
    storage.conn, storage.bucket, storage.prefix_path = client, bucket, ""
    monkeypatch.setitem(resources._state, "storage", storage)
    async_engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    @asynccontextmanager
    async def connect() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    monkeypatch.setattr(browser_module, "async_db_connection", connect)

    async def controlled_actions(self: Browser, task_text: str, download_dir: str, available_file_paths: list[str], profile_dir: Path) -> Any:
        # Only the third-party action boundary is controlled; model resolution,
        # runtime authority, local preparation and persistence use real inputs.
        model = await self._build_browser_llm()
        assert model.model == "browser-consumer" and model.temperature == 0
        assert len(available_file_paths) == 1
        assert Path(available_file_paths[0]).read_bytes() == b"web browser source bytes"
        assert "Inspect uploaded source" in task_text and profile_dir.is_dir()
        (Path(download_dir) / "result.txt").write_bytes(b"browser consumer download")
        record["actions"].append({"node": self._id, "uploads": len(available_file_paths), "profile": str(profile_dir)})
        save()
        return SimpleNamespace(final_result=lambda: "browser consumer complete", is_successful=lambda: True)

    monkeypatch.setattr(Browser, "_run_browser_use_async", controlled_actions)
    save()  # Register exact bucket before any external write.
    try:
        client.make_bucket(bucket)
        with Session(env["engine"]) as db:
            model = TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="browser-consumer", mdl_type="chat", api_key="test-only", api_base="http://127.0.0.1:1/v1", status="1")
            db.add(model)
            db.commit()
            record["modelId"] = model.id
        save()
        root = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
        runner = root / "node_modules/.bin/tsx"
        assert runner.is_file(), "Installed fixed Web checkout is required"
        result = subprocess.run(
            [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(Path(__file__).resolve().parents[1] / "support/browser_web_consumer.mts")],
            cwd=root,
            env={**os.environ, "BROWSER_CONSUMER_BASE": env["base"], "BROWSER_CONSUMER_TOKEN": env["jwts"][0]},
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        consumer = json.loads(result.stdout.splitlines()[-1])
        assert consumer["passed"] and len(record["actions"]) == 2
        assert all(not Path(action["profile"]).exists() for action in record["actions"])
        with Session(env["engine"]) as db:
            canvas = db.get(UserCanvas, consumer["id"])
            assert canvas is not None and canvas.dsl["components"]["Browser:consumer"]["obj"]["params"]["temperature"] == 0
            conversation = db.get(API4Conversation, consumer["sessionId"])
            assert conversation is not None and conversation.dialog_id == canvas.id
            assert [message["role"] for message in conversation.message] == ["user", "assistant"]
            files = list(db.scalars(select(File).where(File.tenant_id == owner, File.created_by == owner, File.type != "folder")))
            assert len(files) == 2
            for file in files:
                assert storage.get_bytes(file.parent_id, file.location) == b"browser consumer download"
        record.update(consumer=consumer, sqlAndObjectReadback=True)
        save()
    finally:
        asyncio.run(async_engine.dispose())
        with Session(env["engine"]) as db:
            db.execute(delete(File).where(File.tenant_id == owner, File.created_by == owner))
            if "modelId" in record:
                db.execute(delete(TenantLLM).where(TenantLLM.id == record["modelId"]))
            db.commit()
            assert db.scalar(select(File.id).where(File.tenant_id == owner, File.created_by == owner).limit(1)) is None
        if client.bucket_exists(bucket):
            for obj in client.list_objects(bucket, recursive=True):
                client.remove_object(bucket, obj.object_name)
            client.remove_bucket(bucket)
        record["bucketAbsent"] = not client.bucket_exists(bucket)
        record["ownedSqlRowsAbsent"] = True
        save()
        assert record["bucketAbsent"]
