"""Resolve one Feishu Provider Account through its immutable Channel link.

Provider credentials still belong to ``ChannelSecret`` during the I2.1/I4
transition.  This adapter is the only composition seam that knows both the
framework-neutral identity context and that temporary Channel ownership
shape.  It deliberately lives outside :mod:`api.identity` so identity core
does not acquire a dependency on the Channel control plane.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import NoReturn, TypeGuard

from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret, SecretStore
from api.channel_providers import resolve_path
from api.db import IdentityProviderHealthState
from api.db.db_models import (
    ChannelSecret,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    Tenant,
)
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    FeishuDomain,
    FeishuProviderCredential,
    ProviderCredentialError,
    ProviderErrorCode,
)
from api.identity.validation import valid_provider_context
from common.constants import StatusEnum

_FEISHU_PROVIDER = "feishu"
_FEISHU_DOMAINS = frozenset(FeishuDomain)


class ChannelProviderCredentialResolver:
    """Read and decrypt the credential for exactly one Provider Account.

    The returned value is intended to live only for the duration of one
    provider call.  This class has no cache and never logs or embeds provider
    identifiers in an error.  The caller owns the request-scoped session; no
    commit or mutation occurs here.
    """

    def __init__(self, db: AsyncSession, secret_store: SecretStore) -> None:
        self._db = db
        self._secret_store = secret_store

    async def resolve(self, context: ProviderContext) -> FeishuProviderCredential:
        if not valid_provider_context(context) or context.provider != _FEISHU_PROVIDER:
            _credential_unavailable()

        statement = (
            select(
                IdentityProviderAccount.id,
                IdentityProviderAccount.provider_account_key,
                IdentityProviderAccount.identity_health_state,
                IdentityProviderChannelLink.tenant_id,
                IdentityProviderChannelLink.provider,
                IdentityProviderChannelLink.channel_id,
                ChatChannel.tenant_id,
                ChatChannel.channel,
                ChatChannel.config,
                ChannelSecret.ciphertext,
                ChannelSecret.key_id,
                ChannelSecret.version,
            )
            .join(
                Tenant,
                and_(
                    Tenant.id == IdentityProviderAccount.tenant_id,
                    Tenant.status == StatusEnum.VALID.value,
                ),
            )
            .join(
                IdentityProviderChannelLink,
                and_(
                    IdentityProviderChannelLink.provider_account_id == IdentityProviderAccount.id,
                    IdentityProviderChannelLink.tenant_id == IdentityProviderAccount.tenant_id,
                    IdentityProviderChannelLink.provider == IdentityProviderAccount.provider,
                ),
            )
            .join(
                ChatChannel,
                and_(
                    ChatChannel.id == IdentityProviderChannelLink.channel_id,
                    ChatChannel.tenant_id == IdentityProviderChannelLink.tenant_id,
                    ChatChannel.channel == IdentityProviderChannelLink.provider,
                ),
            )
            .outerjoin(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
            .where(
                IdentityProviderAccount.id == context.provider_account_id,
                IdentityProviderAccount.tenant_id == context.tenant_id,
                IdentityProviderAccount.provider == context.provider,
                IdentityProviderAccount.provider_tenant_key == context.provider_tenant_key,
                IdentityProviderAccount.provider_account_key == context.provider_account_key,
                IdentityProviderAccount.identity_revision == context.provider_account_revision,
                IdentityProviderAccount.last_scope_change_at.is_not_distinct_from(
                    context.provider_account_last_scope_change_at,
                ),
            )
            .limit(2)
        )
        try:
            rows = (await self._db.execute(statement)).all()
        except SQLAlchemyError:
            _credential_unavailable()

        # Both sides are UNIQUE in the current schema.  Still require exactly
        # one row here so a damaged/custom database cannot make us pick an
        # arbitrary credential by query order.
        if len(rows) != 1:
            _credential_unavailable()

        (
            account_id,
            account_key,
            health_state,
            link_tenant_id,
            link_provider,
            channel_id,
            channel_tenant_id,
            channel_provider,
            channel_config,
            ciphertext,
            key_id,
            version,
        ) = rows[0]
        if (
            account_id != context.provider_account_id
            or account_key != context.provider_account_key
            or health_state == IdentityProviderHealthState.DISABLED.value
            or link_tenant_id != context.tenant_id
            or link_provider != context.provider
            or channel_tenant_id != context.tenant_id
            or channel_provider != context.provider
            or not _nonempty(channel_id)
            or not isinstance(channel_config, Mapping)
            or not _nonempty(ciphertext)
            or not _nonempty(key_id)
            or type(version) is not int
            or version < 1
        ):
            _credential_unavailable()

        # JSONB returns a dict in production.  Make a shallow mapping copy so
        # downstream helpers cannot retain an ORM-owned mutable value.
        public_config = dict(channel_config)
        app_id = resolve_path(public_config, "credential.app_id")
        if (
            app_id != context.provider_account_key
            or not _nonempty(app_id)
            # A legacy/plaintext secret in ChatChannel.config is a contaminated
            # row, not an alternate credential source.  Refuse it loudly.
            or resolve_path(public_config, "credential.app_secret") is not None
        ):
            _credential_unavailable()
        domain = _resolve_domain(public_config)

        try:
            plaintext = await self._secret_store.decrypt(
                tenant_id=context.tenant_id,
                channel_id=channel_id,
                encrypted=EncryptedSecret(
                    ciphertext=ciphertext,
                    key_id=key_id,
                    version=version,
                ),
            )
            app_secret = plaintext.get("app_secret") if isinstance(plaintext, Mapping) else None
        except Exception:
            # SecretStore implementations may be backed by a local cipher,
            # KMS, or another injected adapter.  None of their exception text
            # is part of this public contract.  CancelledError inherits
            # BaseException and still propagates so task cancellation remains
            # cooperative.
            _credential_unavailable()
        if not _nonempty(app_secret) or len(app_secret) > 4_096:
            _credential_unavailable()

        return FeishuProviderCredential(
            provider_account_id=context.provider_account_id,
            app_id=app_id,
            app_secret=app_secret,
            domain=domain,
            credential_generation=version,
        )


@dataclass(frozen=True, slots=True)
class _DetachedCredentialMaterial:
    """Exact DB projection copied before the session is closed."""

    channel_id: str = field(repr=False)
    app_id: str = field(repr=False)
    ciphertext: str = field(repr=False)
    key_id: str = field(repr=False)
    version: int
    domain: FeishuDomain


class SessionFactoryChannelProviderCredentialResolver:
    """Resolve DB material in a short session, then decrypt after it closes.

    ``SecretStore.decrypt`` may use a remote KMS.  Keeping that call outside
    the session prevents a provider/KMS network wait from retaining an
    implicit SQLAlchemy transaction or connection.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_store: SecretStore,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store

    async def resolve(self, context: ProviderContext) -> FeishuProviderCredential:
        async with self._session_factory() as session:
            material = await _load_detached_material(session, context)
        try:
            plaintext = await self._secret_store.decrypt(
                tenant_id=context.tenant_id,
                channel_id=material.channel_id,
                encrypted=EncryptedSecret(
                    ciphertext=material.ciphertext,
                    key_id=material.key_id,
                    version=material.version,
                ),
            )
            app_secret = plaintext.get("app_secret") if isinstance(plaintext, Mapping) else None
        except Exception:
            _credential_unavailable()
        if not _nonempty(app_secret) or len(app_secret) > 4_096:
            _credential_unavailable()
        return FeishuProviderCredential(
            provider_account_id=context.provider_account_id,
            app_id=material.app_id,
            app_secret=app_secret,
            domain=material.domain,
            credential_generation=material.version,
        )


