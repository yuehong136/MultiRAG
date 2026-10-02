"""Opt-in Go/Python acceptance; requires the real CGO tokenizer build.

Run this file explicitly with MULTIRAG_TEST_GO. It is separate from the
Python-only integration job so that job does not acquire native Go dependencies.
"""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from requests import Session as HTTPSession
from sqlalchemy.orm import Session

from api.db import CanvasCategory
from api.db.db_models import Document, Knowledgebase, Task, UserTenant
from api.db.services.task_service import has_canceled
from common.constants import TaskStatus
from common.exceptions import TaskCanceledException
from core.flow.pipeline import Pipeline
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import read_binding
from tests.integration.test_agent_completion_close import close_case
from tests.integration.test_agent_update_release import release_api as release_api
from tests.integration.test_task_cancellation import cancel, persistent_state
from tests.integration.test_task_cancellation import cancel_api as cancel_api
from tests.integration.test_task_cancellation import test_agent_run_bound_before_first_frame_cancel_and_sibling_isolation as run_agent_case
from tests.integration.test_task_cancellation import wait_gate as wait_gate
from tests.integration.test_task_cancellation_terminal import terminal_case


@pytest.fixture
def go_task_api(cancel_api: dict[str, Any], tmp_path: Any) -> Iterator[str]:
    go = os.environ.get("MULTIRAG_TEST_GO") or shutil.which("go")
    if not go:
        pytest.fail("Go task acceptance requires Go >=1.25; set MULTIRAG_TEST_GO to its executable")
    env = cancel_api
    url = env["engine"].url
    redis = REDIS_CONN.REDIS.connection_pool.connection_kwargs
    config = {
        "Database": {"Driver": "postgres", "Host": url.host, "Port": url.port, "Database": url.database, "Username": url.username, "Password": url.password, "Schema": "usr_ai"},
        "RedisAddr": f"{redis['host']}:{redis['port']}",
        "RedisPassword": redis.get("password") or "",
        "RedisDB": redis["db"],
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    output_path = tmp_path / "go-live.log"
    with output_path.open("w") as output:
        process = subprocess.Popen(
            [go, "test", "./internal/handler", "-run", "^TestTaskLiveServer$", "-count=1", "-v"],
            env={**os.environ, "MULTIRAG_TASK_LIVE_CONFIG": str(config_path)},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while not (tmp_path / "base").exists() and time.monotonic() < deadline:
                assert process.poll() is None, output_path.read_text()
                time.sleep(0.1)
            assert (tmp_path / "base").exists(), output_path.read_text()
            yield (tmp_path / "base").read_text()
        finally:
            (tmp_path / "stop").touch()
            try:
                result = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
                raise
            config_path.unlink()
            assert result == 0, output_path.read_text()
            print(output_path.read_text())
            # No credentials are retained; the transcript contains only pool,
            # listener and assertion status, not connection parameters.
            shutil.copyfile(output_path, "/tmp/multirag-488-go-live.log")


def test_go_http_cancels_python_document_and_dataflow(cancel_api: dict[str, Any], go_task_api: str) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    with HTTPSession() as go:
        go.headers["Authorization"] = f"Bearer {env['keys'][0]}"
        assert go.post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": "invalid"}, timeout=10).status_code == 401
        denied = go.post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][1]}"}, timeout=10)
        assert denied.json()["code"] == 109 and not has_canceled(task_id)
        response = go.patch(go_task_api + f"/api/v1/tasks/{task_id}", json={"action": "stop"}, timeout=10)
        assert response.json() == {"code": 0, "message": "success", "data": True}
        assert has_canceled(task_id)
        state = persistent_state(env, task_id)
        assert state[0] == -1 and state[2:4] == (TaskStatus.CANCEL.value, 0)
        assert cancel(env, task_id).json()["retcode"] == 0 and persistent_state(env, task_id) == state
        assert go.get(go_task_api + f"/api/v1/tasks/{task_id}", timeout=10).status_code == 404

        dsl = {"components": {"File": {"obj": {"component_name": "File", "params": {}}, "downstream": [], "upstream": []}}, "path": []}
        flow_id = (
            env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"go pipeline {uuid4().hex}", "dsl": dsl, "canvas_category": CanvasCategory.DataFlow}, timeout=30).json()["data"]["id"]
        )
        queued = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json={"agent_id": flow_id}, timeout=30)
        assert queued.json()["retcode"] == 0
        flow_task_id = queued.json()["data"]["message_id"]
        env["task_ids"].append(flow_task_id)
        assert go.post(go_task_api + f"/api/v1/tasks/{flow_task_id}/cancel", timeout=10).json()["code"] == 0
        assert read_binding(flow_task_id)[1]["state"] == "cancel_requested"
        pipeline = Pipeline(dsl, tenant_id=env["owners"][0], doc_id="dataflow_x", task_id=flow_task_id, flow_id=flow_id)
        with pytest.raises(TaskCanceledException):
            pipeline.callback("File", 1, "Go cancellation observed")
        with Session(env["engine"]) as db:
            assert db.get(Task, flow_task_id).progress == -1


