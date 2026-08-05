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
    UserTenant,
)
from api.db.services.user_service import UserTenantService
from common.constants import StatusEnum

ModelT = TypeVar("ModelT", ChatChannel, ChannelSecret, ChannelBinding, ChannelRuntimeStatus)


@runtime_checkable
class ChannelRepository(Protocol):
    async def list_channels(self, tenant_id: str) -> tuple[list[ChatChannel], int]: ...

    async def get_channel(self, tenant_id: str, channel_id: str, *, for_update: bool = False) -> ChatChannel | None: ...

    async def get_secret(self, channel_id: str, *, for_update: bool = False) -> ChannelSecret | None: ...

    async def get_binding(self, channel_id: str, *, for_update: bool = False) -> ChannelBinding | None: ...

    async def get_runtime(self, binding_id: str, *, for_update: bool = False) -> ChannelRuntimeStatus | None: ...

    async def list_enabled_channels(self, tenant_id: str, provider: str) -> list[ChatChannel]: ...

    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ChatChannel, ChannelBinding, ChannelSecret | None] | None: ...

    async def list_runtime_bindings(
        self,
    ) -> list[tuple[ChatChannel, ChannelBinding, ChannelSecret | None]]: ...

    async def resolve_dialog_owner(self, dialog_id: str) -> str | None: ...

    async def resolve_canvas_owner(self, canvas_id: str) -> tuple[str, str] | None: ...

    async def user_can_update_tenant_resources(self, user_id: str, tenant_id: str) -> bool: ...

    async def canvas_revision_is_latest_published(
        self,
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

    async def list_enabled_channels(self, tenant_id: str, provider: str) -> list[ChatChannel]:
        """Enabled channels of one provider inside one tenant.

        Rows rather than a JSON-path count, because the account identifier
        lives at a provider-specific place inside ``config``; keeping the
        extraction in Python avoids a dialect-specific JSONB query and puts
        that knowledge in one place for the provider spec to take over later.
        Bounded by ``ix_chat_channels_tenant_channel`` and by how many channels
        a tenant realistically has.
        """

        statement = select(ChatChannel).where(
            ChatChannel.tenant_id == tenant_id,
            ChatChannel.channel == provider,
            ChatChannel.status == 1,
        )
        return list((await self._db.scalars(statement)).all())

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

    async def resolve_dialog_owner(self, dialog_id: str) -> str | None:
        """Owning tenant of a dialog, independent of who is asking.

        Ownership and authorization are answered separately now: matching only
        the caller's own tenant made every team-shared target invisible to the
        backend while the frontend dropdown happily listed it, so picking one
        produced a rejection the UI then swallowed. Dialogs carry no per-object
        share flag -- unlike ``UserCanvas.permission`` -- so tenant membership
        plus role is the whole test for them.
        """

        statement = select(Dialog.tenant_id).where(
            Dialog.id == dialog_id,
            Dialog.status == "1",
        )
        return await self._db.scalar(statement)

    async def resolve_canvas_owner(self, canvas_id: str) -> tuple[str, str] | None:
        """``(owning tenant, permission)`` for an agent canvas, or None."""

        statement = select(UserCanvas.user_id, UserCanvas.permission).where(
            UserCanvas.id == canvas_id,
            UserCanvas.canvas_category == "agent_canvas",
        )
        row = (await self._db.execute(statement)).first()
        return None if row is None else (row[0], row[1])

    async def user_can_update_tenant_resources(self, user_id: str, tenant_id: str) -> bool:
        """Async mirror of ``UserTenantService.get_role_in_tenant`` + the predicate.

        The predicate itself is imported rather than restated, so the role set
        stays defined in exactly one place; only the lookup is reimplemented,
        because the service-layer version takes a sync ``Session`` and this
        package is pure-async by contract (``scripts/check_async_sync_db.py``).
        """

        if user_id == tenant_id:
            return True
        statement = select(UserTenant.role).where(
            UserTenant.user_id == user_id,
            UserTenant.tenant_id == tenant_id,
            UserTenant.status == StatusEnum.VALID.value,
        )
        role = await self._db.scalar(statement)
        return UserTenantService.can_update_tenant_resources(role)

    async def canvas_revision_is_latest_published(
        self,
        canvas_id: str,
        revision_id: str,
    ) -> bool:
        """Whether ``revision_id`` is still the newest released version.

        No tenant filter: ownership is resolved by ``resolve_canvas_owner`` so
        that "not yours" and "stale revision" stay two distinct answers to two
        distinct admin actions.
        """

        statement = (
            select(UserCanvasVersion.id)
            .join(UserCanvas, UserCanvas.id == UserCanvasVersion.user_canvas_id)
            .where(
                UserCanvas.id == canvas_id,
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
