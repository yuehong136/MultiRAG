"""Real authenticated HTTP cancellation, scratch PostgreSQL and owned Redis.

The Agent wait is a controllable test component boundary; Canvas, Begin,
VariableAssigner, Message, sessions, authorization and storage are real.
"""

import asyncio
import json
import os
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- load the runner before legacy component nest_asyncio patches
from requests import Session as HTTPSession
from sqlalchemy.orm import Session

from agent.component.variable_assigner import VariableAssigner
from api.apps.services.dataset_api_service import run_index
from api.db import CanvasCategory, UserTenantRole
from api.db.db_models import API4Conversation, Document, File, File2Document, Knowledgebase, Task, UserTenant
from api.db.services.document_service import DocumentService
from api.db.services.task_service import TaskService, has_canceled, queue_tasks
from common import settings
from common.constants import TaskStatus
from common.exceptions import TaskCanceledException
from core.flow.pipeline import Pipeline
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import TASK_CANCEL_MARKER, binding_key, finish_runtime, read_binding, register_runtime, request_runtime_cancel
from tests.integration.test_agent_update_release import denied_membership, join_member, message_dsl, read_state, sse_events, update
from tests.integration.test_agent_update_release import release_api as release_api


@pytest.fixture
def cancel_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    queue = f"cancel-test:{uuid4().hex}"
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    kb_id, doc_id, file_id = uuid4().hex, uuid4().hex, uuid4().hex
    owner = env["owners"][0]
    try:
        with Session(env["engine"]) as db:
            db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name="Cancellation scratch", embd_id="", permission="team"))
            db.add(Document(id=doc_id, kb_id=kb_id, parser_id="naive", type="doc", created_by=owner, name="cancel.txt", location="cancel.txt", run=TaskStatus.RUNNING.value))
            db.add(File(id=file_id, parent_id=owner, tenant_id=owner, created_by=owner, name="cancel.txt", location="cancel.txt", type="doc"))
            db.add(File2Document(id=uuid4().hex, file_id=file_id, document_id=doc_id))
            db.commit()
            doc = db.get(Document, doc_id)
            assert doc is not None
            queue_tasks(db, doc.to_dict(), "unused", "cancel.txt", 0)
            task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == doc_id))
            assert task_id
        env["task_ids"].append(task_id)
        env.update(kb_id=kb_id, doc_id=doc_id, document_task_id=task_id, queue=queue)
        yield env
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Task).where(sa.or_(Task.id.in_(env["task_ids"]), Task.doc_id == doc_id)))
            db.execute(sa.delete(Document).where(Document.id == doc_id))
            db.execute(sa.delete(File2Document).where(File2Document.document_id == doc_id))
            db.execute(sa.delete(File).where(File.id == file_id))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == kb_id))
            db.commit()
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.id.in_(env["task_ids"]))) == 0
            assert db.get(Document, doc_id) is None and db.get(Knowledgebase, kb_id) is None
            assert db.get(File, file_id) is None
        redis = REDIS_CONN.REDIS
        redis.delete(queue)
        assert not redis.exists(queue)
        print("task acceptance cleanup: Task/Document/KB rows and owned queue removed; parent verifies auth, Canvas, sessions, keys and listener")


def cancel(env: dict[str, Any], task_id: str, *, method: str = "post", principal: int = 0, api_key: bool = False) -> Any:
    path = f"/api/v1/tasks/{task_id}/cancel" if method == "post" else f"/api/v1/tasks/{task_id}"
    if method == "put":
        path = f"/v1/canvas/cancel/{task_id}"
    return env["client"].request(
        method,
        env["base"] + path,
        json={"action": "stop"} if method == "patch" else None,
        headers={"Authorization": f"Bearer {env['keys'][principal] if api_key else env['jwts'][principal]}"},
        timeout=30,
    )


def persistent_state(env: dict[str, Any], task_id: str) -> tuple[float, str | None, str, float, str | None]:
    with Session(env["engine"]) as db:
        task = db.get(Task, task_id)
        doc = db.get(Document, env["doc_id"])
        assert task is not None and doc is not None
        return task.progress, task.progress_msg, doc.run, doc.progress, doc.progress_msg


