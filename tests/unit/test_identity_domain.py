"""Closed unit tests for the framework-neutral EIM-I3 identity domain."""

from __future__ import annotations

import inspect
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from datetime import UTC, datetime
from typing import cast, get_type_hints

import pytest
from beartype.roar import BeartypeCallHintParamViolation

import api.identity.contracts as identity_contracts
from api.identity.contracts import (
    AliasKey,
    CasOutcome,
    CasResult,
    ExternalIdentityAliasInsert,
    ExternalIdentityInsert,
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityLookupRepository,
    IdentityMutationRepository,
    IdentityRepository,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    IdentityResolutionSnapshot,
    IdentityResolutionStatus,
    IdentityStateTransition,
    InsertOutcome,
    InsertResult,
    ProviderAccountControlRepository,
    ProviderAccountHealthCAS,
    ProviderAccountInsertResult,
    ProviderAccountRecord,
    ProviderAliasType,
    ProviderContext,
    ProviderTenantInsertResult,
    ProviderTenantRecord,
    ProvisioningAction,
    ProvisioningDecision,
    ProvisioningMode,
    ProvisioningPolicySnapshot,
    UserMembershipRecord,
    VerifiedIdentityActivation,
    VerifiedIdentityMutationRepository,
    VerifiedOwnershipRepository,
    VerifiedProviderAccountOnboarding,
    VerifiedProviderTenantOnboarding,
)
from api.identity.policy import decide_provisioning
from api.identity.service import IdentityService

_NOW = datetime(2026, 8, 12, 9, 30, tzinfo=UTC)
_SENSITIVE = "must-not-appear-in-repr"


def _context(**changes: object) -> ProviderContext:
    context = ProviderContext(
        tenant_id="tenant-1",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        provider_account_id="provider-account-secret",
        provider_account_key="provider-app-secret",
        provider_account_revision=7,
    )
    return replace(context, **changes)


def _account(
    context: ProviderContext | None = None,
    *,
    health: str = "healthy",
) -> ProviderAccountRecord:
    scoped = context or _context()
    return ProviderAccountRecord(
        id=scoped.provider_account_id,
        tenant_id=scoped.tenant_id,
        provider=scoped.provider,
        provider_tenant_key=scoped.provider_tenant_key,
        provider_account_key=scoped.provider_account_key,
        identity_revision=scoped.provider_account_revision,
        identity_health_state=health,
    )


def _identity(
    context: ProviderContext | None = None,
    *,
    state: str = "active",
) -> ExternalIdentityRecord:
    scoped = context or _context()
    return ExternalIdentityRecord(
        id="external-identity-secret",
        tenant_id=scoped.tenant_id,
        user_id="platform-user-secret",
        provider=scoped.provider,
        provider_tenant_key=scoped.provider_tenant_key,
        subject_type="provider_user_id",
        subject_value="provider-subject-secret",
        state=state,
        verified_at=_NOW,
        last_seen_at=_NOW,
        identity_revision=4,
    )


def _membership(
    identity: ExternalIdentityRecord | None = None,
    *,
    tenant_id: str = "tenant-1",
    role: str = "normal",
) -> UserMembershipRecord:
    linked = identity or _identity()
    return UserMembershipRecord(
        user_id=linked.user_id,
        tenant_id=tenant_id,
        role=role,
    )


def _request(context: ProviderContext | None = None) -> IdentityResolutionRequest:
    return IdentityResolutionRequest(
        context=context or _context(),
        alias=AliasKey(
            alias_type=ProviderAliasType.OPEN_ID,
            alias_value="provider-alias-secret",
        ),
    )


