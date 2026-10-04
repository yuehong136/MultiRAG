"""Web origin HTTP/SQL acceptance on scratch PostgreSQL and real Redis."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import AgentExecutionOrigin, API4Conversation
from api.db.services.agent_execution_service import save_agent_session, snapshot_digest
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.identity.run_context import RunContext
from tests.support.agent_update_release import join_member, message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api


def create_agent(env: dict[str, Any]) -> str:
    response = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Execution origin", "dsl": message_dsl("original")}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    return response.json()["data"]["id"]


@pytest.mark.parametrize("mode", ["draft", "published"])
def test_http_session_origin_is_fixed_across_edits_and_publications(release_api: dict[str, Any], mode: str) -> None:
    env = release_api
    agent_id = create_agent(env)
    update(env, agent_id, {"release": True})
    original_revision = read_state(env, agent_id)["versions"][0]["id"]
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/{agent_id}/sessions",
        json={"release": mode == "published", "run_context": {"agent_revision_id": "forged"}, "prepared_run": "forged", "user_id": "display-user"},
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    session_id = response.json()["data"]["id"]
    with Session(env["engine"]) as db:
        origin = db.get(AgentExecutionOrigin, session_id)
        assert origin is not None and origin.execution_mode == mode and origin.platform_user_id == env["owners"][0]
        assert origin.agent_revision_id == (original_revision if mode == "published" else None)
        assert origin.snapshot_digest == snapshot_digest(origin.snapshot_dsl)
        original_origin = origin.to_dict()
        # Simulate stale/tampered mutable state. Executable settings must come
        # from the origin, while legitimate history remains server-persisted.
        conversation = db.get(API4Conversation, session_id)
        assert conversation is not None
        runtime_dsl = json.loads(json.dumps(conversation.dsl))
        runtime_dsl["components"]["Message:answer"]["obj"]["params"]["content"] = ["wrong runtime config"]
        conversation.dsl = runtime_dsl
        db.commit()
    update(env, agent_id, {"dsl": message_dsl("new publication"), "release": True})
    response = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completion",
        json={
            "agent_id": agent_id,
            "session_id": session_id,
            "release": True,
            "query": "continue",
            "stream": False,
            "user_id": "forged-owner",
            "run_context": {"tenant_id": "forged"},
            "prepared_run": "forged",
        },
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.json()["data"]["data"]["content"] == "original"
    with Session(env["engine"]) as db:
        assert db.get(AgentExecutionOrigin, session_id).to_dict() == original_origin
        conversation = db.get(API4Conversation, session_id)
        assert conversation is not None and len(conversation.message) >= 3
    response = env["client"].delete(f"{env['base']}/api/v1/agents/{agent_id}/sessions/{session_id}", timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    with Session(env["engine"]) as db:
        assert db.get(AgentExecutionOrigin, session_id) is None


@pytest.mark.parametrize("openai_compatible", [False, True])
def test_http_completion_passes_authenticated_context_before_canvas_construction(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, openai_compatible: bool) -> None:
    from agent.canvas import Canvas

    env = release_api
    agent_id = create_agent(env)
    update(env, agent_id, {"release": True})
    revision_id = read_state(env, agent_id)["versions"][0]["id"]
    observed: list[RunContext | None] = []
    original_init = Canvas.__init__

    def capture(self: Canvas, *args: Any, **kwargs: Any) -> None:
        observed.append(kwargs.get("run_context"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(Canvas, "__init__", capture)
    payload: dict[str, Any] = {"agent_id": agent_id, "release": True, "stream": False, "user_id": "forged", "run_context": {"agent_revision_id": "forged"}, "prepared_run": {"caller_id": "forged"}}
    if openai_compatible:
        payload.update({"openai-compatible": True, "messages": [{"role": "user", "content": "first"}]})
    response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json=payload, timeout=30)
    assert response.status_code == 200, response.text
    assert observed and observed[-1] is not None
    context = observed[-1]
    assert context.agent_revision_id == revision_id and context.platform_user_id == env["owners"][0] and context.mcp_read_only
    with Session(env["engine"]) as db:
        origin = db.scalar(sa.select(AgentExecutionOrigin).where(AgentExecutionOrigin.agent_id == agent_id))
        assert origin is not None and origin.agent_revision_id == revision_id and origin.platform_user_id == env["owners"][0]


def test_http_shared_published_sessions_are_owned_by_runner(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db import UserTenantRole
    from api.db.db_models import UserTenant

    env = release_api
    agent_id = create_agent(env)
    update(env, agent_id, {"release": True, "permission": "team"})
    join_member(env, monkeypatch, 1, UserTenantRole.NORMAL)
    headers = {"Authorization": f"Bearer {env['jwts'][1]}"}
    created = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completion", headers=headers, json={"agent_id": agent_id, "release": True, "stream": False, "user_id": env["owners"][0]}, timeout=30
    )
    assert created.status_code == 200 and created.json()["retcode"] == 0, created.text
    session_id = created.json()["data"]["session_id"]
    with Session(env["engine"]) as db:
        origin = db.get(AgentExecutionOrigin, session_id)
        assert origin is not None and origin.platform_user_id == env["owners"][1] and origin.tenant_id == env["owners"][0]
    owner_response = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", json={"agent_id": agent_id, "session_id": session_id, "stream": False}, timeout=30)
    assert owner_response.status_code == 403 and owner_response.json()["retcode"] != 0
    with Session(env["engine"]) as db:
        db.execute(sa.delete(UserTenant).where(UserTenant.user_id == env["owners"][1], UserTenant.tenant_id == env["owners"][0]))
        db.commit()
    removed = env["client"].post(f"{env['base']}/api/v1/agents/chat/completion", headers=headers, json={"agent_id": agent_id, "session_id": session_id, "stream": False}, timeout=30)
    assert removed.status_code == 403 and removed.json()["retcode"] != 0


async def test_origin_failure_rolls_back_the_session_row(bootstrapped_async_engine: AsyncEngine) -> None:
    identifier = uuid4().hex
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(identifier),
        membership=TenantMembershipEvidence(identifier, identifier),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )
    context = RunContext(tenant_id=identifier, principal=principal, agent_id=identifier, agent_revision_id=identifier)
    async with AsyncSession(bootstrapped_async_engine, expire_on_commit=False) as db:
        with pytest.raises(ValueError):
            await save_agent_session(db, {"id": identifier, "dialog_id": identifier, "user_id": identifier, "source": "agent", "dsl": {}}, context=context, snapshot={"invalid_json": float("nan")})
        assert await db.get(API4Conversation, identifier) is None and await db.get(AgentExecutionOrigin, identifier) is None


def test_origin_migration_preserves_material_and_refuses_destructive_downgrade(bootstrapped_engine: sa.Engine, alembic_cfg: Any) -> None:
    table = AgentExecutionOrigin.__table__
    identifier = uuid4().hex
    with bootstrapped_engine.begin() as connection:
        alembic_cfg.attributes["connection"] = connection
        command.stamp(alembic_cfg, "a9c810f1d2e3")
        command.upgrade(alembic_cfg, "head")
        connection.execute(API4Conversation.__table__.insert().values(id=identifier, dialog_id=identifier, user_id=identifier, source="agent", dsl={}))
        connection.execute(
            table.insert().values(
                id=identifier,
                tenant_id=identifier,
                platform_user_id=identifier,
                agent_id=identifier,
                execution_mode="published",
                agent_revision_id=identifier,
                snapshot_digest="a" * 64,
                snapshot_dsl={},
            )
        )
    try:
        with pytest.raises(RuntimeError, match="provenance"), bootstrapped_engine.begin() as connection:
            alembic_cfg.attributes["connection"] = connection
            command.downgrade(alembic_cfg, "a9c810f1d2e3")
        with bootstrapped_engine.connect() as connection:
            assert connection.scalar(sa.select(table.c.id).where(table.c.id == identifier)) == identifier
            assert connection.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == "b0d2e4f6a8c0"
    finally:
        with bootstrapped_engine.begin() as connection:
            connection.execute(sa.delete(API4Conversation).where(API4Conversation.id == identifier))
