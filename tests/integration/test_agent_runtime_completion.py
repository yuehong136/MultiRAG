"""Actual components, auth, HTTP, SQL and Redis; explicit result/fault boundaries only."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component.message import Message
from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db.db_models import API4Conversation
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import read_state, sse_events
from tests.support.agent_update_release import release_api as release_api


def make_agent(env: dict[str, Any], *, empty_content: bool = False) -> str:
    from tests.support.agent_update_release import message_dsl

    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Runtime result", "dsl": message_dsl("" if empty_content else "runtime answer")}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    return response.json()["data"]["id"]


@pytest.mark.parametrize("surface", ["runtime", "session"])
@pytest.mark.parametrize("stream", [False, True])
def test_complete_results_and_durable_state(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, surface: str, stream: bool) -> None:
    env = release_api
    agent_id = make_agent(env)
    owner = env["owners"][0]
    before = read_state(env, agent_id)
    replica_key = CanvasReplicaService._replica_key(agent_id, owner, owner)
    replica_before = REDIS_CONN.get(replica_key)
    reference = {"chunks": {"0": {"chunk_id": "controlled-chunk", "content_with_weight": "controlled reference"}}, "doc_aggs": [{"doc_id": "controlled-doc"}]}
    original = Message._invoke

    def model_outputs(self: Message, **kwargs: Any) -> None:
        original(self, **kwargs)
        self.set_output("structured", {"count": 2})
        self._canvas.retrieval.append(copy.deepcopy(reference))

    monkeypatch.setattr(Message, "_invoke", model_outputs)
    request = {"agent_id": agent_id, "query": "runtime question", "stream": stream, "return_trace": True}
    if surface == "session":
        session = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30).json()
        assert session["retcode"] == 0
        request["session_id"] = session["data"]["id"]
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json=request, timeout=30)
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        assert response.text.endswith("data:[DONE]\n\n")
        assert all(event.get("event") != "error" for event in events)
        assert "".join(event["data"].get("content", "") for event in events if event.get("event") == "message") == "runtime answer"
        assert any(event.get("data", {}).get("reference") == reference for event in events)
        node = next(event for event in events if event.get("event") == "node_finished" and event["data"]["component_id"] == "Message:answer")
        assert node["data"]["outputs"]["structured"] == {"count": 2}
        assert len(node["data"]["trace"]) == 2
    else:
        assert response.headers["content-type"].startswith("application/json") and response.json()["retcode"] == 0, response.text
        data = response.json()["data"]["data"]
        assert data["content"] == "runtime answer" and data["reference"] == reference
        assert data["structured"] == {"Message:answer": {"count": 2}}
        assert len(data["trace"]) == 2
    assert read_state(env, agent_id) == before
    if surface == "runtime":
        replica = CanvasReplicaService.load_for_run(agent_id, owner, owner)
        assert replica["dsl"]["globals"]["sys.query"] == "runtime question"
        assert replica["dsl"]["components"]["Message:answer"]["obj"]["params"]["outputs"]["structured"]["value"] == {"count": 2}
    else:
        assert REDIS_CONN.get(replica_key) == replica_before
        with Session(env["engine"]) as db:
            conversation = db.get(API4Conversation, request["session_id"])
            assert conversation is not None and conversation.message[-1]["content"] == "runtime answer"
            references = conversation.reference
            if isinstance(references, dict):
                references = [references] if "chunks" in references else [value for _, value in sorted(references.items(), key=lambda item: int(item[0]))]
            assert references[-1] == reference
        saved = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/sessions/{request['session_id']}", timeout=30).json()
        assert saved["retcode"] == 0 and saved["data"]["messages"][-1]["reference"][0]["citation_id"] == "0"
        first_reference = copy.deepcopy(reference)
        reference.clear()
        reference.update({"chunks": {"1": {"chunk_id": "second-chunk", "content_with_weight": "second reference"}}, "doc_aggs": []})
        second = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={**request, "query": "second question", "stream": False}, timeout=30)
        assert second.json()["retcode"] == 0
        with Session(env["engine"]) as db:
            conversation = db.get(API4Conversation, request["session_id"])
            assert conversation.reference == [first_reference, reference]
        saved = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/sessions/{request['session_id']}", timeout=30).json()
        assistant = [message for message in saved["data"]["messages"][1:] if message["role"] == "assistant"]
        assert assistant[0]["reference"][0]["id"] == "controlled-chunk" and assistant[1]["reference"][0]["id"] == "second-chunk"


@pytest.mark.parametrize("failure", ["exception", "event", "empty", "empty_output", "cancel", "write", "lock"])
@pytest.mark.parametrize("stream", [False, True])
def test_failed_runtime_never_commits_or_emits_success(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str, stream: bool) -> None:
    env = release_api
    agent_id = make_agent(env, empty_content=failure == "empty_output")
    owner = env["owners"][0]
    key = CanvasReplicaService._replica_key(agent_id, owner, owner)
    before, replica_before = read_state(env, agent_id), REDIS_CONN.get(key)
    original = Canvas.run

    async def faulty_run(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        if failure == "empty":
            return
        async for event in original(self, **kwargs):
            yield event
            if event["event"] == "message" and failure == "exception":
                raise RuntimeError("controlled execution failure")
        if failure == "event":
            yield {"event": "error", "data": {"error": "controlled late failure"}}
        if failure == "cancel":
            import asyncio

            cancelled = await asyncio.to_thread(env["client"].post, f"{env['base']}/api/v1/tasks/{self.task_id}/cancel", timeout=30)
            assert cancelled.status_code == 200 and cancelled.json().get("retcode", cancelled.json().get("code")) == 0, cancelled.text

    monkeypatch.setattr(Canvas, "run", faulty_run)
    if failure == "write":
        original_write = REDIS_CONN.set_obj
        monkeypatch.setattr(REDIS_CONN, "set_obj", lambda target, *args, **kwargs: False if target == key else original_write(target, *args, **kwargs))
    if failure == "lock":
        monkeypatch.setattr(CanvasReplicaService, "_acquire_lock_with_retry", lambda *args: None)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "query": "failure query", "stream": stream}, timeout=30)
    if stream:
        events = sse_events(response.text)
        assert any(event.get("code", 0) != 0 for event in events)
        assert not any(event.get("event") in {"message_end", "workflow_finished"} for event in events)
        assert "[DONE]" not in response.text
    else:
        assert response.json()["retcode"] != 0 and not response.json().get("data")
    assert REDIS_CONN.get(key) == replica_before and read_state(env, agent_id) == before


@pytest.mark.parametrize("stream", [False, True])
def test_empty_session_records_failure_without_successful_assistant(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    env = release_api
    agent_id = make_agent(env)
    session = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30).json()
    assert session["retcode"] == 0
    session_id = session["data"]["id"]

    async def empty(self: Canvas, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        if False:
            yield {}

    monkeypatch.setattr(Canvas, "run", empty)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "session_id": session_id, "query": "empty session", "stream": stream}, timeout=30)
    if stream:
        assert any(event.get("event") == "error" for event in sse_events(response.text)) and "[DONE]" not in response.text
    else:
        assert response.json()["retcode"] != 0
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, session_id)
        assert conversation is not None and conversation.errors
        assert conversation.message[-1]["role"] == "user" and conversation.message[-1]["content"] == "empty session"


@pytest.mark.parametrize("surface", ["runtime", "session"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_user_fill_pause_and_resume(release_api: dict[str, Any], surface: str, stream: bool) -> None:
    from tests.support.agent_update_release import message_dsl

    env = release_api
    dsl = message_dsl("Form complete")
    dsl["components"]["begin"]["downstream"] = ["UserFillUp:form"]
    dsl["components"]["Message:answer"]["upstream"] = ["UserFillUp:form"]
    dsl["components"]["UserFillUp:form"] = {
        "obj": {"component_name": "UserFillUp", "params": {"inputs": {"name": {"type": "line", "name": "Name", "value": None}}, "tips": "Enter a name"}},
        "upstream": ["begin"],
        "downstream": ["Message:answer"],
    }
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Real form", "dsl": dsl}, timeout=30).json()
    assert created["retcode"] == 0
    agent_id = created["data"]["id"]
    request: dict[str, Any] = {"agent_id": agent_id, "query": "start form", "stream": stream}
    if surface == "session":
        session = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30).json()
        assert session["retcode"] == 0
        request["session_id"] = session["data"]["id"]
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json=request, timeout=30)
    if stream:
        events = sse_events(response.text)
        form = next(event for event in events if event.get("event") == "user_inputs")
        assert "[DONE]" in response.text and not any(event.get("event") == "error" for event in events)
    else:
        assert response.json()["retcode"] == 0, response.text
        form = response.json()["data"]
    assert form["event"] == "user_inputs" and form["data"]["inputs"]["name"]["name"] == "Name"
    if surface == "session":
        with Session(env["engine"]) as db:
            stored = db.get(API4Conversation, request["session_id"]).dsl
    else:
        stored = CanvasReplicaService.load_for_run(agent_id, env["owners"][0], env["owners"][0])["dsl"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored["path"][0] == "UserFillUp:form"
    resumed = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions", json={**request, "query": "form submitted", "stream": False, "inputs": {"name": {"type": "line", "value": "Alice"}}}, timeout=30
    )
    assert resumed.json()["retcode"] == 0 and resumed.json()["data"]["data"]["content"] == "Form complete", resumed.text


@pytest.mark.parametrize("failure", ["read", "corruption"])
def test_replica_read_failure_is_not_missing_state(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = release_api
    agent_id = make_agent(env)
    owner = env["owners"][0]
    key = CanvasReplicaService._replica_key(agent_id, owner, owner)
    original_get = REDIS_CONN.REDIS.get
    if failure == "corruption":
        assert REDIS_CONN.REDIS.set(key, "invalid-json", ex=60)
    before = original_get(key)
    tasks = len(env["task_ids"])

    def unavailable(target: str, *args: Any, **kwargs: Any) -> Any:
        if target == key:
            raise ConnectionError("controlled replica read failure")
        return original_get(target, *args, **kwargs)

    if failure == "read":
        monkeypatch.setattr(REDIS_CONN.REDIS, "get", unavailable)
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "query": "must not bootstrap", "stream": False}, timeout=30)
    assert response.json()["retcode"] != 0 and original_get(key) == before
    assert len(env["task_ids"]) == tasks
