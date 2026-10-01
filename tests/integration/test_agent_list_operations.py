"""Real Begin/ListOperations/Message HTTP runs and SQL/Redis DSL boundaries.

The shared fixture supplies scratch auth/SQL, owned Redis cleanup and a real
listener. No component, model/provider, business response or storage is faked.
"""

import json
import os
import subprocess
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from requests import Response
from sqlalchemy.orm import Session

from api.apps.sdk.session import CreateAgentSessionRequest, create_agent_session
from api.db.db_models import API4Conversation
from core.utils.redis_conn import REDIS_CONN
from tests.integration.test_agent_update_release import read_state, sse_events, update
from tests.integration.test_agent_update_release import release_api as release_api

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


@pytest.mark.parametrize("stream", [False, True])
def test_http_new_and_legacy_matrix(release_api: dict[str, Any], stream: bool) -> None:
    env = release_api
    cases: list[tuple[dict[str, Any], list[Any], Any]] = [
        ({"operations_version": 2, "operations": "nth", "n": 2}, ["b"], _ITEMS),
        ({"operations_version": 2, "operations": "nth", "n": -2}, ["d"], _ITEMS),
        ({"operations_version": 2, "operations": "head", "n": 2, "strict": "false"}, ["a", "b"], _ITEMS),
        ({"operations_version": 2, "operations": "tail", "n": 2, "strict": True}, ["d", "e"], _ITEMS),
        ({"operations_version": 2, "operations": "head", "n": 6}, _ITEMS, _ITEMS),
        ({"operations_version": 2, "operations": "tail", "n": 6}, _ITEMS, _ITEMS),
        ({"operations_version": 2, "operations": "nth", "n": 0}, [], _ITEMS),
        ({"operations_version": 2, "operations": "head", "n": 2}, [], None),
    ]
    for operation, expected in [("topN", ["a", "b"]), ("head", ["b"]), ("tail", ["d"]), (None, ["a", "b"])]:
        for n, result in [(2, expected), (-1, []), (0, []), (6, _ITEMS if operation in (None, "topN") else [])]:
            params = {"n": n}
            if operation:
                params["operations"] = operation
            cases.append((params, result, _ITEMS))
    for index, (params, expected, items) in enumerate(cases):
        source = list_dsl(params)
        agent_id = create(env, source)
        update(env, agent_id, {"release": True})
        before = read_state(env, agent_id)
        replica = REDIS_CONN.get(replica_key(env, agent_id))
        response = run(env, agent_id, stream=stream, items=items, api_key=index == 0)
        state = session_state(env, agent_id)
        assert_success(response, stream, expected, state["id"])
        assert_runtime(state, expected, params.get("operations_version", 1))
        assert state["dsl"]["globals"]["sys.conversation_turns"] == 1
        assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica
    if not stream:
        smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, text=True, capture_output=True, timeout=60)
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "params",
    [
        {"operations_version": 2, "operations": "nth", "n": 0, "strict": True},
        {"operations_version": 2, "operations": "head", "n": 6, "strict": "true"},
        {"operations_version": 2, "operations": "tail", "n": -1, "strict": True},
    ],
)
def test_http_strict_errors_are_not_success(release_api: dict[str, Any], stream: bool, params: dict[str, Any]) -> None:
    env = release_api
    agent_id = create(env, list_dsl(params))
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    response = run(env, agent_id, stream=stream)
    assert response.status_code == 200
    if stream:
        events = sse_events(response.text)
        assert events[-1]["event"] == "error" and events[-1]["code"] != 0
        assert not any(event.get("event") in {"message", "message_end", "workflow_finished"} for event in events)
    else:
        assert response.json()["retcode"] != 0 and response.json().get("data") is not True
    state = session_state(env, agent_id)
    assert state["errors"] and not any(message["role"] == "assistant" for message in state["message"])
    output = state["dsl"]["components"][_NODE]["obj"]["params"]["outputs"]
    assert output["result"]["value"] == [] and output["first"]["value"] is None and output["last"]["value"] is None
    assert "strict mode" in output["_ERROR"]["value"]
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("draft_version", [1, 2])
def test_http_sessions_continuation_reset_and_debug_boundaries(release_api: dict[str, Any], stream: bool, draft_version: int) -> None:
    env = release_api
    agent_id = create(env, list_dsl({"operations": "head", "n": 2}, env_input=True))
    update(env, agent_id, {"release": True})
    draft_params = {"operations": "head", "n": 2}
    if draft_version == 2:
        draft_params["operations_version"] = 2
    update(env, agent_id, {"dsl": list_dsl(draft_params, env_input=True, items=list("vwxyz"))})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    ids = []
    for release, version in [(True, 1), (False, draft_version)]:
        body = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": release}, timeout=30).json()
        assert body["retcode"] == 0
        ids.append(body["data"]["id"])
        state = session_state(env, agent_id, ids[-1])
        assert state["dsl"]["components"][_NODE]["obj"]["params"]["operations_version"] == version
        assert state["dsl"]["history"] == [] and state["dsl"]["globals"]["sys.conversation_turns"] == 0
    with Session(env["engine"]) as db:
        old = db.get(API4Conversation, ids[0])
        old_dsl = deepcopy(old.dsl)
        old_dsl["components"][_NODE]["obj"]["params"].pop("operations_version")
        old_dsl["globals"]["env.items"] = list("fghij")
        old_dsl["globals"]["sys.conversation_turns"] = 3
        old_dsl["history"] = [["user", "prior"]]
        old.dsl = old_dsl
        db.commit()
    untouched = session_state(env, agent_id, ids[1])
    for turn in (4, 5):
        response = run(env, agent_id, stream=stream, session_id=ids[0])
        state = session_state(env, agent_id, ids[0])
        assert_success(response, stream, ["g"], ids[0])
        assert_runtime(state, ["g"], 1)
        assert state["dsl"]["globals"]["env.items"] == list("fghij")
        assert state["dsl"]["variables"]["items"]["value"] == _ITEMS
        assert state["dsl"]["globals"]["sys.conversation_turns"] == turn
        assert [entry[1] for entry in state["dsl"]["history"] if entry[0] == "user"] == ["prior"] + ["list round"] * (turn - 3)
        assert session_state(env, agent_id, ids[1]) == untouched
        assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica
    # The new explicit draft session executes its own persisted definition.
    response = run(env, agent_id, stream=stream, session_id=ids[1])
    expected = ["w"] if draft_version == 1 else ["v", "w"]
    assert_success(response, stream, expected, ids[1])
    assert_runtime(session_state(env, agent_id, ids[1]), expected, draft_version)
    with Session(env["engine"]) as db:
        conversations = {s.id: s.to_dict() for s in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))}
    for _ in range(2):
        current = read_state(env, agent_id)
        debug = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/components/{_NODE}/debug", json={"params": {}}, timeout=30)
        assert debug.status_code == 200 and debug.json()["retcode"] == 0 and debug.json()["data"]["result"] == expected
        assert read_state(env, agent_id) == current
        reset = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/reset", json={}, timeout=30)
        assert reset.status_code == 200 and reset.json()["retcode"] == 0
        state = read_state(env, agent_id)
        assert state["dsl"]["components"][_NODE]["obj"]["params"]["operations_version"] == draft_version
        assert state["dsl"]["components"][_NODE]["obj"]["params"]["operations"] == "head"
        assert state["versions"] == before["versions"] and state["release"] == before["release"]
        assert REDIS_CONN.get(replica_key(env, agent_id)) == replica
        with Session(env["engine"]) as db:
            assert {s.id: s.to_dict() for s in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))} == conversations


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("strict_error", [False, True])
def test_openai_real_list_execution_and_errors(release_api: dict[str, Any], stream: bool, strict_error: bool) -> None:
    env = release_api
    params = {"operations_version": 2, "operations": "nth", "n": 0 if strict_error else -1, "strict": strict_error}
    agent_id = create(env, list_dsl(params, env_input=True))
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    response = run(env, agent_id, stream=stream, openai=True)
    state = session_state(env, agent_id)
    if strict_error:
        assert response.status_code == 200
        if stream:
            events = sse_events(response.text)
            assert events[-1].get("error") and not any("choices" in event for event in events)
        else:
            assert response.json().get("error") and "choices" not in response.json()
        assert state["errors"] and not any(message["role"] == "assistant" for message in state["message"])
    else:
        assert_success(response, stream, ["e"], state["id"], openai=True)
        assert_runtime(state, ["e"], 2)
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica


