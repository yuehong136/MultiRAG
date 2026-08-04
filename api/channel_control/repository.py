"""Async SQLAlchemy repository for channel control-plane state."""

from __future__ import annotations

from typing import Protocol, TypeVar, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import (
    ChannelBinding,
    ChannelRuntimeStatus,
    ChannelSecret,
    ChatChannel,
    Dialog,
    UserCanvas,
    UserCanvasVersion,
)

ModelT = TypeVar("ModelT", ChatChannel, ChannelSecret, ChannelBinding, ChannelRuntimeStatus)


@runtime_checkable
class ChannelRepository(Protocol):
    async def list_channels(self, tenant_id: str) -> tuple[list[ChatChannel], int]: ...

    async def get_channel(self, tenant_id: str, channel_id: str, *, for_update: bool = False) -> ChatChannel | None: ...

    async def get_secret(self, channel_id: str, *, for_update: bool = False) -> ChannelSecret | None: ...

    async def get_binding(self, channel_id: str, *, for_update: bool = False) -> ChannelBinding | None: ...

    async def get_runtime(self, binding_id: str, *, for_update: bool = False) -> ChannelRuntimeStatus | None: ...

    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ChatChannel, ChannelBinding, ChannelSecret | None] | None: ...

    async def list_runtime_bindings(
        self,
    ) -> list[tuple[ChatChannel, ChannelBinding, ChannelSecret | None]]: ...

    async def dialog_belongs_to_tenant(self, tenant_id: str, dialog_id: str) -> bool: ...

    async def canvas_revision_is_latest_published(
        self,
        tenant_id: str,
        canvas_id: str,
        revision_id: str,
    ) -> bool: ...

    def add(self, model: ModelT) -> None: ...

    async def delete(self, model: ModelT) -> None: ...

    async def flush(self) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class SqlAlchemyChannelRepository:
    """Pure-async repository; it never opens or bridges a sync session."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def list_channels(self, tenant_id: str) -> tuple[list[ChatChannel], int]:
        filters = (ChatChannel.tenant_id == tenant_id,)
        statement = select(ChatChannel).where(*filters).order_by(ChatChannel.create_time.desc())
        channels = list((await self._db.scalars(statement)).all())
        total = int((await self._db.scalar(select(func.count()).select_from(ChatChannel).where(*filters))) or 0)
        return channels, total

    async def get_channel(self, tenant_id: str, channel_id: str, *, for_update: bool = False) -> ChatChannel | None:
        statement = select(ChatChannel).where(
            ChatChannel.id == channel_id,
            ChatChannel.tenant_id == tenant_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return (await self._db.scalars(statement)).first()

    async def get_secret(self, channel_id: str, *, for_update: bool = False) -> ChannelSecret | None:
        statement = select(ChannelSecret).where(ChannelSecret.channel_id == channel_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._db.scalars(statement)).first()

    async def get_binding(self, channel_id: str, *, for_update: bool = False) -> ChannelBinding | None:
        statement = select(ChannelBinding).where(ChannelBinding.channel_id == channel_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._db.scalars(statement)).first()

    async def get_runtime(self, binding_id: str, *, for_update: bool = False) -> ChannelRuntimeStatus | None:
        statement = select(ChannelRuntimeStatus).where(ChannelRuntimeStatus.binding_id == binding_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._db.scalars(statement)).first()

    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ChatChannel, ChannelBinding, ChannelSecret | None] | None:
        statement = (
            select(ChatChannel, ChannelBinding, ChannelSecret)
            .join(ChannelBinding, ChannelBinding.channel_id == ChatChannel.id)
            .outerjoin(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
            .where(ChannelBinding.id == binding_id)
        )
        if for_update:
            statement = statement.with_for_update(of=(ChatChannel, ChannelBinding))
        row = (await self._db.execute(statement)).first()
        if row is None:
            return None
        return row[0], row[1], row[2]

    async def list_runtime_bindings(
        self,
    ) -> list[tuple[ChatChannel, ChannelBinding, ChannelSecret | None]]:
        statement = (
            select(ChatChannel, ChannelBinding, ChannelSecret)
            .join(ChannelBinding, ChannelBinding.channel_id == ChatChannel.id)
            .outerjoin(ChannelSecret, ChannelSecret.channel_id == ChatChannel.id)
            .where(
                ChatChannel.status == 1,
                ChannelBinding.enabled.is_(True),
            )
            .order_by(ChannelBinding.id)
        )
        rows = (await self._db.execute(statement)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def dialog_belongs_to_tenant(self, tenant_id: str, dialog_id: str) -> bool:
        statement = select(Dialog.id).where(
            Dialog.id == dialog_id,
            Dialog.tenant_id == tenant_id,
            Dialog.status == "1",
        )
        return (await self._db.scalar(statement)) is not None

    async def canvas_revision_is_latest_published(
        self,
        tenant_id: str,
        canvas_id: str,
        revision_id: str,
    ) -> bool:
        statement = (
            select(UserCanvasVersion.id)
            .join(UserCanvas, UserCanvas.id == UserCanvasVersion.user_canvas_id)
            .where(
                UserCanvas.id == canvas_id,
                UserCanvas.user_id == tenant_id,
                UserCanvas.canvas_category == "agent_canvas",
                UserCanvasVersion.user_canvas_id == canvas_id,
                UserCanvasVersion.release.is_(True),
            )
            .order_by(UserCanvasVersion.create_time.desc())
            .limit(1)
        )
        return (await self._db.scalar(statement)) == revision_id

    def add(self, model: ModelT) -> None:
        self._db.add(model)

    async def delete(self, model: ModelT) -> None:
        await self._db.delete(model)

    async def flush(self) -> None:
        await self._db.flush()

    async def commit(self) -> None:
        await self._db.commit()

    async def rollback(self) -> None:
        await self._db.rollback()
