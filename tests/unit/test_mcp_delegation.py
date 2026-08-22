"""EIM-P3 authority, isolation and per-operation credential contracts."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.identity.mcp_delegation.contracts import DelegationErrorCode, McpDelegationError
from api.identity.mcp_delegation.policy import load_grant_policy_snapshot, load_tool_policy_snapshot
from api.identity.mcp_delegation.service import McpDelegationService
from api.identity.mcp_issuer.contracts import IssuedMcpAccessToken
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from api.identity.run_context import RunContext
from common.constants import MCPServerType

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _canonical_revision(document: dict[str, object], revision_field: str) -> str:
    unsigned = {key: value for key, value in document.items() if key != revision_field}
    encoded = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _write_snapshots(
    tmp_path: Path,
    *,
    user_ids: tuple[str, ...] = ("user-a",),
) -> tuple[Path, Path, str]:
    tool_policy: dict[str, object] = {
        "profile": "secure",
        "services": [
            {
                "id": "leave",
                "namespace": "leave",
                "scopes": ["leave:read", "leave:submit"],
            }
        ],
        "snapshot_format": 2,
        "tools": [
            {
                "accepted_acr_values": [],
                "effect": "read",
                "enterprise_subject": None,
                "external_requirements": [],
                "name": "leave_get_balance",
                "replay_mode": "reusable",
                "required_amr": [],
                "required_scopes": ["leave:read"],
                "service_id": "leave",
            },
            {
                "accepted_acr_values": [],
                "effect": "side_effect",
                "enterprise_subject": None,
                "external_requirements": [],
                "name": "leave_submit_leave",
                "replay_mode": "single_use",
                "required_amr": [],
                "required_scopes": ["leave:submit"],
                "service_id": "leave",
            },
        ],
    }
    policy_revision = _canonical_revision(tool_policy, "policy_revision")
    tool_policy["policy_revision"] = policy_revision

    grant_policy: dict[str, object] = {
        "bindings": [
            {
                "audience": "https://gateway.ofmcp.example/mcp",
                "mcp_server_id": "server-a",
                "resource_name": "ofmcp_gateway",
            }
        ],
        "credential_generation": 7,
        "grants": [
            {
                "agent_id": "agent-a",
                "agent_revision_id": "release-a",
                "allowed_scopes": ["leave:read", "leave:submit"],
                "platform_user_id": user_id,
                "resource_name": "ofmcp_gateway",
                "tenant_id": "tenant-a",
            }
            for user_id in user_ids
        ],
        "policy_revision": policy_revision,
        "snapshot_format": 1,
    }
    grant_policy["grant_revision"] = _canonical_revision(grant_policy, "grant_revision")

    tool_path = tmp_path / "tool-policies.json"
    grant_path = tmp_path / "mcp-grants.json"
    tool_path.write_text(json.dumps(tool_policy), encoding="utf-8")
    grant_path.write_text(json.dumps(grant_policy), encoding="utf-8")
    return tool_path, grant_path, policy_revision


def _principal(user_id: str = "user-a") -> Principal:
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=user_id),
        membership=TenantMembershipEvidence(
            platform_user_id=user_id,
            tenant_id="tenant-a",
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=NOW,
        ),
    )


class _RecordingIssuer:
    def __init__(self) -> None:
        self.requests: list[tuple[object, object]] = []

    def resource_audience(self, resource_name: str) -> str | None:
        return {
            "ofmcp_gateway": "https://gateway.ofmcp.example/mcp",
        }.get(resource_name)

    def issue(self, request: object, grant: object) -> IssuedMcpAccessToken:
        self.requests.append((request, grant))
        sequence = len(self.requests)
        return IssuedMcpAccessToken(
            compact=f"sensitive-bearer-{sequence}",
            expires_at=NOW + timedelta(minutes=5),
            scopes=request.requested_scopes,  # type: ignore[attr-defined]
            kid="fixture",
        )


def _server(*, server_id: str = "server-a", tenant_id: str = "tenant-a") -> SimpleNamespace:
    return SimpleNamespace(
        id=server_id,
        tenant_id=tenant_id,
        url="https://gateway.ofmcp.example/mcp",
        server_type=MCPServerType.STREAMABLE_HTTP,
        headers={},
    )


def _run_context(user_id: str = "user-a") -> RunContext:
    return RunContext(
        tenant_id="tenant-a",
        principal=_principal(user_id),
        agent_id="agent-a",
        agent_revision_id="release-a",
    )


def test_snapshot_loaders_verify_both_canonical_revisions(tmp_path: Path) -> None:
    tool_path, grant_path, policy_revision = _write_snapshots(tmp_path)

    tool_policy = load_tool_policy_snapshot(tool_path)
    grant_policy = load_grant_policy_snapshot(grant_path, tool_policy=tool_policy)

    assert tool_policy.policy_revision == grant_policy.policy_revision == policy_revision
    assert grant_policy.credential_generation == 7

    tampered = json.loads(grant_path.read_text(encoding="utf-8"))
    tampered["grants"][0]["allowed_scopes"] = ["leave:read"]
    grant_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(McpDelegationError) as raised:
        load_grant_policy_snapshot(grant_path, tool_policy=tool_policy)
    assert raised.value.code is DelegationErrorCode.SNAPSHOT_INVALID


def test_authority_snapshot_rejects_group_writable_input(tmp_path: Path) -> None:
    tool_path, _, _ = _write_snapshots(tmp_path)
    tool_path.chmod(0o664)

    with pytest.raises(McpDelegationError) as raised:
        load_tool_policy_snapshot(tool_path)
    assert raised.value.code is DelegationErrorCode.SNAPSHOT_INVALID


def test_provider_uses_canonical_tool_and_issues_a_new_token_per_logical_call(tmp_path: Path) -> None:
    tool_path, grant_path, _ = _write_snapshots(tmp_path)
    issuer = _RecordingIssuer()
    tool_policy = load_tool_policy_snapshot(tool_path)
    service = McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=load_grant_policy_snapshot(grant_path, tool_policy=tool_policy),
        issuer=issuer,
    )
    provider = service.bind(mcp_server=_server(), run_context=_run_context())
    assert provider is not None
    assert provider.is_authorized("leave_submit_leave") is True
    assert provider.is_authorized("leave_submit_leave_9") is False

    first = provider.credential_for("leave_submit_leave")
    second = provider.credential_for("leave_submit_leave")
    interaction_authorization = provider.interaction_authorization("leave_submit_leave")

    assert first.bearer != second.bearer
    assert first.canonical_tool_name == "leave_submit_leave"
    assert first.resource_name == "ofmcp_gateway"
    assert first.replay_mode == "single_use"
    assert first.credential_generation == 7
    assert interaction_authorization.effect == "side_effect"
    assert interaction_authorization.replay_mode == "single_use"
    assert interaction_authorization.credential_generation == 7
    assert len(issuer.requests) == 2
    assert "sensitive-bearer" not in repr(first)

    with pytest.raises(McpDelegationError) as raised:
        provider.credential_for("leave_submit_leave_9")
    assert raised.value.code is DelegationErrorCode.TOOL_POLICY_NOT_FOUND
    assert len(issuer.requests) == 2


@pytest.mark.parametrize(
    ("policy_field", "policy_value"),
    [
        ("required_amr", ["webauthn"]),
        ("accepted_acr_values", ["urn:multirag:assurance:enterprise-verified"]),
        (
            "enterprise_subject",
            {
                "issuer": "https://hr.example",
                "tenant": "hr-tenant-a",
                "type": "workcode",
            },
        ),
    ],
)
def test_unproven_tool_assurance_is_denied_before_token_issuance(
    tmp_path: Path,
    policy_field: str,
    policy_value: object,
) -> None:
    tool_path, grant_path, _ = _write_snapshots(tmp_path)
    document = json.loads(tool_path.read_text(encoding="utf-8"))
    document["tools"][0][policy_field] = policy_value
    document["policy_revision"] = _canonical_revision(document, "policy_revision")
    tool_path.write_text(json.dumps(document), encoding="utf-8")

    grants = json.loads(grant_path.read_text(encoding="utf-8"))
    grants["policy_revision"] = document["policy_revision"]
    grants["grant_revision"] = _canonical_revision(grants, "grant_revision")
    grant_path.write_text(json.dumps(grants), encoding="utf-8")

    issuer = _RecordingIssuer()
    tool_policy = load_tool_policy_snapshot(tool_path)
    service = McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=load_grant_policy_snapshot(grant_path, tool_policy=tool_policy),
        issuer=issuer,
    )
    provider = service.bind(mcp_server=_server(), run_context=_run_context())
    assert provider is not None
    assert provider.is_authorized("leave_get_balance") is False

    with pytest.raises(McpDelegationError) as raised:
        provider.credential_for("leave_get_balance")
    assert raised.value.code is DelegationErrorCode.ASSURANCE_DENIED
    assert issuer.requests == []


@pytest.mark.parametrize(
    ("server", "context", "expected"),
    [
        (_server(tenant_id="other-tenant"), _run_context(), DelegationErrorCode.SERVER_TENANT_MISMATCH),
        (_server(), RunContext(tenant_id="tenant-a", principal=_principal()), DelegationErrorCode.CONTEXT_REQUIRED),
        (_server(), _run_context("user-b"), DelegationErrorCode.GRANT_NOT_FOUND),
    ],
)
def test_delegated_binding_fails_closed_before_network(
    tmp_path: Path,
    server: SimpleNamespace,
    context: RunContext,
    expected: DelegationErrorCode,
) -> None:
    tool_path, grant_path, _ = _write_snapshots(tmp_path)
    tool_policy = load_tool_policy_snapshot(tool_path)
    service = McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=load_grant_policy_snapshot(grant_path, tool_policy=tool_policy),
        issuer=_RecordingIssuer(),
    )

    with pytest.raises(McpDelegationError) as raised:
        provider = service.bind(mcp_server=server, run_context=context)
        assert provider is not None
        provider.credential_for("leave_get_balance")
    assert raised.value.code is expected


def test_unbound_server_preserves_legacy_static_mode(tmp_path: Path) -> None:
    tool_path, grant_path, _ = _write_snapshots(tmp_path)
    tool_policy = load_tool_policy_snapshot(tool_path)
    service = McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=load_grant_policy_snapshot(grant_path, tool_policy=tool_policy),
        issuer=_RecordingIssuer(),
    )

    assert service.bind(mcp_server=_server(server_id="legacy-server"), run_context=None) is None


def test_concurrent_principals_never_share_a_bearer_or_decision_key(tmp_path: Path) -> None:
    tool_path, grant_path, _ = _write_snapshots(
        tmp_path,
        user_ids=("user-a", "user-b"),
    )

    class SubjectIssuer(_RecordingIssuer):
        def issue(self, request: object, grant: object) -> IssuedMcpAccessToken:
            result = super().issue(request, grant)
            return IssuedMcpAccessToken(
                compact=f"opaque-{request.principal.platform_user_id}-{len(self.requests)}",  # type: ignore[attr-defined]
                expires_at=result.expires_at,
                scopes=result.scopes,
                kid=result.kid,
            )

    issuer = SubjectIssuer()
    tool_policy = load_tool_policy_snapshot(tool_path)
    service = McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=load_grant_policy_snapshot(grant_path, tool_policy=tool_policy),
        issuer=issuer,
    )
    providers = [service.bind(mcp_server=_server(), run_context=_run_context(user_id)) for user_id in ("user-a", "user-b")]
    assert all(provider is not None for provider in providers)

    with ThreadPoolExecutor(max_workers=2) as pool:
        credentials = list(
            pool.map(
                lambda provider: provider.credential_for("leave_get_balance"),  # type: ignore[union-attr]
                providers,
            )
        )

    assert credentials[0].bearer.startswith("opaque-user-a-")
    assert credentials[1].bearer.startswith("opaque-user-b-")
    assert credentials[0].bearer != credentials[1].bearer
