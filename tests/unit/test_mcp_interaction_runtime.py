"""U14 resume composition always rebinds live Principal and policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from api.identity.mcp_delegation.service import McpInteractionAuthorization
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
)
from api.identity.mcp_interactions.runtime import ReauthorizingInteractionExecutor
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from common.mcp_interactions import (
    InteractionEffect,
    InteractionLeaseFence,
    InteractionRequest,
    InteractionResume,
)


def _principal() -> Principal:
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id="user-a"),
        membership=TenantMembershipEvidence(
            platform_user_id="user-a",
            tenant_id="tenant-a",
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=datetime.now(UTC),
        ),
    )


def _resume() -> InteractionResume:
    request = InteractionRequest(
        tenant_id="tenant-a",
        platform_user_id="user-a",
        external_identity_id="identity-a",
        identity_revision=2,
        agent_id="agent-a",
        agent_revision_id="release-a",
        mcp_server_id="server-a",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        original_arguments={"days": 1},
        input_requests={"leave-form": {"method": "elicitation/create"}},
        output_schema={
            "type": "object",
            "properties": {"status": {"const": "prepared"}},
        },
        request_state="opaque-state",
        effect=InteractionEffect.PREPARE,
        replay_mode="reusable",
        policy_revision="old-policy-evidence",
        credential_generation=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        interaction_id="interaction-a",
        previous_revision=1,
        lease_fence=InteractionLeaseFence(
            job_id="job-a",
            owner="worker-a",
            attempt=1,
        ),
    )
    return InteractionResume(
        interaction_id="interaction-a",
        revision=1,
        request=request,
        input_responses={"leave-form": {"action": "cancel"}},
    )


async def test_resume_rebinds_current_provider_and_closes_request_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = SimpleNamespace()
    executor = ReauthorizingInteractionExecutor(handler=handler)  # type: ignore[arg-type]
    principal = _principal()
    server = SimpleNamespace(
        id="server-a",
        tenant_id="tenant-a",
        url="https://mcp.example/leave",
        variables={},
    )
    load_calls: list[InteractionResume] = []

    async def load_live(resume: InteractionResume):
        load_calls.append(resume)
        return principal, server

    monkeypatch.setattr(executor, "_load_live_authority", load_live)
    authorization_calls: list[str] = []

    class _Provider:
        def is_authorized(self, tool_name: str) -> bool:
            authorization_calls.append(tool_name)
            return True

        def interaction_authorization(
            self,
            _tool_name: str,
        ) -> McpInteractionAuthorization:
            return McpInteractionAuthorization(
                effect="prepare",
                replay_mode="reusable",
                policy_revision="current-policy",
                credential_generation=9,
            )

    provider = _Provider()
    resolver_calls: list[dict[str, object]] = []

    def resolve_provider(**kwargs: object) -> object:
        resolver_calls.append(kwargs)
        return provider

    monkeypatch.setattr(
        "api.identity.mcp_interactions.runtime.resolve_mcp_credential_provider",
        resolve_provider,
    )
    sessions: list[dict[str, object]] = []
    session_instances: list[object] = []

    class _Session:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            sessions.append(kwargs)
            session_instances.append(self)
            self.closed = False

        async def resume_tool_call(self, resume: InteractionResume) -> object:
            assert resume is _resume_value
            return {"status": "prepared"}

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "api.identity.mcp_interactions.runtime.MCPToolCallSession",
        _Session,
    )
    _resume_value = _resume()

    result = await executor.execute(_resume_value)

    assert result == {"status": "prepared"}
    assert load_calls == [_resume_value]
    assert authorization_calls == ["prepare_leave"]
    assert len(resolver_calls) == 1
    run_context = resolver_calls[0]["run_context"]
    assert run_context.principal is principal
    assert sessions[0]["interaction_handler"] is handler
    assert sessions[0]["tool_output_schemas"] == {
        "prepare_leave": _resume_value.request.output_schema,
    }
    assert session_instances[0].closed is True


async def test_resume_denies_when_live_policy_no_longer_binds_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = ReauthorizingInteractionExecutor(handler=SimpleNamespace())  # type: ignore[arg-type]
    server = SimpleNamespace(
        id="server-a",
        tenant_id="tenant-a",
        url="https://mcp.example/leave",
        variables={},
    )

    async def load_live(_resume: InteractionResume):
        return _principal(), server

    monkeypatch.setattr(executor, "_load_live_authority", load_live)
    monkeypatch.setattr(
        "api.identity.mcp_interactions.runtime.resolve_mcp_credential_provider",
        lambda **_kwargs: None,
    )

    with pytest.raises(InteractionStateError) as raised:
        await executor.execute(_resume())

    assert raised.value.code is InteractionErrorCode.REAUTHORIZATION_DENIED
