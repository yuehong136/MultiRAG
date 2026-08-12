"""Real PostgreSQL tests for the EIM-I3 identity repository boundary."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import (
    ExternalIdentityState,
    IdentityProviderHealthState,
    UserAccountKind,
    UserTenantRole,
)
from api.db.db_models import (
    EnterpriseSubjectLink,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityEventReceipt,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import (
    AliasKey,
    CasOutcome,
    ExternalIdentityAliasInsert,
    ExternalIdentityInsert,
    IdentityErrorCode,
    IdentityResolutionRequest,
    IdentityResolutionStatus,
    IdentityStateTransition,
    InsertOutcome,
    ProviderAccountHealthCAS,
    ProviderAliasType,
    ProviderContext,
    ProvisioningMode,
    VerifiedIdentityActivation,
    VerifiedProviderAccountOnboarding,
    VerifiedProviderTenantOnboarding,
)
from api.identity.repository import IdentityRepositoryError, SqlAlchemyIdentityRepository
from api.identity.service import IdentityService
from common.constants import StatusEnum

_CREATED_TENANT_IDS: set[str] = set()
_CREATED_USER_IDS: set[str] = set()


@dataclass(frozen=True, slots=True)
class _Seed:
    tenant_id: str
    user_id: str
    other_user_id: str
    provider_tenant_id: str
    provider_account_id: str
    identity_id: str
    alias_id: str
    membership_id: str
    provider_tenant_key: str
    provider_account_key: str
    subject_value: str
    alias_value: str
    now: datetime

    @property
    def context(self) -> ProviderContext:
        return ProviderContext(
            tenant_id=self.tenant_id,
            provider="feishu",
            provider_tenant_key=self.provider_tenant_key,
            provider_account_id=self.provider_account_id,
            provider_account_key=self.provider_account_key,
            provider_account_revision=1,
        )

    @property
    def request(self) -> IdentityResolutionRequest:
        return IdentityResolutionRequest(
            context=self.context,
            alias=AliasKey(
                alias_type=ProviderAliasType.OPEN_ID,
                alias_value=self.alias_value,
            ),
        )


class _CountingPolicyResolver:
    def __init__(self, mode: ProvisioningMode = ProvisioningMode.LINK_ONLY) -> None:
        self.mode = mode
        self.calls: list[str] = []

    async def get_mode(self, tenant_id: str) -> ProvisioningMode:
        self.calls.append(tenant_id)
        return self.mode


def _new_seed() -> _Seed:
    suffix = uuid.uuid4().hex
    seed = _Seed(
        tenant_id=uuid.uuid4().hex,
        user_id=uuid.uuid4().hex,
        other_user_id=uuid.uuid4().hex,
        provider_tenant_id=uuid.uuid4().hex,
        provider_account_id=uuid.uuid4().hex,
        identity_id=uuid.uuid4().hex,
        alias_id=uuid.uuid4().hex,
        membership_id=uuid.uuid4().hex,
        provider_tenant_key=f"tenant-key-{suffix}",
        provider_account_key=f"app-key-{suffix}",
        subject_value=f"provider-user-{suffix}",
        alias_value=f"open-id-{suffix}",
        now=datetime(2026, 8, 12, 12, 0, tzinfo=UTC),
    )
    _CREATED_TENANT_IDS.add(seed.tenant_id)
    _CREATED_USER_IDS.update((seed.user_id, seed.other_user_id))
    return seed


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean_committed_identity_repository_rows(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[None]:
    """Keep real commit/concurrency semantics without polluting later migration tests."""

    _CREATED_TENANT_IDS.clear()
    _CREATED_USER_IDS.clear()
    try:
        yield
    finally:
        tenant_ids = tuple(_CREATED_TENANT_IDS)
        user_ids = tuple(_CREATED_USER_IDS)
        _CREATED_TENANT_IDS.clear()
        _CREATED_USER_IDS.clear()
        factory = _factory(bootstrapped_async_engine)
        async with factory.begin() as session:
            if tenant_ids:
                for model in (
                    IdentityEventReceipt,
                    ExternalIdentityAlias,
                    EnterpriseSubjectLink,
                    ExternalIdentity,
                    IdentityProviderChannelLink,
                    IdentityProviderAccount,
                    IdentityProviderTenant,
                    UserTenant,
                ):
                    await session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            if tenant_ids:
                await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


def _tenant(seed: _Seed) -> Tenant:
    return Tenant(
        id=seed.tenant_id,
        name="Identity repository tenant",
        llm_id="test-llm",
        embd_id="test-embedding",
        asr_id="test-asr",
        img2txt_id="test-image",
        parser_ids="naive",
    )


def _user(user_id: str, nickname: str) -> User:
    return User(
        id=user_id,
        nickname=nickname,
        email=None,
        password=None,
        account_kind=UserAccountKind.EXTERNAL.value,
        login_channel="feishu",
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        status=StatusEnum.VALID.value,
    )


async def _seed_graph(
    factory: async_sessionmaker[AsyncSession],
    seed: _Seed,
    *,
    with_identity: bool = True,
    with_alias: bool = True,
    with_membership: bool = True,
    identity_state: str = ExternalIdentityState.ACTIVE.value,
) -> None:
    if with_alias and not with_identity:
        raise ValueError("an alias requires a canonical identity")

    async with factory.begin() as session:
        session.add_all(
            [
                _tenant(seed),
                _user(seed.user_id, "Identity repository user"),
                _user(seed.other_user_id, "Identity repository other user"),
            ]
        )
        await session.flush()
        session.add(
            IdentityProviderTenant(
                id=seed.provider_tenant_id,
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                verified_at=seed.now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=seed.provider_account_id,
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_key=seed.provider_account_key,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        await session.flush()

        if with_membership:
            session.add_all(
                [
                    UserTenant(
                        id=seed.membership_id,
                        user_id=seed.user_id,
                        tenant_id=seed.tenant_id,
                        role=UserTenantRole.NORMAL.value,
                        invited_by=seed.user_id,
                        status=StatusEnum.VALID.value,
                    ),
                    UserTenant(
                        id=uuid.uuid4().hex,
                        user_id=seed.other_user_id,
                        tenant_id=seed.tenant_id,
                        role=UserTenantRole.NORMAL.value,
                        invited_by=seed.user_id,
                        status=StatusEnum.VALID.value,
                    ),
                ]
            )

        if not with_identity:
            return
        session.add(
            ExternalIdentity(
                id=seed.identity_id,
                tenant_id=seed.tenant_id,
                user_id=seed.user_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                subject_type="user_id",
                subject_value=seed.subject_value,
                state=identity_state,
                verified_at=seed.now if identity_state == ExternalIdentityState.ACTIVE.value else None,
                identity_revision=1,
                attributes={"display_name": "Test User"},
            )
        )
        await session.flush()
        if with_alias:
            session.add(
                ExternalIdentityAlias(
                    id=seed.alias_id,
                    tenant_id=seed.tenant_id,
                    external_identity_id=seed.identity_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    provider_account_key=seed.provider_account_key,
                    alias_type=ProviderAliasType.OPEN_ID,
                    alias_value=seed.alias_value,
                    verified_at=seed.now,
                )
            )


def _identity_insert(
    seed: _Seed,
    *,
    user_id: str | None = None,
    subject_value: str | None = None,
) -> ExternalIdentityInsert:
    return ExternalIdentityInsert(
        context=seed.context,
        user_id=user_id or seed.user_id,
        subject_type="user_id",
        subject_value=subject_value or seed.subject_value,
        attributes=(("display_name", "Test User"),),
    )


def _alias_insert(
    seed: _Seed,
    identity_id: str,
    *,
    alias_value: str | None = None,
) -> ExternalIdentityAliasInsert:
    return ExternalIdentityAliasInsert(
        context=seed.context,
        external_identity_id=identity_id,
        alias_type=ProviderAliasType.OPEN_ID,
        alias_value=alias_value or seed.alias_value,
        verified_at=seed.now,
    )


async def test_provider_account_is_channel_independent_and_cross_tenant_hidden(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=False)
    assert not hasattr(seed.context, "channel_id")

    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)
        account = await repository.get_provider_account(seed.context)
        assert account is not None
        assert account.context() == seed.context
        assert await session.scalar(select(func.count()).select_from(IdentityProviderChannelLink).where(IdentityProviderChannelLink.provider_account_id == seed.provider_account_id)) == 0

        wrong_tenant = ProviderContext(
            tenant_id=uuid.uuid4().hex,
            provider=seed.context.provider,
            provider_tenant_key=seed.provider_tenant_key,
            provider_account_id=seed.provider_account_id,
            provider_account_key=seed.provider_account_key,
            provider_account_revision=1,
        )
        assert await repository.get_provider_account(wrong_tenant) is None


async def test_verified_ownership_onboarding_is_narrow_idempotent_and_fail_closed(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    other_tenant_id = uuid.uuid4().hex
    _CREATED_TENANT_IDS.add(other_tenant_id)
    async with factory.begin() as session:
        session.add_all(
            [
                _tenant(seed),
                Tenant(
                    id=other_tenant_id,
                    name="Other identity tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
            ]
        )

    tenant_command = VerifiedProviderTenantOnboarding(
        tenant_id=seed.tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        verified_at=seed.now,
    )
    account_command = VerifiedProviderAccountOnboarding(
        tenant_id=seed.tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        provider_account_key=seed.provider_account_key,
    )
    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        tenant_created = await repository.insert_verified_provider_tenant(tenant_command)
        account_created = await repository.insert_verified_provider_account(account_command)
    assert tenant_created.outcome is InsertOutcome.CREATED
    assert account_created.outcome is InsertOutcome.CREATED

    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        tenant_existing = await repository.insert_verified_provider_tenant(tenant_command)
        account_existing = await repository.insert_verified_provider_account(account_command)
    assert tenant_existing.outcome is InsertOutcome.EXISTING
    assert account_existing.outcome is InsertOutcome.EXISTING
    assert tenant_existing.record.id == tenant_created.record.id
    assert account_existing.record.id == account_created.record.id

    conflicting_tenant = VerifiedProviderTenantOnboarding(
        tenant_id=other_tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        verified_at=seed.now,
    )
    async with factory.begin() as session:
        with pytest.raises(IdentityRepositoryError) as tenant_error:
            await SqlAlchemyIdentityRepository(session).insert_verified_provider_tenant(conflicting_tenant)
    assert tenant_error.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT

    conflicting_account = VerifiedProviderAccountOnboarding(
        tenant_id=other_tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        provider_account_key=seed.provider_account_key,
    )
    async with factory.begin() as session:
        with pytest.raises(IdentityRepositoryError) as account_error:
            await SqlAlchemyIdentityRepository(session).insert_verified_provider_account(conflicting_account)
    assert account_error.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT

    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityProviderTenant)
                .where(
                    IdentityProviderTenant.provider == "feishu",
                    IdentityProviderTenant.provider_tenant_key == seed.provider_tenant_key,
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityProviderAccount)
                .where(
                    IdentityProviderAccount.provider == "feishu",
                    IdentityProviderAccount.provider_account_key == seed.provider_account_key,
                )
            )
            == 1
        )


async def test_service_validates_authoritative_account_before_alias_miss_policy(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=False)
    policy = _CountingPolicyResolver()

    async with factory() as session:
        service = IdentityService(SqlAlchemyIdentityRepository(session), policy)
        wrong_contexts = (
            ProviderContext(
                tenant_id=uuid.uuid4().hex,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_id=seed.provider_account_id,
                provider_account_key=seed.provider_account_key,
                provider_account_revision=1,
            ),
            ProviderContext(
                tenant_id=seed.tenant_id,
                provider="dingtalk",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_id=seed.provider_account_id,
                provider_account_key=seed.provider_account_key,
                provider_account_revision=1,
            ),
            ProviderContext(
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key="wrong-provider-tenant",
                provider_account_id=seed.provider_account_id,
                provider_account_key=seed.provider_account_key,
                provider_account_revision=1,
            ),
            ProviderContext(
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_id=uuid.uuid4().hex,
                provider_account_key=seed.provider_account_key,
                provider_account_revision=1,
            ),
            ProviderContext(
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_id=seed.provider_account_id,
                provider_account_key=seed.provider_account_key,
                provider_account_revision=2,
            ),
        )
        for context in wrong_contexts:
            result = await service.resolve_external_identity(
                IdentityResolutionRequest(
                    context=context,
                    alias=AliasKey(
                        alias_type=ProviderAliasType.OPEN_ID,
                        alias_value="missing-alias",
                    ),
                )
            )
            assert result.status is IdentityResolutionStatus.CONFLICT
        assert policy.calls == []

        wrong_scope_generation = ProviderContext(
            tenant_id=seed.tenant_id,
            provider="feishu",
            provider_tenant_key=seed.provider_tenant_key,
            provider_account_id=seed.provider_account_id,
            provider_account_key=seed.provider_account_key,
            provider_account_revision=1,
            provider_account_last_scope_change_at=seed.now,
        )
        scope_generation_result = await service.resolve_external_identity(
            IdentityResolutionRequest(
                context=wrong_scope_generation,
                alias=AliasKey(
                    alias_type=ProviderAliasType.OPEN_ID,
                    alias_value="missing-alias",
                ),
            )
        )
        assert scope_generation_result.status is IdentityResolutionStatus.CONFLICT
        assert policy.calls == []

        valid_missing = await service.resolve_external_identity(
            IdentityResolutionRequest(
                context=seed.context,
                alias=AliasKey(
                    alias_type=ProviderAliasType.OPEN_ID,
                    alias_value="missing-alias",
                ),
            )
        )
        assert valid_missing.status is IdentityResolutionStatus.MISSING
        assert valid_missing.error_code is IdentityErrorCode.LINK_REQUIRED
        assert valid_missing.provider_verification_required is True
        assert policy.calls == [seed.tenant_id]

    async with factory.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == seed.tenant_id).values(status=StatusEnum.INVALID.value))
    async with factory() as session:
        inactive_tenant = await IdentityService(
            SqlAlchemyIdentityRepository(session),
            policy,
        ).resolve_external_identity(seed.request)
    assert inactive_tenant.status is IdentityResolutionStatus.CONFLICT
    assert policy.calls == [seed.tenant_id]


async def test_resolution_rechecks_active_user_and_membership_on_every_query(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)

    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)
        snapshot = await repository.resolve_identity(seed.request)
        assert snapshot is not None
        assert snapshot.identity.id == seed.identity_id
        assert snapshot.membership is not None
        assert snapshot.membership.role == UserTenantRole.NORMAL.value

    async with factory.begin() as session:
        await session.execute(update(User).where(User.id == seed.user_id).values(is_active=False))
    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)
        inactive_user = await repository.resolve_identity(seed.request)
        assert inactive_user is not None
        assert inactive_user.identity is not None
        assert inactive_user.identity.id == seed.identity_id
        assert inactive_user.membership is None
        policy = _CountingPolicyResolver()
        result = await IdentityService(repository, policy).resolve_external_identity(seed.request)
        assert result.status is IdentityResolutionStatus.INACTIVE
        assert result.error_code is IdentityErrorCode.INACTIVE
        assert policy.calls == []

    async with factory.begin() as session:
        await session.execute(update(User).where(User.id == seed.user_id).values(is_active=True))
        await session.execute(update(UserTenant).where(UserTenant.id == seed.membership_id).values(status=StatusEnum.INVALID.value))
    async with factory() as session:
        snapshot = await SqlAlchemyIdentityRepository(session).resolve_identity(seed.request)
        assert snapshot is not None
        assert snapshot.identity.id == seed.identity_id
        assert snapshot.membership is None


async def test_alias_before_scope_change_requires_provider_reverification(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)
    scope_change = seed.now + timedelta(minutes=5)
    async with factory.begin() as session:
        await session.execute(update(IdentityProviderAccount).where(IdentityProviderAccount.id == seed.provider_account_id).values(last_scope_change_at=scope_change))

    context = ProviderContext(
        tenant_id=seed.tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        provider_account_id=seed.provider_account_id,
        provider_account_key=seed.provider_account_key,
        provider_account_revision=1,
        provider_account_last_scope_change_at=scope_change,
    )
    request = IdentityResolutionRequest(
        context=context,
        alias=AliasKey(
            alias_type=ProviderAliasType.OPEN_ID,
            alias_value=seed.alias_value,
        ),
    )
    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        policy = _CountingPolicyResolver()
        stale = await IdentityService(repository, policy).resolve_external_identity(request)
        assert stale.status is IdentityResolutionStatus.INACTIVE
        assert stale.provider_verification_required is True
        assert policy.calls == []

        refreshed = await repository.insert_alias(
            ExternalIdentityAliasInsert(
                context=context,
                external_identity_id=seed.identity_id,
                alias_type=ProviderAliasType.OPEN_ID,
                alias_value=seed.alias_value,
                verified_at=scope_change,
            )
        )
        assert refreshed.outcome is InsertOutcome.EXISTING
        resolved = await IdentityService(repository, policy).resolve_external_identity(request)
        assert resolved.status is IdentityResolutionStatus.RESOLVED

        with pytest.raises(IdentityRepositoryError) as stale_verification:
            await repository.insert_alias(
                ExternalIdentityAliasInsert(
                    context=context,
                    external_identity_id=seed.identity_id,
                    alias_type=ProviderAliasType.OPEN_ID,
                    alias_value=seed.alias_value,
                    verified_at=seed.now,
                )
            )
        assert stale_verification.value.code is IdentityErrorCode.REVISION_CONFLICT
        alias_verified_at = await session.scalar(select(ExternalIdentityAlias.verified_at).where(ExternalIdentityAlias.id == seed.alias_id))
        assert alias_verified_at == scope_change


async def test_duplicate_active_memberships_fail_closed(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)
    async with factory.begin() as session:
        session.add(
            UserTenant(
                id=uuid.uuid4().hex,
                user_id=seed.user_id,
                tenant_id=seed.tenant_id,
                role=UserTenantRole.ADMIN.value,
                invited_by=seed.user_id,
                status=StatusEnum.VALID.value,
            )
        )

    async with factory() as session:
        with pytest.raises(IdentityRepositoryError) as raised:
            await SqlAlchemyIdentityRepository(session).resolve_identity(seed.request)
    assert raised.value.code is IdentityErrorCode.LINK_CONFLICT
    assert seed.alias_value not in str(raised.value)
    assert seed.provider_tenant_key not in str(raised.value)


async def test_identity_and_alias_inserts_are_idempotent_and_never_overwrite_conflicts(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=True)
    identity_command = _identity_insert(seed)

    async with factory.begin() as session:
        await session.execute(update(IdentityProviderAccount).where(IdentityProviderAccount.id == seed.provider_account_id).values(identity_health_state=IdentityProviderHealthState.DEGRADED.value))
    async with factory.begin() as session:
        with pytest.raises(IdentityRepositoryError) as unhealthy_error:
            await SqlAlchemyIdentityRepository(session).insert_identity(identity_command)
    assert unhealthy_error.value.code is IdentityErrorCode.INACTIVE
    async with factory.begin() as session:
        await session.execute(update(IdentityProviderAccount).where(IdentityProviderAccount.id == seed.provider_account_id).values(identity_health_state=IdentityProviderHealthState.HEALTHY.value))

    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        created_identity = await repository.insert_identity(identity_command)
        created_alias = await repository.insert_alias(_alias_insert(seed, created_identity.record.id))
    assert created_identity.outcome is InsertOutcome.CREATED
    assert created_identity.record.state == ExternalIdentityState.PENDING_LINK.value
    assert created_identity.record.verified_at is None
    assert created_identity.record.identity_revision == 1
    assert created_alias.outcome is InsertOutcome.CREATED

    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        existing_identity = await repository.insert_identity(identity_command)
        existing_alias = await repository.insert_alias(_alias_insert(seed, created_identity.record.id))
    assert existing_identity.outcome is InsertOutcome.EXISTING
    assert existing_alias.outcome is InsertOutcome.EXISTING
    assert existing_identity.record.id == created_identity.record.id

    conflicting_identity = _identity_insert(seed, user_id=seed.other_user_id)
    async with factory.begin() as session:
        with pytest.raises(IdentityRepositoryError) as identity_error:
            await SqlAlchemyIdentityRepository(session).insert_identity(conflicting_identity)
    assert identity_error.value.code is IdentityErrorCode.LINK_CONFLICT

    other_subject = f"other-{seed.subject_value}"
    async with factory.begin() as session:
        other_identity = await SqlAlchemyIdentityRepository(session).insert_identity(
            _identity_insert(
                seed,
                user_id=seed.other_user_id,
                subject_value=other_subject,
            )
        )
    async with factory.begin() as session:
        with pytest.raises(IdentityRepositoryError) as alias_error:
            await SqlAlchemyIdentityRepository(session).insert_alias(_alias_insert(seed, other_identity.record.id))
    assert alias_error.value.code is IdentityErrorCode.LINK_CONFLICT

    async with factory() as session:
        identity_owner = await session.scalar(
            select(ExternalIdentity.user_id).where(
                ExternalIdentity.tenant_id == seed.tenant_id,
                ExternalIdentity.subject_value == seed.subject_value,
            )
        )
        alias_owner = await session.scalar(
            select(ExternalIdentityAlias.external_identity_id).where(
                ExternalIdentityAlias.tenant_id == seed.tenant_id,
                ExternalIdentityAlias.alias_value == seed.alias_value,
            )
        )
    assert identity_owner == seed.user_id
    assert alias_owner == created_identity.record.id


async def test_concurrent_identity_and_alias_inserts_have_one_winner(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=True)
    identity_command = _identity_insert(seed)

    async def insert_identity_once() -> tuple[InsertOutcome, str]:
        async with factory.begin() as session:
            result = await SqlAlchemyIdentityRepository(session).insert_identity(identity_command)
            return result.outcome, result.record.id

    identity_results = await asyncio.gather(
        insert_identity_once(),
        insert_identity_once(),
    )
    assert {outcome for outcome, _identity_id in identity_results} == {
        InsertOutcome.CREATED,
        InsertOutcome.EXISTING,
    }
    identity_ids = {identity_id for _outcome, identity_id in identity_results}
    assert len(identity_ids) == 1
    identity_id = identity_ids.pop()

    async def insert_alias_once() -> tuple[InsertOutcome, str]:
        async with factory.begin() as session:
            result = await SqlAlchemyIdentityRepository(session).insert_alias(_alias_insert(seed, identity_id))
            return result.outcome, result.record.id

    alias_results = await asyncio.gather(
        insert_alias_once(),
        insert_alias_once(),
    )
    assert {outcome for outcome, _identity_id in alias_results} == {
        InsertOutcome.CREATED,
        InsertOutcome.EXISTING,
    }
    assert {record_id for _outcome, record_id in alias_results} == {identity_id}


async def test_identity_state_cas_rejects_stale_and_requires_explicit_verified_activation(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)
    old_audit_date = seed.now - timedelta(days=1)
    async with factory.begin() as session:
        await session.execute(update(ExternalIdentity).where(ExternalIdentity.id == seed.identity_id).values(update_date=old_audit_date, update_time=1))

    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        inactive = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=1,
                target_state=ExternalIdentityState.INACTIVE.value,
            )
        )
        stale = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=1,
                target_state=ExternalIdentityState.INACTIVE.value,
            )
        )
        ordinary_reactivation = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=2,
                target_state=ExternalIdentityState.ACTIVE.value,
            )
        )
        reverified_at = seed.now + timedelta(minutes=5)
        older_activation = await repository.activate_verified_identity(
            VerifiedIdentityActivation(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=2,
                verified_at=seed.now - timedelta(seconds=1),
            )
        )
        activated = await repository.activate_verified_identity(
            VerifiedIdentityActivation(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=2,
                verified_at=reverified_at,
            )
        )
        conflict = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=3,
                target_state=ExternalIdentityState.CONFLICT.value,
            )
        )
        conflict_recovery = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=4,
                target_state=ExternalIdentityState.ACTIVE.value,
            )
        )
        conflict_terminal = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=seed.identity_id,
                expected_revision=4,
                target_state=ExternalIdentityState.REVOKED.value,
            )
        )

    revoked_identity_id = uuid.uuid4().hex
    async with factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=revoked_identity_id,
                tenant_id=seed.tenant_id,
                user_id=seed.user_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                subject_type="user_id",
                subject_value=f"revoked-{seed.subject_value}",
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=seed.now,
                identity_revision=1,
                attributes={},
            )
        )

    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        revoked = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=revoked_identity_id,
                expected_revision=1,
                target_state=ExternalIdentityState.REVOKED.value,
            )
        )
        revoked_recovery = await repository.cas_identity_state(
            IdentityStateTransition(
                context=seed.context,
                external_identity_id=revoked_identity_id,
                expected_revision=2,
                target_state=ExternalIdentityState.INACTIVE.value,
            )
        )

    assert (inactive.outcome, inactive.revision) == (CasOutcome.APPLIED, 2)
    assert (stale.outcome, stale.revision) == (CasOutcome.REVISION_CONFLICT, 2)
    assert ordinary_reactivation.outcome is CasOutcome.INVALID_TRANSITION
    assert older_activation.outcome is CasOutcome.INVALID_TRANSITION
    assert (activated.outcome, activated.revision) == (CasOutcome.APPLIED, 3)
    assert (conflict.outcome, conflict.revision) == (CasOutcome.APPLIED, 4)
    assert conflict_recovery.outcome is CasOutcome.INVALID_TRANSITION
    assert conflict_terminal.outcome is CasOutcome.INVALID_TRANSITION
    assert (revoked.outcome, revoked.revision) == (CasOutcome.APPLIED, 2)
    assert revoked_recovery.outcome is CasOutcome.INVALID_TRANSITION

    async with factory() as session:
        state, revision, verified_at, update_date, update_time = (
            await session.execute(
                select(
                    ExternalIdentity.state,
                    ExternalIdentity.identity_revision,
                    ExternalIdentity.verified_at,
                    ExternalIdentity.update_date,
                    ExternalIdentity.update_time,
                ).where(ExternalIdentity.id == seed.identity_id)
            )
        ).one()
    assert state == ExternalIdentityState.CONFLICT.value
    assert revision == 4
    assert verified_at == reverified_at
    assert update_date is not None and update_date.replace(tzinfo=UTC) > old_audit_date
    assert update_time is not None and update_time > 1

    async with factory() as session:
        revoked_state, revoked_revision = (
            await session.execute(
                select(
                    ExternalIdentity.state,
                    ExternalIdentity.identity_revision,
                ).where(ExternalIdentity.id == revoked_identity_id)
            )
        ).one()
    assert revoked_state == ExternalIdentityState.REVOKED.value
    assert revoked_revision == 2


async def test_concurrent_identity_state_cas_has_one_winner(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)
    command = IdentityStateTransition(
        context=seed.context,
        external_identity_id=seed.identity_id,
        expected_revision=1,
        target_state=ExternalIdentityState.INACTIVE.value,
    )

    async def transition_once() -> tuple[CasOutcome, int | None]:
        async with factory.begin() as session:
            result = await SqlAlchemyIdentityRepository(session).cas_identity_state(command)
            return result.outcome, result.revision

    results = await asyncio.gather(transition_once(), transition_once())
    assert sorted(outcome.value for outcome, _revision in results) == sorted([CasOutcome.APPLIED.value, CasOutcome.REVISION_CONFLICT.value])
    assert {revision for _outcome, revision in results} == {2}


async def test_provider_account_health_cas_has_one_winner_and_preserves_ownership(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=False)
    commands = (
        ProviderAccountHealthCAS(
            context=seed.context,
            target_health_state=IdentityProviderHealthState.HEALTHY.value,
            last_scope_change_at=seed.now,
        ),
        ProviderAccountHealthCAS(
            context=seed.context,
            target_health_state=IdentityProviderHealthState.ERROR.value,
            error_code="provider_unavailable",
            last_directory_event_at=seed.now + timedelta(minutes=1),
        ),
    )

    async def update_once(command: ProviderAccountHealthCAS) -> tuple[CasOutcome, int | None]:
        async with factory.begin() as session:
            result = await SqlAlchemyIdentityRepository(session).cas_provider_account_health(command)
            return result.outcome, result.revision

    results = await asyncio.gather(*(update_once(command) for command in commands))
    assert sorted(outcome.value for outcome, _revision in results) == sorted([CasOutcome.APPLIED.value, CasOutcome.REVISION_CONFLICT.value])
    assert {revision for _outcome, revision in results} == {2}

    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.provider_account_id)
        assert account is not None
        assert account.tenant_id == seed.tenant_id
        assert account.provider == "feishu"
        assert account.provider_tenant_key == seed.provider_tenant_key
        assert account.provider_account_key == seed.provider_account_key
        assert account.identity_revision == 2
        assert account.identity_health_state in {
            IdentityProviderHealthState.HEALTHY.value,
            IdentityProviderHealthState.ERROR.value,
        }


async def test_health_cas_preserves_optional_times_rejects_rewind_and_updates_audit(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    scope_time = seed.now
    event_time = seed.now + timedelta(minutes=1)
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=False)
    async with factory.begin() as session:
        await session.execute(
            update(IdentityProviderAccount)
            .where(IdentityProviderAccount.id == seed.provider_account_id)
            .values(
                last_scope_change_at=scope_time,
                last_directory_event_at=event_time,
            )
        )

    context = ProviderContext(
        tenant_id=seed.tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        provider_account_id=seed.provider_account_id,
        provider_account_key=seed.provider_account_key,
        provider_account_revision=1,
        provider_account_last_scope_change_at=scope_time,
    )
    async with factory() as session:
        before = await session.get(IdentityProviderAccount, seed.provider_account_id)
        assert before is not None
        before_update_time = before.update_time

    async with factory.begin() as session:
        applied = await SqlAlchemyIdentityRepository(session).cas_provider_account_health(
            ProviderAccountHealthCAS(
                context=context,
                target_health_state=IdentityProviderHealthState.DEGRADED.value,
            )
        )
    assert (applied.outcome, applied.revision) == (CasOutcome.APPLIED, 2)

    async with factory() as session:
        after = await session.get(IdentityProviderAccount, seed.provider_account_id)
        assert after is not None
        assert after.last_scope_change_at == scope_time
        assert after.last_directory_event_at == event_time
        assert after.update_time is not None
        assert before_update_time is None or after.update_time >= before_update_time

    stale_marker_context = replace(
        context,
        provider_account_revision=2,
        provider_account_last_scope_change_at=None,
    )
    async with factory.begin() as session:
        stale_marker = await SqlAlchemyIdentityRepository(session).cas_provider_account_health(
            ProviderAccountHealthCAS(
                context=stale_marker_context,
                target_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
    assert (stale_marker.outcome, stale_marker.revision) == (
        CasOutcome.REVISION_CONFLICT,
        2,
    )

    current_context = ProviderContext(
        tenant_id=seed.tenant_id,
        provider="feishu",
        provider_tenant_key=seed.provider_tenant_key,
        provider_account_id=seed.provider_account_id,
        provider_account_key=seed.provider_account_key,
        provider_account_revision=2,
        provider_account_last_scope_change_at=scope_time,
    )
    async with factory.begin() as session:
        rewind = await SqlAlchemyIdentityRepository(session).cas_provider_account_health(
            ProviderAccountHealthCAS(
                context=current_context,
                target_health_state=IdentityProviderHealthState.HEALTHY.value,
                last_scope_change_at=scope_time - timedelta(seconds=1),
                last_directory_event_at=event_time - timedelta(seconds=1),
            )
        )
    assert (rewind.outcome, rewind.revision) == (CasOutcome.INVALID_TRANSITION, 2)


async def test_get_provider_account_for_update_holds_row_lock(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=False)
    locking_session = factory()
    await locking_session.begin()
    update_started = asyncio.Event()

    async def update_health() -> tuple[CasOutcome, int | None]:
        async with factory.begin() as session:
            update_started.set()
            result = await SqlAlchemyIdentityRepository(session).cas_provider_account_health(
                ProviderAccountHealthCAS(
                    context=seed.context,
                    target_health_state=IdentityProviderHealthState.HEALTHY.value,
                )
            )
            return result.outcome, result.revision

    task: asyncio.Task[tuple[CasOutcome, int | None]] | None = None
    try:
        locked = await SqlAlchemyIdentityRepository(locking_session).get_provider_account(
            seed.context,
            for_update=True,
        )
        assert locked is not None
        task = asyncio.create_task(update_health())
        await update_started.wait()
        await asyncio.sleep(0.1)
        assert not task.done()
        await locking_session.commit()
        assert await asyncio.wait_for(task, timeout=2) == (CasOutcome.APPLIED, 2)
    finally:
        if locking_session.in_transaction():
            await locking_session.rollback()
        await locking_session.close()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_failed_alias_conflict_rolls_back_prior_identity_insert(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)
    uncommitted_subject = f"rollback-{seed.subject_value}"

    with pytest.raises(IdentityRepositoryError) as raised:
        async with factory.begin() as session:
            repository = SqlAlchemyIdentityRepository(session)
            identity = await repository.insert_identity(
                _identity_insert(
                    seed,
                    user_id=seed.other_user_id,
                    subject_value=uncommitted_subject,
                )
            )
            assert identity.outcome is InsertOutcome.CREATED
            await repository.insert_alias(_alias_insert(seed, identity.record.id))
    assert raised.value.code is IdentityErrorCode.LINK_CONFLICT

    async with factory() as session:
        rolled_back_count = await session.scalar(
            select(func.count())
            .select_from(ExternalIdentity)
            .where(
                ExternalIdentity.tenant_id == seed.tenant_id,
                ExternalIdentity.subject_value == uncommitted_subject,
            )
        )
        alias_owner = await session.scalar(select(ExternalIdentityAlias.external_identity_id).where(ExternalIdentityAlias.id == seed.alias_id))
    assert rolled_back_count == 0
    assert alias_owner == seed.identity_id


async def test_repository_contract_repr_and_errors_do_not_expose_provider_identifiers(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed)

    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)
        account = await repository.get_provider_account(seed.context)
        snapshot = await repository.resolve_identity(seed.request)
    assert account is not None
    assert snapshot is not None

    error = IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)
    rendered = " ".join(
        (
            repr(seed.context),
            repr(seed.request),
            repr(account),
            repr(snapshot),
            repr(error),
            str(error),
        )
    )
    for secret in (
        seed.provider_tenant_key,
        seed.provider_account_id,
        seed.provider_account_key,
        seed.identity_id,
        seed.user_id,
        seed.subject_value,
        seed.alias_value,
    ):
        assert secret not in rendered
    assert IdentityErrorCode.LINK_CONFLICT.value in rendered


async def test_repository_redacts_driver_errors_with_sensitive_bind_parameters(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    secret = f"driver-secret-{uuid.uuid4().hex}"

    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)

        async def fail_with_bound_parameter(*_args: object, **_kwargs: object) -> object:
            raise DBAPIError.instance(
                statement="SELECT :provider_secret",
                params={"provider_secret": secret},
                orig=RuntimeError("driver failed"),
                dbapi_base_err=RuntimeError,
            )

        session.execute = fail_with_bound_parameter  # type: ignore[method-assign]
        context = ProviderContext(
            tenant_id=uuid.uuid4().hex,
            provider="feishu",
            provider_tenant_key=secret,
            provider_account_id=uuid.uuid4().hex,
            provider_account_key=secret,
            provider_account_revision=1,
        )
        with pytest.raises(IdentityRepositoryError) as raised:
            await repository.resolve_identity(
                IdentityResolutionRequest(
                    context=context,
                    alias=AliasKey(
                        alias_type=ProviderAliasType.OPEN_ID,
                        alias_value=secret,
                    ),
                )
            )
    assert raised.value.code is IdentityErrorCode.REPOSITORY_UNAVAILABLE
    assert secret not in repr(raised.value)
    assert secret not in str(raised.value)


async def test_invalid_mutation_inputs_are_rejected_before_database(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = _new_seed()
    await _seed_graph(factory, seed, with_identity=False, with_alias=False, with_membership=True)
    secret = "s" + " " * 255
    async with factory.begin() as session:
        repository = SqlAlchemyIdentityRepository(session)
        with pytest.raises(IdentityRepositoryError) as raised:
            await repository.insert_identity(
                ExternalIdentityInsert(
                    context=seed.context,
                    user_id=seed.user_id,
                    subject_type="user_id",
                    subject_value=secret,
                )
            )
        # The validation happens before any statement on the repository path.
        assert await session.scalar(text("SELECT 1")) == 1
    assert raised.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert secret not in repr(raised.value)

    async with factory() as session:
        repository = SqlAlchemyIdentityRepository(session)

        async def fail_database_call(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("invalid command reached the database")

        session.execute = fail_database_call  # type: ignore[method-assign]
        session.scalar = fail_database_call  # type: ignore[method-assign]

        with pytest.raises(IdentityRepositoryError) as tenant_onboarding_error:
            await repository.insert_verified_provider_tenant(
                VerifiedProviderTenantOnboarding(
                    tenant_id=seed.tenant_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    verified_at=seed.now.replace(tzinfo=None),
                )
            )
        with pytest.raises(IdentityRepositoryError) as account_onboarding_error:
            await repository.insert_verified_provider_account(
                VerifiedProviderAccountOnboarding(
                    tenant_id=seed.tenant_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    provider_account_key=secret,
                )
            )
        with pytest.raises(IdentityRepositoryError) as identity_error:
            await repository.insert_identity(
                ExternalIdentityInsert(
                    context=seed.context,
                    user_id=seed.user_id,
                    subject_type="user_id",
                    subject_value=secret,
                )
            )
        with pytest.raises(IdentityRepositoryError) as alias_error:
            await repository.insert_alias(
                ExternalIdentityAliasInsert(
                    context=seed.context,
                    external_identity_id="x" * 33,
                    alias_type=ProviderAliasType.OPEN_ID,
                    alias_value=seed.alias_value,
                    verified_at=seed.now,
                )
            )
        with pytest.raises(IdentityRepositoryError) as state_error:
            await repository.cas_identity_state(
                IdentityStateTransition(
                    context=seed.context,
                    external_identity_id=seed.identity_id,
                    expected_revision=1,
                    target_state="unknown",
                )
            )
        with pytest.raises(IdentityRepositoryError) as activation_error:
            await repository.activate_verified_identity(
                VerifiedIdentityActivation(
                    context=seed.context,
                    external_identity_id=seed.identity_id,
                    expected_revision=1,
                    verified_at=seed.now.replace(tzinfo=None),
                )
            )
        with pytest.raises(IdentityRepositoryError) as health_error:
            await repository.cas_provider_account_health(
                ProviderAccountHealthCAS(
                    context=replace(seed.context, provider_account_key=secret),
                    target_health_state=IdentityProviderHealthState.HEALTHY.value,
                )
            )

    assert alias_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert tenant_onboarding_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert account_onboarding_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert identity_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert state_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert activation_error.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert health_error.value.code is IdentityErrorCode.ASSERTION_INVALID
