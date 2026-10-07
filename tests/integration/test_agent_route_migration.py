"""Canonical Agent consumers against HTTP, scratch SQL and real Redis."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.channels.agent_bridge import AgentExecutionError, MultiRAGAgentClient
from api.db.db_models import API4Conversation
from tests.support.agent_update_release import message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api


def test_native_client_uses_authorized_canonical_agent(release_api: dict[str, Any]) -> None:
    env = release_api
    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Canonical consumer", "dsl": message_dsl("published reply")}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    agent_id = response.json()["data"]["id"]
    update(env, agent_id, {"release": True})
    before = read_state(env, agent_id)

    async def ask_twice() -> str:
        consumer = MultiRAGAgentClient(base_url=env["base"], agent_id=agent_id, api_token=env["keys"][0])
        try:
            await consumer.preflight()
            first = await consumer.ask(question="first native question", session_id=None)
            second = await consumer.ask(question="continued native question", session_id=first.session_id)
            assert first.content == second.content == "published reply"
            assert second.session_id == first.session_id
            return first.session_id
        finally:
            await consumer.close()

    session_id = asyncio.run(ask_twice())
    with Session(env["engine"]) as db:
        conversation = db.get(API4Conversation, session_id)
        assert conversation is not None and conversation.dialog_id == agent_id and conversation.user_id == env["owners"][0]
        assert [m["content"] for m in conversation.message if m["role"] == "user"] == ["first native question", "continued native question"]
        assert conversation.message[-1]["content"] == "published reply"
        count = db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id))

    for authorization in ("", "invalid", env["keys"][1], env["foreign_jwt"]):
        denied = requests.post(
            f"{env['base']}/api/v1/agents/chat/completion",
            headers={"Authorization": f"Bearer {authorization}"},
            json={"agent_id": agent_id, "release": True, "stream": False},
            timeout=30,
        )
        assert denied.status_code in (401, 403) and denied.json().get("retcode", denied.json().get("code")) != 0
    retired = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/completions", json={"query": "retired"}, timeout=30)
    assert retired.status_code == 404
    paths = env["client"].get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
    assert "/api/v1/agents/{agent_id}/completions" not in paths
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == count
    assert read_state(env, agent_id) == before

    async def forbidden_consumer() -> None:
        consumer = MultiRAGAgentClient(base_url=env["base"], agent_id=agent_id, api_token=env["keys"][1])
        try:
            with pytest.raises(AgentExecutionError):
                await consumer.ask(question="forbidden", session_id=None)
        finally:
            await consumer.close()

    asyncio.run(forbidden_consumer())
