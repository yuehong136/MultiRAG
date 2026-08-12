"""Real-database persistence checks for the channel control plane."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_control.schemas import ChannelCreateRequest
from api.channel_control.secret_store import EncryptedSecret
from api.channel_control.service import ChannelControlService
from api.db.db_models import (
    ChannelBinding,
    ChannelSecret,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    Tenant,
)


class _AcceptingTargetRepository(SqlAlchemyChannelRepository):
    """Keep this test focused on transaction ordering, not Canvas fixtures."""

    async def resolve_canvas_owner(self, canvas_id: str) -> tuple[str, str] | None:
        del canvas_id
        return "3" * 32, "me"

    async def canvas_revision_is_latest_published(
        self,
        canvas_id: str,
        revision_id: str,
    ) -> bool:
        del canvas_id, revision_id
        return True


class _OpaqueSecretStore:
    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext
        return EncryptedSecret(ciphertext="v1.opaque-test-value", key_id="test-key", version=version)

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id, encrypted
        return {"app_secret": "not-used"}


async def test_create_channel_persists_parent_before_fk_children(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    request = ChannelCreateRequest.model_validate(
        {
            "name": "persistence-order",
            "channel": "feishu",
            "config": {
                "credential": {"app_id": "cli_test", "app_secret": "test-secret"},
                "domain": "feishu",
            },
            "binding": {
                "target_type": "multirag.canvas_agent",
                "target_id": "1" * 32,
                "target_revision_id": "2" * 32,
                "enabled": False,
            },
        }
    )

    async with factory() as session:
        service = ChannelControlService(_AcceptingTargetRepository(session), _OpaqueSecretStore())
        created = await service.create_channel("3" * 32, request)
        channel_id = created["id"]

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChatChannel).where(ChatChannel.id == channel_id)) == 1
        assert await session.scalar(select(func.count()).select_from(ChannelSecret).where(ChannelSecret.channel_id == channel_id)) == 1
        assert await session.scalar(select(func.count()).select_from(ChannelBinding).where(ChannelBinding.channel_id == channel_id)) == 1

        channel = await session.get(ChatChannel, channel_id)
        assert channel is not None
        await session.delete(channel)
        await session.commit()

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(ChannelSecret).where(ChannelSecret.channel_id == channel_id)) == 0
        assert await session.scalar(select(func.count()).select_from(ChannelBinding).where(ChannelBinding.channel_id == channel_id)) == 0


async def test_repository_resolves_provider_account_only_through_tenant_safe_link(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    tenant_id = f"t-{suffix}"
    channel_id = f"c-{suffix}"
    account_id = f"a-{suffix}"
    link_id = f"l-{suffix}"
    provider_tenant_id = f"p-{suffix}"
    provider_tenant_key = f"tk-{suffix}"
    provider_account_key = f"cli-{suffix}"
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)

    async with factory.begin() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Channel identity link test",
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
                name="linked-channel",
                channel="feishu",
                config={"credential": {"app_id": provider_account_key}},
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
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=account_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                provider_account_key=provider_account_key,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderChannelLink(
                id=link_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_account_id=account_id,
                channel_id=channel_id,
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )

    async with factory() as session:
        repository = SqlAlchemyChannelRepository(session)
        account = await repository.get_linked_identity_provider_account(
            tenant_id,
            channel_id,
            for_update=True,
        )
        assert account is not None
        assert account.id == account_id
        assert account.provider_account_key == provider_account_key
        assert (
            await repository.get_linked_identity_provider_account(
                "wrong-tenant",
                channel_id,
            )
            is None
        )

    async with factory.begin() as session:
        await session.execute(sa.delete(IdentityProviderChannelLink).where(IdentityProviderChannelLink.id == link_id))
        await session.execute(sa.delete(IdentityProviderAccount).where(IdentityProviderAccount.id == account_id))
        await session.execute(sa.delete(IdentityProviderTenant).where(IdentityProviderTenant.id == provider_tenant_id))
        await session.execute(sa.delete(ChatChannel).where(ChatChannel.id == channel_id))
        await session.execute(sa.delete(Tenant).where(Tenant.id == tenant_id))
