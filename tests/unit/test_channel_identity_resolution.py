"""Closed unit contracts for Channel-to-EIM Principal promotion."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_execution.errors import ChannelIdentityResolutionError
from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionTargetRef,
    TrustedChannelContext,
)
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProviderContext,
    ProvisioningAction,
    UserMembershipRecord,
)
from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectErrorCode,
    EnterpriseSubjectEvidenceService,
    EnterpriseSubjectResolutionStatus,
    EnterpriseSubjectServiceResult,
)
from api.identity.principal import (
    AuthenticationSource,
    EnterpriseSubject,
    IdentityAssurance,
    VerifiedEnterpriseSubjectEvidence,
)
from api.identity.providers.contracts import (
    ExternalIdentityAssertion,
    ProviderDirectoryStatus,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.provisioning_contracts import (
    ProvisionIdentityRequest,
    ProvisioningOutcome,
    ProvisioningResult,
    ProvisioningStatus,
    ReverifyResolvedIdentityRequest,
)
from api.identity_adapters.channel_runtime import (
    ChannelIdentityAuthority,
    ChannelIdentityAuthorityStatus,
    ChannelIdentityResolver,
    IdentityProviderRegistry,
    SqlAlchemyChannelIdentityAuthorityResolver,
)

_PROOF_AT = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
_FINAL_AT = _PROOF_AT + timedelta(seconds=1)
_VALIDATED_AT = _PROOF_AT + timedelta(seconds=2)


def _context() -> TrustedChannelContext:
    return TrustedChannelContext(
        binding_id="binding-1",
        tenant_id="tenant-1",
        target=ExecutionTargetRef(
            target_type="multirag.canvas_agent",
            target_id="target-1",
            revision_id="revision-1",
        ),
        enabled=True,
        binding_generation=7,
        provider="feishu",
    )


def _provider_context(**changes: object) -> ProviderContext:
    return replace(
        ProviderContext(
            tenant_id="tenant-1",
            provider="feishu",
            provider_tenant_key="provider-tenant-secret",
            provider_account_id="provider-account-secret",
            provider_account_key="provider-app-secret",
            provider_account_revision=3,
        ),
        **changes,
    )


def _command(
    *,
    provider: str = "feishu",
    tenant_key: str = "provider-tenant-secret",
    subject: str = "open-id-secret",
    identifiers: tuple[tuple[str, str], ...] | None = None,
    include_identity: bool = True,
) -> ChannelExecutionCommand:
    actor: dict[str, object] = {
        "provider": provider,
        "subject": subject,
        "conversation": "chat-secret",
    }
    if include_identity:
        actor["identity"] = {
            "provider": provider,
            "provider_tenant_key": tenant_key,
            "identifiers": [
                {"kind": kind, "value": value}
                for kind, value in (
                    identifiers
                    or (
                        ("open_id", "open-id-secret"),
                        ("user_id", "provider-user-secret"),
                        ("union_id", "union-id-secret"),
                    )
                )
            ],
        }
    return ChannelExecutionCommand.model_validate(
        {
            "event_id": "event-1",
            "conversation_key": "feishu:chat:user",
            "message": {"type": "text", "content": "hello"},
            "actor": actor,
        }
    )


def _identity(*, verified_at: datetime = _FINAL_AT) -> ExternalIdentityRecord:
    return ExternalIdentityRecord(
        id="external-identity-secret",
        tenant_id="tenant-1",
        user_id="platform-user-secret",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        subject_type="user_id",
        subject_value="provider-user-secret",
        state="active",
        verified_at=verified_at,
        last_seen_at=verified_at,
        identity_revision=2,
        attributes=(("display_name", "Database display name"),),
    )


def _resolved(*, verified_at: datetime = _FINAL_AT) -> IdentityResolutionResult:
    identity = _identity(verified_at=verified_at)
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.RESOLVED,
        identity=identity,
        membership=UserMembershipRecord(
            user_id=identity.user_id,
            tenant_id=identity.tenant_id,
            role="normal",
        ),
    )


def _jit_plan() -> IdentityResolutionResult:
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.MISSING,
        provisioning_action=ProvisioningAction.CREATE_NORMAL_MEMBER,
        provisioning_policy_revision=1,
        provider_verification_required=True,
    )


def _link_only_plan() -> IdentityResolutionResult:
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.MISSING,
        error_code=IdentityErrorCode.LINK_REQUIRED,
        provisioning_action=ProvisioningAction.REQUIRE_LINK,
        provisioning_policy_revision=1,
        provider_verification_required=True,
    )


def _provider_result(
    *,
    status: ProviderIdentityStatus = ProviderIdentityStatus.RESOLVED,
    error_code: ProviderErrorCode | None = None,
    verified_at: datetime = _PROOF_AT,
    display_name: str = "Provider display name",
) -> ProviderIdentityResult:
    identity = None
    if status is ProviderIdentityStatus.RESOLVED:
        identity = ProviderIdentity(
            provider="feishu",
            provider_tenant_key="provider-tenant-secret",
            provider_account_id="provider-account-secret",
            provider_user_id="provider-user-secret",
            verified_at=verified_at,
            open_id="open-id-secret",
            union_id="union-id-secret",
            display_name=display_name,
            provider_status=ProviderDirectoryStatus.ACTIVE,
        )
    return ProviderIdentityResult(
        status=status,
        identity=identity,
        error_code=error_code,
    )


class _AuthorityResolver:
    def __init__(
        self,
        authority: ChannelIdentityAuthority,
        *,
        order: list[str] | None = None,
    ) -> None:
        self.authority = authority
        self.order = order
        self.calls: list[TrustedChannelContext] = []

    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
    ) -> ChannelIdentityAuthority:
        if self.order is not None:
            self.order.append("authority")
        self.calls.append(context)
        return self.authority


class _IdentityReader:
    def __init__(
        self,
        results: list[IdentityResolutionResult],
        *,
        order: list[str] | None = None,
    ) -> None:
        self.results = list(results)
        self.order = order
        self.requests: list[IdentityResolutionRequest] = []

    async def resolve(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionResult:
        if self.order is not None:
            self.order.append("initial_i3" if not self.requests else "final_i3")
        self.requests.append(request)
        return self.results.pop(0)


class _Provider:
    def __init__(
        self,
        result: ProviderIdentityResult,
        *,
        order: list[str] | None = None,
    ) -> None:
        self.result = result
        self.order = order
        self.calls: list[tuple[ProviderContext, ExternalIdentityAssertion]] = []

    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult:
        if self.order is not None:
            self.order.append("i4")
        self.calls.append((context, assertion))
        return self.result

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise AssertionError("Channel promotion must use resolve, not refresh")


class _ProvisioningService:
    def __init__(
        self,
        *,
        result: ProvisioningResult | None = None,
        order: list[str] | None = None,
    ) -> None:
        self.order = order
        self.result = result or ProvisioningResult(
            status=ProvisioningStatus.RESOLVED,
            outcome=ProvisioningOutcome.JIT_CREATED,
            identity=_identity(verified_at=_PROOF_AT),
            membership=UserMembershipRecord(
                user_id="platform-user-secret",
                tenant_id="tenant-1",
                role="normal",
            ),
        )
        self.provision_requests: list[ProvisionIdentityRequest] = []
        self.reverify_requests: list[ReverifyResolvedIdentityRequest] = []

    async def provision_verified_identity(
        self,
        request: ProvisionIdentityRequest,
    ) -> ProvisioningResult:
        if self.order is not None:
            self.order.append("i6")
        self.provision_requests.append(request)
        return self.result

    async def reverify_resolved_identity(
        self,
        request: ReverifyResolvedIdentityRequest,
    ) -> ProvisioningResult:
        if self.order is not None:
            self.order.append("i6")
        self.reverify_requests.append(request)
        return replace(
            self.result,
            outcome=ProvisioningOutcome.ALREADY_BOUND,
        )


class _EnterpriseSubjectService:
    def __init__(
        self,
        result: EnterpriseSubjectServiceResult,
        *,
        order: list[str] | None = None,
    ) -> None:
        self.result = result
        self.order = order
        self.calls: list[tuple[str, str, ProviderIdentity]] = []

    async def resolve(
        self,
        *,
        platform_user_id: str,
        tenant_id: str,
        provider_identity: ProviderIdentity,
    ) -> EnterpriseSubjectServiceResult:
        if self.order is not None:
            self.order.append("i5")
        self.calls.append((platform_user_id, tenant_id, provider_identity))
        return self.result


class _MalformedEnterpriseSubjectService:
    async def resolve(
        self,
        *,
        platform_user_id: str,
        tenant_id: str,
        provider_identity: ProviderIdentity,
    ) -> object:
        del platform_user_id, tenant_id, provider_identity
        return object()


class _AuthorityRows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _AuthoritySession:
    def __init__(
        self,
        *,
        base: list[Any],
        scalar_results: list[list[Any]],
        error: SQLAlchemyError | None = None,
    ) -> None:
        self.base = base
        self.scalar_results = list(scalar_results)
        self.error = error

    async def execute(self, statement: object) -> _AuthorityRows:
        del statement
        if self.error is not None:
            raise self.error
        return _AuthorityRows(self.base)

    async def scalars(self, statement: object) -> _AuthorityRows:
        del statement
        return _AuthorityRows(self.scalar_results.pop(0))


class _AuthoritySessionContext:
    def __init__(self, session: _AuthoritySession) -> None:
        self._session = session

    async def __aenter__(self) -> _AuthoritySession:
        return self._session

    async def __aexit__(self, *exc_info: object) -> None:
        del exc_info


class _AuthoritySessionFactory(async_sessionmaker[AsyncSession]):
    def __init__(self, session: _AuthoritySession) -> None:
        super().__init__()
        self._session = session

    def __call__(self) -> _AuthoritySessionContext:
        return _AuthoritySessionContext(self._session)


def _resolver(
    *,
    authority: ChannelIdentityAuthority | None = None,
    reader_results: list[IdentityResolutionResult] | None = None,
    provider_result: ProviderIdentityResult | None = None,
    provisioning_result: ProvisioningResult | None = None,
    enterprise_subject_service: EnterpriseSubjectEvidenceService | None = None,
    now: Callable[[], datetime] = lambda: _VALIDATED_AT,
) -> tuple[
    ChannelIdentityResolver,
    _AuthorityResolver,
    _IdentityReader,
    _Provider,
    _ProvisioningService,
]:
    authority_resolver = _AuthorityResolver(
        authority
        or ChannelIdentityAuthority(
            ChannelIdentityAuthorityStatus.LINKED,
            _provider_context(),
        )
    )
    reader = _IdentityReader(reader_results or [_jit_plan(), _resolved()])
    provider = _Provider(provider_result or _provider_result())
    provisioning = _ProvisioningService(result=provisioning_result)
    resolver = ChannelIdentityResolver(
        authority_resolver=authority_resolver,
        identity_reader=reader,
        provider_registry=IdentityProviderRegistry({"feishu": lambda: provider}),
        provisioning_service_factory=lambda: provisioning,
        enterprise_subject_service=enterprise_subject_service,
        now=now,
    )
    return resolver, authority_resolver, reader, provider, provisioning


async def test_no_link_preserves_legacy_context_without_identity_work() -> None:
    authority = _AuthorityResolver(ChannelIdentityAuthority(ChannelIdentityAuthorityStatus.NO_LINK))
    reader = _IdentityReader([])
    provider = _Provider(_provider_result())
    provisioning = _ProvisioningService()
    factory_calls = 0

    def _factory() -> _ProvisioningService:
        nonlocal factory_calls
        factory_calls += 1
        return provisioning

    resolver = ChannelIdentityResolver(
        authority_resolver=authority,
        identity_reader=reader,
        provider_registry=IdentityProviderRegistry({"feishu": lambda: provider}),
        provisioning_service_factory=_factory,
        now=lambda: _VALIDATED_AT,
    )
    context = _context()

    result = await resolver.resolve(
        context=context,
        command=_command(include_identity=False),
    )

    assert result is context
    assert authority.calls == [context]
    assert reader.requests == []
    assert provider.calls == []
    assert factory_calls == 0
    assert provisioning.provision_requests == []
    assert provisioning.reverify_requests == []


async def test_action_actor_resolution_enforces_durable_provider_account_fence() -> None:
    resolver, _, reader, provider, _ = _resolver()

    result = await resolver.resolve_actor(
        context=_context(),
        actor=_command().actor,
        expected_provider_account_id="provider-account-secret",
    )

    assert result.principal is not None
    assert result.principal_id == "platform-user-secret"
    assert len(reader.requests) == 2
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "authority",
    [
        ChannelIdentityAuthority(
            ChannelIdentityAuthorityStatus.LINKED,
            _provider_context(provider_account_id="replacement-account-secret"),
        ),
        ChannelIdentityAuthority(ChannelIdentityAuthorityStatus.NO_LINK),
    ],
    ids=["account-relinked", "link-removed"],
)
async def test_action_actor_resolution_rejects_changed_provider_authority_before_i3(
    authority: ChannelIdentityAuthority,
) -> None:
    resolver, _, reader, provider, _ = _resolver(authority=authority)

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve_actor(
            context=_context(),
            actor=_command().actor,
            expected_provider_account_id="provider-account-secret",
        )

    assert exc_info.value.code == "IDENTITY_REVISION_CONFLICT"
    assert reader.requests == []
    assert provider.calls == []


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("base-missing", ChannelIdentityAuthorityStatus.INVALID),
        ("base-ambiguous", ChannelIdentityAuthorityStatus.INVALID),
        ("generation-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("binding-disabled", ChannelIdentityAuthorityStatus.INVALID),
        ("target-type-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("target-id-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("target-revision-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("channel-disabled", ChannelIdentityAuthorityStatus.INVALID),
        ("tenant-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("provider-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("no-link", ChannelIdentityAuthorityStatus.NO_LINK),
        ("link-ambiguous", ChannelIdentityAuthorityStatus.INVALID),
        ("link-tenant-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("link-provider-mismatch", ChannelIdentityAuthorityStatus.INVALID),
        ("dangling-account", ChannelIdentityAuthorityStatus.INVALID),
        ("account-ambiguous", ChannelIdentityAuthorityStatus.INVALID),
        ("account-nonhealthy", ChannelIdentityAuthorityStatus.INVALID),
        ("healthy-linked", ChannelIdentityAuthorityStatus.LINKED),
    ],
)
async def test_sql_authority_resolver_three_states_and_damage_matrix(
    scenario: str,
    expected: ChannelIdentityAuthorityStatus,
) -> None:
    binding = SimpleNamespace(
        generation=7,
        enabled=True,
        target_type="multirag.canvas_agent",
        target_id="target-1",
        target_revision_id="revision-1",
    )
    channel = SimpleNamespace(
        id="channel-1",
        status=1,
        tenant_id="tenant-1",
        channel="feishu",
    )
    link = SimpleNamespace(
        channel_id="channel-1",
        tenant_id="tenant-1",
        provider="feishu",
        provider_account_id="account-1",
    )
    account = SimpleNamespace(
        id="account-1",
        tenant_id="tenant-1",
        provider="feishu",
        provider_tenant_key="provider-tenant-secret",
        provider_account_key="provider-app-secret",
        identity_revision=3,
        last_scope_change_at=None,
        identity_health_state="healthy",
    )
    base: list[Any] = [(binding, channel)]
    links: list[Any] = [link]
    accounts: list[Any] = [account]
    if scenario == "base-missing":
        base = []
    elif scenario == "base-ambiguous":
        base = [*base, *base]
    elif scenario == "generation-mismatch":
        binding.generation = 8
    elif scenario == "binding-disabled":
        binding.enabled = False
    elif scenario == "target-type-mismatch":
        binding.target_type = "multirag.dialog"
    elif scenario == "target-id-mismatch":
        binding.target_id = "other-target"
    elif scenario == "target-revision-mismatch":
        binding.target_revision_id = "other-revision"
    elif scenario == "channel-disabled":
        channel.status = 0
    elif scenario == "tenant-mismatch":
        channel.tenant_id = "other-tenant"
    elif scenario == "provider-mismatch":
        channel.channel = "other-provider"
    elif scenario == "no-link":
        links = []
    elif scenario == "link-ambiguous":
        links = [*links, *links]
    elif scenario == "link-tenant-mismatch":
        link.tenant_id = "other-tenant"
    elif scenario == "link-provider-mismatch":
        link.provider = "other-provider"
    elif scenario == "dangling-account":
        accounts = []
    elif scenario == "account-ambiguous":
        accounts = [*accounts, *accounts]
    elif scenario == "account-nonhealthy":
        account.identity_health_state = "degraded"
    session = _AuthoritySession(
        base=base,
        scalar_results=[links, accounts],
    )
    resolver = SqlAlchemyChannelIdentityAuthorityResolver(_AuthoritySessionFactory(session))

    result = await resolver.resolve(context=_context())

    assert result.status is expected
    assert (result.context is not None) is (expected is ChannelIdentityAuthorityStatus.LINKED)


async def test_sql_authority_database_error_is_stable_and_redacted() -> None:
    resolver = SqlAlchemyChannelIdentityAuthorityResolver(
        _AuthoritySessionFactory(
            _AuthoritySession(
                base=[],
                scalar_results=[],
                error=SQLAlchemyError("sensitive database detail"),
            )
        )
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context())

    assert exc_info.value.code == "IDENTITY_REPOSITORY_UNAVAILABLE"
    assert "sensitive database detail" not in str(exc_info.value)


async def test_linked_jit_runs_initial_i3_i4_i6_and_fresh_i3() -> None:
    resolver, _, reader, provider, provisioning = _resolver()

    result = await resolver.resolve(context=_context(), command=_command())

    assert len(provider.calls) == 1
    assert [request.alias.alias_value for request in reader.requests] == [
        "open-id-secret",
        "open-id-secret",
    ]
    assert len(provisioning.provision_requests) == 1
    assert provisioning.reverify_requests == []
    assert result.principal is not None
    assert result.principal_id == "platform-user-secret"
    assert result.principal.display_name == "Database display name"
    assert result.principal.authentication.source is AuthenticationSource.ENTERPRISE_IDENTITY
    assert result.principal.authentication.assurance is IdentityAssurance.DIRECTORY_VERIFIED
    assert result.principal.authentication.validated_at == _VALIDATED_AT
    assert result.principal.authentication.assurance_verified_at == _FINAL_AT
    assert result.principal.authentication.external_identity_id == "external-identity-secret"
    assert result.principal.provider_identity is not None
    assert result.principal.provider_identity.provider == "feishu"
    assert result.principal.provider_identity.provider_tenant == "provider-tenant-secret"
    assert result.principal.provider_identity.provider_account_id == "provider-account-secret"
    assert result.principal.provider_identity.subject_type == "user_id"
    assert result.principal.provider_identity.subject == "provider-user-secret"
    assert result.principal.provider_identity.verified_at == _FINAL_AT


async def test_resolved_enterprise_subject_promotes_persisted_evidence() -> None:
    subject = EnterpriseSubject(
        subject_type="employee_no",
        subject="employee-number-secret",
        issuer="feishu_contact",
        issuer_tenant="provider-tenant-secret",
        verified_at=_PROOF_AT,
    )
    service = _EnterpriseSubjectService(
        EnterpriseSubjectServiceResult(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            evidence=VerifiedEnterpriseSubjectEvidence(
                platform_user_id="platform-user-secret",
                tenant_id="tenant-1",
                enterprise_subject=subject,
            ),
        )
    )
    resolver, _, _, provider, _ = _resolver(
        enterprise_subject_service=service,
    )

    result = await resolver.resolve(context=_context(), command=_command())

    assert result.principal is not None
    assert result.principal.authentication.assurance is IdentityAssurance.ENTERPRISE_VERIFIED
    assert result.principal.authentication.assurance_verified_at == _PROOF_AT
    assert result.principal.enterprise_subject == subject
    assert service.calls == [
        (
            "platform-user-secret",
            "tenant-1",
            provider.result.identity,
        )
    ]


async def test_resolver_owned_proof_uses_post_i5_validation_time() -> None:
    pre_i5 = _VALIDATED_AT
    subject_verified_at = pre_i5 + timedelta(seconds=1)
    post_i5 = subject_verified_at + timedelta(seconds=1)
    times = iter((pre_i5, post_i5))
    subject = EnterpriseSubject(
        subject_type="workcode",
        subject="workcode-secret",
        issuer="oa_hr",
        issuer_tenant="enterprise-tenant-secret",
        verified_at=subject_verified_at,
    )
    service = _EnterpriseSubjectService(
        EnterpriseSubjectServiceResult(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            evidence=VerifiedEnterpriseSubjectEvidence(
                platform_user_id="platform-user-secret",
                tenant_id="tenant-1",
                enterprise_subject=subject,
            ),
        )
    )
    resolver, _, _, _, _ = _resolver(
        enterprise_subject_service=service,
        now=lambda: next(times),
    )

    result = await resolver.resolve(context=_context(), command=_command())

    assert result.principal is not None
    assert result.principal.authentication.assurance_verified_at == subject_verified_at
    assert result.principal.authentication.validated_at == post_i5


@pytest.mark.parametrize(
    "status",
    [
        EnterpriseSubjectResolutionStatus.NOT_FOUND,
        EnterpriseSubjectResolutionStatus.UNAVAILABLE,
    ],
)
async def test_nonfatal_subject_absence_preserves_directory_principal(
    status: EnterpriseSubjectResolutionStatus,
) -> None:
    service = _EnterpriseSubjectService(
        EnterpriseSubjectServiceResult(status=status),
    )
    resolver, _, _, _, _ = _resolver(enterprise_subject_service=service)

    result = await resolver.resolve(context=_context(), command=_command())

    assert result.principal is not None
    assert result.principal.authentication.assurance is IdentityAssurance.DIRECTORY_VERIFIED
    assert result.principal.enterprise_subject is None
    assert len(service.calls) == 1


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (EnterpriseSubjectResolutionStatus.AMBIGUOUS, "IDENTITY_LINK_CONFLICT"),
        (EnterpriseSubjectResolutionStatus.INACTIVE, "IDENTITY_INACTIVE"),
    ],
)
async def test_unsafe_subject_states_reject_linked_execution(
    status: EnterpriseSubjectResolutionStatus,
    expected_code: str,
) -> None:
    resolver, _, _, _, _ = _resolver(
        enterprise_subject_service=_EnterpriseSubjectService(
            EnterpriseSubjectServiceResult(status=status),
        )
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == expected_code


@pytest.mark.parametrize(
    ("error_code", "expected_code"),
    [
        (
            EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE,
            "IDENTITY_REPOSITORY_UNAVAILABLE",
        ),
        (
            EnterpriseSubjectErrorCode.RESOLUTION_INVALID,
            "IDENTITY_ASSERTION_INVALID",
        ),
    ],
)
async def test_fatal_subject_failure_maps_to_stable_identity_error(
    error_code: EnterpriseSubjectErrorCode,
    expected_code: str,
) -> None:
    resolver, _, _, _, _ = _resolver(
        enterprise_subject_service=_EnterpriseSubjectService(
            EnterpriseSubjectServiceResult(
                status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
                error_code=error_code,
                fatal=True,
            ),
        )
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == expected_code


async def test_malformed_subject_service_result_fails_closed() -> None:
    resolver, _, _, _, _ = _resolver(
        enterprise_subject_service=_MalformedEnterpriseSubjectService(),
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == "IDENTITY_ASSERTION_INVALID"


async def test_linked_resolution_order_is_authority_i3_i4_i6_final_i3() -> None:
    order: list[str] = []
    authority = _AuthorityResolver(
        ChannelIdentityAuthority(
            ChannelIdentityAuthorityStatus.LINKED,
            _provider_context(),
        ),
        order=order,
    )
    reader = _IdentityReader([_jit_plan(), _resolved()], order=order)
    provider = _Provider(_provider_result(), order=order)
    provisioning = _ProvisioningService(order=order)
    subject_service = _EnterpriseSubjectService(
        EnterpriseSubjectServiceResult(
            status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
        ),
        order=order,
    )
    resolver = ChannelIdentityResolver(
        authority_resolver=authority,
        identity_reader=reader,
        provider_registry=IdentityProviderRegistry({"feishu": lambda: provider}),
        provisioning_service_factory=lambda: provisioning,
        enterprise_subject_service=subject_service,
        now=lambda: _VALIDATED_AT,
    )

    await resolver.resolve(context=_context(), command=_command())

    assert order == ["authority", "initial_i3", "i4", "i6", "final_i3", "i5"]


async def test_active_identity_uses_reverify_then_fresh_i3() -> None:
    initial = _resolved(verified_at=_PROOF_AT - timedelta(minutes=1))
    resolver, _, reader, _, provisioning = _resolver(
        reader_results=[initial, _resolved()],
    )

    result = await resolver.resolve(context=_context(), command=_command())

    assert len(reader.requests) == 2
    assert provisioning.provision_requests == []
    assert len(provisioning.reverify_requests) == 1
    assert provisioning.reverify_requests[0].resolution is initial
    assert result.principal is not None
    assert result.principal.authentication.assurance_verified_at == _FINAL_AT


@pytest.mark.parametrize(
    ("command", "expected_code"),
    [
        (_command(include_identity=False), "IDENTITY_ASSERTION_INVALID"),
        (
            _command(identifiers=(("future_id", "unknown-secret"),)),
            "IDENTITY_ASSERTION_INVALID",
        ),
        (
            _command(identifiers=(("user_id", "provider-user-secret"),)),
            "IDENTITY_ASSERTION_INVALID",
        ),
        (
            _command(subject="different-open-id"),
            "IDENTITY_ASSERTION_INVALID",
        ),
        (
            _command(tenant_key="different-provider-tenant"),
            "IDENTITY_TENANT_MISMATCH",
        ),
        (
            _command(provider="other-provider"),
            "IDENTITY_PROVIDER_MISMATCH",
        ),
    ],
)
async def test_linked_assertion_matrix_fails_closed_before_i4(
    command: ChannelExecutionCommand,
    expected_code: str,
) -> None:
    resolver, _, reader, provider, provisioning = _resolver()

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=command)

    assert exc_info.value.code == expected_code
    assert reader.requests == []
    assert provider.calls == []
    assert provisioning.provision_requests == []


async def test_invalid_link_authority_fails_closed_before_actor_material() -> None:
    resolver, _, reader, provider, _ = _resolver(
        authority=ChannelIdentityAuthority(ChannelIdentityAuthorityStatus.INVALID),
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(
            context=_context(),
            command=_command(include_identity=False),
        )

    assert exc_info.value.code == "IDENTITY_REPOSITORY_UNAVAILABLE"
    assert reader.requests == []
    assert provider.calls == []


async def test_i6_hmac_readiness_fails_before_provider_network() -> None:
    authority = _AuthorityResolver(
        ChannelIdentityAuthority(
            ChannelIdentityAuthorityStatus.LINKED,
            _provider_context(),
        )
    )
    reader = _IdentityReader([_jit_plan()])
    provider = _Provider(_provider_result())

    def _misconfigured_factory() -> _ProvisioningService:
        raise ValueError("missing HMAC keyring")

    resolver = ChannelIdentityResolver(
        authority_resolver=authority,
        identity_reader=reader,
        provider_registry=IdentityProviderRegistry({"feishu": lambda: provider}),
        provisioning_service_factory=_misconfigured_factory,
        now=lambda: _VALIDATED_AT,
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == "IDENTITY_REPOSITORY_UNAVAILABLE"
    assert len(reader.requests) == 1
    assert provider.calls == []


@pytest.mark.parametrize(
    "initial",
    [
        IdentityResolutionResult(
            status=IdentityResolutionStatus.CONFLICT,
            error_code=IdentityErrorCode.LINK_CONFLICT,
        ),
        IdentityResolutionResult(
            status=IdentityResolutionStatus.INACTIVE,
            error_code=IdentityErrorCode.INACTIVE,
        ),
        IdentityResolutionResult(
            status=IdentityResolutionStatus.MISSING,
            provider_verification_required=True,
        ),
        replace(
            _resolved(),
            identity=replace(_identity(), state="revoked"),
        ),
        replace(
            _resolved(),
            membership=UserMembershipRecord(
                user_id="platform-user-secret",
                tenant_id="tenant-1",
                role="invite",
            ),
        ),
    ],
    ids=[
        "conflict",
        "inactive-without-reverification-plan",
        "malformed-missing-plan",
        "revoked-identity",
        "invalid-membership-role",
    ],
)
async def test_ineligible_initial_i3_never_calls_i4_or_i6(
    initial: IdentityResolutionResult,
) -> None:
    resolver, _, reader, provider, provisioning = _resolver(
        reader_results=[initial],
    )

    with pytest.raises(ChannelIdentityResolutionError):
        await resolver.resolve(context=_context(), command=_command())

    assert len(reader.requests) == 1
    assert provider.calls == []
    assert provisioning.provision_requests == []
    assert provisioning.reverify_requests == []


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (ProviderIdentityStatus.NOT_FOUND, "IDENTITY_NOT_FOUND"),
        (ProviderIdentityStatus.NOT_IN_SCOPE, "IDENTITY_NOT_IN_SCOPE"),
        (ProviderIdentityStatus.INACTIVE, "IDENTITY_INACTIVE"),
        (ProviderIdentityStatus.UNAVAILABLE, "IDENTITY_PROVIDER_UNAVAILABLE"),
        (ProviderIdentityStatus.CONFLICT, "IDENTITY_LINK_CONFLICT"),
        (ProviderIdentityStatus.INVALID, "IDENTITY_ASSERTION_INVALID"),
    ],
)
async def test_i4_status_is_mapped_after_eligible_i3_without_i6(
    status: ProviderIdentityStatus,
    expected_code: str,
) -> None:
    resolver, _, reader, provider, provisioning = _resolver(
        provider_result=_provider_result(status=status),
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == expected_code
    assert len(provider.calls) == 1
    assert len(reader.requests) == 1
    assert provisioning.provision_requests == []


async def test_link_only_i6_rejection_never_promotes_or_final_reads() -> None:
    resolver, _, reader, _, provisioning = _resolver(
        reader_results=[_link_only_plan()],
        provisioning_result=ProvisioningResult(
            status=ProvisioningStatus.REJECTED,
            error_code=IdentityErrorCode.LINK_REQUIRED,
        ),
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == "IDENTITY_LINK_REQUIRED"
    assert len(reader.requests) == 1
    assert len(provisioning.provision_requests) == 1
    assert provisioning.reverify_requests == []


@pytest.mark.parametrize(
    "final_verified_at",
    [
        _PROOF_AT - timedelta(microseconds=1),
        _VALIDATED_AT + timedelta(microseconds=1),
    ],
)
async def test_final_i3_proof_time_outside_current_validation_window_fails_closed(
    final_verified_at: datetime,
) -> None:
    resolver, _, _, _, _ = _resolver(
        reader_results=[_jit_plan(), _resolved(verified_at=final_verified_at)],
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == "IDENTITY_ASSERTION_INVALID"


async def test_naive_final_i3_timestamp_fails_closed() -> None:
    resolver, _, _, _, _ = _resolver(
        reader_results=[
            _jit_plan(),
            _resolved(verified_at=_FINAL_AT.replace(tzinfo=None)),
        ],
    )

    with pytest.raises(ChannelIdentityResolutionError) as exc_info:
        await resolver.resolve(context=_context(), command=_command())

    assert exc_info.value.code == "IDENTITY_ASSERTION_INVALID"


async def test_final_i3_may_be_newer_than_current_i4_proof_without_fabrication() -> None:
    concurrent_verified_at = _PROOF_AT + timedelta(milliseconds=500)
    resolver, _, _, _, _ = _resolver(
        reader_results=[_jit_plan(), _resolved(verified_at=concurrent_verified_at)],
    )

    result = await resolver.resolve(context=_context(), command=_command())

    assert result.principal is not None
    assert result.principal.authentication.assurance_verified_at == concurrent_verified_at


@pytest.mark.parametrize(
    "drift",
    [
        "subject",
        "tenant",
        "provider",
        "provider_tenant",
        "platform_user",
        "membership_user",
    ],
)
async def test_final_i3_canonical_drift_is_rejected_even_with_newer_proof(
    drift: str,
) -> None:
    identity = _identity(verified_at=_FINAL_AT)
    membership = UserMembershipRecord(
        user_id=identity.user_id,
        tenant_id=identity.tenant_id,
        role="normal",
    )
    if drift == "subject":
        identity = replace(identity, subject_value="different-provider-user")
    elif drift == "tenant":
        identity = replace(identity, tenant_id="different-tenant")
        membership = replace(membership, tenant_id="different-tenant")
    elif drift == "provider":
        identity = replace(identity, provider="different-provider")
    elif drift == "provider_tenant":
        identity = replace(
            identity,
            provider_tenant_key="different-provider-tenant",
        )
    elif drift == "platform_user":
        identity = replace(identity, user_id="different-platform-user")
        membership = replace(membership, user_id="different-platform-user")
    else:
        membership = replace(membership, user_id="different-membership-user")
    final = IdentityResolutionResult(
        status=IdentityResolutionStatus.RESOLVED,
        identity=identity,
        membership=membership,
    )
    resolver, _, _, _, _ = _resolver(
        reader_results=[_jit_plan(), final],
    )

    with pytest.raises(ChannelIdentityResolutionError):
        await resolver.resolve(context=_context(), command=_command())


async def test_trusted_context_repr_and_consistency_do_not_leak_principal() -> None:
    resolver, _, _, _, _ = _resolver()
    result = await resolver.resolve(context=_context(), command=_command())

    rendered = repr(result)
    assert "platform-user-secret" not in rendered
    assert "external-identity-secret" not in rendered
    assert "provider-user-secret" not in rendered
    assert "Database display name" not in rendered
    with pytest.raises(ValueError, match="inconsistent"):
        replace(_context(), principal_id="platform-user-secret")
