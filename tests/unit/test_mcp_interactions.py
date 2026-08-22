"""Transport-neutral EIM-U14 interaction contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from common.mcp_interactions import (
    InteractionEffect,
    InteractionRequest,
    InteractionResume,
    MCPInteractionPaused,
    canonical_call_digest,
)


def _request(**overrides: object) -> InteractionRequest:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "platform_user_id": "user-a",
        "external_identity_id": "identity-a",
        "identity_revision": 7,
        "agent_id": "agent-a",
        "agent_revision_id": "release-a",
        "mcp_server_id": "server-a",
        "resource_name": "leave-service",
        "resource_uri": "https://mcp.example/leave",
        "tool_name": "prepare_leave",
        "original_arguments": {"days": 1, "reason": "private-reason"},
        "input_requests": {"leave-form": {"method": "elicitation/create"}},
        "output_schema": {
            "type": "object",
            "properties": {"status": {"const": "prepared"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "request_state": "opaque-request-state",
        "effect": InteractionEffect.PREPARE,
        "replay_mode": "reusable",
        "policy_revision": "policy-a",
        "credential_generation": 3,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    values.update(overrides)
    return InteractionRequest(**values)  # type: ignore[arg-type]


def test_call_digest_is_canonical_and_bound_to_server_resource_tool_and_arguments() -> None:
    first = canonical_call_digest(
        mcp_server_id="server-a",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        arguments={"reason": "annual", "days": 1},
    )
    reordered = canonical_call_digest(
        mcp_server_id="server-a",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        arguments={"days": 1, "reason": "annual"},
    )
    changed = canonical_call_digest(
        mcp_server_id="server-a",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        arguments={"days": 2, "reason": "annual"},
    )

    assert first == reordered
    assert first != changed
    assert len(first) == 64


def test_interaction_request_repr_never_contains_continuation_arguments_or_identity() -> None:
    request = _request()

    rendered = repr(request)

    for secret in (
        "opaque-request-state",
        "private-reason",
        "user-a",
        "identity-a",
        "prepared",
    ):
        assert secret not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_revision_id", ""),
        ("resource_name", ""),
        ("request_state", ""),
        ("effect", InteractionEffect.SIDE_EFFECT),
    ],
)
def test_interaction_request_rejects_incomplete_binding_and_side_effect(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="interaction request is invalid"):
        _request(**{field: value})


def test_resume_binds_same_call_and_keeps_sensitive_values_out_of_repr() -> None:
    request = _request()
    resume = InteractionResume(
        interaction_id="interaction-a",
        revision=1,
        request=request,
        input_responses={"leave-form": {"action": "accept", "content": {"start": "2026-09-01"}}},
    )

    rendered = repr(resume)

    assert resume.call_digest == request.call_digest
    assert "2026-09-01" not in rendered
    assert "opaque-request-state" not in rendered


def test_pause_signal_bypasses_upstream_exception_to_tool_result_conversion() -> None:
    paused = MCPInteractionPaused(interaction_id="interaction-a", revision=1)

    assert isinstance(paused, BaseException)
    assert not isinstance(paused, Exception)
    assert "interaction-a" not in str(paused)
    assert "interaction-a" not in repr(paused)