class _LookupRepository:
    def __init__(self, snapshot: IdentityResolutionSnapshot | None) -> None:
        self.snapshot = snapshot
        self.requests: list[IdentityResolutionRequest] = []

    async def resolve_identity(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionSnapshot | None:
        self.requests.append(request)
        return self.snapshot

    async def get_provider_account(
        self,
        context: ProviderContext,
        *,
        for_update: bool = False,
    ) -> ProviderAccountRecord | None:
        del context, for_update
        return self.snapshot.account if self.snapshot is not None else None


class _PolicyResolver:
    def __init__(self, mode: object = ProvisioningMode.LINK_ONLY) -> None:
        self.mode = mode
        self.tenant_ids: list[str] = []

    async def get_policy(
        self,
        tenant_id: str,
    ) -> ProvisioningPolicySnapshot | None:
        self.tenant_ids.append(tenant_id)
        if isinstance(self.mode, Exception):
            raise self.mode
        if isinstance(self.mode, ProvisioningMode):
            return ProvisioningPolicySnapshot(
                tenant_id=tenant_id,
                mode=self.mode,
                revision=9,
                link_code_ttl_seconds=600,
                changed_at=_NOW,
            )
        return cast(ProvisioningPolicySnapshot | None, self.mode)


def _service(
    snapshot: IdentityResolutionSnapshot | None,
    *,
    mode: object = ProvisioningMode.LINK_ONLY,
) -> tuple[IdentityService, _LookupRepository, _PolicyResolver]:
    repository = _LookupRepository(snapshot)
    policy = _PolicyResolver(mode)
    return IdentityService(repository, policy), repository, policy


@pytest.mark.parametrize(
    ("mode", "action", "member_role"),
    [
        (ProvisioningMode.PREPROVISIONED, ProvisioningAction.BIND_PREPROVISIONED, None),
        (ProvisioningMode.LINK_ONLY, ProvisioningAction.REQUIRE_LINK, None),
        (ProvisioningMode.JIT, ProvisioningAction.CREATE_NORMAL_MEMBER, "normal"),
    ],
)
def test_provisioning_policy_has_three_explicit_provider_neutral_modes(
    mode: ProvisioningMode,
    action: ProvisioningAction,
    member_role: str | None,
) -> None:
    assert decide_provisioning(mode) == ProvisioningDecision(
        action=action,
        member_role=member_role,
    )


def test_unknown_provisioning_mode_fails_closed() -> None:
    with pytest.raises(
        (ValueError, BeartypeCallHintParamViolation),
        match=r"unsupported provisioning mode|violates type hint",
    ):
        decide_provisioning(cast(ProvisioningMode, "unknown"))


@pytest.mark.parametrize(
    ("mode", "action", "error_code"),
    [
        (ProvisioningMode.PREPROVISIONED, ProvisioningAction.BIND_PREPROVISIONED, None),
        (ProvisioningMode.LINK_ONLY, ProvisioningAction.REQUIRE_LINK, IdentityErrorCode.LINK_REQUIRED),
        (ProvisioningMode.JIT, ProvisioningAction.CREATE_NORMAL_MEMBER, None),
    ],
)
async def test_valid_account_with_missing_alias_returns_verification_gated_plan_only(
    mode: ProvisioningMode,
    action: ProvisioningAction,
    error_code: IdentityErrorCode | None,
) -> None:
    context = _context()
    snapshot = IdentityResolutionSnapshot(
        account=_account(context),
        identity=None,
        membership=None,
    )
    service, repository, policy = _service(snapshot, mode=mode)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.MISSING,
        error_code=error_code,
        provisioning_action=action,
        provisioning_policy_revision=9,
        provider_verification_required=True,
    )
    assert policy.tenant_ids == [context.tenant_id]


@pytest.mark.parametrize("bad_mode", ["unknown", None, RuntimeError("policy unavailable")])
async def test_missing_alias_with_unknown_or_unavailable_policy_fails_closed(
    bad_mode: object,
) -> None:
    context = _context()
    snapshot = IdentityResolutionSnapshot(account=_account(context), identity=None, membership=None)
    service, repository, _policy = _service(snapshot, mode=bad_mode)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.CONFLICT,
        error_code=IdentityErrorCode.POLICY_UNAVAILABLE,
    )


async def test_invalid_provider_context_never_becomes_an_alias_miss() -> None:
    context = _context()
    service, repository, policy = _service(None, mode=ProvisioningMode.JIT)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.CONFLICT,
        error_code=IdentityErrorCode.TENANT_MISMATCH,
    )
    assert repository.requests == [_request(context)]
    assert policy.tenant_ids == []

    oversized_context = _context(provider_account_key="x" * 256)
    oversized_context_result = await service.resolve_external_identity(
        _request(oversized_context),
    )
    oversized_alias_result = await service.resolve_external_identity(
        replace(
            _request(context),
            alias=AliasKey(
                alias_type=ProviderAliasType.OPEN_ID,
                alias_value="x" * 256,
            ),
        )
    )
    assert oversized_context_result.error_code is IdentityErrorCode.ASSERTION_INVALID
    assert oversized_alias_result.error_code is IdentityErrorCode.ASSERTION_INVALID
    trailing_space_result = await service.resolve_external_identity(
        _request(_context(provider_account_key="x" + " " * 255)),
    )
    assert trailing_space_result.error_code is IdentityErrorCode.ASSERTION_INVALID
    assert repository.requests == [_request(context)]


async def test_database_account_scope_mismatch_never_reaches_policy() -> None:
    requested = _context()
    actual = _context(provider_account_revision=8)
    snapshot = IdentityResolutionSnapshot(account=_account(actual), identity=None, membership=None)
    service, repository, policy = _service(snapshot, mode=ProvisioningMode.JIT)

    result = await service.resolve_external_identity(_request(requested))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.CONFLICT,
        error_code=IdentityErrorCode.PROVIDER_MISMATCH,
    )
    assert policy.tenant_ids == []


@pytest.mark.parametrize(
    "invalid_context",
    [
        _context(tenant_id=""),
        _context(provider=" "),
        _context(provider_tenant_key=""),
        _context(provider_account_id=""),
        _context(provider_account_key=""),
        _context(provider_account_revision=0),
        _context(provider_account_revision=1 << 63),
        _context(provider_account_last_scope_change_at=_NOW.replace(tzinfo=None)),
    ],
)
async def test_structurally_invalid_context_is_rejected_before_repository(
    invalid_context: ProviderContext,
) -> None:
    service, repository, policy = _service(None)

    result = await service.resolve_external_identity(_request(invalid_context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.CONFLICT,
        error_code=IdentityErrorCode.ASSERTION_INVALID,
    )
    assert repository.requests == []
    assert policy.tenant_ids == []


@pytest.mark.parametrize("health", ["pending", "degraded", "error", "disabled", "unknown"])
async def test_only_healthy_provider_account_can_resolve_identity(health: str) -> None:
    context = _context()
    identity = _identity(context)
    snapshot = IdentityResolutionSnapshot(
        account=_account(context, health=health),
        identity=identity,
        membership=_membership(identity),
    )
    service, repository, policy = _service(snapshot)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.INACTIVE,
        error_code=IdentityErrorCode.INACTIVE,
    )
    assert policy.tenant_ids == []


@pytest.mark.parametrize("state", ["pending_link", "inactive", "revoked", "unknown"])
async def test_non_active_identity_states_fail_closed(state: str) -> None:
    context = _context(
        **({"provider_account_last_scope_change_at": _NOW} if state == "revoked" else {}),
    )
    identity = _identity(context, state=state)
    snapshot = IdentityResolutionSnapshot(
        account=(replace(_account(context), last_scope_change_at=_NOW) if state == "revoked" else _account(context)),
        identity=identity,
        membership=_membership(identity),
        alias_verified_at=None,
    )
    service, repository, policy = _service(snapshot)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.INACTIVE,
        error_code=IdentityErrorCode.INACTIVE,
    )
    assert policy.tenant_ids == []


async def test_conflicting_identity_has_a_stable_conflict_result() -> None:
    context = _context(provider_account_last_scope_change_at=_NOW)
    identity = _identity(context, state="conflict")
    snapshot = IdentityResolutionSnapshot(
        account=replace(_account(context), last_scope_change_at=_NOW),
        identity=identity,
        membership=_membership(identity),
        alias_verified_at=None,
    )
    service, repository, policy = _service(snapshot)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.CONFLICT,
        error_code=IdentityErrorCode.LINK_CONFLICT,
    )
    assert policy.tenant_ids == []


@pytest.mark.parametrize(
    "membership",
    [
        None,
        _membership(tenant_id="other-tenant"),
        UserMembershipRecord(user_id="other-user", tenant_id="tenant-1", role="normal"),
        _membership(role="invite"),
        _membership(role="unknown"),
    ],
)
async def test_active_identity_requires_matching_active_membership(
    membership: UserMembershipRecord | None,
) -> None:
    context = _context()
    identity = _identity(context)
    snapshot = IdentityResolutionSnapshot(
        account=_account(context),
        identity=identity,
        membership=membership,
    )
    service, repository, policy = _service(snapshot)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.INACTIVE,
        error_code=IdentityErrorCode.INACTIVE,
    )
    assert policy.tenant_ids == []


async def test_active_identity_and_membership_resolve_without_provisioning_or_writes() -> None:
    context = _context()
    identity = _identity(context)
    membership = _membership(identity, role="admin")
    snapshot = IdentityResolutionSnapshot(
        account=_account(context),
        identity=identity,
        membership=membership,
    )
    service, repository, policy = _service(snapshot)

    result = await service.resolve_external_identity(_request(context))

    assert result == IdentityResolutionResult(
        status=IdentityResolutionStatus.RESOLVED,
        identity=identity,
        membership=membership,
    )
    assert len(repository.requests) == 1
    assert policy.tenant_ids == []
    assert not hasattr(result, "principal")


def _all_dto_instances() -> tuple[object, ...]:
    context = ProviderContext(
        tenant_id="tenant-1",
        provider="feishu",
        provider_tenant_key=_SENSITIVE,
        provider_account_id=_SENSITIVE,
        provider_account_key=_SENSITIVE,
        provider_account_revision=1,
    )
    alias = AliasKey(alias_type=ProviderAliasType.OPEN_ID, alias_value=_SENSITIVE)
    account = ProviderAccountRecord(
        id=_SENSITIVE,
        tenant_id="tenant-1",
        provider="feishu",
        provider_tenant_key=_SENSITIVE,
        provider_account_key=_SENSITIVE,
        identity_revision=1,
        identity_health_state="healthy",
        identity_health_error_code=_SENSITIVE,
    )
    provider_tenant = ProviderTenantRecord(
        id=_SENSITIVE,
        tenant_id="tenant-1",
        provider="feishu",
        provider_tenant_key=_SENSITIVE,
        verified_at=_NOW,
    )
    identity = ExternalIdentityRecord(
        id=_SENSITIVE,
        tenant_id="tenant-1",
        user_id=_SENSITIVE,
        provider="feishu",
        provider_tenant_key=_SENSITIVE,
        subject_type="provider_user_id",
        subject_value=_SENSITIVE,
        state="active",
        verified_at=_NOW,
        last_seen_at=_NOW,
        identity_revision=1,
        attributes=(("display_name", _SENSITIVE),),
    )
    membership = UserMembershipRecord(user_id=_SENSITIVE, tenant_id="tenant-1", role="normal")
    request = IdentityResolutionRequest(context=context, alias=alias)

    return (
        context,
        alias,
        account,
        provider_tenant,
        identity,
        membership,
        request,
        IdentityResolutionSnapshot(account=account, identity=identity, membership=membership),
        IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=identity,
            membership=membership,
        ),
        ProvisioningPolicySnapshot(
            tenant_id="tenant-1",
            mode=ProvisioningMode.JIT,
            revision=1,
            link_code_ttl_seconds=600,
            changed_at=_NOW,
        ),
        ProvisioningDecision(action=ProvisioningAction.CREATE_NORMAL_MEMBER, member_role="normal"),
        InsertResult(outcome=InsertOutcome.CREATED, record=identity),
        ProviderTenantInsertResult(outcome=InsertOutcome.CREATED, record=provider_tenant),
        ProviderAccountInsertResult(outcome=InsertOutcome.CREATED, record=account),
        ExternalIdentityInsert(
            context=context,
            user_id=_SENSITIVE,
            subject_type="provider_user_id",
            subject_value=_SENSITIVE,
            attributes=(("display_name", _SENSITIVE),),
        ),
        ExternalIdentityAliasInsert(
            context=context,
            external_identity_id=_SENSITIVE,
            alias_type=ProviderAliasType.OPEN_ID,
            alias_value=_SENSITIVE,
            verified_at=_NOW,
        ),
        VerifiedProviderTenantOnboarding(
            tenant_id="tenant-1",
            provider="feishu",
            provider_tenant_key=_SENSITIVE,
            verified_at=_NOW,
        ),
        VerifiedProviderAccountOnboarding(
            tenant_id="tenant-1",
            provider="feishu",
            provider_tenant_key=_SENSITIVE,
            provider_account_key=_SENSITIVE,
        ),
        IdentityStateTransition(
            context=context,
            external_identity_id=_SENSITIVE,
            expected_revision=1,
            target_state="inactive",
        ),
        VerifiedIdentityActivation(
            context=context,
            external_identity_id=_SENSITIVE,
            expected_revision=1,
            verified_at=_NOW,
        ),
        ProviderAccountHealthCAS(
            context=context,
            target_health_state="error",
            error_code=_SENSITIVE,
        ),
        CasResult(outcome=CasOutcome.APPLIED, revision=2),
    )


