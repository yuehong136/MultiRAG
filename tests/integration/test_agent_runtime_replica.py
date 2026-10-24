"""Runtime draft bootstrap and commits with real HTTP, scratch SQL and owned Redis keys."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before legacy Canvas asyncio compatibility
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db import UserTenantRole
from api.db.db_models import AgentExecutionOrigin, API4Conversation
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import join_member, message_dsl, read_state, sse_events, update
from tests.support.agent_update_release import release_api as release_api


def create_agent(env: dict[str, Any]) -> str:
    result = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Runtime draft", "dsl": message_dsl("draft reply")}, timeout=30)
    assert result.status_code == 200 and result.json()["retcode"] == 0, result.text
    return result.json()["data"]["id"]


def test_missing_replica_bootstraps_authorized_sql_draft(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    agent_id = create_agent(env)
    update(env, agent_id, {"dsl": message_dsl("older published reply"), "release": True})
    update(env, agent_id, {"dsl": message_dsl("draft reply")})
    update(env, agent_id, {"permission": "team"})
    join_member(env, monkeypatch, 1, UserTenantRole.NORMAL)
    before = read_state(env, agent_id)
    owner = env["owners"][0]
    runtime_user = uuid4().hex
    key = CanvasReplicaService._replica_key(agent_id, owner, runtime_user)
    assert REDIS_CONN.get(key) is None
    keys_before = sorted(REDIS_CONN.REDIS.keys(f"canvas:replica:{agent_id}:*"))
    denied = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions", headers={"Authorization": f"Bearer {env['keys'][1]}"}, json={"agent_id": agent_id, "user_id": owner, "tenant_id": owner}, timeout=30
    )
    assert denied.status_code == 403 and denied.json()["retcode"] != 0
    assert sorted(REDIS_CONN.REDIS.keys(f"canvas:replica:{agent_id}:*")) == keys_before
    result = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "user_id": runtime_user, "query": "runtime query"}, timeout=30)
    assert result.status_code == 200 and result.headers["content-type"].startswith("text/event-stream"), result.text
    events = sse_events(result.text)
    assert any(event["event"] == "message" and event["data"]["content"] == "draft reply" for event in events)
    payload = json.loads(REDIS_CONN.get(key))
    assert (payload["canvas_id"], payload["tenant_id"], payload["runtime_user_id"]) == (agent_id, owner, runtime_user)
    assert payload["dsl"]["globals"]["sys.query"] == "runtime query"
    assert 0 < REDIS_CONN.REDIS.ttl(key) <= CanvasReplicaService.TTL_SECS
    assert CanvasReplicaService.load_for_run(agent_id, env["owners"][1], runtime_user) is None
    assert read_state(env, agent_id) == before
    with Session(env["engine"]) as db:
        conversation = db.scalar(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id))
        assert conversation is not None and conversation.source == "agent"
        assert conversation.message[-1]["content"] == "draft reply"
        assert db.get(AgentExecutionOrigin, conversation.id).platform_user_id == owner


def test_concurrent_initialization_keeps_one_replica(release_api: dict[str, Any]) -> None:
    env = release_api
    agent_id = create_agent(env)
    owner = env["owners"][0]
    runtime_user = uuid4().hex

    def bootstrap(number: int) -> dict[str, Any]:
        return CanvasReplicaService.bootstrap(agent_id, owner, runtime_user, message_dsl(f"candidate {number}"))

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(bootstrap, range(24)))
    assert all(result == results[0] for result in results)
    assert CanvasReplicaService.load_for_run(agent_id, owner, runtime_user) == results[0]
    assert CanvasReplicaService.replace_for_set(agent_id, owner, runtime_user, message_dsl("editor save"))
    assert bootstrap(100)["dsl"] == message_dsl("editor save")
    assert CanvasReplicaService.bootstrap(agent_id, owner, uuid4().hex, message_dsl("separate user"))["dsl"] == message_dsl("separate user")
    assert CanvasReplicaService.bootstrap(agent_id, env["owners"][1], runtime_user, message_dsl("separate tenant"))["dsl"] == message_dsl("separate tenant")
    assert bootstrap(101)["dsl"] == message_dsl("editor save")


@pytest.mark.parametrize("failure", ["write", "lock", "ownership"])
def test_bootstrap_failure_has_no_execution_or_false_success(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = release_api
    agent_id = create_agent(env)
    owner, runtime_user = env["owners"][0], uuid4().hex
    key = CanvasReplicaService._replica_key(agent_id, owner, runtime_user)
    if failure == "ownership":
        payload = CanvasReplicaService._build_payload(agent_id, env["owners"][1], runtime_user, message_dsl("foreign"))
        assert REDIS_CONN.set_obj(key, payload)
    elif failure == "lock":
        monkeypatch.setattr(CanvasReplicaService, "_acquire_lock_with_retry", lambda *args: None)
    else:
        monkeypatch.setattr(REDIS_CONN, "set_obj", lambda *args, **kwargs: False)
    before = read_state(env, agent_id)
    tasks = len(env["task_ids"])
    result = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", json={"agent_id": agent_id, "user_id": runtime_user}, timeout=30)
    assert result.headers["content-type"].startswith("application/json")
    assert result.json()["retcode"] != 0 and result.json().get("data") is None
    assert len(env["task_ids"]) == tasks and read_state(env, agent_id) == before


@pytest.mark.parametrize("query,question,expected", [("primary", "secondary", "primary"), ("", "fallback", "fallback"), (None, "fallback", "fallback")])
def test_runtime_question_headers_keep_authenticated_context(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, query: str | None, question: str, expected: str) -> None:
    env = release_api
    agent_id = create_agent(env)
    owner, runtime_user = env["owners"][0], uuid4().hex
    original_init = Canvas.__init__
    construction: list[tuple[Any, Any]] = []

    def observe(self: Canvas, *args: Any, **kwargs: Any) -> None:
        construction.append((kwargs.get("custom_header"), kwargs.get("run_context")))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Canvas, "__init__", observe)
    before = read_state(env, agent_id)
    headers = {"x-runtime-marker": "test-only"}
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions",
        json={
            "agent_id": agent_id,
            "query": query,
            "question": question,
            "custom_header": headers,
            "user_id": runtime_user,
            "tenant_id": env["owners"][1],
            "run_context": {"tenant_id": env["owners"][1]},
            "prepared_run": {},
        },
        timeout=30,
    )
    assert response.status_code == 200 and any(event.get("event") == "message_end" for event in sse_events(response.text)), response.text
    assert len(construction) == 1 and construction[0][0] == headers
    context = construction[0][1]
    assert context.tenant_id == owner and context.platform_user_id == owner and context.draft_target.agent_id == agent_id
    replica = CanvasReplicaService.load_for_run(agent_id, owner, runtime_user)
    assert replica["dsl"]["globals"]["sys.query"] == expected and replica["dsl"]["globals"]["sys.user_id"] == ""
    assert read_state(env, agent_id) == before
