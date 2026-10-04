"""Actual beta-token Agentbot execution and independent session/state readback."""

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

from api.db.db_models import API4Conversation, APIToken
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_list_operations import assert_runtime, create, list_dsl, replica_key, session_state
from tests.support.agent_update_release import read_state, sse_events, update
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def agentbot_api(release_api: dict[str, Any]) -> dict[str, Any]:
    # The shared fixture's API tokens have no beta value. Provision only these
    # owned scratch rows; the shared teardown deletes and independently checks
    # the same tokens, users, canvases, sessions, Redis keys and listener.
    with Session(release_api["engine"]) as db:
        for key in release_api["keys"]:
            token = db.scalar(sa.select(APIToken).where(APIToken.token == key))
            assert token is not None
            token.beta = uuid4().hex
        db.commit()
    return release_api


def beta_token(env: dict[str, Any], index: int = 0) -> str:
    with Session(env["engine"]) as db:
        beta = db.scalar(sa.select(APIToken.beta).where(APIToken.token == env["keys"][index]))
        assert beta
        return beta


def run_agentbot(
    env: dict[str, Any], agent_id: str, *, stream: bool, session_id: str | None = None, inputs: dict[str, Any] | None = None, release: bool = True, query_release: bool = False
) -> Response:
    payload: dict[str, Any] = {"query": "list round", "stream": stream, "inputs": inputs or {}}
    if session_id:
        payload["session_id"] = session_id
    if not query_release:
        payload["release"] = release
    return env["client"].post(
        f"{env['base']}/api/v1/agentbots/{agent_id}/completions",
        headers={"Authorization": f"Bearer {beta_token(env)}"},
        params={"release": str(release).lower()} if query_release else {},
        json=payload,
        timeout=30,
    )


def assert_agentbot_success(response: Response, stream: bool, session_id: str, expected: list[Any]) -> None:
    assert response.status_code == 200, response.text
    if stream:
        events = sse_events(response.text)
        assert events and all(event.get("session_id") == session_id for event in events)
        assert not any(event.get("event") == "error" or event.get("code", 0) != 0 for event in events)
        assert events[-1]["event"] == "message_end" and any(event.get("event") == "workflow_finished" for event in events)
        content = "".join(event["data"]["content"] for event in events if event.get("event") == "message")
    else:
        body = response.json()
        assert body["code"] == 0 and body["data"]["event"] == "message_end"
        assert body["data"]["session_id"] == session_id
        content = body["data"]["data"]["content"]
    assert json.loads(content) == expected and "[DONE]" not in response.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("scenario", ["strict", "v2", "legacy"])
def test_agentbot_first_and_continued_runs_persist_results(agentbot_api: dict[str, Any], stream: bool, scenario: str) -> None:
    env = agentbot_api
    params = {"operations": "head", "n": 2}
    if scenario != "legacy":
        params.update({"operations_version": 2, "strict": True})
    if scenario == "strict":
        params.update({"operations": "nth", "n": 0})
    agent_id = create(env, list_dsl(params, env_input=True))
    update(env, agent_id, {"release": True})
    # Change the draft before either request; published runs and continuation
    # must keep their own parameters/defaults, never execute this new draft.
    update(env, agent_id, {"dsl": list_dsl({"operations_version": 2, "operations": "tail", "n": 1}, env_input=True, items=list("vwxyz"))})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    session_id = None
    sibling_id = None
    sibling = None
    for turn in (1, 2):
        if turn == 2 and scenario == "legacy":
            with Session(env["engine"]) as db:
                row = db.get(API4Conversation, session_id)
                assert row is not None
                stored = json.loads(row.dsl) if isinstance(row.dsl, str) else deepcopy(row.dsl)
                stored["components"]["ListOperations:list"]["obj"]["params"].pop("operations_version")
                stored["globals"]["env.items"] = list("fghij")
                row.dsl = json.dumps(stored) if isinstance(row.dsl, str) else stored
                db.commit()
        response = run_agentbot(env, agent_id, stream=stream, session_id=session_id, release=turn == 1, query_release=not stream)
        state = session_state(env, agent_id, session_id)
        session_id = state["id"]
        assert response.status_code == 200
        if scenario == "strict":
            if stream:
                events = sse_events(response.text)
                assert events[-1]["event"] == "error" and events[-1]["code"] != 0 and "strict mode" in events[-1]["message"]
                assert not any(event.get("event") in {"message", "message_end", "workflow_finished"} for event in events)
                assert "[DONE]" not in response.text
            else:
                assert response.json()["code"] != 0 and "strict mode" in response.json()["message"] and "data" not in response.json()
            assert "strict mode" in state["errors"]
            assert [m["role"] for m in state["message"]] == ["user"] * turn
            params = state["dsl"]["components"]["ListOperations:list"]["obj"]["params"]
            assert params["operations_version"] == 2
            outputs = params["outputs"]
            assert outputs["result"]["value"] == [] and outputs["first"]["value"] is None and outputs["last"]["value"] is None
            assert "strict mode" in outputs["_ERROR"]["value"]
        else:
            expected = (["b"] if turn == 1 else ["g"]) if scenario == "legacy" else ["a", "b"]
            assert_agentbot_success(response, stream, session_id, expected)
            assert_runtime(state, expected, 1 if scenario == "legacy" else 2)
            assert [m["role"] for m in state["message"]] == ["user", "assistant"] * turn
        assert state["dsl"]["globals"]["sys.conversation_turns"] == turn
        assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica
        if turn == 1:
            body = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": False}, timeout=30).json()
            assert body["retcode"] == 0
            sibling_id = body["data"]["id"]
            sibling = session_state(env, agent_id, sibling_id)
        else:
            assert session_state(env, agent_id, sibling_id) == sibling
    if not stream and scenario == "v2":
        smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, text=True, capture_output=True, timeout=60)
        assert smoke.returncode == 0, smoke.stdout + smoke.stderr


