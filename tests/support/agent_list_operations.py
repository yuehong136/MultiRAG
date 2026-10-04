"""Real Begin/ListOperations/Message HTTP runs and SQL/Redis DSL boundaries.

The shared fixture supplies scratch auth/SQL, owned Redis cleanup and a real
listener. No component, model/provider, business response or storage is faked.
"""

import json
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from requests import Response
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation
from tests.support.agent_update_release import release_api as release_api
from tests.support.agent_update_release import sse_events

_ITEMS = list("abcde")

_NODE = "ListOperations:list"


def list_dsl(params: dict[str, Any], *, env_input: bool = False, items: list[Any] | None = None) -> dict[str, Any]:
    dsl: dict[str, Any] = {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {"prologue": "List test", "inputs": {"items": {"type": "array", "optional": True}}}}, "upstream": [], "downstream": [_NODE]},
            _NODE: {
                "obj": {"component_name": "ListOperations", "params": {"query": "{env.items}" if env_input else "{begin@items}", **params}},
                "upstream": ["begin"],
                "downstream": ["Message:answer"],
            },
            "Message:answer": {"obj": {"component_name": "Message", "params": {"content": ["{ListOperations:list@result}"]}}, "upstream": [_NODE], "downstream": []},
        },
        "path": [],
        "history": [],
        "retrieval": [],
    }
    if env_input:
        dsl["variables"] = {"items": {"type": "array<string>", "value": _ITEMS if items is None else items}}
        dsl["globals"] = {"env.items": ["stale"], "sys.query": "", "sys.user_id": "", "sys.conversation_turns": 0, "sys.files": [], "sys.history": []}
    return dsl


def create(env: dict[str, Any], dsl: dict[str, Any]) -> str:
    response = env["client"].post(env["base"] + "/api/v1/agents", json={"title": f"List operation acceptance {uuid4().hex}", "dsl": dsl}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    return response.json()["data"]["id"]


def replica_key(env: dict[str, Any], agent_id: str) -> str:
    return f"canvas:replica:{agent_id}:{env['owners'][0]}:{env['owners'][0]}"


def session_state(env: dict[str, Any], agent_id: str, session_id: str | None = None) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        query = sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id)
        if session_id:
            query = query.where(API4Conversation.id == session_id)
        records = list(db.scalars(query))
        assert len(records) == 1
        result = records[0].to_dict()
        result["dsl"] = json.loads(result["dsl"]) if isinstance(result["dsl"], str) else result["dsl"]
        return result


def run(env: dict[str, Any], agent_id: str, *, stream: bool, items: Any = _ITEMS, session_id: str | None = None, openai: bool = False, api_key: bool = False) -> Response:
    payload: dict[str, Any] = {"agent_id": agent_id, "release": not openai, "stream": stream, "inputs": {"items": {"type": "array", "value": items}}}
    if session_id:
        payload["session_id"] = session_id
    if openai:
        payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "list round"}]})
    else:
        payload["query"] = "list round"
    return env["client"].post(env["base"] + "/api/v1/agents/chat/completion", json=payload, headers={"Authorization": f"Bearer {env['keys'][0] if api_key else env['jwts'][0]}"}, timeout=30)


def assert_success(response: Response, stream: bool, expected: list[Any], session_id: str, *, openai: bool = False) -> None:
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        if openai:
            assert events and all("error" not in event for event in events)
            content = "".join(event["choices"][0]["delta"].get("content", "") for event in events)
            assert "data: [DONE]" in response.text
        else:
            assert events and all(event.get("session_id") == session_id for event in events)
            assert not any(event.get("event") == "error" for event in events)
            assert any(event.get("event") == "message_end" for event in events)
            content = "".join(event["data"]["content"] for event in events if event.get("event") == "message")
    elif openai:
        assert "error" not in response.json()
        content = response.json()["choices"][0]["message"]["content"]
    else:
        assert response.json()["retcode"] == 0 and response.json()["data"]["event"] == "message_end"
        assert response.json()["data"]["session_id"] == session_id
        content = response.json()["data"]["data"]["content"]
    assert json.loads(content) == expected


def assert_runtime(state: dict[str, Any], expected: list[Any], version: int) -> None:
    params = state["dsl"]["components"][_NODE]["obj"]["params"]
    assert params["operations_version"] == version and not state["errors"]
    assert params["outputs"]["result"]["value"] == expected
    assert params["outputs"]["first"]["value"] == (expected[0] if expected else None)
    assert params["outputs"]["last"]["value"] == (expected[-1] if expected else None)
    assert json.loads(state["message"][-1]["content"]) == expected and state["message"][-1]["role"] == "assistant"
