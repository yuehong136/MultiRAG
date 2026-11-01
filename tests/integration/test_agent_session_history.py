"""Fresh sessions clear template history; continued sessions retain their turns."""

import json
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, APIToken
from tests.support.agent_update_release import message_dsl, read_state, sse_events
from tests.support.agent_update_release import release_api as release_api


def stored_session(env: dict[str, Any], agent_id: str, session_id: str | None = None) -> tuple[str, dict[str, Any]]:
    with Session(env["engine"]) as reader:
        statement = select(API4Conversation).where(API4Conversation.dialog_id == agent_id)
        if session_id is not None:
            statement = statement.where(API4Conversation.id == session_id)
        conversation = reader.scalar(statement)
        assert conversation is not None and not conversation.errors
        dsl = json.loads(conversation.dsl) if isinstance(conversation.dsl, str) else conversation.dsl
        return conversation.id, dsl


@pytest.mark.parametrize("surface", ["explicit", "release", "openai", "beta"])
@pytest.mark.parametrize("stream", [False, True])
def test_http_fresh_and_continued_session_history(release_api: dict[str, Any], surface: str, stream: bool) -> None:
    env = release_api
    dsl = message_dsl("Answer:{sys.query}")
    dsl["history"] = [["user", "private template question"], ["assistant", {"content": "private template answer"}]]
    dsl["globals"] = {"sys.history": ["private template history"], "sys.query": "stale query", "sys.conversation_turns": 7}
    dsl["path"] = ["Message:answer"]
    created = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "History boundary", "dsl": dsl}, timeout=30)
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    agent_id = created.json()["data"]["id"]
    payload: dict[str, Any] = {"agent_id": agent_id, "query": "first", "stream": stream}
    headers = {"Authorization": f"Bearer {env['jwts'][0]}"}
    path = "/api/v1/agents/chat/completions"
    session_id = None
    if surface == "explicit":
        response = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        session_id = response.json()["data"]["id"]
        _, fresh = stored_session(env, agent_id, session_id)
        assert fresh["history"] == [] and fresh["globals"]["sys.history"] == [] and fresh["path"] == []
        payload["session_id"] = session_id
    elif surface == "release":
        response = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", json={"release": True}, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        payload["release"] = True
    elif surface == "openai":
        payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "first"}]})
    else:
        path = f"/api/v1/agentbots/{agent_id}/completions"
        beta = uuid4().hex
        with Session(env["engine"]) as db:
            db.execute(update(APIToken).where(APIToken.token == env["keys"][0]).values(beta=beta))
            db.commit()
        headers = {"Authorization": f"Bearer {beta}"}
        payload.pop("agent_id")
    before = read_state(env, agent_id)

    for turn, query in enumerate(["first", "second"], start=1):
        payload["query"] = query
        if surface == "openai":
            payload["messages"] = [{"role": "user", "content": query}]
        response = env["client"].post(env["base"] + path, headers=headers, json=payload, timeout=30)
        assert response.status_code == 200, response.text
        if stream:
            events = sse_events(response.text)
            assert events and all(event.get("code", 0) == 0 and event.get("event") != "error" for event in events), response.text
        else:
            assert response.json().get("retcode", 0) == 0, response.text
        session_id, saved = stored_session(env, agent_id, session_id)
        assert "private template" not in json.dumps(saved["history"])
        assert "private template" not in json.dumps(saved["globals"]["sys.history"])
        assert [value for role, value in saved["history"] if role == "user"] == ["first", "second"][:turn]
        assert saved["globals"]["sys.conversation_turns"] == turn
        payload["session_id"] = session_id
    assert read_state(env, agent_id) == before