@pytest.mark.parametrize("stream", [False, True])
def test_agentbot_beta_auth_rejections_do_not_run(agentbot_api: dict[str, Any], stream: bool) -> None:
    env = agentbot_api
    agent_id = create(env, list_dsl({"operations_version": 2, "operations": "head", "n": 2}, env_input=True))
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    tasks = list(env["task_ids"])
    url = f"{env['base']}/api/v1/agentbots/{agent_id}/completions"
    for credential in ("", "Bearer invalid-beta", f"Bearer {env['keys'][0]}", f"Bearer {env['jwts'][0]}"):
        response = env["client"].post(url, headers={"Authorization": credential}, json={"query": "list round", "release": True, "stream": stream}, timeout=30)
        assert response.status_code == 401 and response.json()["retcode"] != 0 and response.json()["data"] is False
    # This beta value is valid, but its tenant does not own this Agent.
    response = env["client"].post(url, headers={"Authorization": f"Bearer {beta_token(env, 1)}"}, json={"release": True, "stream": stream}, timeout=30)
    assert response.status_code == 200
    if stream:
        events = sse_events(response.text)
        assert len(events) == 1 and events[0]["code"] != 0 and "do not own" in events[0]["message"]
    else:
        assert response.json()["code"] != 0 and "do not own" in response.json()["message"] and "data" not in response.json()
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 0
    assert env["task_ids"] == tasks
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica


@pytest.mark.parametrize("stream", [False, True])
def test_agentbot_user_inputs_pause_and_resume(agentbot_api: dict[str, Any], stream: bool) -> None:
    env = agentbot_api
    dsl = list_dsl({"operations_version": 2, "operations": "head", "n": 2, "strict": True})
    components = dsl["components"]
    components["begin"]["downstream"] = ["Message:before"]
    components["Message:before"] = {"obj": {"component_name": "Message", "params": {"content": ["Before input"]}}, "upstream": ["begin"], "downstream": ["UserFillUp:input"]}
    components["UserFillUp:input"] = {
        "obj": {"component_name": "UserFillUp", "params": {"inputs": {"items": {"name": "items", "type": "array", "optional": False}}, "tips": "Provide items"}},
        "upstream": ["Message:before"],
        "downstream": ["ListOperations:list"],
    }
    components["ListOperations:list"]["upstream"] = ["UserFillUp:input"]
    components["ListOperations:list"]["obj"]["params"]["query"] = "{UserFillUp:input@items}"
    agent_id = create(env, dsl)
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)
    replica = REDIS_CONN.get(replica_key(env, agent_id))
    response = run_agentbot(env, agent_id, stream=stream)
    state = session_state(env, agent_id)
    assert response.status_code == 200 and not state["errors"]
    if stream:
        frames = sse_events(response.text)
        paused = [frame for frame in frames if frame["event"] == "user_inputs"]
        assert len(paused) == 1 and not any(frame["event"] == "workflow_finished" for frame in frames)
        terminal = paused[0]
    else:
        assert response.json()["code"] == 0
        terminal = response.json()["data"]
        assert terminal["event"] == "user_inputs" and terminal["data"]["content"] == "Before input"
    assert terminal["session_id"] == state["id"] and terminal["data"]["tips"] == "Provide items"
    assert terminal["data"]["inputs"]["items"]["type"] == "array" and state["dsl"]["path"][0] == "UserFillUp:input"
    assert [m["content"] for m in state["message"]] == ["list round", "Before input"]
    response = run_agentbot(env, agent_id, stream=stream, session_id=state["id"], inputs={"items": {"type": "array", "value": list("abcde")}})
    assert_agentbot_success(response, stream, state["id"], ["a", "b"])
    state = session_state(env, agent_id, state["id"])
    assert_runtime(state, ["a", "b"], 2)
    assert state["dsl"]["globals"]["sys.conversation_turns"] == 2
    assert len(state["message"]) == 4 and state["message"][1]["content"] == "Before input"
    assert read_state(env, agent_id) == before and REDIS_CONN.get(replica_key(env, agent_id)) == replica