@pytest.mark.parametrize("method", ["post", "patch", "put"])
@pytest.mark.parametrize("api_key", [False, True])
def test_document_cancel_real_queue_http_and_late_worker(cancel_api: dict[str, Any], method: str, api_key: bool) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    with Session(env["engine"]) as db:
        db.execute(sa.update(Task).where(Task.id == task_id).values(progress_msg=None))
        db.execute(sa.update(Document).where(Document.id == env["doc_id"]).values(run=TaskStatus.SCHEDULE.value, progress_msg=None))
        db.commit()
    response = cancel(env, task_id, method=method, api_key=api_key)
    assert response.status_code == 200 and response.json() == {"retcode": 0, "retmsg": "success", "data": True}
    before = persistent_state(env, task_id)
    assert before[0] == -1 and before[2:4] == (TaskStatus.CANCEL.value, 0)
    assert before[1].count(TASK_CANCEL_MARKER) == before[4].count(TASK_CANCEL_MARKER) == 1
    assert REDIS_CONN.REDIS.get(f"{task_id}-cancel") and 0 < REDIS_CONN.REDIS.ttl(f"{task_id}-cancel") <= 86400
    assert has_canceled(task_id)
    assert cancel(env, task_id, method=method).json()["retcode"] == 0
    with Session(env["engine"]) as db:
        TaskService.update_progress(db, task_id, {"progress": 1, "progress_msg": "late success"})
        db.commit()
        assert TaskService.get_task(db, task_id) is None
        DocumentService.begin2parse(db, env["doc_id"])
        DocumentService.update_progress(db)
    assert persistent_state(env, task_id) == before


@pytest.mark.parametrize("role,status,allowed", [("owner", "1", True), ("normal", "1", True), ("admin", "1", True), ("invite", "1", False), ("normal", "0", False)])
def test_membership_is_current_and_joined(cancel_api: dict[str, Any], role: str, status: str, allowed: bool) -> None:
    env = cancel_api
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=uuid4().hex, user_id=env["owners"][1], tenant_id=env["owners"][0], role=role, status=status, invited_by=env["owners"][0]))
        db.commit()
    response = cancel(env, env["document_task_id"], principal=1)
    assert (response.json()["retcode"] == 0) is allowed
    assert bool(REDIS_CONN.REDIS.exists(f"{env['document_task_id']}-cancel")) is allowed
    assert (persistent_state(env, env["document_task_id"])[0] == -1) is allowed


@pytest.mark.parametrize("invalid", ["foreign", "orphan", "deleted_document", "deleted_dataset"])
def test_denied_never_writes(cancel_api: dict[str, Any], invalid: str) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    with Session(env["engine"]) as db:
        if invalid == "orphan":
            db.execute(sa.update(Task).where(Task.id == task_id).values(doc_id=uuid4().hex))
        elif invalid == "deleted_document":
            db.execute(sa.update(Document).where(Document.id == env["doc_id"]).values(status="0"))
        elif invalid == "deleted_dataset":
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb_id"]).values(status="0"))
        db.commit()
    before = persistent_state(env, task_id)
    response = cancel(env, task_id, principal=1 if invalid == "foreign" else 0)
    assert response.json()["retcode"] == 109 and response.json()["data"] is False
    assert not REDIS_CONN.REDIS.exists(f"{task_id}-cancel") and persistent_state(env, task_id) == before


@pytest.mark.parametrize("kind", ["graph", "raptor", "mindmap"])
def test_graph_task_trusted_creation_and_no_document_cancel(cancel_api: dict[str, Any], kind: str) -> None:
    env = cancel_api
    with Session(env["engine"]) as db:
        db.execute(sa.update(Document).where(Document.id == env["doc_id"]).values(run=TaskStatus.DONE.value, progress=1))
        db.commit()
        ok, result = run_index(db, env["owners"][0], env["kb_id"], kind)
        assert ok, result
        task_id = result["task_id"]
        env["task_ids"].append(task_id)
        task = db.get(Task, task_id)
        assert task.doc_id == "graph_raptor_x"
    before = persistent_state(env, task_id)
    assert cancel(env, task_id, principal=1).json()["retcode"] == 109
    assert not has_canceled(task_id)
    assert cancel(env, task_id).json()["retcode"] == 0
    after = persistent_state(env, task_id)
    assert after[0] == -1 and after[1].count(TASK_CANCEL_MARKER) == 1 and after[2:] == before[2:]
    assert has_canceled(task_id)