@pytest.mark.parametrize("mode", ["draft", "published", "continued"])
def test_go_http_cancels_python_agent_runs(cancel_api: dict[str, Any], wait_gate: dict[str, Any], go_task_api: str, mode: str) -> None:
    run_agent_case(cancel_api, wait_gate, mode, cancel_base=go_task_api)


@pytest.mark.parametrize("role,status,allowed", [("normal", "1", True), ("admin", "1", True), ("invite", "1", False), ("normal", "0", False)])
def test_go_current_membership(cancel_api: dict[str, Any], go_task_api: str, role: str, status: str, allowed: bool) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][1], tenant_id=env["owners"][0], role=role, status=status, invited_by=env["owners"][0]))
        db.commit()
    response = env["client"].post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][1]}"}, timeout=30)
    assert (response.json()["code"] == 0) is allowed
    assert has_canceled(task_id) is allowed
    assert (persistent_state(env, task_id)[0] == -1) is allowed


def test_go_sql_failure_rolls_back_and_compensates_nonce(cancel_api: dict[str, Any], go_task_api: str) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    before = persistent_state(env, task_id)
    constraint = "task_cancel_" + uuid4().hex
    with env["engine"].begin() as connection:
        connection.execute(sa.text(f'ALTER TABLE usr_ai.t_ai_tasks ADD CONSTRAINT "{constraint}" CHECK (progress >= 0)'))
    try:
        response = env["client"].post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][0]}"}, timeout=30)
        assert response.json()["code"] == 100 and response.json()["data"] is False
        assert persistent_state(env, task_id) == before
        assert not REDIS_CONN.REDIS.exists(f"{task_id}-cancel")
    finally:
        with env["engine"].begin() as connection:
            connection.execute(sa.text(f'ALTER TABLE usr_ai.t_ai_tasks DROP CONSTRAINT "{constraint}"'))


def test_go_graph_binding_and_terminal_noops(cancel_api: dict[str, Any], go_task_api: str) -> None:
    from api.apps.services.dataset_api_service import run_index

    env = cancel_api
    with Session(env["engine"]) as db:
        db.execute(sa.update(Document).where(Document.id == env["doc_id"]).values(run=TaskStatus.DONE.value, progress=1))
        db.commit()
        ok, result = run_index(db, env["owners"][0], env["kb_id"], "graph")
        assert ok, result
        task_id = result["task_id"]
        env["task_ids"].append(task_id)
        assert db.get(Knowledgebase, env["kb_id"]).graphrag_task_id == task_id
    with HTTPSession() as client:
        client.headers["Authorization"] = f"Bearer {env['keys'][0]}"
        response = client.post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", timeout=30)
        assert response.json()["code"] == 0 and has_canceled(task_id)
        before = persistent_state(env, task_id)
        assert before[0] == -1 and before[2:4] == (TaskStatus.DONE.value, 1)
        assert client.post(go_task_api + f"/api/v1/tasks/{task_id}/cancel", timeout=30).json()["code"] == 0
        assert persistent_state(env, task_id) == before
        unknown = uuid4().hex
        assert client.post(go_task_api + f"/api/v1/tasks/{unknown}/cancel", timeout=30).json()["code"] == 0
        assert not REDIS_CONN.REDIS.exists(f"{unknown}-cancel")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "mode,window,winner", [("first", "after_events", "cancel"), ("continued", "after_events", "cancel"), ("published", "finish", "cancel"), ("continued", "persistence", "finish")]
)
def test_go_http_at_python_terminal_boundary(cancel_api: dict[str, Any], go_task_api: str, monkeypatch: pytest.MonkeyPatch, mode: str, window: str, winner: str, stream: bool) -> None:
    terminal_case(cancel_api, monkeypatch, surface="rest", mode=mode, stream=stream, window=window, winner=winner, cancel_base=go_task_api)


@pytest.mark.parametrize("surface", ["rest", "beta", "openai"])
@pytest.mark.parametrize("mode,window,winner", [("first", "send", "cancel"), ("continued", "canvas", "cancel"), ("published", "send", "finish")])
def test_go_http_at_python_close_boundary(cancel_api: dict[str, Any], go_task_api: str, monkeypatch: pytest.MonkeyPatch, surface: str, mode: str, window: str, winner: str) -> None:
    close_case(cancel_api, monkeypatch, surface=surface, mode=mode, window=window, winner=winner, cancel_base=go_task_api)
