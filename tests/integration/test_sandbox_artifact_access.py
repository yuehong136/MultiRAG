"""Sandbox file authorization uses a server-created binding, not message text."""

import base64
import json
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import sessionmaker

from api.apps.services.sandbox_artifact_service import _artifact_accessible
from api.db.db_models import API4Conversation, User, UserCanvas
from core.utils.redis_conn import REDIS_CONN
from core.utils.sandbox_artifact_registry import get_artifact_binding, record_artifact_binding
from tests.support.agent_update_release import release_api as release_api


async def test_artifact_is_bound_before_assistant_message_is_saved(bootstrapped_async_engine: AsyncEngine) -> None:
    owner_id = uuid4().hex
    other_id = uuid4().hex
    canvas_id = uuid4().hex
    session_id = uuid4().hex
    run_id = uuid4().hex
    filename = f"{uuid4().hex}.pdf"
    stolen_filename = f"{uuid4().hex}.pdf"
    binding_keys = [f"multirag:sandbox_artifact:{name}" for name in (filename, stolen_filename)]

    async with bootstrapped_async_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            async with AsyncSession(bind=connection) as db:
                db.add_all(
                    [
                        User(id=owner_id, nickname="Artifact owner", email=f"{owner_id}@example.test"),
                        User(id=other_id, nickname="Other user", email=f"{other_id}@example.test"),
                        UserCanvas(id=canvas_id, user_id=owner_id, title="Artifact canvas"),
                        API4Conversation(
                            id=session_id,
                            dialog_id=canvas_id,
                            user_id=owner_id,
                            source="agent",
                            message=[{"role": "user", "content": "Create a PDF."}],
                        ),
                    ]
                )
                await db.flush()

                assert record_artifact_binding(filename, owner_id, run_id, session_id)
                assert get_artifact_binding(filename) is not None
                # CodeExec has uploaded the object, but the assistant message is
                # not persisted until the stream ends.
                assert await _artifact_accessible(db, filename, owner_id, run_id, session_id)
                assert not await _artifact_accessible(db, filename, other_id, run_id, session_id)
                assert not await _artifact_accessible(db, filename, owner_id, uuid4().hex, session_id)
                assert not await _artifact_accessible(db, filename, owner_id, run_id, uuid4().hex)
                assert not await _artifact_accessible(db, filename, owner_id, None, session_id)

                # Even a saved assistant message repeating another user's URL
                # cannot forge that object's server-created binding.
                assert record_artifact_binding(stolen_filename, other_id, uuid4().hex, None)
                conversation = await db.get(API4Conversation, session_id)
                assert conversation is not None
                conversation.message = [{"role": "assistant", "content": f"[Other PDF](/api/v1/documents/artifact/{stolen_filename})"}]
                await db.flush()
                assert not await _artifact_accessible(db, stolen_filename, owner_id, run_id, session_id)
                assert not await _artifact_accessible(db, stolen_filename, owner_id, None, None)

                await db.delete(conversation)
                await db.flush()
                assert not await _artifact_accessible(db, filename, owner_id, run_id, session_id)
        finally:
            await transaction.rollback()
            for key in binding_keys:
                REDIS_CONN.delete(key)


