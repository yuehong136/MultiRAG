"""Web execution identity, snapshot configuration and legacy provenance rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import AgentExecutionOrigin, API4Conversation, UserCanvasVersion
from api.db.services.agent_execution_service import execution_context, restore_execution_snapshot, snapshot_digest
from api.db.services.canvas_service import prepare_agent_run
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, Principal, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.identity.run_context import DraftExecutionTarget, RunContext


def principal(user_id: str = "owner") -> Principal:
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(user_id),
        membership=TenantMembershipEvidence(user_id, user_id),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )


def test_snapshot_restores_configuration_and_keeps_runtime_values() -> None:
    snapshot = {
        "components": {
            "agent": {
                "obj": {"component_name": "Agent", "params": {"sys_prompt": "approved", "mcp": [{"mcp_id": "approved-server"}], "outputs": {"answer": {"type": "string", "value": ""}}}},
                "downstream": ["end"],
            }
        },
        "history": [],
        "globals": {},
    }
    runtime = json.loads(json.dumps(snapshot))
    runtime["components"]["agent"]["obj"]["params"].update(sys_prompt="replacement", mcp=[{"mcp_id": "other-server"}])
    runtime["components"]["agent"]["downstream"] = ["injected"]
    runtime["components"]["agent"]["obj"]["params"]["outputs"]["answer"]["value"] = "previous answer"
    runtime["history"] = [["question", "answer"]]
    restored = json.loads(restore_execution_snapshot(snapshot, runtime))
    assert restored["components"]["agent"]["downstream"] == ["end"]
    params = restored["components"]["agent"]["obj"]["params"]
    assert params["sys_prompt"] == "approved" and params["mcp"] == [{"mcp_id": "approved-server"}]
    assert params["outputs"]["answer"]["value"] == "previous answer" and restored["history"] == runtime["history"]
    assert snapshot["components"]["agent"]["obj"]["params"]["outputs"]["answer"]["value"] == ""


def test_draft_digest_covers_parameters_not_only_graph() -> None:
    first = execution_context(principal=principal(), tenant_id="owner", agent_id="agent", dsl={"graph": {}, "components": {"prompt": "first"}})
    second = execution_context(principal=principal(), tenant_id="owner", agent_id="agent", dsl={"graph": {}, "components": {"prompt": "second"}})
    assert first.draft_target != second.draft_target and first.agent_revision_id is None
    with pytest.raises(ValueError):
        RunContext(tenant_id="owner", agent_id="agent", agent_revision_id="published", draft_target=DraftExecutionTarget("agent", "a" * 64))


class ExecutionSession(AsyncSession):
    def __init__(self, *, origin: SimpleNamespace | None = None, caller_id: str = "owner", version_available: bool = True) -> None:
        super().__init__()
        self.origin = origin
        self.caller_id = caller_id
        self.version_available = version_available
        self.canvas = SimpleNamespace(user_id="owner", permission="team", dsl={"components": {}}, id="agent")

    async def scalar(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
        if "user_tenant" in str(statement):
            return "verified-membership"
        if "user_canvas_version" in str(statement):
            return SimpleNamespace(id="release-exact", dsl={"components": {}}, title="Published")
        return self.canvas

    async def get(self, model: Any, key: Any, **kwargs: Any) -> Any:
        if model is API4Conversation:
            return SimpleNamespace(
                dialog_id="agent",
                source="agent",
                dsl={"components": {}, "history": [["question", "answer"]]},
                version_title="ambiguous title",
                to_dict=lambda: {"id": "session", "dialog_id": "agent", "source": "agent"},
            )
        if model is AgentExecutionOrigin:
            return self.origin
        if model is UserCanvasVersion:
            return SimpleNamespace(user_canvas_id="agent", release=True) if self.version_available else None
        raise AssertionError(model)


def origin(*, mode: str = "published", user_id: str = "owner") -> SimpleNamespace:
    snapshot = {"components": {}, "history": []}
    return SimpleNamespace(
        agent_id="agent",
        tenant_id="owner",
        platform_user_id=user_id,
        execution_mode=mode,
        agent_revision_id="release-original" if mode == "published" else None,
        snapshot_digest=snapshot_digest(snapshot),
        snapshot_dsl=snapshot,
    )


async def test_resume_uses_original_revision_and_rechecks_caller() -> None:
    prepared = await prepare_agent_run(ExecutionSession(origin=origin()), "agent", "owner", session_id="session", release_mode=True, principal=principal())
    assert prepared.run_context is not None and prepared.run_context.agent_revision_id == "release-original"
    assert json.loads(prepared.dsl)["history"] == [["question", "answer"]]
    with pytest.raises(PermissionError, match="caller"):
        await prepare_agent_run(ExecutionSession(origin=origin()), "agent", "other", session_id="session", principal=principal("other"))


async def test_legacy_session_does_not_guess_a_published_revision() -> None:
    prepared = await prepare_agent_run(ExecutionSession(), "agent", "owner", session_id="session", release_mode=True, principal=principal())
    assert prepared.run_context is not None and prepared.run_context.agent_revision_id is None and prepared.run_context.draft_target is None


async def test_removed_publication_and_tampered_origin_are_refused() -> None:
    with pytest.raises(LookupError, match="unavailable"):
        await prepare_agent_run(ExecutionSession(origin=origin(), version_available=False), "agent", "owner", session_id="session", principal=principal())
    tampered = origin()
    tampered.snapshot_dsl["injected"] = True
    with pytest.raises(PermissionError, match="snapshot"):
        await prepare_agent_run(ExecutionSession(origin=tampered), "agent", "owner", session_id="session", principal=principal())


async def test_team_release_rebinds_verified_tenant_without_changing_caller() -> None:
    prepared = await prepare_agent_run(ExecutionSession(caller_id="member"), "agent", "member", release_mode=True, principal=principal("member"))
    assert prepared.run_context is not None and prepared.run_context.principal is not None
    assert prepared.run_context.principal.platform_user_id == "member" and prepared.run_context.tenant_id == "owner"
    with pytest.raises(PermissionError, match="owner"):
        await prepare_agent_run(ExecutionSession(caller_id="member"), "agent", "member", principal=principal("member"))
