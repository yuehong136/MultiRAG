"""MCP connector seams for persistent MRTR pause and exact resume."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    build_principal_from_resolved_identity,
)
from api.identity.run_context import RunContext
from common.mcp_interactions import (
    InteractionLeaseFence,
    InteractionReceipt,
    InteractionRequest,
    InteractionResume,
    MCPInteractionPaused,
)
from common.mcp_tool_call_conn import (
    MCPRequestCredential,
    MCPToolCallSession,
    _LegacyElicitationBridge,
)
from mcp.types import CallToolResult, ElicitRequest, ElicitRequestFormParams, InputRequiredResult, TextContent


class _Handler:
    def __init__(self, *, ttl_seconds: int = 600) -> None:
        self.requests: list[InteractionRequest] = []
        self.ttl_seconds = ttl_seconds

    async def pause(self, request: InteractionRequest) -> InteractionReceipt:
        self.requests.append(request)
        return InteractionReceipt(interaction_id="interaction-a", revision=1)


class _CredentialProvider:
    resource_name = "leave-service"
    tls_ssl_context = None

    def __init__(self) -> None:
        self.calls = 0

    def is_authorized(self, canonical_tool_name: str) -> bool:
        return canonical_tool_name == "prepare_leave"

    def credential_for(self, canonical_tool_name: str) -> MCPRequestCredential:
        self.calls += 1
        return MCPRequestCredential(
            bearer=f"bearer-{self.calls}",
            resource_name=self.resource_name,
            canonical_tool_name=canonical_tool_name,
            policy_revision="policy-a",
            credential_generation=4,
            effect="prepare",
            replay_mode="reusable",
        )


def _run_context() -> RunContext:
    verified_at = datetime(2026, 8, 23, tzinfo=UTC)
    principal = build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=ExternalIdentityRecord(
                id="identity-a",
                tenant_id="tenant-a",
                user_id="user-a",
                provider="feishu",
                provider_tenant_key="provider-tenant-a",
                subject_type="user_id",
                subject_value="provider-user-a",
                state="active",
                verified_at=verified_at,
                last_seen_at=verified_at,
                identity_revision=7,
            ),
            membership=UserMembershipRecord(
                user_id="user-a",
                tenant_id="tenant-a",
                role="normal",
            ),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=verified_at,
            assurance_verified_at=verified_at,
            provider="feishu",
            external_identity_id="identity-a",
        ),
    )
    return RunContext(
        tenant_id="tenant-a",
        principal=principal,
        agent_id="agent-a",
        agent_revision_id="release-a",
    )


def _bare_session(*, ttl_seconds: int = 600) -> MCPToolCallSession:
    session = object.__new__(MCPToolCallSession)
    session._mcp_server = SimpleNamespace(
        id="server-a",
        url="https://mcp.example/leave",
    )
    session._call_context = _run_context()
    session._last_tool_call_meta = None
    session._credential_provider = _CredentialProvider()
    session._interaction_handler = _Handler(ttl_seconds=ttl_seconds)
    session._tool_output_schemas = {
        "prepare_leave": {
            "type": "object",
            "properties": {"status": {"const": "prepared"}},
        }
    }
    return session


def _input_required() -> InputRequiredResult:
    return InputRequiredResult(
        inputRequests={
            "leave-form": ElicitRequest(
                params=ElicitRequestFormParams(
                    message="Need leave dates",
                    requestedSchema={
                        "type": "object",
                        "properties": {"start": {"type": "string"}},
                    },
                )
            )
        },
        requestState="opaque-request-state",
    )


async def test_input_required_is_persisted_and_never_returned_to_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session()

    async def fake_call(*_args: object, **_kwargs: object) -> InputRequiredResult:
        return _input_required()

    monkeypatch.setattr(session, "_call_delegated_tool", fake_call)

    with pytest.raises(MCPInteractionPaused) as raised:
        await session._call_mcp_tool("prepare_leave", {"days": 1})

    handler = session._interaction_handler
    assert raised.value.interaction_id == "interaction-a"
    assert len(handler.requests) == 1
    request = handler.requests[0]
    assert request.tool_name == "prepare_leave"
    assert request.request_state == "opaque-request-state"
    assert request.original_arguments == {"days": 1}
    assert request.output_schema == session._tool_output_schemas["prepare_leave"]
    assert request.identity_revision == 7
    assert session.get_last_tool_call_meta() is None


async def test_interaction_expiry_uses_non_default_host_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session(ttl_seconds=37)

    async def fake_call(*_args: object, **_kwargs: object) -> InputRequiredResult:
        return _input_required()

    monkeypatch.setattr(session, "_call_delegated_tool", fake_call)
    before = datetime.now(UTC)

    with pytest.raises(MCPInteractionPaused):
        await session._call_mcp_tool("prepare_leave", {"days": 1})

    request = session._interaction_handler.requests[-1]
    after = datetime.now(UTC)
    assert request.expires_at is not None
    assert before + timedelta(seconds=37) <= request.expires_at
    assert request.expires_at <= after + timedelta(seconds=37)


async def test_resume_passes_exact_arguments_input_responses_and_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session()
    observed: list[dict[str, Any]] = []

    async def fake_call(**kwargs: Any) -> CallToolResult:
        observed.append(kwargs)
        return CallToolResult(content=[TextContent(text="completed")], isError=False)

    monkeypatch.setattr(session, "_call_delegated_tool", fake_call)
    expires_at = datetime.now(UTC) + timedelta(minutes=10)

    await session._call_mcp_tool(
        "prepare_leave",
        {"days": 1},
        input_responses={"leave-form": {"action": "accept", "content": {"start": "2026-09-01"}}},
        request_state="opaque-request-state",
        interaction_expires_at=expires_at,
    )

    assert len(observed) == 1
    assert observed[0]["name"] == "prepare_leave"
    assert observed[0]["arguments"] == {"days": 1}
    assert observed[0]["input_responses"] == {
        "leave-form": {
            "action": "accept",
            "content": {"start": "2026-09-01"},
        }
    }
    assert observed[0]["request_state"] == "opaque-request-state"
    assert observed[0]["request_timeout"] == 60
    assert isinstance(observed[0]["credential"], MCPRequestCredential)


async def test_resume_propagates_the_exact_lease_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session()
    fence = InteractionLeaseFence(
        job_id="job-a",
        owner="worker-a",
        attempt=3,
    )
    credential = session._credential_provider.credential_for("prepare_leave")
    request = session._interaction_request(
        name="prepare_leave",
        arguments={"days": 1},
        input_requests={"leave-form": {"method": "elicitation/create"}},
        request_state="opaque-request-state",
        credential=credential,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        interaction_id="interaction-a",
        interaction_revision=1,
        interaction_lease_fence=fence,
    )
    resume = InteractionResume(
        interaction_id="interaction-a",
        revision=1,
        request=request,
        input_responses={"leave-form": {"action": "cancel"}},
    )
    observed: dict[str, Any] = {}

    async def fake_call(
        _name: str,
        _arguments: dict[str, Any],
        **kwargs: Any,
    ) -> str:
        observed.update(kwargs)
        return "completed"

    monkeypatch.setattr(session, "_call_mcp_tool", fake_call)

    assert await session.resume_tool_call(resume) == "completed"
    assert observed["interaction_lease_fence"] == fence


def test_pause_signal_is_not_collapsed_into_tool_error_by_sync_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session()
    session._close = False
    session._event_loop = SimpleNamespace()

    class _Future:
        def result(self, timeout: float) -> str:
            del timeout
            raise MCPInteractionPaused(interaction_id="interaction-a", revision=1)

    def fake_schedule(coroutine: object, *_args: object, **_kwargs: object) -> _Future:
        coroutine.close()  # type: ignore[union-attr]
        return _Future()

    monkeypatch.setattr(
        "common.mcp_tool_call_conn.asyncio.run_coroutine_threadsafe",
        fake_schedule,
    )

    with pytest.raises(MCPInteractionPaused):
        session.tool_call("prepare_leave", {"days": 1})


async def test_declared_legacy_guard_pauses_then_replays_only_exact_response() -> None:
    session = _bare_session()
    credential = session._credential_provider.credential_for("prepare_leave")
    params = ElicitRequestFormParams(
        message="Need leave dates",
        requestedSchema={
            "type": "object",
            "properties": {"start": {"type": "string"}},
            "required": ["start"],
            "additionalProperties": False,
        },
    )
    initial = _LegacyElicitationBridge(
        session=session,
        name="prepare_leave",
        arguments={"days": 1},
        credential=credential,
        input_responses=None,
        expected_input_requests=None,
        expected_request_state=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        interaction_id=None,
        interaction_revision=None,
    )

    cancelled = await initial(None, params)

    assert cancelled.action == "cancel"
    with pytest.raises(MCPInteractionPaused):
        initial.raise_if_paused_or_incomplete()
    persisted = session._interaction_handler.requests[-1]
    request_id = next(iter(persisted.input_requests))
    resume = _LegacyElicitationBridge(
        session=session,
        name="prepare_leave",
        arguments={"days": 1},
        credential=credential,
        input_responses={
            request_id: {
                "action": "accept",
                "content": {"start": "2026-09-01"},
            }
        },
        expected_input_requests=dict(persisted.input_requests),
        expected_request_state=persisted.request_state,
        expires_at=persisted.expires_at,
        interaction_id="interaction-a",
        interaction_revision=1,
    )

    response = await resume(None, params)
    resume.raise_if_paused_or_incomplete()

    assert response.action == "accept"
    assert response.content == {"start": "2026-09-01"}


async def test_legacy_guard_drift_fails_closed() -> None:
    session = _bare_session()
    credential = session._credential_provider.credential_for("prepare_leave")
    bridge = _LegacyElicitationBridge(
        session=session,
        name="prepare_leave",
        arguments={"days": 1},
        credential=credential,
        input_responses={"legacy-wrong": {"action": "cancel"}},
        expected_input_requests={"legacy-wrong": {"method": "elicitation/create"}},
        expected_request_state="legacy.callback.v1.wrong",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        interaction_id="interaction-a",
        interaction_revision=1,
    )

    with pytest.raises(ValueError, match="guard changed"):
        await bridge(
            None,
            ElicitRequestFormParams(
                message="Changed guard",
                requestedSchema={"type": "object", "properties": {}},
            ),
        )


async def test_stateless_legacy_envelope_is_persisted_before_model_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _bare_session()
    session._legacy_interaction_tools = frozenset({"prepare_leave"})
    result = CallToolResult(
        content=[TextContent(text="adapter envelope")],
        structuredContent={
            "result": {
                "kind": "com.ofmcp/input-required",
                "version": 1,
                "guard_digest": "a" * 64,
                "input_requests": {
                    "leave-form": {
                        "method": "elicitation/create",
                        "params": {
                            "mode": "form",
                            "message": "Need leave dates",
                            "requestedSchema": {
                                "type": "object",
                                "properties": {"start": {"type": "string"}},
                            },
                        },
                    }
                },
            }
        },
        isError=False,
    )

    async def fake_call(*_args: object, **_kwargs: object) -> CallToolResult:
        return result

    monkeypatch.setattr(session, "_call_delegated_tool", fake_call)

    with pytest.raises(MCPInteractionPaused):
        await session._call_mcp_tool("prepare_leave", {"days": 1})

    persisted = session._interaction_handler.requests[-1]
    assert persisted.request_state == f"legacy.ofmcp.v1.{'a' * 64}"
    assert persisted.input_requests.keys() == {"leave-form"}
    assert session.get_last_tool_call_meta() is None