def test_every_domain_dto_is_frozen_slotted_and_redacts_sensitive_fields() -> None:
    instances = _all_dto_instances()
    declared_dto_types = {value for value in vars(identity_contracts).values() if isinstance(value, type) and value.__module__ == identity_contracts.__name__ and is_dataclass(value)}

    assert {type(instance) for instance in instances} == declared_dto_types
    for instance in instances:
        dto_type = type(instance)
        assert dto_type.__dataclass_params__.frozen is True
        assert "__slots__" in vars(dto_type)
        assert not hasattr(instance, "__dict__")
        assert _SENSITIVE not in repr(instance)
        with pytest.raises(FrozenInstanceError):
            setattr(instance, fields(instance)[0].name, object())


def _public_protocol_methods(protocol: type[object]) -> set[str]:
    return {name for name, value in inspect.getmembers(protocol) if callable(value) and not name.startswith("_")}


def test_repository_ports_have_exact_non_overlapping_least_privilege_surfaces() -> None:
    lookup_methods = _public_protocol_methods(IdentityLookupRepository)
    mutation_methods = _public_protocol_methods(IdentityMutationRepository)
    verified_mutation_methods = _public_protocol_methods(VerifiedIdentityMutationRepository)
    account_control_methods = _public_protocol_methods(ProviderAccountControlRepository)
    ownership_methods = _public_protocol_methods(VerifiedOwnershipRepository)
    aggregate_methods = _public_protocol_methods(IdentityRepository)

    assert lookup_methods == {"get_provider_account", "resolve_identity"}
    assert mutation_methods == {"insert_identity", "cas_identity_state"}
    assert verified_mutation_methods == {"insert_alias", "activate_verified_identity"}
    assert account_control_methods == {"cas_provider_account_health"}
    assert ownership_methods == {
        "insert_verified_provider_tenant",
        "insert_verified_provider_account",
    }
    assert lookup_methods.isdisjoint(mutation_methods)
    assert lookup_methods.isdisjoint(ownership_methods)
    assert mutation_methods.isdisjoint(ownership_methods)
    assert verified_mutation_methods.isdisjoint(lookup_methods | mutation_methods | ownership_methods)
    assert account_control_methods.isdisjoint(lookup_methods | mutation_methods | verified_mutation_methods | ownership_methods)
    assert aggregate_methods == lookup_methods | mutation_methods | verified_mutation_methods | account_control_methods

    forbidden = {
        "add",
        "save",
        "update",
        "delete",
        "commit",
        "rollback",
        "begin",
        "bind_channel",
        "rebind_channel",
        "unlink_channel",
    }

    assert lookup_methods.isdisjoint(forbidden)
    assert mutation_methods.isdisjoint(forbidden)
    assert verified_mutation_methods.isdisjoint(forbidden)
    assert account_control_methods.isdisjoint(forbidden)
    assert ownership_methods.isdisjoint(forbidden)
    assert aggregate_methods.isdisjoint(forbidden)
    assert not issubclass(IdentityRepository, VerifiedOwnershipRepository)


def test_identity_service_holds_only_the_lookup_port() -> None:
    service, repository, _policy = _service(None)

    assert set(vars(service)) == {"_repository", "_policy_resolver"}
    assert get_type_hints(IdentityService.__init__)["repository"] is IdentityLookupRepository
    assert isinstance(repository, IdentityLookupRepository)
    assert not isinstance(repository, IdentityMutationRepository)
    assert not isinstance(repository, VerifiedOwnershipRepository)
    assert set(vars(repository)) == {"snapshot", "requests"}
    assert _public_protocol_methods(type(repository)) == {
        "get_provider_account",
        "resolve_identity",
    }
    assert not hasattr(service, "_mutation_repository")
    assert not hasattr(service, "_ownership_repository")
    service_source = inspect.getsource(sys.modules[IdentityService.__module__])
    assert "IdentityMutationRepository" not in service_source
    assert "IdentityRepository" not in service_source
    assert "VerifiedOwnershipRepository" not in service_source


def test_importing_identity_domain_does_not_load_framework_or_channel_packages() -> None:
    script = """
import sys
import api.identity
banned = sorted(
    name for name in sys.modules
    if name == "fastmcp"
    or name.startswith("fastmcp.")
    or name == "api.apps"
    or name.startswith("api.apps.")
    or name == "api.channels"
    or name.startswith("api.channels.")
    or name == "api.channel_control"
    or name.startswith("api.channel_control.")
)
print(banned)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