def test_http_validation_unknown_and_terminal(cancel_api: dict[str, Any]) -> None:
    env = cancel_api
    task_id = env["document_task_id"]
    with HTTPSession() as client:
        assert client.post(env["base"] + f"/api/v1/tasks/{task_id}/cancel", timeout=10).status_code == 401
    for body in [{}, {"action": "start"}, {"action": None}, {"action": "stop", "user_id": env["owners"][0]}]:
        assert env["client"].patch(env["base"] + f"/api/v1/tasks/{task_id}", json=body, timeout=10).status_code == 422
    assert cancel(env, "a" * 33).status_code == 422
    assert cancel(env, uuid4().hex).json()["retcode"] == 0
    assert env["client"].get(env["base"] + f"/api/v1/tasks/{task_id}", timeout=10).status_code == 405
    for progress in [-1, 1]:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Task).where(Task.id == task_id).values(progress=progress))
            db.commit()
        before = persistent_state(env, task_id)
        assert cancel(env, task_id).json()["retcode"] == 0
        assert persistent_state(env, task_id) == before and not has_canceled(task_id)


@pytest.mark.parametrize("failure", ["redis_false", "redis_exception", "sql"])
def test_cross_store_failure_is_honest_and_compensated(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    before = persistent_state(env, task_id)
    redis = REDIS_CONN.REDIS
    original_set = redis.set

    def failing_set(key: str, *args: Any, **kwargs: Any) -> Any:
        if key == f"{task_id}-cancel":
            if failure == "redis_exception":
                raise ConnectionError("test injected Redis failure")
            return False
        return original_set(key, *args, **kwargs)

    def failing_sql(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if "UPDATE usr_ai.t_ai_tasks" in statement:
            raise RuntimeError("test injected SQL failure")

    if failure.startswith("redis"):
        monkeypatch.setattr(redis, "set", failing_set)
    else:
        # The HTTP AsyncEngine uses separate connections to the same DB; apply
        # at the engine class for this owned test only, never a business DB.
        sa.event.listen(sa.engine.Engine, "before_cursor_execute", failing_sql)
    try:
        response = cancel(env, task_id)
        assert response.json()["retcode"] == 100 and response.json()["data"] is False
    finally:
        if failure == "sql":
            sa.event.remove(sa.engine.Engine, "before_cursor_execute", failing_sql)
    assert persistent_state(env, task_id) == before and not redis.exists(f"{task_id}-cancel")


def wait_dsl() -> dict[str, Any]:
    dsl = message_dsl("completed")
    dsl["components"]["begin"]["downstream"] = ["VariableAssigner:wait"]
    dsl["components"]["VariableAssigner:wait"] = {
        "obj": {"component_name": "VariableAssigner", "params": {"variables": []}},
        "upstream": ["begin"],
        "downstream": ["Message:answer"],
    }
    dsl["components"]["Message:answer"]["upstream"] = ["VariableAssigner:wait"]
    return dsl


@pytest.fixture
def wait_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    gate: dict[str, Any] = {"enabled": False, "entered": {}, "release": {}}

    async def controlled_wait(self: VariableAssigner, **kwargs: Any) -> None:
        task_id = self._canvas.task_id
        if gate["enabled"]:
            release = gate["release"].setdefault(task_id, threading.Event())
            gate["entered"].setdefault(task_id, threading.Event()).set()
            if not await asyncio.to_thread(release.wait, 20):
                raise RuntimeError("Test wait deadline exceeded")
        self._invoke(**kwargs)

    monkeypatch.setattr(VariableAssigner, "_invoke_async", controlled_wait, raising=False)
    yield gate
    for event in gate["release"].values():
        event.set()


@pytest.mark.parametrize("mode", ["draft", "published", "continued"])
def test_agent_run_bound_before_first_frame_cancel_and_sibling_isolation(cancel_api: dict[str, Any], wait_gate: dict[str, Any], mode: str, cancel_base: str | None = None) -> None:
    env = cancel_api

    def request_cancel(task_id: str, principal: int = 0) -> Any:
        if cancel_base is None:
            return cancel(env, task_id, principal=principal)
        return env["client"].post(cancel_base + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['keys'][principal]}"}, timeout=30)

    def business_code(response: Any) -> int:
        body = response.json()
        return body["retcode"] if "retcode" in body else body["code"]

    response = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"cancel {uuid4().hex}", "dsl": wait_dsl()}, timeout=30)
    assert response.json()["retcode"] == 0
    canvas_id = response.json()["data"]["id"]
    update(env, canvas_id, {"dsl": wait_dsl(), "release": True})
    before = read_state(env, canvas_id)
    replica = REDIS_CONN.REDIS.get(f"canvas:replica:{canvas_id}:{env['owners'][0]}:{env['owners'][0]}")
    payload: dict[str, Any] = {"agent_id": canvas_id, "query": "stop", "release": mode != "draft", "stream": True, "user_id": env["owners"][1]}
    if mode == "draft":
        payload["user_id"] = env["owners"][0]
    if mode == "continued":
        initial = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json={**payload, "query": "first"}, timeout=30)
        payload["session_id"] = sse_events(initial.text)[0]["session_id"]
    wait_gate["enabled"] = True
    # Keep a sibling active in the same Canvas, with a separate attempt ID.
    with HTTPSession() as first_client, HTTPSession() as sibling_client:
        first_client.headers.update(env["client"].headers)
        sibling_client.headers.update(env["client"].headers)
        response = first_client.post(env["base"] + "/api/v1/agents/chat/completion", json=payload, stream=True, timeout=30)
        iterator = response.iter_lines()
        first = json.loads(next(line for line in iterator if line.startswith(b"data:"))[5:])
        task_id = first["task_id"]
        assert task_id != first["message_id"] and read_binding(task_id)[1]["principal_id"] == env["owners"][0]
        assert wait_gate["entered"].setdefault(task_id, threading.Event()).wait(10)
        sibling = sibling_client.post(env["base"] + "/api/v1/agents/chat/completion", json={**payload, "query": "sibling", "session_id": None}, stream=True, timeout=30)
        sibling_iter = sibling.iter_lines()
        sibling_first = json.loads(next(line for line in sibling_iter if line.startswith(b"data:"))[5:])
        sibling_id = sibling_first["task_id"]
        assert sibling_id != task_id and not has_canceled(sibling_id)
        assert business_code(request_cancel(task_id, principal=1)) == 109
        assert not has_canceled(task_id)
        assert business_code(request_cancel(task_id)) == 0
        cancel_nonce = REDIS_CONN.REDIS.get(f"{task_id}-cancel")
        assert read_binding(task_id)[1]["state"] == "cancel_requested"
        assert not has_canceled(sibling_id) and read_binding(sibling_id)[1]["state"] == "active"
        wait_gate["release"][task_id].set()
        tail = [json.loads(line[5:]) for line in iterator if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]
        assert REDIS_CONN.REDIS.get(f"{task_id}-cancel") == cancel_nonce
        assert REDIS_CONN.REDIS.ttl(f"{task_id}-cancel") > 86000
        assert any(event.get("event") == "error" or event.get("code", 0) != 0 for event in tail)
        assert not any(event.get("event") == "message_end" for event in tail)
        assert wait_gate["entered"].setdefault(sibling_id, threading.Event()).wait(10)
        wait_gate["release"][sibling_id].set()
        sibling_tail = [json.loads(line[5:]) for line in sibling_iter if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]
        assert any(event.get("event") == "message_end" for event in sibling_tail)
        assert read_binding(sibling_id)[1]["state"] == "finished"
    with Session(env["engine"]) as db:
        assert db.get(Task, task_id) is None and db.get(Task, sibling_id) is None
        if mode != "draft":
            session_id = first["session_id"]
            session = db.get(API4Conversation, session_id)
            assert session and "canceled" in session.errors.lower()
            assert session.message[-1]["role"] == "user"
    assert read_state(env, canvas_id) == before
    if mode != "draft":
        assert REDIS_CONN.REDIS.get(f"canvas:replica:{canvas_id}:{env['owners'][0]}:{env['owners'][0]}") == replica


