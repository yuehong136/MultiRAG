"""Closed unit tests for framework-neutral EIM-I6 provisioning."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from api.identity.contracts import (
    AliasKey,
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProviderAliasType,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
    ProvisioningPolicySnapshot,
    UserMembershipRecord,
)
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from api.identity.providers.contracts import (
    ProviderDirectoryStatus,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.provisioning import HmacLinkCodeCodec, IdentityProvisioningService
from api.identity.provisioning_contracts import (
    IdentityProvisioningRepository,
    LinkCodeGrantRecord,
    LinkCodeIssueCommand,
    LinkCodeIssueRequest,
    LinkCodeIssueStatus,
    ProvisionIdentityRequest,
    ProvisioningOutcome,
    ProvisioningPolicyAdministrationRepository,
    ProvisioningPolicyCreate,
    ProvisioningPolicyUpdate,
    ProvisioningPolicyWriteOutcome,
    ProvisioningPolicyWriteResult,
    ProvisioningRepositoryError,
    ProvisioningResult,
    ProvisioningStatus,
    VerifiedProvisioningCommand,
)

_NOW = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
_KEY = b"k" * 32
_SENSITIVE = "must-never-appear"


def _context(**changes: object) -> ProviderContext:
    return replace(
        ProviderContext(
            tenant_id="tenant-1",
            provider="feishu",
            provider_tenant_key="provider-tenant-secret",
            provider_account_id="provider-account-secret",
            provider_account_key="provider-app-secret",
            provider_account_revision=7,
            provider_account_last_scope_change_at=_NOW - timedelta(minutes=4),
        ),
        **changes,
    )


def _resolution_request(context: ProviderContext | None = None) -> IdentityResolutionRequest:
    return IdentityResolutionRequest(
        context=context or _context(),
        alias=AliasKey(
            alias_type=ProviderAliasType.OPEN_ID,
            alias_value="open-id-secret",
        ),
    )


def _plan(action: ProvisioningAction) -> IdentityResolutionResult:
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.MISSING,
        error_code=(IdentityErrorCode.LINK_REQUIRED if action is ProvisioningAction.REQUIRE_LINK else None),
        provisioning_action=action,
        provisioning_policy_revision=11,
        provider_verification_required=True,
    )


def _stale_alias_plan() -> IdentityResolutionResult:
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.INACTIVE,
        error_code=IdentityErrorCode.INACTIVE,
        provider_verification_required=True,
    )


def _provider_result(**changes: object) -> ProviderIdentityResult:
    identity = ProviderIdentity(
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        provider_account_id="provider-account-secret",
        provider_user_id="stable-user-secret",
        verified_at=_NOW,
        open_id="open-id-secret",
        union_id="union-id-secret",
        employee_no="employee-number-must-not-persist",
        display_name="  Alice\tExample  ",
        provider_status=ProviderDirectoryStatus.ACTIVE,
    )
    identity_changes = changes.pop("identity", None)
    if isinstance(identity_changes, dict):
        identity = replace(identity, **identity_changes)
    result = ProviderIdentityResult(
        status=ProviderIdentityStatus.RESOLVED,
        identity=identity,
    )
    return replace(result, **changes)


def _provision_request(
    action: ProvisioningAction = ProvisioningAction.CREATE_NORMAL_MEMBER,
    *,
    context: ProviderContext | None = None,
    resolution: IdentityResolutionResult | None = None,
    provider_result: ProviderIdentityResult | None = None,
    link_code: str | None = None,
) -> ProvisionIdentityRequest:
    return ProvisionIdentityRequest(
        resolution_request=_resolution_request(context),
        resolution=resolution or _plan(action),
        provider_result=provider_result or _provider_result(),
        link_code=link_code,
    )


def _identity() -> ExternalIdentityRecord:
    return ExternalIdentityRecord(
        id="identity-secret",
        tenant_id="tenant-1",
        user_id="platform-user-secret",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        subject_type="provider_user_id",
        subject_value="stable-user-secret",
        state="active",
        verified_at=_NOW,
        last_seen_at=_NOW,
        identity_revision=2,
    )


def _successful_result() -> ProvisioningResult:
    identity = _identity()
    return ProvisioningResult(
        status=ProvisioningStatus.RESOLVED,
        outcome=ProvisioningOutcome.JIT_CREATED,
        identity=identity,
        membership=UserMembershipRecord(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            role="normal",
        ),
    )


def _policy(
    mode: ProvisioningMode = ProvisioningMode.LINK_ONLY,
    *,
    ttl: int = 600,
) -> ProvisioningPolicySnapshot:
    return ProvisioningPolicySnapshot(
        tenant_id="tenant-1",
        mode=mode,
        revision=11,
        link_code_ttl_seconds=ttl,
        changed_at=_NOW,
    )


def _principal(*, tenant_id: str = "tenant-1"):
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(
            platform_user_id="authenticated-user-secret",
            display_name="Authenticated user",
        ),
        membership=TenantMembershipEvidence(
            platform_user_id="authenticated-user-secret",
            tenant_id=tenant_id,
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=_NOW,
        ),
    )


class _Repository:
    def __init__(self) -> None:
        self.provision_commands: list[VerifiedProvisioningCommand] = []
        self.issue_commands: list[LinkCodeIssueCommand] = []
        self.provision_result = _successful_result()
        self.provision_error: IdentityErrorCode | None = None
        self.issue_error: IdentityErrorCode | None = None

    async def provision_verified_identity(
        self,
        command: VerifiedProvisioningCommand,
    ) -> ProvisioningResult:
        self.provision_commands.append(command)
        if self.provision_error is not None:
            raise ProvisioningRepositoryError(self.provision_error)
        return self.provision_result

    async def issue_link_code(
        self,
        command: LinkCodeIssueCommand,
    ) -> LinkCodeGrantRecord:
        self.issue_commands.append(command)
        if self.issue_error is not None:
            raise ProvisioningRepositoryError(self.issue_error)
        return LinkCodeGrantRecord(
            id="grant-secret",
            issued_at=command.issued_at,
            expires_at=command.expires_at,
            policy_revision=command.policy_revision,
            provider_account_revision=command.provider_account_revision,
            provider_account_last_scope_change_at=command.provider_account_last_scope_change_at,
        )


class _PolicyResolver:
    def __init__(self, snapshot: object = None) -> None:
        self.snapshot = _policy() if snapshot is None else snapshot
        self.tenant_ids: list[str] = []

    async def get_policy(self, tenant_id: str) -> ProvisioningPolicySnapshot | None:
        self.tenant_ids.append(tenant_id)
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot if isinstance(self.snapshot, ProvisioningPolicySnapshot) else None


def _codec(*, token_bytes=lambda size: b"t" * size) -> HmacLinkCodeCodec:
    return HmacLinkCodeCodec(
        keys={"key-1": _KEY},
        active_key_id="key-1",
        token_bytes=token_bytes,
    )


def _service(
    *,
    repository: _Repository | None = None,
    policy: _PolicyResolver | None = None,
    codec: HmacLinkCodeCodec | None = None,
    now=lambda: _NOW,
) -> tuple[IdentityProvisioningService, _Repository, _PolicyResolver]:
    repo = repository or _Repository()
    resolver = policy or _PolicyResolver()
    return (
        IdentityProvisioningService(
            repo,
            resolver,
            codec or _codec(),
            now=now,
        ),
        repo,
        resolver,
    )


def test_link_code_codec_is_192_bit_hmac_keyed_and_domain_separated() -> None:
    codec = _codec()

    material = codec.issue()
    parsed = codec.digest(material.raw_code)
    fingerprint = codec.fingerprint(("low-entropy-user", "low-entropy-provider"))

    assert parsed is not None
    assert parsed.key_id == material.key_id
    assert parsed.digest == material.digest
    assert len(material.digest) == 64
    assert fingerprint.digest != material.digest
    assert _SENSITIVE not in repr(material)
    assert material.raw_code not in repr(material)
    assert material.digest not in repr(material)
    assert fingerprint.digest not in repr(fingerprint)


@pytest.mark.parametrize(
    "raw_code",
    [
        "",
        "missing-separator",
        "unknown.dHR0dHR0dHR0dHR0dHR0dHR0dHR0dHR0",
        "key-1.not+urlsafe",
        "key-1.dG9vLXNob3J0",
        "key-1." + "x" * 600,
    ],
)
def test_link_code_codec_rejects_malformed_or_wrong_generation(raw_code: str) -> None:
    assert _codec().digest(raw_code) is None


@pytest.mark.parametrize(
    "token_bytes",
    [
        lambda _size: b"short",
        lambda _size: b"long" * 20,
        lambda _size: bytearray(b"t" * 24),
    ],
)
def test_link_code_entropy_source_must_return_exact_24_byte_bytes(token_bytes) -> None:
    with pytest.raises(ValueError, match="entropy source"):
        _codec(token_bytes=token_bytes).issue()


@pytest.mark.parametrize(
    ("keys", "active_key_id"),
    [
        ({}, "key-1"),
        ({"key-1": b"short"}, "key-1"),
        ({"bad key": _KEY}, "bad key"),
        ({"key-1": _KEY}, "missing"),
    ],
)
def test_link_code_codec_requires_explicit_strong_keyring(
    keys: dict[str, bytes],
    active_key_id: str,
) -> None:
    with pytest.raises(ValueError, match="keyring"):
        HmacLinkCodeCodec(keys=keys, active_key_id=active_key_id)


@pytest.mark.parametrize(
    "action",
    [
        ProvisioningAction.BIND_PREPROVISIONED,
        ProvisioningAction.REQUIRE_LINK,
        ProvisioningAction.CREATE_NORMAL_MEMBER,
    ],
)
async def test_all_three_modes_project_only_verified_minimal_commands(
    action: ProvisioningAction,
) -> None:
    service, repository, _policy_resolver = _service()
    link_code = _codec().issue().raw_code if action is ProvisioningAction.REQUIRE_LINK else None

    result = await service.provision_verified_identity(
        _provision_request(action, link_code=link_code),
    )

    assert result.status is ProvisioningStatus.RESOLVED
    [command] = repository.provision_commands
    assert command.action is action
    assert command.policy_revision == 11
    assert command.subject_value == "stable-user-secret"
    assert command.asserted_alias.alias_type is ProviderAliasType.OPEN_ID
    assert tuple(alias.alias_type for alias in command.aliases) == (
        ProviderAliasType.OPEN_ID,
        ProviderAliasType.UNION_ID,
    )
    assert command.jit_nickname == "Alice Example"
    assert not hasattr(command, "target_user_id")
    assert not hasattr(command, "employee_no")
    assert "employee-number-must-not-persist" not in repr(command)
    assert "stable-user-secret" not in repr(command)
    assert len(command.request_digest) == 64
    assert command.request_digest not in repr(command)
    if action is ProvisioningAction.REQUIRE_LINK:
        assert command.link_code_key_id == "key-1"
        assert command.link_code_digest is not None
    else:
        assert command.link_code_key_id is None
        assert command.link_code_digest is None


async def test_link_only_without_code_reaches_canonical_first_repository_path() -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(ProvisioningAction.REQUIRE_LINK),
    )

    assert result.status is ProvisioningStatus.RESOLVED
    [command] = repository.provision_commands
    assert command.action is ProvisioningAction.REQUIRE_LINK
    assert command.link_code_digest is None


async def test_stale_alias_reverification_does_not_fabricate_policy_or_target() -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(resolution=_stale_alias_plan()),
    )

    assert result.status is ProvisioningStatus.RESOLVED
    [command] = repository.provision_commands
    assert command.action is None
    assert command.policy_revision is None
    assert command.link_code_digest is None
    assert not hasattr(command, "target_user_id")


@pytest.mark.parametrize(
    "resolution",
    [
        IdentityResolutionResult(status=IdentityResolutionStatus.RESOLVED),
        replace(_plan(ProvisioningAction.CREATE_NORMAL_MEMBER), provisioning_policy_revision=None),
        replace(_plan(ProvisioningAction.CREATE_NORMAL_MEMBER), provisioning_action=None),
        replace(_plan(ProvisioningAction.CREATE_NORMAL_MEMBER), provider_verification_required=False),
        replace(_plan(ProvisioningAction.CREATE_NORMAL_MEMBER), error_code=IdentityErrorCode.INACTIVE),
        replace(_stale_alias_plan(), provisioning_action=ProvisioningAction.REQUIRE_LINK, provisioning_policy_revision=1),
    ],
)
async def test_only_exact_i3_plan_shapes_can_trigger_repository(
    resolution: IdentityResolutionResult,
) -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(resolution=resolution),
    )

    assert result == ProvisioningResult(
        status=ProvisioningStatus.REJECTED,
        error_code=IdentityErrorCode.TRANSITION_INVALID,
    )
    assert repository.provision_commands == []


@pytest.mark.parametrize(
    ("provider_result", "error_code"),
    [
        (
            _provider_result(identity={"provider": "dingtalk"}),
            IdentityErrorCode.PROVIDER_MISMATCH,
        ),
        (
            _provider_result(identity={"provider_tenant_key": "other"}),
            IdentityErrorCode.TENANT_MISMATCH,
        ),
        (
            _provider_result(identity={"provider_account_id": "other"}),
            IdentityErrorCode.PROVIDER_MISMATCH,
        ),
        (
            _provider_result(identity={"open_id": "other"}),
            IdentityErrorCode.ASSERTION_INVALID,
        ),
        (
            _provider_result(identity={"provider_status": ProviderDirectoryStatus.ACTIVE, "verified_at": _NOW - timedelta(seconds=301)}),
            IdentityErrorCode.ASSERTION_INVALID,
        ),
        (
            _provider_result(identity={"verified_at": _NOW + timedelta(microseconds=1)}),
            IdentityErrorCode.ASSERTION_INVALID,
        ),
        (
            _provider_result(status=ProviderIdentityStatus.INACTIVE, identity=None, error_code=ProviderErrorCode.INACTIVE),
            IdentityErrorCode.INACTIVE,
        ),
        (
            _provider_result(status=ProviderIdentityStatus.CONFLICT, identity=None, error_code=ProviderErrorCode.LINK_CONFLICT),
            IdentityErrorCode.LINK_CONFLICT,
        ),
        (
            _provider_result(status=ProviderIdentityStatus.NOT_FOUND, identity=None, error_code=ProviderErrorCode.NOT_FOUND),
            IdentityErrorCode.NOT_FOUND,
        ),
    ],
)
async def test_provider_proof_must_be_fresh_active_and_exactly_scoped(
    provider_result: ProviderIdentityResult,
    error_code: IdentityErrorCode,
) -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(provider_result=provider_result),
    )

    assert result.error_code is error_code
    assert repository.provision_commands == []


async def test_scope_marker_newer_than_proof_rejects_before_repository() -> None:
    context = _context(provider_account_last_scope_change_at=_NOW + timedelta(seconds=1))
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(context=context),
    )

    assert result.error_code is IdentityErrorCode.ASSERTION_INVALID
    assert repository.provision_commands == []


async def test_proof_at_exact_five_minute_freshness_boundary_is_accepted() -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(
            context=_context(
                provider_account_last_scope_change_at=_NOW - timedelta(minutes=6),
            ),
            provider_result=_provider_result(
                identity={"verified_at": _NOW - timedelta(minutes=5)},
            ),
        )
    )

    assert result.status is ProvisioningStatus.RESOLVED
    assert len(repository.provision_commands) == 1


async def test_provision_reads_clock_once_and_rejects_invalid_clock() -> None:
    calls = 0

    def invalid_now() -> datetime:
        nonlocal calls
        calls += 1
        return _NOW.replace(tzinfo=None)

    service, repository, _policy_resolver = _service(now=invalid_now)

    result = await service.provision_verified_identity(_provision_request())

    assert calls == 1
    assert result.error_code is IdentityErrorCode.REPOSITORY_UNAVAILABLE
    assert repository.provision_commands == []


@pytest.mark.parametrize(
    "display_name",
    [
        None,
        "\u3000",
        "Ａ" * 101,
        "  First\n  Last  ",
    ],
)
async def test_jit_nickname_is_presentation_only_normalized_and_bounded(
    display_name: str | None,
) -> None:
    service, repository, _policy_resolver = _service()

    await service.provision_verified_identity(
        _provision_request(provider_result=_provider_result(identity={"display_name": display_name})),
    )

    [command] = repository.provision_commands
    assert 1 <= len(command.jit_nickname) <= 100
    if display_name is None or not display_name.strip():
        assert command.jit_nickname == "Enterprise user"
    elif "First" in display_name:
        assert command.jit_nickname == "First Last"
    else:
        assert command.jit_nickname == "A" * 100


async def test_invalid_link_code_is_indistinguishable_from_link_required() -> None:
    service, repository, _policy_resolver = _service()

    result = await service.provision_verified_identity(
        _provision_request(
            ProvisioningAction.REQUIRE_LINK,
            link_code="not-a-valid-code",
        ),
    )

    assert result.error_code is IdentityErrorCode.LINK_REQUIRED
    assert repository.provision_commands == []


@pytest.mark.parametrize(
    "repository_error",
    [
        IdentityErrorCode.NOT_FOUND,
        IdentityErrorCode.REVISION_CONFLICT,
        IdentityErrorCode.TRANSITION_INVALID,
        IdentityErrorCode.OWNERSHIP_CONFLICT,
        IdentityErrorCode.INACTIVE,
        IdentityErrorCode.LINK_CONFLICT,
    ],
)
async def test_link_grant_failures_share_one_safe_outward_result(
    repository_error: IdentityErrorCode,
) -> None:
    repository = _Repository()
    repository.provision_error = repository_error
    service, _repository, _policy_resolver = _service(repository=repository)

    result = await service.provision_verified_identity(
        _provision_request(
            ProvisioningAction.REQUIRE_LINK,
            link_code=_codec().issue().raw_code,
        ),
    )

    assert result == ProvisioningResult(
        status=ProvisioningStatus.REJECTED,
        error_code=IdentityErrorCode.LINK_REQUIRED,
    )


async def test_non_link_repository_errors_keep_stable_safe_code() -> None:
    repository = _Repository()
    repository.provision_error = IdentityErrorCode.REPOSITORY_UNAVAILABLE
    service, _repository, _policy_resolver = _service(repository=repository)

    result = await service.provision_verified_identity(_provision_request())

    assert result.error_code is IdentityErrorCode.REPOSITORY_UNAVAILABLE


async def test_link_code_issue_uses_authoritative_policy_and_principal_target() -> None:
    policy = _PolicyResolver(_policy(ttl=137))
    service, repository, resolver = _service(policy=policy)

    result = await service.issue_link_code(
        LinkCodeIssueRequest(
            context=_context(),
            principal=_principal(),
        )
    )

    assert result.status is LinkCodeIssueStatus.ISSUED
    assert result.code is not None
    assert result.code not in repr(result)
    [command] = repository.issue_commands
    assert resolver.tenant_ids == ["tenant-1"]
    assert command.target_user_id == "authenticated-user-secret"
    assert command.policy_revision == 11
    assert command.provider_account_revision == 7
    assert command.provider_account_last_scope_change_at == _NOW - timedelta(minutes=4)
    assert command.expires_at - command.issued_at == timedelta(seconds=137)
    assert command.code_digest not in repr(command)
    assert command.target_user_id not in repr(command)
    assert not hasattr(LinkCodeIssueRequest, "ttl")


@pytest.mark.parametrize(
    ("snapshot", "error_code"),
    [
        (False, IdentityErrorCode.POLICY_UNAVAILABLE),
        (RuntimeError("unavailable"), IdentityErrorCode.POLICY_UNAVAILABLE),
        (_policy(ProvisioningMode.JIT), IdentityErrorCode.TRANSITION_INVALID),
        (_policy(ProvisioningMode.PREPROVISIONED), IdentityErrorCode.TRANSITION_INVALID),
        (replace(_policy(), link_code_ttl_seconds=59), IdentityErrorCode.POLICY_UNAVAILABLE),
        (replace(_policy(), link_code_ttl_seconds=901), IdentityErrorCode.POLICY_UNAVAILABLE),
    ],
)
async def test_link_code_issue_fails_closed_without_exact_link_only_policy(
    snapshot: object,
    error_code: IdentityErrorCode,
) -> None:
    service, repository, _resolver = _service(policy=_PolicyResolver(snapshot))

    result = await service.issue_link_code(
        LinkCodeIssueRequest(context=_context(), principal=_principal()),
    )

    assert result.error_code is error_code
    assert result.code is None
    assert repository.issue_commands == []


async def test_link_code_issue_rejects_cross_tenant_principal() -> None:
    service, repository, policy = _service()

    result = await service.issue_link_code(
        LinkCodeIssueRequest(
            context=_context(),
            principal=_principal(tenant_id="tenant-2"),
        )
    )

    assert result.error_code is IdentityErrorCode.ASSERTION_INVALID
    assert repository.issue_commands == []
    assert policy.tenant_ids == []


def _public_methods(protocol: type[object]) -> set[str]:
    return {name for name, value in inspect.getmembers(protocol) if callable(value) and not name.startswith("_")}


def test_i6_ports_are_narrow_and_have_no_generic_crud_or_transaction_controls() -> None:
    provisioning = _public_methods(IdentityProvisioningRepository)
    administration = _public_methods(ProvisioningPolicyAdministrationRepository)

    assert provisioning == {"issue_link_code", "provision_verified_identity"}
    assert administration == {"create_policy", "cas_policy"}
    assert provisioning.isdisjoint(administration)
    forbidden = {
        "add",
        "save",
        "update",
        "delete",
        "commit",
        "rollback",
        "begin",
        "merge_users",
        "reparent_identity",
    }
    assert provisioning.isdisjoint(forbidden)
    assert administration.isdisjoint(forbidden)


def test_policy_admin_commands_are_explicit_revisioned_and_have_no_delete() -> None:
    create = ProvisioningPolicyCreate(
        tenant_id="tenant-1",
        mode=ProvisioningMode.LINK_ONLY,
        link_code_ttl_seconds=600,
        changed_at=_NOW,
    )
    update = ProvisioningPolicyUpdate(
        tenant_id="tenant-1",
        expected_revision=1,
        mode=ProvisioningMode.JIT,
        link_code_ttl_seconds=300,
        changed_at=_NOW,
    )
    result = ProvisioningPolicyWriteResult(
        outcome=ProvisioningPolicyWriteOutcome.APPLIED,
        snapshot=_policy(),
    )

    assert create.mode is ProvisioningMode.LINK_ONLY
    assert update.expected_revision == 1
    assert result.snapshot == _policy()
    assert _public_methods(ProvisioningPolicyAdministrationRepository) == {
        "create_policy",
        "cas_policy",
    }


def test_i6_dtos_are_frozen_slotted_and_redact_all_identifiers_and_digests() -> None:
    codec = _codec()
    material = codec.issue()
    command = LinkCodeIssueCommand(
        context=_context(),
        target_user_id=_SENSITIVE,
        digest_key_id=_SENSITIVE,
        code_digest=_SENSITIVE,
        policy_revision=1,
        provider_account_revision=7,
        provider_account_last_scope_change_at=_NOW,
        issued_at=_NOW,
        expires_at=_NOW + timedelta(minutes=10),
    )
    instances = (
        material,
        codec.fingerprint((_SENSITIVE,)),
        LinkCodeIssueRequest(context=_context(), principal=_principal()),
        command,
        LinkCodeGrantRecord(
            id=_SENSITIVE,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(minutes=10),
            policy_revision=1,
            provider_account_revision=7,
            provider_account_last_scope_change_at=_NOW,
        ),
        _provision_request(),
        _successful_result(),
    )

    for instance in instances:
        dto_type = type(instance)
        assert dto_type.__dataclass_params__.frozen is True
        assert "__slots__" in vars(dto_type)
        assert not hasattr(instance, "__dict__")
        assert _SENSITIVE not in repr(instance)
        with pytest.raises(FrozenInstanceError):
            setattr(instance, fields(instance)[0].name, object())