def test_version_error_is_before_stream_and_sdk_helper_keeps_legacy(release_api: dict[str, Any]) -> None:
    env = release_api
    invalid = create(env, list_dsl({"operations_version": 3, "operations": "head", "n": 2}))
    update(env, invalid, {"release": True})
    before = read_state(env, invalid)
    for stream in (False, True):
        response = run(env, invalid, stream=stream)
        assert response.status_code == 500 and "application/json" in response.headers["content-type"], response.text
        assert response.json() == {"retcode": 100, "retmsg": "Agent completion failed.", "data": False}
        with Session(env["engine"]) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == invalid)) == 0
        assert read_state(env, invalid) == before
    agent_id = create(env, list_dsl({"operations": "tail", "n": 2}, env_input=True))
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    with Session(env["engine"]) as db:
        response = create_agent_session(agent_id, CreateAgentSessionRequest(release=True), db, env["owners"][0])
        body = json.loads(response.body)
        assert body["code"] == 0
        session_id = body["data"]["id"]
    state = session_state(env, agent_id, session_id)
    assert state["dsl"]["components"][_NODE]["obj"]["params"]["operations_version"] == 1
    response = run(env, agent_id, stream=False, session_id=session_id)
    assert_success(response, False, ["d"], session_id)
    assert_runtime(session_state(env, agent_id, session_id), ["d"], 1)
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica
