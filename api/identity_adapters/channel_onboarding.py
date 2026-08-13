"""Privileged Channel-to-identity onboarding adapters for EIM-I6.1.

The source adapter closes its read session before decrypting.  The verifier
then performs Feishu Auth V3 and Tenant V2 without any database session.  Only
the repository's explicit ``apply`` method opens the authoritative write
transaction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn, TypeGuard

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret, SecretStore
from api.channel_providers import provider_spec, resolve_path
from api.db import IdentityProviderHealthState
from api.db.db_models import (
    ChannelSecret,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
)
from api.identity.contracts import IdentityErrorCode, ProviderContext
from api.identity.onboarding_contracts import (
    ChannelOnboardingAction,
    ChannelOnboardingError,
    ChannelOnboardingResult,
    ChannelOnboardingSourceSnapshot,
    ChannelOnboardingStatus,
    FeishuChannelOnboardingSource,
    VerifiedChannelOnboardingIntent,
    VerifiedFeishuInstallation,
)
from api.identity.providers.contracts import (
    FeishuDirectoryClient,
    FeishuDirectoryClientError,
    FeishuDomain,
    FeishuProviderCredential,
    ProviderErrorCode,
)
from api.identity.validation import valid_provider_context
from common.constants import StatusEnum

_PROVIDER = "feishu"
_MAX_PROVIDER_PROOF_AGE = timedelta(minutes=5)
_AUTH_CREDENTIAL_ERROR_CODES = frozenset({10015, 20002})
_VALID_HEALTH_STATES = frozenset(state.value for state in IdentityProviderHealthState)
_ADVISORY_DOMAIN = b"multirag:eim-i6.1:channel-onboarding:v1\x00"


class ChannelFeishuCredentialSource:
    """Read one tenant-active Feishu Channel and its exact secret version.

    The Channel may be disabled: verified onboarding is deliberately allowed
    before its worker is exposed to traffic.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_store: SecretStore,
    ) -> None:
        self._session_factory = session_factory
        self._secret_store = secret_store

    async def load(self, channel_id: str) -> FeishuChannelOnboardingSource:
        if type(channel_id) is not str or not channel_id.strip() or len(channel_id) > 32:
            _raise_identity(IdentityErrorCode.ASSERTION_INVALID)
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(
                            ChatChannel.id,
                            ChatChannel.tenant_id,
                            ChatChannel.channel,
                            ChatChannel.config,
                            ChatChannel.generation,
                            ChannelSecret.ciphertext,
                            ChannelSecret.key_id,
                            ChannelSecret.version,
                        )
                        .join(
                            Tenant,
                            and_(
                                Tenant.id == ChatChannel.tenant_id,
                                Tenant.status == StatusEnum.VALID.value,
                            ),
                        )
                        .join(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
                        .where(
                            ChatChannel.id == channel_id,
                            ChatChannel.channel == _PROVIDER,
                        )
                        .limit(2)
                    )
                ).all()
        except SQLAlchemyError:
            _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        if len(rows) != 1:
            _raise_identity(IdentityErrorCode.NOT_FOUND)

        (
            stored_channel_id,
            tenant_id,
            provider,
            channel_config,
            generation,
            ciphertext,
            key_id,
            secret_version,
        ) = rows[0]
        projection = _public_config_projection(channel_config)
        if (
            stored_channel_id != channel_id
            or not _nonempty(tenant_id, max_length=32)
            or provider != _PROVIDER
            or type(generation) is not int
            or generation < 1
            or projection is None
            or not _nonempty(ciphertext)
            or not _nonempty(key_id, max_length=128)
            or type(secret_version) is not int
            or secret_version < 1
        ):
            _raise_provider(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        public_config, app_id, domain, config_digest = projection
        envelope_digest = _secret_envelope_digest(
            channel_id=channel_id,
            ciphertext=ciphertext,
            key_id=key_id,
            version=secret_version,
        )

        # The database session above is already closed.  A local cipher, KMS,
        # or remote secret store can therefore never extend the DB transaction.
        try:
            plaintext = await self._secret_store.decrypt(
                tenant_id=tenant_id,
                channel_id=channel_id,
                encrypted=EncryptedSecret(
                    ciphertext=ciphertext,
                    key_id=key_id,
                    version=secret_version,
                ),
            )
        except Exception:
            _raise_provider(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        if not isinstance(plaintext, Mapping) or set(plaintext) != {"app_secret"}:
            _raise_provider(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
        app_secret = plaintext.get("app_secret")
        if not _nonempty(app_secret, max_length=4_096):
            _raise_provider(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)

        return FeishuChannelOnboardingSource(
            snapshot=ChannelOnboardingSourceSnapshot(
                channel_id=channel_id,
                tenant_id=tenant_id,
                provider=_PROVIDER,
                channel_generation=generation,
                public_config_digest=config_digest,
                secret_version=secret_version,
                secret_envelope_digest=envelope_digest,
                provider_account_key=app_id,
                domain=domain,
            ),
            app_secret=app_secret,
        )


class LarkFeishuInstallationVerifier:
    """Verify app ownership through official Auth V3 and Tenant V2 calls."""

    def __init__(
        self,
        directory_client: FeishuDirectoryClient,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._directory_client = directory_client
        self._now = now

    async def verify(self, source: FeishuChannelOnboardingSource) -> VerifiedFeishuInstallation:
        try:
            credential = FeishuProviderCredential(
                provider_account_id="onboarding",
                app_id=source.snapshot.provider_account_key,
                app_secret=source.app_secret,
                credential_generation=source.snapshot.secret_version,
                domain=source.snapshot.domain,
            )
            token_response = await self._directory_client.fetch_tenant_token(credential)
        except ChannelOnboardingError:
            raise
        except FeishuDirectoryClientError:
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        except Exception:
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        if token_response.code != 0 or not 200 <= token_response.http_status < 300:
            if token_response.code in _AUTH_CREDENTIAL_ERROR_CODES:
                _raise_provider(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        tenant_access_token = token_response.tenant_access_token
        if not _nonempty(tenant_access_token, max_length=16_384):
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        try:
            tenant_response = await self._directory_client.get_tenant(
                credential,
                tenant_access_token=tenant_access_token,
            )
        except FeishuDirectoryClientError:
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        except Exception:
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        finally:
            tenant_access_token = ""
        if tenant_response.code != 0 or not 200 <= tenant_response.http_status < 300 or not _nonempty(tenant_response.tenant_key, max_length=255):
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        try:
            verified_at = self._now()
        except Exception:
            _raise_provider(ProviderErrorCode.PROVIDER_UNAVAILABLE)
        try:
            return VerifiedFeishuInstallation(
                provider=_PROVIDER,
                provider_tenant_key=tenant_response.tenant_key,
                provider_account_key=source.snapshot.provider_account_key,
                verified_at=verified_at,
            )
        except (ChannelOnboardingError, ValueError, TypeError):
            _raise_provider(ProviderErrorCode.ASSERTION_INVALID)


@dataclass(slots=True)
class _IdentityState:
    provider_tenant: IdentityProviderTenant | None
    account: IdentityProviderAccount | None
    policy: IdentityTenantPolicy | None
    link: IdentityProviderChannelLink | None
    actions: tuple[ChannelOnboardingAction, ...]


class SqlAlchemyVerifiedChannelOnboardingRepository:
    """Preview read-only state and atomically apply a freshly verified intent."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def preview(self, intent: VerifiedChannelOnboardingIntent) -> tuple[ChannelOnboardingAction, ...]:
        _require_intent(intent)
        try:
            async with self._session_factory() as session:
                channel = await _active_channel(session, intent, for_update=False)
                state = await _identity_state(session, intent, for_update=False)
                secret = await _channel_secret(session, intent, for_update=False)
                _require_source_unchanged(intent, channel, secret)
                return state.actions
        except ChannelOnboardingError:
            raise
        except SQLAlchemyError:
            _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)

    async def apply(self, intent: VerifiedChannelOnboardingIntent) -> ChannelOnboardingResult:
        _require_intent(intent)
        try:
            async with self._session_factory.begin() as session:
                # Fixed order: Channel -> Tenant -> advisory domains -> Policy
                # -> ProviderTenant -> Account/Link -> Secret.  The shared
                # Channel -> Account/Link -> Secret subsequence matches Channel
                # update, while the explicit Tenant lock closes concurrent
                # tenant deactivation after the initial joined read.
                channel = await _active_channel(session, intent, for_update=True)
                await _active_tenant_for_update(session, intent.source.tenant_id)
                await _advisory_lock(
                    session,
                    "tenant-provider",
                    intent.source.tenant_id,
                    intent.source.provider,
                )
                await _advisory_lock(
                    session,
                    "external-provider-tenant",
                    intent.source.provider,
                    intent.proof.provider_tenant_key,
                )
                state = await _identity_state(session, intent, for_update=True)
                secret = await _channel_secret(session, intent, for_update=True)
                _require_source_unchanged(intent, channel, secret)
                operation_now = await _database_now(session)
                _require_fresh_proof(intent, operation_now)

                provider_tenant = state.provider_tenant
                if provider_tenant is None:
                    provider_tenant = IdentityProviderTenant(
                        id=uuid.uuid4().hex,
                        tenant_id=intent.source.tenant_id,
                        provider=intent.source.provider,
                        provider_tenant_key=intent.proof.provider_tenant_key,
                        verified_at=intent.proof.verified_at,
                    )
                    session.add(provider_tenant)
                    await session.flush()

                account = state.account
                if account is None:
                    account = IdentityProviderAccount(
                        id=uuid.uuid4().hex,
                        tenant_id=intent.source.tenant_id,
                        provider=intent.source.provider,
                        provider_tenant_key=intent.proof.provider_tenant_key,
                        provider_account_key=intent.proof.provider_account_key,
                        identity_revision=1,
                        identity_health_state=IdentityProviderHealthState.HEALTHY.value,
                        identity_health_error_code=None,
                    )
                    session.add(account)
                    await session.flush()
                elif account.identity_health_state != IdentityProviderHealthState.HEALTHY.value:
                    account.identity_health_state = IdentityProviderHealthState.HEALTHY.value
                    account.identity_health_error_code = None
                    account.identity_revision += 1
                    await session.flush()

                policy = state.policy
                if policy is None:
                    policy = IdentityTenantPolicy(
                        id=intent.source.tenant_id,
                        tenant_id=intent.source.tenant_id,
                        mode=intent.mode.value,
                        revision=1,
                        link_code_ttl_seconds=intent.link_code_ttl_seconds,
                        changed_at=operation_now,
                    )
                    session.add(policy)
                    await session.flush()

                link = state.link
                if link is None:
                    link = IdentityProviderChannelLink(
                        id=uuid.uuid4().hex,
                        tenant_id=intent.source.tenant_id,
                        provider=intent.source.provider,
                        provider_account_id=account.id,
                        channel_id=intent.source.channel_id,
                        linked_at=operation_now,
                    )
                    session.add(link)
                    await session.flush()

                context = ProviderContext(
                    tenant_id=account.tenant_id,
                    provider=account.provider,
                    provider_tenant_key=account.provider_tenant_key,
                    provider_account_id=account.id,
                    provider_account_key=account.provider_account_key,
                    provider_account_revision=account.identity_revision,
                    provider_account_last_scope_change_at=account.last_scope_change_at,
                )
                if not valid_provider_context(context):
                    _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
                return ChannelOnboardingResult(
                    status=(ChannelOnboardingStatus.APPLIED if state.actions else ChannelOnboardingStatus.UNCHANGED),
                    actions=state.actions,
                    mode=intent.mode,
                    policy_revision=policy.revision,
                    provider_account_revision=account.identity_revision,
                    context=context,
                )
        except ChannelOnboardingError:
            raise
        except SQLAlchemyError:
            _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)


async def _active_channel(
    session: AsyncSession,
    intent: VerifiedChannelOnboardingIntent,
    *,
    for_update: bool,
) -> ChatChannel:
    statement = (
        select(ChatChannel)
        .join(
            Tenant,
            and_(
                Tenant.id == ChatChannel.tenant_id,
                Tenant.status == StatusEnum.VALID.value,
            ),
        )
        .where(
            ChatChannel.id == intent.source.channel_id,
            ChatChannel.tenant_id == intent.source.tenant_id,
            ChatChannel.channel == intent.source.provider,
        )
    )
    if for_update:
        statement = statement.with_for_update(of=ChatChannel)
    rows = (await session.scalars(statement.limit(2))).all()
    if len(rows) != 1:
        _raise_identity(IdentityErrorCode.REVISION_CONFLICT)
    return rows[0]


async def _active_tenant_for_update(session: AsyncSession, tenant_id: str) -> None:
    found = await session.scalar(
        select(Tenant.id)
        .where(
            Tenant.id == tenant_id,
            Tenant.status == StatusEnum.VALID.value,
        )
        .with_for_update()
    )
    if found is None:
        _raise_identity(IdentityErrorCode.TENANT_MISMATCH)


async def _channel_secret(
    session: AsyncSession,
    intent: VerifiedChannelOnboardingIntent,
    *,
    for_update: bool,
) -> ChannelSecret:
    statement = select(ChannelSecret).where(ChannelSecret.channel_id == intent.source.channel_id)
    if for_update:
        statement = statement.with_for_update()
    rows = (await session.scalars(statement.limit(2))).all()
    if len(rows) != 1:
        _raise_identity(IdentityErrorCode.REVISION_CONFLICT)
    return rows[0]


async def _identity_state(
    session: AsyncSession,
    intent: VerifiedChannelOnboardingIntent,
    *,
    for_update: bool,
) -> _IdentityState:
    policy_statement = select(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id == intent.source.tenant_id)
    if for_update:
        policy_statement = policy_statement.with_for_update()
    policy = await session.scalar(policy_statement)
    if policy is not None and (policy.mode != intent.mode.value or policy.link_code_ttl_seconds != intent.link_code_ttl_seconds):
        _raise_identity(IdentityErrorCode.TRANSITION_INVALID)

    scope_statement = (
        select(IdentityProviderTenant)
        .where(
            IdentityProviderTenant.tenant_id == intent.source.tenant_id,
            IdentityProviderTenant.provider == intent.source.provider,
        )
        .order_by(IdentityProviderTenant.id)
    )
    if for_update:
        scope_statement = scope_statement.with_for_update()
    scope_rows = list((await session.scalars(scope_statement)).all())
    if len(scope_rows) > 1 or (scope_rows and scope_rows[0].provider_tenant_key != intent.proof.provider_tenant_key):
        _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)

    ownership_statement = select(IdentityProviderTenant).where(
        IdentityProviderTenant.provider == intent.source.provider,
        IdentityProviderTenant.provider_tenant_key == intent.proof.provider_tenant_key,
    )
    if for_update:
        ownership_statement = ownership_statement.with_for_update()
    ownership = await session.scalar(ownership_statement)
    if ownership is not None and ownership.tenant_id != intent.source.tenant_id:
        _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)
    provider_tenant = scope_rows[0] if scope_rows else ownership
    if provider_tenant is not None and (
        provider_tenant.tenant_id != intent.source.tenant_id
        or provider_tenant.provider != intent.source.provider
        or provider_tenant.provider_tenant_key != intent.proof.provider_tenant_key
        or (ownership is not None and ownership.id != provider_tenant.id)
    ):
        _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)

    account_statement = select(IdentityProviderAccount).where(
        IdentityProviderAccount.provider == intent.source.provider,
        IdentityProviderAccount.provider_tenant_key == intent.proof.provider_tenant_key,
        IdentityProviderAccount.provider_account_key == intent.proof.provider_account_key,
    )
    if for_update:
        account_statement = account_statement.with_for_update()
    account = await session.scalar(account_statement)
    if account is not None and (
        account.tenant_id != intent.source.tenant_id
        or account.provider != intent.source.provider
        or account.provider_tenant_key != intent.proof.provider_tenant_key
        or account.provider_account_key != intent.proof.provider_account_key
    ):
        _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)
    if account is not None:
        if account.identity_health_state == IdentityProviderHealthState.DISABLED.value:
            _raise_identity(IdentityErrorCode.INACTIVE)
        if account.identity_health_state not in _VALID_HEALTH_STATES or account.identity_revision < 1:
            _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)

    channel_link_statement = select(IdentityProviderChannelLink).where(
        IdentityProviderChannelLink.channel_id == intent.source.channel_id,
    )
    if for_update:
        channel_link_statement = channel_link_statement.with_for_update()
    link = await session.scalar(channel_link_statement)
    if link is not None and (account is None or link.provider_account_id != account.id or link.tenant_id != intent.source.tenant_id or link.provider != intent.source.provider):
        _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)
    if link is None and account is not None:
        account_link_statement = select(IdentityProviderChannelLink).where(
            IdentityProviderChannelLink.provider_account_id == account.id,
        )
        if for_update:
            account_link_statement = account_link_statement.with_for_update()
        account_link = await session.scalar(account_link_statement)
        if account_link is not None:
            if account_link.channel_id != intent.source.channel_id:
                _raise_identity(IdentityErrorCode.OWNERSHIP_CONFLICT)
            link = account_link

    actions: list[ChannelOnboardingAction] = []
    if provider_tenant is None:
        actions.append(ChannelOnboardingAction.CREATE_PROVIDER_TENANT)
    if account is None:
        actions.append(ChannelOnboardingAction.CREATE_PROVIDER_ACCOUNT)
    if account is None or account.identity_health_state != IdentityProviderHealthState.HEALTHY.value:
        actions.append(ChannelOnboardingAction.MARK_PROVIDER_ACCOUNT_HEALTHY)
    if policy is None:
        actions.append(ChannelOnboardingAction.CREATE_TENANT_POLICY)
    if link is None:
        actions.append(ChannelOnboardingAction.CREATE_CHANNEL_LINK)
    return _IdentityState(
        provider_tenant=provider_tenant,
        account=account,
        policy=policy,
        link=link,
        actions=tuple(actions),
    )


def _require_intent(intent: VerifiedChannelOnboardingIntent) -> None:
    if (
        not isinstance(intent, VerifiedChannelOnboardingIntent)
        or intent.source.provider != _PROVIDER
        or intent.proof.provider != _PROVIDER
        or intent.source.provider_account_key != intent.proof.provider_account_key
        or not _nonempty(intent.source.channel_id, max_length=32)
        or not _nonempty(intent.source.tenant_id, max_length=32)
        or not _nonempty(intent.proof.provider_tenant_key, max_length=255)
        or not _nonempty(intent.proof.provider_account_key, max_length=255)
    ):
        _raise_identity(IdentityErrorCode.ASSERTION_INVALID)


def _require_source_unchanged(
    intent: VerifiedChannelOnboardingIntent,
    channel: ChatChannel,
    secret: ChannelSecret,
) -> None:
    projection = _public_config_projection(channel.config)
    if projection is None:
        _raise_identity(IdentityErrorCode.REVISION_CONFLICT)
    _, app_id, domain, config_digest = projection
    if (
        channel.id != intent.source.channel_id
        or channel.tenant_id != intent.source.tenant_id
        or channel.channel != intent.source.provider
        or channel.generation != intent.source.channel_generation
        or config_digest != intent.source.public_config_digest
        or app_id != intent.source.provider_account_key
        or domain is not intent.source.domain
        or secret.channel_id != intent.source.channel_id
        or secret.version != intent.source.secret_version
        or _secret_envelope_digest(
            channel_id=secret.channel_id,
            ciphertext=secret.ciphertext,
            key_id=secret.key_id,
            version=secret.version,
        )
        != intent.source.secret_envelope_digest
    ):
        _raise_identity(IdentityErrorCode.REVISION_CONFLICT)


def _require_fresh_proof(intent: VerifiedChannelOnboardingIntent, operation_now: datetime) -> None:
    verified_at = intent.proof.verified_at
    if verified_at.tzinfo is None or verified_at.utcoffset() is None or verified_at > operation_now or verified_at < operation_now - _MAX_PROVIDER_PROOF_AGE:
        _raise_identity(IdentityErrorCode.ASSERTION_INVALID)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _raise_identity(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
    return value


async def _advisory_lock(session: AsyncSession, domain: str, *parts: str) -> None:
    digest = hashlib.sha256()
    digest.update(_ADVISORY_DOMAIN)
    domain_raw = domain.encode()
    digest.update(len(domain_raw).to_bytes(4, "big"))
    digest.update(domain_raw)
    for part in parts:
        raw = part.encode()
        digest.update(len(raw).to_bytes(4, "big"))
        digest.update(raw)
    lock_key = int.from_bytes(digest.digest()[:8], "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _public_config_projection(
    value: object,
) -> tuple[dict[str, object], str, FeishuDomain, str] | None:
    if not isinstance(value, Mapping):
        return None
    public_config: dict[str, object] = {}
    for key, nested in value.items():
        if type(key) is not str:
            return None
        public_config[key] = nested
    if resolve_path(public_config, "credential.app_secret") is not None:
        return None
    try:
        spec = provider_spec(_PROVIDER)
        validated = spec.config_model.model_validate(public_config)
        normalized = validated.model_dump(mode="python", exclude_none=True)
        raw_app_id = spec.account_identity(public_config)
        normalized_app_id = spec.account_identity(normalized)
        domain_value = normalized.get("domain")
        encoded = json.dumps(
            public_config,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError):
        return None
    if not _nonempty(raw_app_id, max_length=128) or raw_app_id != normalized_app_id or type(domain_value) is not str or domain_value not in {domain.value for domain in FeishuDomain}:
        return None
    return (
        public_config,
        raw_app_id,
        FeishuDomain(domain_value),
        _domain_digest(b"public-config\x00", encoded),
    )


def _secret_envelope_digest(
    *,
    channel_id: str,
    ciphertext: str,
    key_id: str,
    version: int,
) -> str:
    parts = (
        channel_id.encode(),
        ciphertext.encode(),
        key_id.encode(),
        str(version).encode(),
    )
    return _domain_digest(b"secret-envelope\x00", *parts)


def _domain_digest(domain: bytes, *parts: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(_ADVISORY_DOMAIN)
    digest.update(domain)
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _nonempty(value: object, *, max_length: int = 1_000_000) -> TypeGuard[str]:
    return type(value) is str and bool(value.strip()) and len(value) <= max_length


def _raise_identity(code: IdentityErrorCode) -> NoReturn:
    raise ChannelOnboardingError(code) from None


def _raise_provider(code: ProviderErrorCode) -> NoReturn:
    raise ChannelOnboardingError(code) from None