def test_dataflow_actual_enqueue_and_pipeline_cancel_observation(cancel_api: dict[str, Any]) -> None:
    env = cancel_api
    dsl = {"components": {"File": {"obj": {"component_name": "File", "params": {}}, "downstream": [], "upstream": []}}, "path": []}
    response = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"pipeline {uuid4().hex}", "dsl": dsl, "canvas_category": CanvasCategory.DataFlow}, timeout=30)
    assert response.json()["retcode"] == 0
    flow_id = response.json()["data"]["id"]
    response = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json={"agent_id": flow_id, "tenant_id": env["owners"][1]}, timeout=30)
    assert response.json()["retcode"] == 0, response.text
    task_id = response.json()["data"]["message_id"]
    env["task_ids"].append(task_id)
    binding = read_binding(task_id)[1]
    assert binding["principal_id"] == binding["tenant_id"] == env["owners"][0] and binding["resource_id"] == flow_id
    with Session(env["engine"]) as db:
        assert db.get(Task, task_id).doc_id == "dataflow_x"
    queue_messages = [json.loads(entry["message"]) for _, entry in REDIS_CONN.REDIS.xrange(env["queue"])]
    queued = next(message for message in queue_messages if message["id"] == task_id)
    assert queued["tenant_id"] == env["owners"][0] and queued["dataflow_id"] == flow_id
    assert cancel(env, task_id, principal=1).json()["retcode"] == 109
    assert cancel(env, task_id, method="patch").json()["retcode"] == 0
    pipeline = Pipeline(dsl, tenant_id=env["owners"][0], doc_id="dataflow_x", task_id=task_id, flow_id=flow_id)
    with pytest.raises(TaskCanceledException):
        pipeline.callback("File", 1, "late completion")
    with Session(env["engine"]) as db:
        assert db.get(Task, task_id).progress == -1


