"""Real-database persistence checks for the channel control plane."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_control.schemas import ChannelCreateRequest
from api.channel_control.secret_store import EncryptedSecret
from api.channel_control.service import ChannelControlService
from api.db.db_models import ChannelBinding, ChannelSecret, ChatChannel


class _AcceptingTargetRepository(SqlAlchemyChannelRepository):
    """Keep this test focused on transaction ordering, not Canvas fixtures."""

    async def canvas_revision_is_latest_published(
        self,
        tenant_id: str,
        canvas_id: str,
        revision_id: str,
    ) -> bool:
        del tenant_id, canvas_id, revision_id
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
