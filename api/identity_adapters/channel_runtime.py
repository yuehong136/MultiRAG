"""Production composition from a claimed Channel event to an EIM Principal.

This adapter is intentionally outside :mod:`api.identity`: ``channel_id`` and
binding generations belong to the Channel control plane, while identity core
continues to consume only a server-built :class:`ProviderContext`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import NoReturn, Protocol, cast, runtime_checkable

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_execution.errors import ChannelIdentityResolutionError
from api.channel_execution.models import (
    ChannelActor,
    ChannelExecutionCommand,
    TrustedChannelContext,
)
from api.db import IdentityProviderHealthState
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
)
from api.identity.contracts import (
    AliasKey,
    IdentityErrorCode,
    IdentityLookupRepository,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProviderAliasType,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
    ProvisioningPolicySnapshot,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    PrincipalBuildError,
    build_principal_from_resolved_identity,
)
from api.identity.providers.contracts import (
    EnterpriseIdentityProvider,
    ExternalIdentityAssertion,
    ExternalIdentityIdentifier,
    ProviderErrorCode,
    ProviderIdentifierKind,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.provisioning import IdentityProvisioningService
from api.identity.provisioning_contracts import (
    ProvisionIdentityRequest,
    ProvisioningRepositoryError,
    ProvisioningStatus,
    ReverifyResolvedIdentityRequest,
)
from api.identity.repository import IdentityRepositoryError, SqlAlchemyIdentityRepository
from api.identity.service import IdentityService
from api.identity.validation import valid_opaque_id, valid_timestamp
from common.constants import StatusEnum


class ChannelIdentityAuthorityStatus(StrEnum):
    NO_LINK = "no_link"
    LINKED = "linked"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ChannelIdentityAuthority:
    """Server-only outcome; provider natural keys remain hidden from repr."""

    status: ChannelIdentityAuthorityStatus
    context: ProviderContext | None = field(default=None, repr=False)


@runtime_checkable
class ChannelIdentityAuthorityResolver(Protocol):
    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
    ) -> ChannelIdentityAuthority: ...


@runtime_checkable
class ChannelIdentityReader(Protocol):
    async def resolve(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionResult: ...


class IdentityProviderRegistry:
    """Lazily create and retain one process-level provider per kind."""

    def __init__(
        self,
        factories: Mapping[str, Callable[[], EnterpriseIdentityProvider]],
    ) -> None:
        self._factories = dict(factories)
        self._providers: dict[str, EnterpriseIdentityProvider] = {}
        self._lock = Lock()

    def get(self, provider: str) -> EnterpriseIdentityProvider | None:
        existing = self._providers.get(provider)
        if existing is not None:
            return existing
        factory = self._factories.get(provider)
        if factory is None:
            return None
        with self._lock:
            existing = self._providers.get(provider)
            if existing is not None:
                return existing
            created = factory()
            if not isinstance(created, EnterpriseIdentityProvider):
                return None
            self._providers[provider] = created
            return created


class SqlAlchemyChannelIdentityAuthorityResolver:
    """Resolve link/account authority in a bounded, DB-only short session."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
    ) -> ChannelIdentityAuthority:
        try:
            async with self._session_factory() as session:
                base = (
                    await session.execute(
                        select(ChannelBinding, ChatChannel)
                        .join(ChatChannel, ChatChannel.id == ChannelBinding.channel_id)
                        .join(
                            Tenant,
                            and_(
                                Tenant.id == ChatChannel.tenant_id,
                                Tenant.status == StatusEnum.VALID.value,
                            ),
                        )
                        .where(ChannelBinding.id == context.binding_id)
                        .limit(2)
                    )
                ).all()
                if len(base) != 1:
                    return _invalid_authority()
                binding, channel = base[0]
                if (
                    binding.generation != context.binding_generation
                    or not binding.enabled
                    or binding.target_type != context.target.target_type
                    or binding.target_id != context.target.target_id
                    or binding.target_revision_id != context.target.revision_id
                    or channel.status != 1
                    or channel.tenant_id != context.tenant_id
                    or channel.channel != context.provider
                ):
                    return _invalid_authority()

                # Root this lookup at the link row.  An inner join could turn a
                # dangling link into a false "no link", while first() could
                # silently choose one row in a damaged database.
                links = (await session.scalars(select(IdentityProviderChannelLink).where(IdentityProviderChannelLink.channel_id == channel.id).limit(2))).all()
                if not links:
                    return ChannelIdentityAuthority(
                        ChannelIdentityAuthorityStatus.NO_LINK,
                    )
                if len(links) != 1:
                    return _invalid_authority()
                link = links[0]
                if link.channel_id != channel.id or link.tenant_id != context.tenant_id or link.provider != context.provider:
                    return _invalid_authority()

                accounts = (
                    await session.scalars(
                        select(IdentityProviderAccount)
                        .join(
                            IdentityProviderTenant,
                            and_(
                                IdentityProviderTenant.tenant_id == IdentityProviderAccount.tenant_id,
                                IdentityProviderTenant.provider == IdentityProviderAccount.provider,
                                IdentityProviderTenant.provider_tenant_key == IdentityProviderAccount.provider_tenant_key,
                            ),
                        )
                        .where(
                            IdentityProviderAccount.id == link.provider_account_id,
                            IdentityProviderAccount.tenant_id == link.tenant_id,
                            IdentityProviderAccount.provider == link.provider,
                        )
                        .limit(2)
                    )
                ).all()
                if len(accounts) != 1:
                    return _invalid_authority()
                account = accounts[0]
                if account.identity_health_state != IdentityProviderHealthState.HEALTHY.value:
                    return _invalid_authority()
                provider_context = ProviderContext(
                    tenant_id=account.tenant_id,
                    provider=account.provider,
                    provider_tenant_key=account.provider_tenant_key,
                    provider_account_id=account.id,
                    provider_account_key=account.provider_account_key,
                    provider_account_revision=account.identity_revision,
                    provider_account_last_scope_change_at=account.last_scope_change_at,
                )
                return ChannelIdentityAuthority(
                    ChannelIdentityAuthorityStatus.LINKED,
                    provider_context,
                )
        except SQLAlchemyError:
            raise ChannelIdentityResolutionError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE.value,
            ) from None


