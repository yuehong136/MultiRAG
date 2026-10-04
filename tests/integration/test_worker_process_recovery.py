"""Kill/restart the production worker across real Redis acknowledgement boundaries."""

import copy
import json
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, PipelineOperationLog, Task
from api.db.services.file_service import FileService
from common.config_utils import CONFIGS
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_parse_retirement import index_snapshot, object_snapshot
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def _private_json(path: Path, value: Any) -> None:
    path.touch(mode=0o600)
    path.write_text(json.dumps(value))


def _launch(env: dict[str, Any], context: dict[str, Any], directory: Path, cleanup: ExitStack) -> subprocess.Popen[bytes]:
    overlay = copy.deepcopy(CONFIGS)
    url = env["engine"].url
    overlay["postgresql"].update(host=url.host, port=url.port, user=url.username, password=url.password, dbname=url.database)
    overlay["minio"].update(bucket=env["bucket"], prefix_path="")
    _private_json(directory / "config.json", overlay)
    _private_json(directory / "context.json", context)
    log = cleanup.enter_context((directory / f"{context['boundary']}.log").open("wb"))
    child = subprocess.Popen(
        [sys.executable, "-m", "tests.support.worker_process"],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=log,
        env={**os.environ, "MULTIRAG_CONFIG_OVERLAY_FILE": str(directory / "config.json"), "MULTIRAG_WORKER_TEST_CONTEXT": str(directory / "context.json")},
    )

    def stop() -> None:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)
        if child.stdin:
            child.stdin.close()

    cleanup.callback(stop)
    return child


def _state(env: dict[str, Any], task_id: str, doc_id: str) -> dict[str, Any]:
    with env["engine"].connect() as reader:
        return {
            "task_progress": reader.scalar(sa.select(Task.progress).where(Task.id == task_id)),
            "task_message": reader.scalar(sa.select(Task.progress_msg).where(Task.id == task_id)),
            "document": dict(reader.execute(sa.select(Document.chunk_num, Document.token_num).where(Document.id == doc_id)).mappings().one()),
            "dataset": dict(reader.execute(sa.select(Knowledgebase.chunk_num, Knowledgebase.token_num).where(Knowledgebase.id == env["kb"])).mappings().one()),
            "logs": reader.scalar(sa.select(sa.func.count()).select_from(PipelineOperationLog).where(PipelineOperationLog.document_id == doc_id)),
            "index": index_snapshot(env),
            "objects": object_snapshot(env),
        }


@pytest.mark.parametrize("boundary", ["before_ack", "after_parse"])
def test_worker_kill_restart_preserves_effects_and_observes_cancellation(parse_api: dict[str, Any], tmp_path: Path, boundary: str) -> None:
    env = parse_api
    with Session(env["engine"]) as db:
        kb = db.get(Knowledgebase, env["kb"])
        errors, files = FileService.upload_document(db, kb, [(b"Worker recovery source: one durable document.\nRepeat delivery must preserve its counters.", "recovery.txt")], env["owners"][0])
        assert not errors and len(files) == 1
        doc_id = files[0][0]["id"]
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    response = requests.post(f"{env['base']}/api/v1/datasets/{env['kb']}/documents/parse", headers=headers, json={"document_ids": [doc_id]}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with env["engine"].connect() as reader:
        task_id = reader.scalar(sa.select(Task.id).where(Task.doc_id == doc_id))
    assert task_id
    context = {"queue": env["queue"], "group": f"recovery-{uuid4().hex}", "consumer": f"worker-{uuid4().hex}", "boundary": boundary, "ready": str(tmp_path / "ready.json")}
    with ExitStack() as cleanup:
        child = _launch(env, context, tmp_path, cleanup)
        deadline = time.monotonic() + 120
        while not Path(context["ready"]).exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert Path(context["ready"]).exists(), f"Worker never reached {boundary}; inspect {tmp_path / (boundary + '.log')}"
        assert json.loads(Path(context["ready"]).read_text())["pid"] == child.pid
        assert REDIS_CONN.REDIS.xpending(env["queue"], context["group"])["pending"] == 1
        child.kill()
        child.wait(timeout=15)
        assert child.returncode != 0
        before = _state(env, task_id, doc_id)
        if boundary == "before_ack":
            assert before["task_progress"] == 1
            assert before["document"]["chunk_num"] > 0 and before["document"]["token_num"] > 0
            assert before["logs"] == 1 and before["index"]
            context["boundary"] = "resume"
        else:
            cancelled = requests.post(f"{env['base']}/api/v1/tasks/{task_id}/cancel", headers=headers, timeout=30)
            assert cancelled.status_code == 200 and cancelled.json().get("code", cancelled.json().get("retcode")) == 0
            before = _state(env, task_id, doc_id)
            assert before["task_progress"] == -1 and before["index"] == []
            context["boundary"] = "resume_cancelled"
        restarted = _launch(env, context, tmp_path, cleanup)
        assert restarted.wait(timeout=120) == 0, f"Inspect {tmp_path / (context['boundary'] + '.log')}"
        after = _state(env, task_id, doc_id)
        assert REDIS_CONN.REDIS.xpending(env["queue"], context["group"])["pending"] == 0
        for field in ["document", "dataset", "index", "objects", "logs"]:
            assert after[field] == before[field], field
        assert after["task_progress"] == before["task_progress"]