async def _load_detached_material(
    db: AsyncSession,
    context: ProviderContext,
) -> _DetachedCredentialMaterial:
    if not valid_provider_context(context) or context.provider != _FEISHU_PROVIDER:
        _credential_unavailable()
    statement = (
        select(
            IdentityProviderAccount.id,
            IdentityProviderAccount.provider_account_key,
            IdentityProviderAccount.identity_health_state,
            IdentityProviderChannelLink.tenant_id,
            IdentityProviderChannelLink.provider,
            IdentityProviderChannelLink.channel_id,
            ChatChannel.tenant_id,
            ChatChannel.channel,
            ChatChannel.config,
            ChannelSecret.ciphertext,
            ChannelSecret.key_id,
            ChannelSecret.version,
        )
        .join(
            Tenant,
            and_(
                Tenant.id == IdentityProviderAccount.tenant_id,
                Tenant.status == StatusEnum.VALID.value,
            ),
        )
        .join(
            IdentityProviderChannelLink,
            and_(
                IdentityProviderChannelLink.provider_account_id == IdentityProviderAccount.id,
                IdentityProviderChannelLink.tenant_id == IdentityProviderAccount.tenant_id,
                IdentityProviderChannelLink.provider == IdentityProviderAccount.provider,
            ),
        )
        .join(
            ChatChannel,
            and_(
                ChatChannel.id == IdentityProviderChannelLink.channel_id,
                ChatChannel.tenant_id == IdentityProviderChannelLink.tenant_id,
                ChatChannel.channel == IdentityProviderChannelLink.provider,
            ),
        )
        .outerjoin(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
        .where(
            IdentityProviderAccount.id == context.provider_account_id,
            IdentityProviderAccount.tenant_id == context.tenant_id,
            IdentityProviderAccount.provider == context.provider,
            IdentityProviderAccount.provider_tenant_key == context.provider_tenant_key,
            IdentityProviderAccount.provider_account_key == context.provider_account_key,
            IdentityProviderAccount.identity_revision == context.provider_account_revision,
            IdentityProviderAccount.last_scope_change_at.is_not_distinct_from(
                context.provider_account_last_scope_change_at,
            ),
        )
        .limit(2)
    )
    try:
        rows = (await db.execute(statement)).all()
    except SQLAlchemyError:
        _credential_unavailable()
    if len(rows) != 1:
        _credential_unavailable()
    (
        account_id,
        account_key,
        health_state,
        link_tenant_id,
        link_provider,
        channel_id,
        channel_tenant_id,
        channel_provider,
        channel_config,
        ciphertext,
        key_id,
        version,
    ) = rows[0]
    if (
        account_id != context.provider_account_id
        or account_key != context.provider_account_key
        or health_state != IdentityProviderHealthState.HEALTHY.value
        or link_tenant_id != context.tenant_id
        or link_provider != context.provider
        or channel_tenant_id != context.tenant_id
        or channel_provider != context.provider
        or not _nonempty(channel_id)
        or not isinstance(channel_config, Mapping)
        or not _nonempty(ciphertext)
        or not _nonempty(key_id)
        or type(version) is not int
        or version < 1
    ):
        _credential_unavailable()
    public_config = dict(channel_config)
    app_id = resolve_path(public_config, "credential.app_id")
    if app_id != context.provider_account_key or not _nonempty(app_id) or resolve_path(public_config, "credential.app_secret") is not None:
        _credential_unavailable()
    return _DetachedCredentialMaterial(
        channel_id=channel_id,
        app_id=app_id,
        ciphertext=ciphertext,
        key_id=key_id,
        version=version,
        domain=_resolve_domain(public_config),
    )


def _resolve_domain(public_config: Mapping[str, object]) -> FeishuDomain:
    domain = public_config.get("domain")
    if domain is None:
        credential = public_config.get("credential")
        if isinstance(credential, Mapping):
            domain = credential.get("domain")
    if not isinstance(domain, str) or domain not in _FEISHU_DOMAINS:
        _credential_unavailable()
    return FeishuDomain(domain)


def _nonempty(value: object) -> TypeGuard[str]:
    return type(value) is str and bool(value.strip())


def _credential_unavailable() -> NoReturn:
    # Suppress internal exception chains at this boundary: SQL bind parameters,
    # ciphertext, and a custom SecretStore's messages are not caller data.
    raise ProviderCredentialError(ProviderErrorCode.CREDENTIAL_UNAVAILABLE) from None