class SqlAlchemyChannelIdentityReader:
    """Open one fresh I3 read session for each resolution request."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def resolve(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionResult:
        try:
            async with self._session_factory() as session:
                return await IdentityService(
                    cast(
                        IdentityLookupRepository,
                        SqlAlchemyIdentityRepository(session),
                    ),
                    _SessionProvisioningPolicyResolver(session),
                ).resolve_external_identity(request)
        except IdentityRepositoryError as exc:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=exc.code,
            )
        except SQLAlchemyError:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            )


class _SessionProvisioningPolicyResolver:
    """Read an alias-miss policy without opening a nested DB session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_policy(
        self,
        tenant_id: str,
    ) -> ProvisioningPolicySnapshot | None:
        if not valid_opaque_id(tenant_id):
            return None
        try:
            model = await self._session.scalar(
                select(IdentityTenantPolicy)
                .join(
                    Tenant,
                    and_(
                        Tenant.id == IdentityTenantPolicy.tenant_id,
                        Tenant.status == StatusEnum.VALID.value,
                    ),
                )
                .where(IdentityTenantPolicy.tenant_id == tenant_id)
            )
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None
        if model is None:
            return None
        try:
            mode = ProvisioningMode(model.mode)
        except ValueError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.POLICY_UNAVAILABLE,
            ) from None
        return ProvisioningPolicySnapshot(
            tenant_id=model.tenant_id,
            mode=mode,
            revision=model.revision,
            link_code_ttl_seconds=model.link_code_ttl_seconds,
            changed_at=model.changed_at,
        )


