"""Real PostgreSQL security tests for Channel-backed identity onboarding."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret
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
from api.identity.contracts import IdentityErrorCode, ProvisioningMode
from api.identity.onboarding import ChannelOnboardingService
from api.identity.onboarding_contracts import (
    ChannelOnboardingAction,
    ChannelOnboardingError,
    ChannelOnboardingRequest,
    ChannelOnboardingStatus,
    FeishuChannelOnboardingSource,
    VerifiedFeishuInstallation,
)
from api.identity.providers.contracts import ProviderErrorCode
from api.identity.provisioning import HmacLinkCodeCodec
from api.identity_adapters.channel_onboarding import (
    ChannelFeishuCredentialSource,
    SqlAlchemyVerifiedChannelOnboardingRepository,
)
from common.constants import StatusEnum

_CREATED_TENANT_IDS: set[str] = set()
_TRIGGER_FAILURE_SENTINEL = "onboarding-trigger-secret-must-not-leak"


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _codec() -> HmacLinkCodeCodec:
    return HmacLinkCodeCodec(
        keys={"integration-key": b"i" * 32},
        active_key_id="integration-key",
    )


class _SecretStore:
    def __init__(self) -> None:
        self.decrypt_calls: list[EncryptedSecret] = []

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext, version
        raise AssertionError("onboarding source must never write credentials")

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id
        self.decrypt_calls.append(encrypted)
        return {"app_secret": f"ephemeral-secret-v{encrypted.version}"}


class _MalformedSecretStore(_SecretStore):
    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        await super().decrypt(
            tenant_id=tenant_id,
            channel_id=channel_id,
            encrypted=encrypted,
        )
        return {
            "app_secret": "malformed-secret-sensitive",
            "unexpected": "unexpected-secret-sensitive",
        }


class _FailingSecretStore(_SecretStore):
    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id, encrypted
        raise RuntimeError("secret-store-exception-sensitive")


class _Verifier:
    def __init__(
        self,
        provider_tenant_key: str,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        self.provider_tenant_key = provider_tenant_key
        self.verified_at = verified_at
        self.calls: list[FeishuChannelOnboardingSource] = []

    async def verify(
        self,
        source: FeishuChannelOnboardingSource,
    ) -> VerifiedFeishuInstallation:
        self.calls.append(source)
        return VerifiedFeishuInstallation(
            provider="feishu",
            provider_tenant_key=self.provider_tenant_key,
            provider_account_key=source.snapshot.provider_account_key,
            verified_at=self.verified_at or datetime.now(UTC),
        )


def _request(channel_id: str) -> ChannelOnboardingRequest:
    return ChannelOnboardingRequest(
        channel_id=channel_id,
        mode=ProvisioningMode.LINK_ONLY,
        link_code_ttl_seconds=600,
    )


def _service(
    factory: async_sessionmaker[AsyncSession],
    *,
    provider_tenant_key: str,
    secret_store: _SecretStore | None = None,
    verifier: object | None = None,
    codec_factory: object = _codec,
) -> tuple[ChannelOnboardingService, _SecretStore]:
    store = secret_store or _SecretStore()
    selected_verifier = verifier or _Verifier(provider_tenant_key)
    assert callable(codec_factory)
    return (
        ChannelOnboardingService(
            ChannelFeishuCredentialSource(factory, store),
            selected_verifier,
            SqlAlchemyVerifiedChannelOnboardingRepository(factory),
            codec_factory,
        ),
        store,
    )


def _new_id() -> str:
    return uuid.uuid4().hex


async def _seed_tenant(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: str,
) -> None:
    _CREATED_TENANT_IDS.add(tenant_id)
    async with factory.begin() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Identity onboarding integration tenant",
                llm_id="test-llm",
                embd_id="test-embedding",
                asr_id="test-asr",
                img2txt_id="test-image",
                parser_ids="naive",
                status=StatusEnum.VALID.value,
            )
        )


async def _seed_channel(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    channel_id: str,
    app_id: str,
    status: int = 1,
) -> None:
    async with factory.begin() as session:
        session.add(
            ChatChannel(
                id=channel_id,
                tenant_id=tenant_id,
                name="Identity onboarding Feishu channel",
                channel="feishu",
                config={
                    "credential": {"app_id": app_id},
                    "domain": "feishu",
                },
                status=status,
                generation=1,
            )
        )
        await session.flush()
        session.add(
            ChannelSecret(
                id=_new_id(),
                channel_id=channel_id,
                ciphertext=f"ciphertext-{channel_id}",
                key_id="integration-channel-key",
                version=1,
            )
        )


async def _identity_snapshot(
    factory: async_sessionmaker[AsyncSession],
    tenant_ids: tuple[str, ...],
) -> tuple[tuple[tuple[object, ...], ...], ...]:
    async with factory() as session:
        provider_tenants = tuple(
            (
                row.id,
                row.tenant_id,
                row.provider,
                row.provider_tenant_key,
                row.verified_at,
            )
            for row in (await session.scalars(select(IdentityProviderTenant).where(IdentityProviderTenant.tenant_id.in_(tenant_ids)).order_by(IdentityProviderTenant.id))).all()
        )
        accounts = tuple(
            (
                row.id,
                row.tenant_id,
                row.provider,
                row.provider_tenant_key,
                row.provider_account_key,
                row.identity_revision,
                row.identity_health_state,
                row.identity_health_error_code,
            )
            for row in (await session.scalars(select(IdentityProviderAccount).where(IdentityProviderAccount.tenant_id.in_(tenant_ids)).order_by(IdentityProviderAccount.id))).all()
        )
        policies = tuple(
            (
                row.id,
                row.tenant_id,
                row.mode,
                row.revision,
                row.link_code_ttl_seconds,
                row.changed_at,
            )
            for row in (await session.scalars(select(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id.in_(tenant_ids)).order_by(IdentityTenantPolicy.id))).all()
        )
        links = tuple(
            (
                row.id,
                row.tenant_id,
                row.provider,
                row.provider_account_id,
                row.channel_id,
                row.linked_at,
            )
            for row in (await session.scalars(select(IdentityProviderChannelLink).where(IdentityProviderChannelLink.tenant_id.in_(tenant_ids)).order_by(IdentityProviderChannelLink.id))).all()
        )
    return provider_tenants, accounts, policies, links


@pytest.fixture(autouse=True)
async def _clean_identity_onboarding_rows(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[None]:
    _CREATED_TENANT_IDS.clear()
    try:
        yield
    finally:
        tenant_ids = tuple(_CREATED_TENANT_IDS)
        _CREATED_TENANT_IDS.clear()
        if tenant_ids:
            factory = _factory(bootstrapped_async_engine)
            async with factory.begin() as session:
                for model in (
                    IdentityProviderChannelLink,
                    IdentityProviderAccount,
                    IdentityProviderTenant,
                    IdentityTenantPolicy,
                ):
                    await session.execute(delete(model).where(model.tenant_id.in_(tenant_ids)))
                await session.execute(delete(ChatChannel).where(ChatChannel.tenant_id.in_(tenant_ids)))
                await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))


async def test_dry_run_is_verified_but_writes_no_identity_state(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    verifier = _Verifier(f"provider-tenant-{_new_id()}")
    service, store = _service(
        factory,
        provider_tenant_key=verifier.provider_tenant_key,
        verifier=verifier,
    )
    before = await _identity_snapshot(factory, (tenant_id,))

    plan = await service.plan(_request(channel_id))

    assert await _identity_snapshot(factory, (tenant_id,)) == before
    assert plan.actions == (
        ChannelOnboardingAction.CREATE_PROVIDER_TENANT,
        ChannelOnboardingAction.CREATE_PROVIDER_ACCOUNT,
        ChannelOnboardingAction.MARK_PROVIDER_ACCOUNT_HEALTHY,
        ChannelOnboardingAction.CREATE_TENANT_POLICY,
        ChannelOnboardingAction.CREATE_CHANNEL_LINK,
    )
    assert len(verifier.calls) == 1
    assert len(store.decrypt_calls) == 1


@pytest.mark.parametrize(
    "invalid_config",
    [
        {"credential": {"app_id": ""}, "domain": "feishu"},
        {
            "credential": {
                "app_id": "cli-malformed-sensitive",
                "app_secret": "plaintext-secret-sensitive",
            },
            "domain": "feishu",
        },
        {"credential": {"app_id": "cli-malformed-sensitive"}, "domain": "unknown"},
    ],
)
async def test_malformed_channel_config_fails_before_decrypt_or_identity_write(
    bootstrapped_async_engine: AsyncEngine,
    invalid_config: dict[str, object],
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    async with factory.begin() as session:
        await session.execute(update(ChatChannel).where(ChatChannel.id == channel_id).values(config=invalid_config))
    service, store = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    before = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request(channel_id))

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert store.decrypt_calls == []
    assert await _identity_snapshot(factory, (tenant_id,)) == before
    for sensitive in (
        "cli-malformed-sensitive",
        "plaintext-secret-sensitive",
    ):
        assert sensitive not in repr(caught.value)


@pytest.mark.parametrize(
    "store",
    [_MalformedSecretStore(), _FailingSecretStore()],
)
async def test_malformed_or_failed_secret_decrypt_is_sanitized_and_writes_nothing(
    bootstrapped_async_engine: AsyncEngine,
    store: _SecretStore,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
        secret_store=store,
    )
    before = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request(channel_id))

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert await _identity_snapshot(factory, (tenant_id,)) == before
    for sensitive in (
        "malformed-secret-sensitive",
        "unexpected-secret-sensitive",
        "secret-store-exception-sensitive",
    ):
        assert sensitive not in repr(caught.value)


async def test_apply_is_atomic_healthy_and_strictly_idempotent(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    app_id = f"cli-{_new_id()}"
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=app_id,
    )
    service, _ = _service(
        factory,
        provider_tenant_key=provider_tenant_key,
    )

    first = await service.apply(await service.plan(_request(channel_id)))
    first_snapshot = await _identity_snapshot(factory, (tenant_id,))
    second_plan = await service.plan(_request(channel_id))
    second = await service.apply(second_plan)

    assert first.status is ChannelOnboardingStatus.APPLIED
    assert first.provider_account_revision == 1
    assert second_plan.actions == ()
    assert second.status is ChannelOnboardingStatus.UNCHANGED
    assert second.actions == ()
    assert second.context == first.context
    assert await _identity_snapshot(factory, (tenant_id,)) == first_snapshot
    async with factory() as session:
        account = await session.scalar(select(IdentityProviderAccount).where(IdentityProviderAccount.tenant_id == tenant_id))
        assert account is not None
        assert account.provider_account_key == app_id
        assert account.identity_health_state == IdentityProviderHealthState.HEALTHY.value
        assert account.identity_revision == 1
        assert account.identity_health_error_code is None


async def test_disabled_channel_can_be_onboarded_before_worker_exposure(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
        status=0,
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )

    result = await service.apply(await service.plan(_request(channel_id)))

    assert result.status is ChannelOnboardingStatus.APPLIED
    assert tuple(len(rows) for rows in await _identity_snapshot(factory, (tenant_id,))) == (1, 1, 1, 1)
    async with factory() as session:
        status = await session.scalar(select(ChatChannel.status).where(ChatChannel.id == channel_id))
        assert status == 0


async def test_two_apps_share_one_provider_tenant_but_keep_distinct_accounts_and_links(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    channel_ids = (_new_id(), _new_id())
    app_ids = (f"cli-{_new_id()}", f"cli-{_new_id()}")
    await _seed_tenant(factory, tenant_id)
    for channel_id, app_id in zip(channel_ids, app_ids, strict=True):
        await _seed_channel(
            factory,
            tenant_id=tenant_id,
            channel_id=channel_id,
            app_id=app_id,
        )

    results = []
    for channel_id in channel_ids:
        service, _ = _service(
            factory,
            provider_tenant_key=provider_tenant_key,
        )
        results.append(await service.apply(await service.plan(_request(channel_id))))

    assert all(result.status is ChannelOnboardingStatus.APPLIED for result in results)
    async with factory() as session:
        provider_tenants = (await session.scalars(select(IdentityProviderTenant).where(IdentityProviderTenant.tenant_id == tenant_id))).all()
        accounts = (await session.scalars(select(IdentityProviderAccount).where(IdentityProviderAccount.tenant_id == tenant_id).order_by(IdentityProviderAccount.provider_account_key))).all()
        links = (await session.scalars(select(IdentityProviderChannelLink).where(IdentityProviderChannelLink.tenant_id == tenant_id))).all()
        assert len(provider_tenants) == 1
        assert provider_tenants[0].provider_tenant_key == provider_tenant_key
        assert [account.provider_account_key for account in accounts] == sorted(app_ids)
        assert len(links) == 2
        assert {link.provider_account_id for link in links} == {account.id for account in accounts}
        assert {link.channel_id for link in links} == set(channel_ids)


async def test_external_provider_tenant_cannot_be_claimed_by_another_multirag_tenant(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_ids = (_new_id(), _new_id())
    channel_ids = (_new_id(), _new_id())
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    for tenant_id, channel_id in zip(tenant_ids, channel_ids, strict=True):
        await _seed_tenant(factory, tenant_id)
        await _seed_channel(
            factory,
            tenant_id=tenant_id,
            channel_id=channel_id,
            app_id=f"cli-{_new_id()}",
        )
    owner, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    await owner.apply(await owner.plan(_request(channel_ids[0])))
    before_conflict = await _identity_snapshot(factory, tenant_ids)
    attacker, _ = _service(factory, provider_tenant_key=provider_tenant_key)

    with pytest.raises(ChannelOnboardingError) as caught:
        await attacker.plan(_request(channel_ids[1]))

    assert caught.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT
    assert await _identity_snapshot(factory, tenant_ids) == before_conflict


async def test_one_multirag_tenant_cannot_claim_a_second_provider_tenant(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_ids = (_new_id(), _new_id())
    await _seed_tenant(factory, tenant_id)
    for channel_id in channel_ids:
        await _seed_channel(
            factory,
            tenant_id=tenant_id,
            channel_id=channel_id,
            app_id=f"cli-{_new_id()}",
        )
    first, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-first-{_new_id()}",
    )
    await first.apply(await first.plan(_request(channel_ids[0])))
    before_conflict = await _identity_snapshot(factory, (tenant_id,))
    second, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-second-{_new_id()}",
    )

    with pytest.raises(ChannelOnboardingError) as caught:
        await second.plan(_request(channel_ids[1]))

    assert caught.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT
    assert await _identity_snapshot(factory, (tenant_id,)) == before_conflict


async def test_existing_account_link_cannot_be_rebound_to_a_second_channel(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    app_id = f"cli-{_new_id()}"
    channel_ids = (_new_id(), _new_id())
    await _seed_tenant(factory, tenant_id)
    for channel_id in channel_ids:
        await _seed_channel(
            factory,
            tenant_id=tenant_id,
            channel_id=channel_id,
            app_id=app_id,
        )
    first, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    await first.apply(await first.plan(_request(channel_ids[0])))
    before_rebind = await _identity_snapshot(factory, (tenant_id,))
    second, _ = _service(factory, provider_tenant_key=provider_tenant_key)

    with pytest.raises(ChannelOnboardingError) as caught:
        await second.plan(_request(channel_ids[1]))

    assert caught.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT
    assert await _identity_snapshot(factory, (tenant_id,)) == before_rebind


async def test_channel_link_cannot_be_rebound_to_a_second_account(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-first-{_new_id()}",
    )
    first, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    await first.apply(await first.plan(_request(channel_id)))
    async with factory.begin() as session:
        channel = await session.get(ChatChannel, channel_id)
        assert channel is not None
        channel.config = {
            "credential": {"app_id": f"cli-second-{_new_id()}"},
            "domain": "feishu",
        }
        channel.generation += 1
    before_rebind = await _identity_snapshot(factory, (tenant_id,))
    second, _ = _service(factory, provider_tenant_key=provider_tenant_key)

    with pytest.raises(ChannelOnboardingError) as caught:
        await second.plan(_request(channel_id))

    assert caught.value.code is IdentityErrorCode.OWNERSHIP_CONFLICT
    assert await _identity_snapshot(factory, (tenant_id,)) == before_rebind


async def test_concurrent_policy_conflict_prevents_all_onboarding_writes(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    plan = await service.plan(_request(channel_id))
    async with factory.begin() as session:
        session.add(
            IdentityTenantPolicy(
                id=tenant_id,
                tenant_id=tenant_id,
                mode=ProvisioningMode.JIT.value,
                revision=1,
                link_code_ttl_seconds=600,
                changed_at=datetime.now(UTC),
            )
        )
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code is IdentityErrorCode.TRANSITION_INVALID
    assert await _identity_snapshot(factory, (tenant_id,)) == before_apply


async def test_tenant_deactivation_after_plan_prevents_all_onboarding_writes(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    plan = await service.plan(_request(channel_id))
    async with factory.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == tenant_id).values(status=StatusEnum.INVALID.value))
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code in {
        IdentityErrorCode.REVISION_CONFLICT,
        IdentityErrorCode.TENANT_MISMATCH,
    }
    assert await _identity_snapshot(factory, (tenant_id,)) == before_apply


@pytest.mark.parametrize("drift", ["generation", "config"])
async def test_channel_authority_drift_after_plan_fails_closed(
    bootstrapped_async_engine: AsyncEngine,
    drift: str,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    app_id = f"cli-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=app_id,
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    plan = await service.plan(_request(channel_id))
    values: dict[str, object]
    if drift == "generation":
        values = {"generation": ChatChannel.generation + 1}
    else:
        values = {
            "config": {
                "credential": {"app_id": app_id},
                "domain": "feishu",
                "allowed_open_ids": ["changed-open-id-sensitive"],
            }
        }
    async with factory.begin() as session:
        await session.execute(update(ChatChannel).where(ChatChannel.id == channel_id).values(**values))
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code is IdentityErrorCode.REVISION_CONFLICT
    assert await _identity_snapshot(factory, (tenant_id,)) == before_apply


async def test_secret_rotation_between_verification_and_apply_fails_closed(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    plan = await service.plan(_request(channel_id))
    async with factory.begin() as session:
        await session.execute(
            update(ChannelSecret)
            .where(ChannelSecret.channel_id == channel_id)
            .values(
                ciphertext="rotated-ciphertext-sensitive",
                key_id="rotated-channel-key-sensitive",
                version=ChannelSecret.version + 1,
            )
        )
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code is IdentityErrorCode.REVISION_CONFLICT
    assert await _identity_snapshot(factory, (tenant_id,)) == before_apply
    assert "rotated-ciphertext-sensitive" not in repr(caught.value)
    assert "rotated-channel-key-sensitive" not in repr(caught.value)


@pytest.mark.parametrize(
    "verified_at",
    [
        datetime.now(UTC) - timedelta(minutes=6),
        datetime.now(UTC) + timedelta(minutes=1),
    ],
)
async def test_stale_or_future_provider_proof_cannot_authorize_writes(
    bootstrapped_async_engine: AsyncEngine,
    verified_at: datetime,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    verifier = _Verifier(
        f"provider-tenant-{_new_id()}",
        verified_at=verified_at,
    )
    service, _ = _service(
        factory,
        provider_tenant_key=verifier.provider_tenant_key,
        verifier=verifier,
    )
    plan = await service.plan(_request(channel_id))
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.apply(plan)

    assert caught.value.code is IdentityErrorCode.ASSERTION_INVALID
    assert await _identity_snapshot(factory, (tenant_id,)) == before_apply


async def test_disabled_existing_account_fails_closed_without_revision_change(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    first, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    result = await first.apply(await first.plan(_request(channel_id)))
    async with factory.begin() as session:
        await session.execute(
            update(IdentityProviderAccount)
            .where(IdentityProviderAccount.id == result.context.provider_account_id)
            .values(
                identity_health_state=IdentityProviderHealthState.DISABLED.value,
                identity_health_error_code=None,
                identity_revision=IdentityProviderAccount.identity_revision + 1,
            )
        )
    before_retry = await _identity_snapshot(factory, (tenant_id,))
    retry, _ = _service(factory, provider_tenant_key=provider_tenant_key)

    with pytest.raises(ChannelOnboardingError) as caught:
        await retry.plan(_request(channel_id))

    assert caught.value.code is IdentityErrorCode.INACTIVE
    assert await _identity_snapshot(factory, (tenant_id,)) == before_retry


@pytest.mark.parametrize(
    ("health_state", "error_code"),
    [
        (IdentityProviderHealthState.PENDING.value, None),
        (IdentityProviderHealthState.ERROR.value, "TEST_PROVIDER_ERROR"),
        (IdentityProviderHealthState.DEGRADED.value, None),
    ],
)
async def test_fresh_proof_recovers_existing_non_disabled_account_once(
    bootstrapped_async_engine: AsyncEngine,
    health_state: str,
    error_code: str | None,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    first, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    initial = await first.apply(await first.plan(_request(channel_id)))
    async with factory.begin() as session:
        await session.execute(
            update(IdentityProviderAccount)
            .where(IdentityProviderAccount.id == initial.context.provider_account_id)
            .values(
                identity_health_state=health_state,
                identity_health_error_code=error_code,
                identity_revision=7,
            )
        )
    recovery, _ = _service(factory, provider_tenant_key=provider_tenant_key)
    plan = await recovery.plan(_request(channel_id))

    result = await recovery.apply(plan)

    assert plan.actions == (ChannelOnboardingAction.MARK_PROVIDER_ACCOUNT_HEALTHY,)
    assert result.provider_account_revision == 8
    async with factory() as session:
        account = await session.get(
            IdentityProviderAccount,
            initial.context.provider_account_id,
        )
        assert account is not None
        assert account.identity_revision == 8
        assert account.identity_health_state == IdentityProviderHealthState.HEALTHY.value
        assert account.identity_health_error_code is None


async def test_late_database_failure_rolls_back_provider_account_policy_and_link(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, _ = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
    )
    plan = await service.plan(_request(channel_id))
    function_name = f"fail_onboarding_link_{uuid.uuid4().hex}"
    trigger_name = f"fail_onboarding_link_{uuid.uuid4().hex}"
    async with factory.begin() as session:
        await session.execute(
            text(
                f"""
                CREATE FUNCTION usr_ai.{function_name}() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN
                    RAISE EXCEPTION '{_TRIGGER_FAILURE_SENTINEL}';
                END;
                $$
                """
            )
        )
        await session.execute(
            text(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE INSERT ON usr_ai.t_ai_identity_provider_channel_links
                FOR EACH ROW EXECUTE FUNCTION usr_ai.{function_name}()
                """
            )
        )
    before_apply = await _identity_snapshot(factory, (tenant_id,))

    try:
        with pytest.raises(ChannelOnboardingError) as caught:
            await service.apply(plan)
        assert caught.value.code is IdentityErrorCode.REPOSITORY_UNAVAILABLE
        assert _TRIGGER_FAILURE_SENTINEL not in str(caught.value)
        assert _TRIGGER_FAILURE_SENTINEL not in repr(caught.value)
        assert await _identity_snapshot(factory, (tenant_id,)) == before_apply
    finally:
        async with factory.begin() as session:
            await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger_name} ON usr_ai.t_ai_identity_provider_channel_links"))
            await session.execute(text(f"DROP FUNCTION IF EXISTS usr_ai.{function_name}()"))