def test_local_codeexec_debug_download_and_downstream_reference(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """Real registered debug/download routes, local execution and exact S3 bytes."""
    from agent.canvas import Canvas
    from agent.sandbox import client as sandbox_client
    from agent.tools import code_exec
    from api.apps.services import sandbox_artifact_service
    from common.config_utils import CONFIGS

    env = release_api
    bucket = f"codeexec-{uuid4().hex}"
    conf = CONFIGS["minio"]
    storage_client = Minio(conf["host"], access_key=conf["user"], secret_key=str(conf["password"]), secure=False)
    objects: list[str] = []
    manifest: dict[str, Any] = {"bucket": bucket, "objects": objects, "redis": [], "absent": False}
    report_dir = request.config.getoption("--test-report-dir")
    manifest_path = Path(report_dir or tmp_path) / "local-codeexec-resources.json"

    def save_manifest() -> None:
        manifest_path.write_text(json.dumps(manifest, indent=2))

    class Storage:
        conn = storage_client

        def put(self, target_bucket: str, name: str, binary: bytes) -> None:
            assert target_bucket == bucket
            objects.append(name)
            manifest["redis"].append(f"multirag:sandbox_artifact:{name}")
            save_manifest()
            self.conn.put_object(bucket, name, BytesIO(binary), len(binary))

        def get(self, target_bucket: str, name: str) -> bytes:
            assert target_bucket == bucket
            response = self.conn.get_object(bucket, name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        def rm(self, target_bucket: str, name: str) -> None:
            assert target_bucket == bucket
            self.conn.remove_object(bucket, name)

    storage = Storage()
    monkeypatch.setattr(code_exec, "settings", SimpleNamespace(STORAGE_IMPL=storage))
    monkeypatch.setattr(sandbox_artifact_service, "settings", SimpleNamespace(STORAGE_IMPL=storage))
    monkeypatch.setattr(code_exec, "SANDBOX_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(sandbox_artifact_service, "SANDBOX_ARTIFACT_BUCKET", bucket)
    monkeypatch.setattr(code_exec.CodeExec, "_lifecycle_configured", False)
    # The file-parser/model boundary is controlled; execution, SQL, Redis, S3,
    # registered API routes and authentication below remain real.
    monkeypatch.setattr(code_exec.FileService, "parse", lambda name, blob, *_args, **_kwargs: name if name.endswith(".png") else blob.decode())
    monkeypatch.setattr(sandbox_client, "SessionLocal", sessionmaker(env["engine"]))
    monkeypatch.setenv("SANDBOX_PROVIDER_TYPE", "local")
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_LOCAL_PYTHON_BIN", sys.executable)
    monkeypatch.setenv("SANDBOX_LOCAL_WORK_DIR", str(tmp_path / "local-runs"))
    sandbox_client.reload_provider()
    csv_bytes = b"name,value\nexample,1\n"
    png_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jB1sAAAAASUVORK5CYII=")
    script = f"""from pathlib import Path
import base64

def main(mode="files"):
    if mode == "fail":
        raise ValueError("controlled local failure")
    if mode == "files":
        Path("artifacts").mkdir(exist_ok=True)
        Path("artifacts/report.csv").write_bytes({csv_bytes!r})
        Path("artifacts/chart.png").write_bytes(base64.b64decode({base64.b64encode(png_bytes).decode()!r}))
    return "Report ready"
"""
    params = {"lang": "python", "script": script, "arguments": {"mode": ""}, "outputs": {"result": {"type": "String", "value": None}, "attachments": {"type": "Array<String>", "value": []}}}
    dsl = {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {}}, "downstream": ["CodeExec:report"], "upstream": []},
            "CodeExec:report": {"obj": {"component_name": "CodeExec", "params": params}, "upstream": ["begin"], "downstream": []},
        },
        "history": [],
        "path": [],
        "retrieval": [],
    }
    save_manifest()  # Record bucket ownership before creation.
    try:
        storage_client.make_bucket(bucket)
        response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Local CodeExec acceptance", "dsl": dsl}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        agent_id = response.json()["data"]["id"]
        url = f"{env['base']}/api/v1/agents/{agent_id}/components/CodeExec:report/debug"
        response = env["client"].post(url, json={"params": {"mode": {"value": "files"}}}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        output = response.json()["data"]
        assert output["_ERROR"] == "" and output["result"] == "Report ready", output
        assert len(output["attachments"]) == 2
        assert any(link.startswith("![chart.png](") for link in output["attachments"])
        assert any(link.startswith("[Download report.csv](") for link in output["attachments"])
        for artifact in output["_ARTIFACTS"]:
            expected = png_bytes if artifact["name"].endswith(".png") else csv_bytes
            download = env["client"].get(env["base"] + artifact["url"], timeout=15)
            assert download.status_code == 200 and download.content == expected
            object_name = artifact["url"].split("/artifact/", 1)[1].split("?", 1)[0]
            assert storage.get(bucket, object_name) == expected  # Independent S3 readback.
            denied = env["client"].get(env["base"] + artifact["url"], headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=15)
            assert denied.json()["retcode"] != 0 and denied.content != expected
        canvas = Canvas(json.dumps(dsl), env["owners"][0], canvas_id=agent_id)
        canvas.get_component("CodeExec:report")["obj"].set_output("attachments", output["attachments"])
        assert canvas.get_variable_value("CodeExec:report@attachments") == output["attachments"]
        for mode in ("empty", "fail"):
            result = env["client"].post(url, json={"params": {"mode": {"value": mode}}}, timeout=30).json()
            assert result["retcode"] == 0
            assert result["data"]["attachments"] == []
            assert bool(result["data"]["_ERROR"]) == (mode == "fail")
        assert list((tmp_path / "local-runs").iterdir()) == []
    finally:
        sandbox_client.reload_provider()
        for name in objects:
            storage_client.remove_object(bucket, name)
            REDIS_CONN.delete(f"multirag:sandbox_artifact:{name}")
        assert not list(storage_client.list_objects(bucket))
        storage_client.remove_bucket(bucket)
        assert not storage_client.bucket_exists(bucket)
        assert all(REDIS_CONN.get(key) is None for key in manifest["redis"])
        manifest["absent"] = True
        save_manifest()