class ChannelIdentityResolver:
    """Promote only linked and freshly verified Channel actors to Principal."""

    def __init__(
        self,
        *,
        authority_resolver: ChannelIdentityAuthorityResolver,
        identity_reader: ChannelIdentityReader,
        provider_registry: IdentityProviderRegistry,
        provisioning_service_factory: Callable[[], IdentityProvisioningService],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._authority_resolver = authority_resolver
        self._identity_reader = identity_reader
        self._provider_registry = provider_registry
        self._provisioning_service_factory = provisioning_service_factory
        self._now = now

    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext:
        """Resolve one message actor without changing the legacy no-link path."""

        return await self.resolve_actor(
            context=context,
            actor=command.actor,
            expected_provider_account_id=None,
        )

    async def resolve_actor(
        self,
        *,
        context: TrustedChannelContext,
        actor: ChannelActor,
        expected_provider_account_id: str | None,
    ) -> TrustedChannelContext:
        """Reverify an actor against current authority and an optional fence.

        The account fence is server-owned durable state captured when the form
        was presented.  It prevents a callback from being promoted after the
        Channel link has moved to a different Provider installation.
        """

        authority = await self._authority_resolver.resolve(context=context)
        if authority.status is ChannelIdentityAuthorityStatus.NO_LINK:
            if expected_provider_account_id is not None:
                _reject(IdentityErrorCode.REVISION_CONFLICT)
            return context
        if authority.status is not ChannelIdentityAuthorityStatus.LINKED or authority.context is None:
            _reject(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        provider_context = authority.context
        if expected_provider_account_id is not None and provider_context.provider_account_id != expected_provider_account_id:
            _reject(IdentityErrorCode.REVISION_CONFLICT)
        assertion = _trusted_provider_assertion(
            provider_context,
            actor,
        )
        request = IdentityResolutionRequest(
            context=provider_context,
            alias=AliasKey(
                alias_type=ProviderAliasType.OPEN_ID,
                alias_value=_open_id(assertion),
            ),
        )
        initial = await self._identity_reader.resolve(request)
        if not _verification_eligible(initial, context=provider_context):
            _reject(initial.error_code or IdentityErrorCode.INACTIVE)
        # Validate I6/HMAC readiness after the server-owned I3 gate but before
        # any Provider network call.  A no-link legacy event never reaches
        # this point, while a linked misconfiguration fails without external
        # traffic.
        try:
            provisioning = self._provisioning_service_factory()
        except Exception:
            _reject(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        provider = self._provider_registry.get(provider_context.provider)
        if provider is None:
            _reject(IdentityErrorCode.PROVIDER_MISMATCH)

        # I4 is invoked for every linked event. Its process-level cache may
        # return the original proof, but neither this adapter nor a cache hit
        # manufactures a newer proof timestamp.
        provider_result = await provider.resolve(provider_context, assertion)
        proof = _require_provider_proof(provider_result)
        if initial.status is IdentityResolutionStatus.RESOLVED:
            written = await provisioning.reverify_resolved_identity(
                ReverifyResolvedIdentityRequest(
                    resolution_request=request,
                    resolution=initial,
                    provider_result=provider_result,
                ),
            )
        elif initial.provider_verification_required:
            written = await provisioning.provision_verified_identity(
                ProvisionIdentityRequest(
                    resolution_request=request,
                    resolution=initial,
                    provider_result=provider_result,
                ),
            )
        else:
            _reject(initial.error_code or IdentityErrorCode.INACTIVE)
        if written.status is not ProvisioningStatus.RESOLVED:
            _reject(written.error_code or IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        written_identity = written.identity
        written_membership = written.membership
        if (
            written_identity is None
            or written_membership is None
            or written_identity.user_id != written_membership.user_id
            or written_identity.tenant_id != provider_context.tenant_id
            or written_membership.tenant_id != provider_context.tenant_id
        ):
            _reject(IdentityErrorCode.REPOSITORY_UNAVAILABLE)

        # Provisioning DTOs and I4 proof are never promoted directly.  A new
        # session must observe the committed, authoritative I3 snapshot.
        final = await self._identity_reader.resolve(request)
        identity = final.identity
        membership = final.membership
        if (
            final.status is not IdentityResolutionStatus.RESOLVED
            or final.error_code is not None
            or final.provisioning_action is not None
            or final.provisioning_policy_revision is not None
            or final.provider_verification_required
            or identity is None
            or membership is None
            or identity.verified_at is None
            or identity.state != "active"
            or identity.tenant_id != provider_context.tenant_id
            or identity.provider != provider_context.provider
            or identity.provider_tenant_key != provider_context.provider_tenant_key
            or identity.subject_type != "user_id"
            or identity.subject_value != proof.provider_user_id
            or identity.user_id != written_identity.user_id
            or membership.user_id != identity.user_id
            or membership.tenant_id != provider_context.tenant_id
            or membership.role not in {"owner", "admin", "normal"}
        ):
            _reject(final.error_code or IdentityErrorCode.INACTIVE)
        validated_at = self._now()
        if not valid_timestamp(validated_at) or not valid_timestamp(identity.verified_at) or identity.verified_at < proof.verified_at or identity.verified_at > validated_at:
            _reject(IdentityErrorCode.ASSERTION_INVALID)
        try:
            principal = build_principal_from_resolved_identity(
                result=final,
                authentication=AuthenticationContext(
                    source=AuthenticationSource.ENTERPRISE_IDENTITY,
                    assurance=IdentityAssurance.DIRECTORY_VERIFIED,
                    validated_at=validated_at,
                    assurance_verified_at=identity.verified_at,
                    provider=identity.provider,
                    external_identity_id=identity.id,
                ),
            )
            return replace(
                context,
                principal_id=principal.platform_user_id,
                principal=principal,
            )
        except (PrincipalBuildError, ValueError):
            _reject(IdentityErrorCode.ASSERTION_INVALID)


def _invalid_authority() -> ChannelIdentityAuthority:
    return ChannelIdentityAuthority(ChannelIdentityAuthorityStatus.INVALID)


def _trusted_provider_assertion(
    context: ProviderContext,
    actor: ChannelActor,
) -> ExternalIdentityAssertion:
    raw = actor.identity
    if raw is None:
        _reject(IdentityErrorCode.ASSERTION_INVALID)
    if raw.provider != context.provider:
        _reject(IdentityErrorCode.PROVIDER_MISMATCH)
    if raw.provider_tenant_key != context.provider_tenant_key:
        _reject(IdentityErrorCode.TENANT_MISMATCH)
    identifiers: list[ExternalIdentityIdentifier] = []
    for identifier in raw.identifiers:
        try:
            kind = ProviderIdentifierKind(identifier.kind)
            identifiers.append(
                ExternalIdentityIdentifier(kind=kind, value=identifier.value),
            )
        except ValueError:
            _reject(IdentityErrorCode.ASSERTION_INVALID)
    try:
        assertion = ExternalIdentityAssertion(
            provider=raw.provider,
            provider_tenant_key=raw.provider_tenant_key,
            identifiers=tuple(identifiers),
        )
    except ValueError:
        _reject(IdentityErrorCode.ASSERTION_INVALID)
    open_id = _open_id(assertion)
    if actor.subject != open_id:
        _reject(IdentityErrorCode.ASSERTION_INVALID)
    return assertion


def _open_id(assertion: ExternalIdentityAssertion) -> str:
    values = [identifier.value for identifier in assertion.identifiers if identifier.kind is ProviderIdentifierKind.OPEN_ID]
    if len(values) != 1:
        _reject(IdentityErrorCode.ASSERTION_INVALID)
    return values[0]


def _require_provider_proof(result: ProviderIdentityResult) -> ProviderIdentity:
    if result.status is not ProviderIdentityStatus.RESOLVED or result.identity is None or result.error_code is not None:
        code = result.error_code.value if result.error_code is not None else _provider_status_code(result.status).value
        raise ChannelIdentityResolutionError(code)
    return result.identity


def _verification_eligible(
    result: IdentityResolutionResult,
    *,
    context: ProviderContext,
) -> bool:
    if result.status is IdentityResolutionStatus.RESOLVED:
        identity = result.identity
        membership = result.membership
        return bool(
            result.error_code is None
            and result.provisioning_action is None
            and result.provisioning_policy_revision is None
            and not result.provider_verification_required
            and identity is not None
            and membership is not None
            and identity.state == "active"
            and identity.tenant_id == context.tenant_id
            and identity.provider == context.provider
            and identity.provider_tenant_key == context.provider_tenant_key
            and identity.subject_type == "user_id"
            and identity.verified_at is not None
            and valid_timestamp(identity.verified_at)
            and identity.user_id == membership.user_id
            and membership.tenant_id == context.tenant_id
            and membership.role in {"owner", "admin", "normal"}
        )
    if result.status is IdentityResolutionStatus.MISSING:
        action = result.provisioning_action
        return bool(
            result.identity is None
            and result.membership is None
            and result.provider_verification_required
            and action
            in {
                ProvisioningAction.BIND_PREPROVISIONED,
                ProvisioningAction.REQUIRE_LINK,
                ProvisioningAction.CREATE_NORMAL_MEMBER,
            }
            and isinstance(result.provisioning_policy_revision, int)
            and result.provisioning_policy_revision >= 1
            and ((action is ProvisioningAction.REQUIRE_LINK and result.error_code is IdentityErrorCode.LINK_REQUIRED) or (action is not ProvisioningAction.REQUIRE_LINK and result.error_code is None))
        )
    return bool(
        result.status is IdentityResolutionStatus.INACTIVE
        and result.error_code is IdentityErrorCode.INACTIVE
        and result.identity is None
        and result.membership is None
        and result.provisioning_action is None
        and result.provisioning_policy_revision is None
        and result.provider_verification_required
    )


def _provider_status_code(status: ProviderIdentityStatus) -> ProviderErrorCode:
    return {
        ProviderIdentityStatus.NOT_FOUND: ProviderErrorCode.NOT_FOUND,
        ProviderIdentityStatus.NOT_IN_SCOPE: ProviderErrorCode.NOT_IN_SCOPE,
        ProviderIdentityStatus.INACTIVE: ProviderErrorCode.INACTIVE,
        ProviderIdentityStatus.UNAVAILABLE: ProviderErrorCode.PROVIDER_UNAVAILABLE,
        ProviderIdentityStatus.CONFLICT: ProviderErrorCode.LINK_CONFLICT,
        ProviderIdentityStatus.INVALID: ProviderErrorCode.ASSERTION_INVALID,
        ProviderIdentityStatus.RESOLVED: ProviderErrorCode.ASSERTION_INVALID,
    }[status]


def _reject(code: IdentityErrorCode) -> NoReturn:
    raise ChannelIdentityResolutionError(code.value)


__all__ = [
    "ChannelIdentityAuthority",
    "ChannelIdentityAuthorityResolver",
    "ChannelIdentityAuthorityStatus",
    "ChannelIdentityReader",
    "ChannelIdentityResolver",
    "IdentityProviderRegistry",
    "SqlAlchemyChannelIdentityAuthorityResolver",
    "SqlAlchemyChannelIdentityReader",
]