def test_runtime_expiry_and_finish_cas_are_noops(cancel_api: dict[str, Any]) -> None:
    env = cancel_api
    canvas_id = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"expiry {uuid4().hex}", "dsl": message_dsl("ok")}, timeout=30).json()["data"]["id"]
    task_id = uuid4().hex
    env["task_ids"].append(task_id)
    register_runtime(task_id, env["owners"][0], env["owners"][0], canvas_id, "agent")
    raw = read_binding(task_id)[0]
    finish_runtime(task_id)
    assert request_runtime_cancel(task_id, raw, uuid4().hex) is False
    assert not REDIS_CONN.REDIS.exists(f"{task_id}-cancel")
    assert cancel(env, task_id).json()["retcode"] == 0
    REDIS_CONN.REDIS.delete(binding_key(task_id))
    assert cancel(env, task_id).json()["retcode"] == 0 and not REDIS_CONN.REDIS.exists(f"{task_id}-cancel")


def test_same_listener_smoke(cancel_api: dict[str, Any]) -> None:
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": cancel_api["base"]}, text=True, capture_output=True, timeout=60)
    Path("/tmp/multirag-488-smoke.log").write_text(smoke.stdout + smoke.stderr)
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr


def test_simultaneous_cancel_is_idempotent(cancel_api: dict[str, Any]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env, task_id = cancel_api, cancel_api["document_task_id"]

    def submit() -> int:
        with HTTPSession() as client:
            response = client.post(env["base"] + f"/api/v1/tasks/{task_id}/cancel", headers={"Authorization": f"Bearer {env['jwts'][0]}"}, timeout=30)
            return response.json()["retcode"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: submit(), range(2))) == [0, 0]
    state = persistent_state(env, task_id)
    assert state[0] == -1 and state[1].count(TASK_CANCEL_MARKER) == state[4].count(TASK_CANCEL_MARKER) == 1


def test_cancel_wins_over_worker_that_already_read_task(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    read, release = threading.Event(), threading.Event()
    original = TaskService.get_by_id
    failures: list[BaseException] = []

    def delayed_read(db: Session, identifier: str) -> Any:
        task = original(db, identifier)
        if threading.current_thread().name == "late-progress" and identifier == task_id:
            read.set()
            assert release.wait(15)
        return task

    monkeypatch.setattr(TaskService, "get_by_id", delayed_read)

    def worker() -> None:
        try:
            with Session(env["engine"]) as db:
                TaskService.update_progress(db, task_id, {"progress": 1, "progress_msg": "late success after old read"})
                db.commit()
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=worker, name="late-progress")
    thread.start()
    try:
        assert read.wait(10)
        assert cancel(env, task_id).json()["retcode"] == 0
        before = persistent_state(env, task_id)
    finally:
        release.set()
        thread.join(15)
    assert not thread.is_alive() and not failures
    assert persistent_state(env, task_id) == before


def test_worker_set_progress_observes_cancel(cancel_api: dict[str, Any]) -> None:
    from core.svr.task_executor import set_progress

    env, task_id = cancel_api, cancel_api["document_task_id"]
    assert cancel(env, task_id).json()["retcode"] == 0
    before = persistent_state(env, task_id)
    with Session(env["engine"]) as db, pytest.raises(TaskCanceledException):
        set_progress(db, task_id, prog=1, msg="late worker completion")
    assert persistent_state(env, task_id) == before


