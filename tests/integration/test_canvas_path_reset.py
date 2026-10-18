"""Authenticated HTTP with real Canvas, PostgreSQL sessions and Redis replicas."""

import json
from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy.orm import Session

from agent.component.message import Message
from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db.db_models import API4Conversation
from tests.support.agent_update_release import read_state, sse_events
from tests.support.agent_update_release import release_api as release_api
from tests.support.canvas_path_reset import paused_dsl


def saved_dsl(env: dict[str, Any], agent_id: str, session_id: str | None, caller: int = 0) -> dict[str, Any]:
    if session_id:
        with Session(env["engine"]) as db:
            conversation = db.get(API4Conversation, session_id)
            assert conversation is not None and not conversation.errors
            value = conversation.dsl
    else:
        replica = CanvasReplicaService.load_for_run(agent_id, env["owners"][0], env["owners"][caller])
        assert replica is not None
        value = replica["dsl"]
    return json.loads(value) if isinstance(value, str) else value


def output(dsl: dict[str, Any], node: str, key: str = "content") -> Any:
    return dsl["components"][node]["obj"]["params"]["outputs"].get(key, {}).get("value")


def run(env: dict[str, Any], request: dict[str, Any], *, caller: int = 0) -> dict[str, Any]:
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", headers={"Authorization": f"Bearer {env['jwts'][caller]}"}, json=request, timeout=30)
    assert response.status_code == 200, response.text
    if request.get("stream"):
        events = sse_events(response.text)
        assert response.text.endswith("data:[DONE]\n\n"), response.text
        assert not any(event.get("event") == "error" or event.get("code", 0) != 0 for event in events), response.text
        terminal = [event for event in events if event.get("event") in {"user_inputs", "workflow_finished"}]
        assert len(terminal) == 1, response.text
        return terminal[0]
    body = response.json()
    assert body["retcode"] == 0, response.text
    return body["data"]


@pytest.mark.parametrize("surface", ["session", "runtime"])
@pytest.mark.parametrize("stream", [False, True])
def test_http_two_pauses_preserve_durable_outputs_and_isolation(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, surface: str, stream: bool) -> None:
    env = release_api
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Two pauses", "dsl": paused_dsl()}, timeout=30)
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    agent_id = created.json()["data"]["id"]
    session_id = None
    other_session_id = None
    if surface == "session":
        sessions = []
        for _ in range(2):
            response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
            sessions.append(response.json()["data"]["id"])
        session_id, other_session_id = sessions
        assert session_id != other_session_id
        fresh = saved_dsl(env, agent_id, other_session_id)
        assert fresh["path"] == [] and fresh["history"] == []
        assert output(fresh, "Message:unvisited") is None
    before = read_state(env, agent_id)
    calls: list[tuple[str, str]] = []
    original = Message._invoke

    def track(self: Message, **kwargs: Any) -> None:
        calls.append((self._id, self._canvas.globals["sys.query"]))
        original(self, **kwargs)

    monkeypatch.setattr(Message, "_invoke", track)
    request: dict[str, Any] = {"agent_id": agent_id, "query": "owner email", "stream": stream}
    if session_id:
        request["session_id"] = session_id
    first = run(env, request)
    assert first["event"] == "user_inputs" and first["data"]["tips"] == "Approve Draft:owner email"
    first_state = saved_dsl(env, agent_id, session_id)
    assert first_state["path"] == ["UserFillUp:first"] and output(first_state, "Message:draft") == "Draft:owner email"

    denied = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", headers={"Authorization": f"Bearer {env['foreign_jwt']}"}, json=request, timeout=30)
    assert denied.status_code == (403 if session_id else 200) and denied.json()["retcode"] == 103, denied.text
    assert saved_dsl(env, agent_id, session_id) == first_state

    # Interleave another session, or another owner-scoped runtime user ID.
    other_request = {**request, "query": "other email"}
    if other_session_id:
        other_request["session_id"] = other_session_id
    else:
        other_request["user_id"] = env["owners"][1]
    other = run(env, other_request)
    assert other["event"] == "user_inputs" and other["data"]["tips"] == "Approve Draft:other email"
    other_state = saved_dsl(env, agent_id, other_session_id, caller=0 if session_id else 1)
    assert output(other_state, "Message:draft") == "Draft:other email"
    assert saved_dsl(env, agent_id, session_id) == first_state

    second = run(env, {**request, "query": "approve", "inputs": {"approval": {"type": "line", "value": "yes"}}})
    assert second["event"] == "user_inputs" and second["data"]["tips"] == "Deliver Draft:owner email/yes"
    second_state = saved_dsl(env, agent_id, session_id)
    assert second_state["path"] == ["UserFillUp:second"]
    assert output(second_state, "Message:draft") == "Draft:owner email" and output(second_state, "UserFillUp:first", "approval") == "yes"
    final = run(env, {**request, "query": "deliver", "inputs": {"delivery": {"type": "line", "value": "sent"}}})
    assert final["event"] == ("message_end" if session_id and not stream else "workflow_finished")
    final_state = saved_dsl(env, agent_id, session_id)
    expected = "Draft:owner email/yes/Draft:owner email/yes/sent"
    assert output(final_state, "Message:answer") == expected
    assert output(final_state, "Message:unvisited") == output(first_state, "Message:unvisited")
    assert final_state["globals"]["sys.conversation_turns"] == 3
    assert calls == [("Message:draft", "owner email"), ("Message:draft", "other email"), ("Message:approved", "approve"), ("Message:answer", "deliver")]
    assert saved_dsl(env, agent_id, other_session_id, caller=0 if session_id else 1) == other_state
    assert read_state(env, agent_id) == before

    # Debug and explicit draft reset keep their existing full-reset scope.
    debug = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/components/Message:draft/debug", json={"params": {}}, timeout=30)
    assert debug.status_code == 200 and debug.json()["retcode"] == 0, debug.text
    assert saved_dsl(env, agent_id, session_id) == final_state
    reset = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/reset", json={}, timeout=30)
    assert reset.status_code == 200 and reset.json()["retcode"] == 0, reset.text
    reset_state = read_state(env, agent_id)["dsl"]
    assert reset_state["path"] == [] and output(reset_state, "Message:unvisited") is None
    assert saved_dsl(env, agent_id, session_id) == final_state
    assert saved_dsl(env, agent_id, other_session_id, caller=0 if session_id else 1) == other_state