async def test_concurrent_identical_apply_converges_without_duplicate_state(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    provider_tenant_key = f"provider-tenant-{_new_id()}"
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    services = [_service(factory, provider_tenant_key=provider_tenant_key)[0] for _ in range(2)]
    plans = await asyncio.gather(*(service.plan(_request(channel_id)) for service in services))

    results = await asyncio.wait_for(
        asyncio.gather(*(service.apply(plan) for service, plan in zip(services, plans, strict=True))),
        timeout=5,
    )

    assert {result.status for result in results} == {
        ChannelOnboardingStatus.APPLIED,
        ChannelOnboardingStatus.UNCHANGED,
    }
    snapshot = await _identity_snapshot(factory, (tenant_id,))
    assert tuple(len(rows) for rows in snapshot) == (1, 1, 1, 1)


async def test_provider_verification_wait_holds_no_channel_or_secret_row_lock(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    verification_started = asyncio.Event()
    release_verification = asyncio.Event()

    class _BlockingVerifier:
        async def verify(
            self,
            source: FeishuChannelOnboardingSource,
        ) -> VerifiedFeishuInstallation:
            verification_started.set()
            await release_verification.wait()
            return VerifiedFeishuInstallation(
                provider="feishu",
                provider_tenant_key=f"provider-tenant-{_new_id()}",
                provider_account_key=source.snapshot.provider_account_key,
                verified_at=datetime.now(UTC),
            )

    service, _ = _service(
        factory,
        provider_tenant_key="unused-provider-tenant",
        verifier=_BlockingVerifier(),
    )
    plan_task = asyncio.create_task(service.plan(_request(channel_id)))
    await asyncio.wait_for(verification_started.wait(), timeout=2)

    try:
        async with factory.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            channel = await session.scalar(select(ChatChannel).where(ChatChannel.id == channel_id).with_for_update(nowait=True))
            secret = await session.scalar(select(ChannelSecret).where(ChannelSecret.channel_id == channel_id).with_for_update(nowait=True))
            assert channel is not None
            assert secret is not None
    finally:
        release_verification.set()

    await asyncio.wait_for(plan_task, timeout=2)
    assert await _identity_snapshot(factory, (tenant_id,)) == ((), (), (), ())


@pytest.mark.parametrize(
    "codec_factory",
    [
        lambda: object(),
        lambda: (_ for _ in ()).throw(ValueError("weak-key-sensitive")),
        lambda: (_ for _ in ()).throw(ValueError("unknown-active-key-sensitive")),
    ],
)
async def test_hmac_not_ready_rejects_before_source_and_all_identity_writes(
    bootstrapped_async_engine: AsyncEngine,
    codec_factory: object,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    tenant_id = _new_id()
    channel_id = _new_id()
    await _seed_tenant(factory, tenant_id)
    await _seed_channel(
        factory,
        tenant_id=tenant_id,
        channel_id=channel_id,
        app_id=f"cli-{_new_id()}",
    )
    service, store = _service(
        factory,
        provider_tenant_key=f"provider-tenant-{_new_id()}",
        codec_factory=codec_factory,
    )
    before = await _identity_snapshot(factory, (tenant_id,))

    with pytest.raises(ChannelOnboardingError) as caught:
        await service.plan(_request(channel_id))

    assert caught.value.code is IdentityErrorCode.POLICY_UNAVAILABLE
    assert store.decrypt_calls == []
    assert await _identity_snapshot(factory, (tenant_id,)) == before
    assert "weak-key-sensitive" not in repr(caught.value)
    assert "unknown-active-key-sensitive" not in repr(caught.value)