@pytest.mark.parametrize("role", ["normal", "admin", "invite", "inactive"])
def test_dataflow_member_binding_uses_resource_owner(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, role: str) -> None:
    env = cancel_api
    allowed = role in {"normal", "admin"}
    caller = 1 if allowed else 2
    if allowed:
        join_member(env, monkeypatch, caller, UserTenantRole(role))
    else:
        denied_membership(env, monkeypatch, "invite" if role == "invite" else "inactive_normal")
    dsl = {"components": {"File": {"obj": {"component_name": "File", "params": {}}, "downstream": [], "upstream": []}}, "path": []}
    created = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"member pipeline {uuid4().hex}", "dsl": dsl, "canvas_category": CanvasCategory.DataFlow}, timeout=30)
    assert created.json()["retcode"] == 0
    flow_id = created.json()["data"]["id"]
    update(env, flow_id, {"permission": "team"})
    headers = {"Authorization": f"Bearer {env['jwts'][caller]}"}
    # The actual GET bootstraps the member's editor replica when authorized.
    env["client"].get(env["base"] + f"/api/v1/agents/{flow_id}", headers=headers, timeout=30)
    before_ids = set(env["task_ids"])
    response = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", headers=headers, json={"agent_id": flow_id}, timeout=30)
    if not allowed:
        assert response.json()["retcode"] != 0 and set(env["task_ids"]) == before_ids
        assert not any(json.loads(entry["message"]).get("dataflow_id") == flow_id for _, entry in REDIS_CONN.REDIS.xrange(env["queue"]))
        return
    assert response.json()["retcode"] == 0, response.text
    task_id = response.json()["data"]["message_id"]
    env["task_ids"].append(task_id)
    binding = read_binding(task_id)[1]
    assert binding["principal_id"] == env["owners"][caller] and binding["tenant_id"] == env["owners"][0] and binding["resource_id"] == flow_id
    queued = next(json.loads(entry["message"]) for _, entry in REDIS_CONN.REDIS.xrange(env["queue"]) if json.loads(entry["message"])["id"] == task_id)
    assert queued["tenant_id"] == env["owners"][0]
    assert cancel(env, task_id, principal=caller).json()["retcode"] == 0
    pipeline = Pipeline(dsl, tenant_id=env["owners"][0], doc_id="dataflow_x", task_id=task_id, flow_id=flow_id)
    with pytest.raises(TaskCanceledException):
        pipeline.callback("File", 1, "member cancellation observed")


@pytest.mark.parametrize("published", [False, True])
def test_binding_failure_prevents_first_frame(cancel_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, published: bool) -> None:
    from api.db.services import task_cancellation_service

    env = cancel_api
    canvas_id = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"binding failure {uuid4().hex}", "dsl": message_dsl("must not run")}, timeout=30).json()["data"]["id"]
    update(env, canvas_id, {"dsl": message_dsl("must not run"), "release": True})
    before = read_state(env, canvas_id)
    attempts: list[str] = []

    def fail_registration(task_id: str, *args: Any, **kwargs: Any) -> None:
        attempts.append(task_id)
        raise ConnectionError("test injected registration failure")

    monkeypatch.setattr(task_cancellation_service, "register_runtime", fail_registration)
    response = env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json={"agent_id": canvas_id, "release": published, "stream": True}, timeout=30)
    assert response.status_code == 500 and "text/event-stream" not in response.headers.get("Content-Type", "")
    assert len(attempts) == 1 and not REDIS_CONN.REDIS.exists(binding_key(attempts[0]), f"{attempts[0]}-cancel")
    with Session(env["engine"]) as db:
        assert not db.scalar(sa.select(API4Conversation.id).where(API4Conversation.dialog_id == canvas_id))
    assert read_state(env, canvas_id) == before


def test_failed_task_without_cancel_marker_can_recover(cancel_api: dict[str, Any]) -> None:
    env, task_id = cancel_api, cancel_api["document_task_id"]
    with Session(env["engine"]) as db:
        db.execute(sa.update(Task).where(Task.id == task_id).values(progress=-1, progress_msg="ordinary retryable failure"))
        db.commit()
        TaskService.update_progress(db, task_id, {"progress": 1, "progress_msg": "retry succeeded"})
        db.commit()
    assert persistent_state(env, task_id)[0] == 1
