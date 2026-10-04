"""Real Begin/ListOperations/Message HTTP runs and SQL/Redis DSL boundaries.

The shared fixture supplies scratch auth/SQL, owned Redis cleanup and a real
listener. No component, model/provider, business response or storage is faked."""

import json
import os
import subprocess
from copy import deepcopy
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.sdk.session import CreateAgentSessionRequest, create_agent_session
from api.db.db_models import API4Conversation
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_list_operations import _ITEMS, _NODE, assert_runtime, assert_success, create, list_dsl, replica_key, run, session_state
from tests.support.agent_update_release import read_state, sse_events, update
from tests.support.agent_update_release import release_api as release_api


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
