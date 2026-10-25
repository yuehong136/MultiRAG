"""Draft runtime logs survive editor changes, failures and disconnected delivery."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component.message import Message
from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db.db_models import AgentExecutionOrigin, API4Conversation
from api.db.services.agent_execution_service import snapshot_digest
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import message_dsl, read_state, sse_events, update
from tests.support.agent_update_release import release_api as release_api
from tests.support.web_consumer import tsx_runner


def create_draft(env: dict[str, Any]) -> str:
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Draft session log", "dsl": message_dsl("SQL draft")}, timeout=30)
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    agent_id = created.json()["data"]["id"]
    owner = env["owners"][0]
    assert CanvasReplicaService.replace_for_set(agent_id, owner, owner, message_dsl("runtime snapshot"))
    return agent_id


@pytest.mark.parametrize("stream", [False, True])
def test_runtime_log_resumes_original_snapshot_and_retains_outputs(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env = release_api
    agent_id = create_draft(env)
    owner = env["owners"][0]
    reference = {"chunks": {"0": {"id": "draft-reference", "content": "reference text"}}, "doc_aggs": []}
    original = Message._invoke

    def outputs(self: Message, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.set_output("structured", {"count": 0, "approved": False})
        self._canvas.retrieval.append(copy.deepcopy(reference))

    monkeypatch.setattr(Message, "_invoke", outputs)
    before = read_state(env, agent_id)
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "name": "Recorded draft", "query": "first", "stream": stream, "return_trace": True}, timeout=30
    )
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        assert response.text.endswith("data:[DONE]\n\n")
        session_id = events[0]["session_id"]
        assert all(item["session_id"] == session_id for item in events)
    else:
        assert response.json()["retcode"] == 0, response.text
        session_id = response.json()["data"]["session_id"]
        assert response.json()["data"]["data"]["content"] == "runtime snapshot"
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, session_id)
        origin = db.get(AgentExecutionOrigin, session_id)
        assert conversation is not None and origin is not None
        assert (conversation.user_id, conversation.exp_user_id, conversation.source, conversation.name, conversation.round) == (owner, owner, "agent", "Recorded draft", 1)
        assert [item["role"] for item in conversation.message] == ["user", "assistant"]
        assert conversation.message[-1]["content"] == "runtime snapshot"
        assert conversation.message[-1]["structured"] == {"Message:answer": {"count": 0, "approved": False}}
        assert len(conversation.message[-1]["trace"]) == 2
        assert conversation.reference == [reference]
        assert origin.execution_mode == "draft" and origin.agent_revision_id is None and origin.platform_user_id == owner
        assert origin.snapshot_digest == snapshot_digest(origin.snapshot_dsl)
        assert origin.snapshot_dsl["components"]["Message:answer"]["obj"]["params"]["content"] == ["runtime snapshot"]
        initial_origin = origin.to_dict()
    assert read_state(env, agent_id) == before
    saved = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/sessions/{session_id}", timeout=30).json()
    assert saved["retcode"] == 0 and "message" not in saved["data"]
    assert saved["data"]["messages"][-1]["reference"][0]["id"] == "draft-reference"
    listed = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/sessions", timeout=30).json()
    assert listed["retcode"] == 0 and session_id in {item["id"] for item in listed["data"]["sessions"]}
    update(env, agent_id, {"dsl": message_dsl("new publication"), "release": True})
    replica_before = REDIS_CONN.get(CanvasReplicaService._replica_key(agent_id, owner, owner))
    resumed = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions",
        json={"agent_id": agent_id, "session_id": session_id, "query": "second", "stream": False, "return_trace": True, "release": True, "user_id": "forged", "run_context": {"tenant_id": "forged"}},
        timeout=30,
    )
    assert resumed.status_code == 200 and resumed.json()["retcode"] == 0, resumed.text
    assert resumed.json()["data"]["session_id"] == session_id and resumed.json()["data"]["data"]["content"] == "runtime snapshot"
    with Session(env["engine"]) as db:
        assert db.get(AgentExecutionOrigin, session_id).to_dict() == initial_origin
        conversation = db.get(API4Conversation, session_id)
        assert conversation.round == 2 and len(conversation.message) == 4 and conversation.reference == [reference, reference]
        assert conversation.message[-1]["structured"] == {"Message:answer": {"count": 0, "approved": False}}
        assert len(conversation.message[-1]["trace"]) == 2
    assert REDIS_CONN.get(CanvasReplicaService._replica_key(agent_id, owner, owner)) == replica_before


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["empty", "exception", "event", "sql", "replica", "cancel"])
def test_runtime_failure_keeps_recoverable_session_without_success(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool, failure: str) -> None:
    env = release_api
    agent_id = create_draft(env)
    owner = env["owners"][0]
    key = CanvasReplicaService._replica_key(agent_id, owner, owner)
    replica_before = REDIS_CONN.get(key)
    before = read_state(env, agent_id)
    original = Canvas.run
    commit_state = {"ready": False, "failed": False}

    async def faulty_run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        if failure == "empty":
            return
        async for item in original(self, **kwargs):
            yield item
            if failure == "exception" and item.get("event") == "message":
                raise RuntimeError("controlled failure")
        if failure == "event":
            yield {"event": "error", "data": {"error": "controlled error event"}}
        if failure == "sql":
            commit_state["ready"] = True
        if failure == "cancel":
            cancelled = await asyncio.to_thread(env["client"].post, f"{env['base']}/api/v1/tasks/{self.task_id}/cancel", timeout=30)
            assert cancelled.status_code == 200 and cancelled.json()["retcode"] == 0, cancelled.text

    def fail_commit(session: Session) -> None:
        if commit_state["ready"] and not commit_state["failed"]:
            commit_state["failed"] = True
            raise RuntimeError("controlled SQL commit failure")

    monkeypatch.setattr(Canvas, "run", faulty_run)
    if failure == "replica":
        original_write = REDIS_CONN.set_obj
        monkeypatch.setattr(REDIS_CONN, "set_obj", lambda target, *args, **kwargs: False if target == key else original_write(target, *args, **kwargs))
    event.listen(Session, "before_commit", fail_commit)
    try:
        response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "query": "attempt", "stream": stream}, timeout=30)
    finally:
        event.remove(Session, "before_commit", fail_commit)
    if stream:
        events = sse_events(response.text)
        error = next(item for item in events if item.get("event") == "error")
        session_id = error["session_id"]
        assert not any(item.get("event") in {"workflow_finished", "message_end"} for item in events) and "[DONE]" not in response.text
    else:
        assert response.json()["retcode"] != 0 and response.json()["data"] is False, response.text
        session_id = response.json()["session_id"]
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, session_id)
        assert conversation is not None and conversation.source == "agent" and conversation.round == 1
        assert conversation.errors and conversation.reference == []
        assert [item["role"] for item in conversation.message] == ["user"]
        assert conversation.message[0]["content"] == "attempt"
        assert db.get(AgentExecutionOrigin, session_id).platform_user_id == owner
        assert not any(row[0] == "assistant" for row in conversation.dsl["history"])
    assert REDIS_CONN.get(key) == replica_before and read_state(env, agent_id) == before
    if failure == "sql":
        assert commit_state["failed"]
    # The id is usable even when no successful event was produced.
    monkeypatch.setattr(Canvas, "run", original)
    retry = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "session_id": session_id, "query": "retry", "stream": False}, timeout=30)
    assert retry.status_code == 200 and retry.json()["retcode"] == 0, retry.text
    assert retry.json()["data"]["data"]["content"] == "runtime snapshot"


@pytest.mark.parametrize("stream", [False, True])
def test_runtime_log_preserves_thinking_markers_on_creation_and_resume(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env = release_api
    agent_id = create_draft(env)
    original = Canvas.run

    async def reasoning_run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"event": "message", "data": {"content": "", "start_to_think": True}}
        yield {"event": "message", "data": {"content": "controlled reasoning"}}
        yield {"event": "message", "data": {"content": "", "end_to_think": True}}
        async for item in original(self, **kwargs):
            yield item

    monkeypatch.setattr(Canvas, "run", reasoning_run)
    session_id = None
    for query in ("initial reasoning", "continued reasoning"):
        response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "session_id": session_id, "query": query, "stream": stream}, timeout=30)
        if stream:
            assert response.text.endswith("data:[DONE]\n\n"), response.text
            received = sse_events(response.text)[0]["session_id"]
        else:
            assert response.json()["retcode"] == 0, response.text
            received = response.json()["data"]["session_id"]
        if session_id is not None:
            assert received == session_id
        session_id = received
        with Session(env["engine"]) as db:
            conversation = db.get(API4Conversation, session_id)
            assert conversation is not None and conversation.message[-1]["content"] == "<think>controlled reasoning</think>runtime snapshot"
        saved = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/sessions/{session_id}", timeout=30).json()
        assert saved["retcode"] == 0 and saved["data"]["messages"][-1]["content"] == "<think>controlled reasoning</think>runtime snapshot"


def test_runtime_disconnect_persists_failed_attempt(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    agent_id = create_draft(env)
    original = Canvas.run

    async def wait_after_message(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async for item in original(self, **kwargs):
            yield item
            if item.get("event") == "message":
                await asyncio.sleep(30)

    monkeypatch.setattr(Canvas, "run", wait_after_message)
    with env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "query": "disconnect", "stream": True}, stream=True, timeout=30) as response:
        for line in response.iter_lines(chunk_size=1):
            if line.startswith(b"data:") and json.loads(line[5:]).get("event") == "message":
                session_id = json.loads(line[5:])["session_id"]
                break
        else:
            pytest.fail("No message received before disconnect")
    deadline = time.monotonic() + 10
    while True:
        with Session(env["engine"]) as db:
            conversation = db.scalar(select(API4Conversation).where(API4Conversation.id == session_id))
            if conversation is not None and conversation.errors:
                assert "disconnect" in conversation.errors
                assert [item["role"] for item in conversation.message] == ["user"] and conversation.reference == [] and conversation.round == 1
                break
        assert time.monotonic() < deadline, "Disconnected runtime did not persist its failure"
        time.sleep(0.05)


@pytest.mark.external_consumer
def test_real_web_draft_log_and_resume(release_api: dict[str, Any]) -> None:
    env = release_api
    agent_id = create_draft(env)
    root = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    runner = tsx_runner(root)
    assert runner.is_file(), "An installed, fixed Web checkout is required"
    completed = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(Path(__file__).resolve().parents[1] / "support/agent_draft_web_consumer.mts")],
        cwd=root,
        env={**os.environ, "AGENT_DRAFT_BASE": env["base"], "AGENT_DRAFT_ID": agent_id, "AGENT_DRAFT_TOKEN": env["jwts"][0]},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["passed"]
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, result["session_id"])
        assert conversation is not None and conversation.round == 2
        assert [item["content"] for item in conversation.message if item["role"] == "user"] == ["Web draft first", "Web draft continued"]
