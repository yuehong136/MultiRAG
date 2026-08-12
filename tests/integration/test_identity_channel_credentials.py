"""Real PostgreSQL checks for the I4 Channel-backed credential join."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret
from api.db import IdentityProviderHealthState
from api.db.db_models import (
    ChannelSecret,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    Tenant,
)
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import ProviderCredentialError, ProviderErrorCode
from api.identity_adapters.channel_credentials import ChannelProviderCredentialResolver


class _VersionedSecretStore:
    def __init__(self) -> None:
        self.calls: list[EncryptedSecret] = []

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext, version
        raise AssertionError("read adapter must not encrypt")

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id
        self.calls.append(encrypted)
        return {"app_secret": f"ephemeral-version-{encrypted.version}"}


async def test_real_database_join_is_exact_and_projects_secret_rotation(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"t-{suffix}"
    channel_id = f"c-{suffix}"
    account_id = f"a-{suffix}"
    provider_tenant_id = f"p-{suffix}"
    link_id = f"l-{suffix}"
    secret_id = f"s-{suffix}"
    provider_tenant_key = f"tk-{suffix}"
    app_id = f"cli-{suffix}"
    now = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    context = ProviderContext(
        tenant_id=tenant_id,
        provider="feishu",
        provider_tenant_key=provider_tenant_key,
        provider_account_id=account_id,
        provider_account_key=app_id,
        provider_account_revision=1,
    )
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)

    async with factory.begin() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="I4 credential join test",
                llm_id="test-llm",
                embd_id="test-embedding",
                asr_id="test-asr",
                img2txt_id="test-image",
                parser_ids="naive",
            )
        )
        session.add(
            ChatChannel(
                id=channel_id,
                tenant_id=tenant_id,
                name="I4 credential channel",
                channel="feishu",
                config={
                    "credential": {"app_id": app_id},
                    "domain": "feishu",
                },
                status=0,
                generation=1,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderTenant(
                id=provider_tenant_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                verified_at=now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=account_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                provider_account_key=app_id,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        await session.flush()
        session.add_all(
            [
                IdentityProviderChannelLink(
                    id=link_id,
                    tenant_id=tenant_id,
                    provider="feishu",
                    provider_account_id=account_id,
                    channel_id=channel_id,
                    linked_at=now,
                ),
                ChannelSecret(
                    id=secret_id,
                    channel_id=channel_id,
                    ciphertext="opaque-version-1",
                    key_id="test-key",
                    version=1,
                ),
            ]
        )

    try:
        store = _VersionedSecretStore()
        async with factory() as session:
            resolver = ChannelProviderCredentialResolver(session, store)
            first = await resolver.resolve(context)
            assert first.provider_account_id == account_id
            assert first.app_id == app_id
            assert first.credential_generation == 1
            assert first.app_secret == "ephemeral-version-1"

            for drifted in (
                replace(context, tenant_id="wrong-tenant"),
                replace(context, provider_account_id="wrong-account"),
                replace(context, provider_account_revision=2),
            ):
                with pytest.raises(ProviderCredentialError) as caught:
                    await resolver.resolve(drifted)
                assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE

        async with factory.begin() as session:
            await session.execute(update(ChannelSecret).where(ChannelSecret.id == secret_id).values(ciphertext="opaque-version-2", version=2))

        async with factory() as session:
            rotated = await ChannelProviderCredentialResolver(session, store).resolve(context)
            assert rotated.credential_generation == 2
            assert rotated.app_secret == "ephemeral-version-2"

        async with factory.begin() as session:
            await session.execute(delete(IdentityProviderChannelLink).where(IdentityProviderChannelLink.id == link_id))

        async with factory() as session:
            with pytest.raises(ProviderCredentialError) as caught:
                await ChannelProviderCredentialResolver(session, store).resolve(context)
            assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
        assert [call.version for call in store.calls] == [1, 2]
    finally:
        async with factory.begin() as session:
            await session.execute(delete(IdentityProviderChannelLink).where(IdentityProviderChannelLink.id == link_id))
            await session.execute(delete(ChannelSecret).where(ChannelSecret.id == secret_id))
            await session.execute(delete(IdentityProviderAccount).where(IdentityProviderAccount.id == account_id))
            await session.execute(delete(IdentityProviderTenant).where(IdentityProviderTenant.id == provider_tenant_id))
            await session.execute(delete(ChatChannel).where(ChatChannel.id == channel_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
