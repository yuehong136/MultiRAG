"""Strict loaders for published A4 tool policy and MultiRAG P3 grants."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from api.identity.mcp_delegation.contracts import (
    DelegatedEnterpriseSubjectRequirement,
    DelegatedServerBinding,
    DelegatedToolPolicy,
    DelegationErrorCode,
    DelegationGrant,
    GrantKey,
    GrantPolicySnapshot,
    McpDelegationError,
    ToolPolicySnapshot,
)

_MAX_SNAPSHOT_BYTES = 1 << 20
_SHA256_HEX = frozenset("0123456789abcdef")


def _reject() -> McpDelegationError:
    return McpDelegationError(DelegationErrorCode.SNAPSHOT_INVALID)


def _load_document(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink():
        raise _reject()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise _reject() from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_SNAPSHOT_BYTES or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o022):
            raise _reject()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_SNAPSHOT_BYTES + 1)
    except OSError:
        raise _reject() from None
    finally:
        os.close(descriptor)
    try:
        if not raw or len(raw) > _MAX_SNAPSHOT_BYTES:
            raise _reject()
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _reject() from exc
    if type(document) is not dict:
        raise _reject()
    return document


def _canonical_revision(document: dict[str, Any], revision_field: str) -> str:
    revision = document.get(revision_field)
    if type(revision) is not str or len(revision) != 64 or any(char not in _SHA256_HEX for char in revision):
        raise _reject()
    unsigned = {key: value for key, value in document.items() if key != revision_field}
    try:
        canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise _reject() from exc
    if not hashlib.sha256(canonical).hexdigest() == revision:
        raise _reject()
    return revision


def _text(value: object, *, max_length: int = 255) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > max_length or not value.isascii():
        raise _reject()
    return value


def _scope_list(value: object, *, allow_empty: bool = False) -> frozenset[str]:
    if type(value) is not list or (not value and not allow_empty):
        raise _reject()
    scopes = [_text(scope, max_length=128) for scope in value]
    if len(scopes) != len(set(scopes)) or any(" " in scope or '"' in scope or "\\" in scope for scope in scopes):
        raise _reject()
    return frozenset(scopes)


def _canonical_https_uri(value: object) -> str:
    uri = _text(value, max_length=4096)
    parsed = urlsplit(uri)
    if parsed.scheme != "https" or not parsed.netloc or parsed.hostname is None or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise _reject()
    try:
        _ = parsed.port
    except ValueError as exc:
        raise _reject() from exc
    return uri


def load_tool_policy_snapshot(path: Path) -> ToolPolicySnapshot:
    document = _load_document(path)
    if set(document) != {"policy_revision", "profile", "services", "snapshot_format", "tools"}:
        raise _reject()
    if document["snapshot_format"] != 2 or document["profile"] != "secure":
        raise _reject()
    policy_revision = _canonical_revision(document, "policy_revision")

    raw_services = document["services"]
    if type(raw_services) is not list or not raw_services or len(raw_services) > 128:
        raise _reject()
    service_scopes: dict[str, frozenset[str]] = {}
    namespaces: set[str] = set()
    scope_registry: set[str] = set()
    for raw_service in raw_services:
        if type(raw_service) is not dict or set(raw_service) != {"id", "namespace", "scopes"}:
            raise _reject()
        service_id = _text(raw_service["id"], max_length=128)
        namespace = _text(raw_service["namespace"], max_length=128)
        scopes = _scope_list(raw_service["scopes"])
        if service_id in service_scopes or namespace in namespaces:
            raise _reject()
        service_scopes[service_id] = scopes
        namespaces.add(namespace)
        scope_registry.update(scopes)

    raw_tools = document["tools"]
    if type(raw_tools) is not list or not raw_tools or len(raw_tools) > 4096:
        raise _reject()
    tools: dict[str, DelegatedToolPolicy] = {}
    expected_tool_fields = {
        "accepted_acr_values",
        "effect",
        "enterprise_subject",
        "external_requirements",
        "name",
        "replay_mode",
        "required_amr",
        "required_scopes",
        "service_id",
    }
    for raw_tool in raw_tools:
        if type(raw_tool) is not dict or set(raw_tool) != expected_tool_fields:
            raise _reject()
        canonical_name = _text(raw_tool["name"])
        service_id = _text(raw_tool["service_id"], max_length=128)
        required_scopes = _scope_list(raw_tool["required_scopes"])
        accepted_acr_values = _scope_list(raw_tool["accepted_acr_values"], allow_empty=True)
        required_amr = _scope_list(raw_tool["required_amr"], allow_empty=True)
        effect = raw_tool["effect"]
        replay_mode = raw_tool["replay_mode"]
        raw_enterprise_subject = raw_tool["enterprise_subject"]
        enterprise_subject: DelegatedEnterpriseSubjectRequirement | None = None
        if raw_enterprise_subject is not None:
            if type(raw_enterprise_subject) is not dict or set(raw_enterprise_subject) != {"issuer", "tenant", "type"}:
                raise _reject()
            enterprise_subject = DelegatedEnterpriseSubjectRequirement(
                subject_type=_text(raw_enterprise_subject["type"], max_length=64),
                issuer=_text(raw_enterprise_subject["issuer"], max_length=128),
                issuer_tenant=_text(raw_enterprise_subject["tenant"]),
            )
        external_requirements = raw_tool["external_requirements"]
        if type(external_requirements) is not list or len(external_requirements) > 128:
            raise _reject()
        for requirement in external_requirements:
            if type(requirement) is not dict or set(requirement) != {"kind", "policy_id"}:
                raise _reject()
            _text(requirement["kind"], max_length=64)
            _text(requirement["policy_id"], max_length=255)
        if (
            canonical_name in tools
            or service_id not in service_scopes
            or not required_scopes.issubset(service_scopes[service_id])
            or effect not in {"read", "prepare", "side_effect"}
            or replay_mode not in {"reusable", "single_use"}
            or (effect == "side_effect" and replay_mode != "single_use")
        ):
            raise _reject()
        tools[canonical_name] = DelegatedToolPolicy(
            canonical_tool_name=canonical_name,
            required_scopes=required_scopes,
            effect=effect,
            replay_mode=replay_mode,
            enterprise_subject=enterprise_subject,
            accepted_acr_values=accepted_acr_values,
            required_amr=required_amr,
        )
    return ToolPolicySnapshot.frozen(
        policy_revision=policy_revision,
        scope_registry=frozenset(scope_registry),
        tools=tools,
    )


def load_grant_policy_snapshot(
    path: Path,
    *,
    tool_policy: ToolPolicySnapshot,
) -> GrantPolicySnapshot:
    document = _load_document(path)
    if set(document) != {
        "bindings",
        "credential_generation",
        "grant_revision",
        "grants",
        "policy_revision",
        "snapshot_format",
    }:
        raise _reject()
    if document["snapshot_format"] != 1 or document["policy_revision"] != tool_policy.policy_revision:
        raise _reject()
    generation = document["credential_generation"]
    if type(generation) is not int or not 1 <= generation <= (1 << 63) - 1:
        raise _reject()
    grant_revision = _canonical_revision(document, "grant_revision")

    raw_bindings = document["bindings"]
    if type(raw_bindings) is not list or not raw_bindings or len(raw_bindings) > 1024:
        raise _reject()
    bindings: dict[str, DelegatedServerBinding] = {}
    resources: set[str] = set()
    for raw_binding in raw_bindings:
        if type(raw_binding) is not dict or set(raw_binding) != {"audience", "mcp_server_id", "resource_name"}:
            raise _reject()
        server_id = _text(raw_binding["mcp_server_id"])
        resource_name = _text(raw_binding["resource_name"], max_length=64)
        audience = _canonical_https_uri(raw_binding["audience"])
        if server_id in bindings or resource_name in resources or any(binding.audience == audience for binding in bindings.values()):
            raise _reject()
        bindings[server_id] = DelegatedServerBinding(
            mcp_server_id=server_id,
            resource_name=resource_name,
            audience=audience,
        )
        resources.add(resource_name)

    raw_grants = document["grants"]
    if type(raw_grants) is not list or not raw_grants or len(raw_grants) > 100_000:
        raise _reject()
    grants: dict[GrantKey, DelegationGrant] = {}
    expected_grant_fields = {
        "agent_id",
        "agent_revision_id",
        "allowed_scopes",
        "platform_user_id",
        "resource_name",
        "tenant_id",
    }
    for raw_grant in raw_grants:
        if type(raw_grant) is not dict or set(raw_grant) != expected_grant_fields:
            raise _reject()
        tenant_id = _text(raw_grant["tenant_id"])
        platform_user_id = _text(raw_grant["platform_user_id"])
        agent_id = _text(raw_grant["agent_id"])
        agent_revision_id = _text(raw_grant["agent_revision_id"])
        resource_name = _text(raw_grant["resource_name"], max_length=64)
        allowed_scopes = _scope_list(raw_grant["allowed_scopes"])
        key = (tenant_id, platform_user_id, agent_id, agent_revision_id, resource_name)
        if resource_name not in resources or not allowed_scopes.issubset(tool_policy.scope_registry) or key in grants:
            raise _reject()
        grants[key] = DelegationGrant(
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            agent_id=agent_id,
            agent_revision_id=agent_revision_id,
            resource_name=resource_name,
            allowed_scopes=allowed_scopes,
        )
    return GrantPolicySnapshot.frozen(
        grant_revision=grant_revision,
        policy_revision=tool_policy.policy_revision,
        credential_generation=generation,
        bindings=bindings,
        grants=grants,
    )


__all__ = ["load_grant_policy_snapshot", "load_tool_policy_snapshot"]
