"""Async SQLAlchemy transaction for Channel-delivered directory events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from api.db import ExternalIdentityState, IdentityEventReceiptState, IdentityProviderHealthState
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityEventReceipt,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    Tenant,
)
from api.identity.contracts import ProviderContext
from api.identity.directory_events import (
    DirectoryEventCommand,
    DirectoryEventError,
    DirectoryEventErrorCode,
    DirectoryEventOutcome,
    DirectoryEventProcessingResult,
    DirectoryEventType,
    DirectoryStatus,
    canonical_directory_event_hash,
)
from api.identity.providers.contracts import ProviderIdentifierKind
from common.constants import StatusEnum


@dataclass(frozen=True, slots=True)
class _LockedAuthority:
    channel: ChatChannel
    account: IdentityProviderAccount | None


class _CasConflict(RuntimeError):
    pass


class SqlAlchemyDirectoryEventRepository:
    """Own one authority/receipt/mutation transaction per provider event."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def process(
        self,
        command: DirectoryEventCommand,
    ) -> DirectoryEventProcessingResult:
        try:
            channel_id = await self._find_channel_id(command.binding_id)
            if channel_id is None:
                raise DirectoryEventError(DirectoryEventErrorCode.BINDING_NOT_FOUND)
            async with self._session_factory() as session:
                async with session.begin():
                    result = await self._process_locked(
                        session,
                        command,
                        channel_id=channel_id,
                    )
                return result
        except DirectoryEventError:
            raise
        except (SQLAlchemyError, _CasConflict) as exc:
            raise DirectoryEventError(DirectoryEventErrorCode.REPOSITORY_UNAVAILABLE) from exc

    async def _find_channel_id(self, binding_id: str) -> str | None:
        """Non-locking hint only; authority is revalidated under ordered locks."""

        async with self._session_factory() as session:
            return await session.scalar(select(ChannelBinding.channel_id).where(ChannelBinding.id == binding_id))

    async def _process_locked(
        self,
        session: AsyncSession,
        command: DirectoryEventCommand,
        *,
        channel_id: str,
    ) -> DirectoryEventProcessingResult:
        authority = await _lock_authority(
            session,
            command,
            channel_id=channel_id,
        )
        account = authority.account
        if account is None:
            return DirectoryEventProcessingResult(
                outcome=DirectoryEventOutcome.NO_LINK,
            )
        database_now = await session.scalar(select(func.clock_timestamp()))
        if database_now is None or command.event_at > database_now + timedelta(minutes=5):
            raise DirectoryEventError(DirectoryEventErrorCode.INVALID)
        event_hash = canonical_directory_event_hash(command)
        receipt, claimed = await _claim_receipt(
            session,
            command,
            account,
            event_hash=event_hash,
        )
        if not claimed:
            return _existing_receipt_result(receipt, event_hash, account)

        operation_now = database_now.astimezone(UTC)
        if command.event_type is DirectoryEventType.SCOPE_UPDATED and account.last_scope_change_at is not None and command.event_at < account.last_scope_change_at:
            _finish_receipt(
                receipt,
                state=IdentityEventReceiptState.SUCCEEDED,
                processed_at=operation_now,
            )
            return DirectoryEventProcessingResult(
                outcome=DirectoryEventOutcome.STALE,
                context=_provider_context(
                    account,
                    revision=account.identity_revision,
                    last_scope_change_at=account.last_scope_change_at,
                ),
            )

        identity, identity_conflict = await _lock_external_identity(
            session,
            command,
            account,
        )
        if identity_conflict:
            new_revision, last_scope_change_at = await _cas_bump_account(
                session,
                account,
                command,
            )
            _finish_receipt(
                receipt,
                state=IdentityEventReceiptState.FAILED,
                processed_at=operation_now,
                error_code=DirectoryEventErrorCode.IDENTITY_CONFLICT,
            )
            return DirectoryEventProcessingResult(
                outcome=DirectoryEventOutcome.FAILED,
                context=_provider_context(
                    account,
                    revision=new_revision,
                    last_scope_change_at=last_scope_change_at,
                ),
                revision_bumped=True,
                error_code=DirectoryEventErrorCode.IDENTITY_CONFLICT,
            )

        if identity is not None:
            receipt.external_identity_id = identity.id
            latest_event_at = await _latest_identity_event_at(
                session,
                identity=identity,
            )
            if latest_event_at is not None and command.event_at < latest_event_at:
                _finish_receipt(
                    receipt,
                    state=IdentityEventReceiptState.SUCCEEDED,
                    processed_at=operation_now,
                )
                return DirectoryEventProcessingResult(
                    outcome=DirectoryEventOutcome.STALE,
                    context=_provider_context(
                        account,
                        revision=account.identity_revision,
                        last_scope_change_at=account.last_scope_change_at,
                    ),
                )
            await _tighten_identity(session, identity, command)

        new_revision, last_scope_change_at = await _cas_bump_account(
            session,
            account,
            command,
        )
        _finish_receipt(
            receipt,
            state=IdentityEventReceiptState.SUCCEEDED,
            processed_at=operation_now,
        )
        return DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.APPLIED,
            context=_provider_context(
                account,
                revision=new_revision,
                last_scope_change_at=last_scope_change_at,
            ),
            revision_bumped=True,
        )


async def _lock_authority(
    session: AsyncSession,
    command: DirectoryEventCommand,
    *,
    channel_id: str,
) -> _LockedAuthority:
    """Lock Channel -> Tenant -> provider scope/account/link -> Binding."""

    channel_rows = (await session.scalars(select(ChatChannel).where(ChatChannel.id == channel_id).limit(2).with_for_update())).all()
    if len(channel_rows) != 1:
        raise DirectoryEventError(DirectoryEventErrorCode.BINDING_NOT_FOUND)
    channel = channel_rows[0]
    if channel.status != 1 or channel.channel != "feishu":
        raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)

    tenant_id = await session.scalar(
        select(Tenant.id)
        .where(
            Tenant.id == channel.tenant_id,
            Tenant.status == StatusEnum.VALID.value,
        )
        .with_for_update()
    )
    if tenant_id is None:
        raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)

    provider_tenants = list(
        (
            await session.scalars(
                select(IdentityProviderTenant)
                .where(
                    IdentityProviderTenant.tenant_id == channel.tenant_id,
                    IdentityProviderTenant.provider == channel.channel,
                )
                .order_by(IdentityProviderTenant.id)
                .with_for_update()
            )
        ).all()
    )

    account_links = (
        await session.execute(
            select(IdentityProviderAccount, IdentityProviderChannelLink)
            .join(
                IdentityProviderChannelLink,
                and_(
                    IdentityProviderChannelLink.provider_account_id == IdentityProviderAccount.id,
                    IdentityProviderChannelLink.tenant_id == IdentityProviderAccount.tenant_id,
                    IdentityProviderChannelLink.provider == IdentityProviderAccount.provider,
                ),
            )
            .where(IdentityProviderChannelLink.channel_id == channel.id)
            .limit(2)
            .with_for_update(of=(IdentityProviderAccount, IdentityProviderChannelLink))
        )
    ).all()
    if len(account_links) > 1:
        raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)
    if not account_links:
        dangling_links = (await session.scalars(select(IdentityProviderChannelLink).where(IdentityProviderChannelLink.channel_id == channel.id).limit(2).with_for_update())).all()
        if dangling_links:
            raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)
        await _lock_binding(session, command, channel)
        return _LockedAuthority(channel=channel, account=None)
    account, link = account_links[0]

    provider_tenant = next(
        (item for item in provider_tenants if item.provider_tenant_key == account.provider_tenant_key),
        None,
    )
    authority_invalid = (
        provider_tenant is None
        or provider_tenant.tenant_id != channel.tenant_id
        or provider_tenant.provider_tenant_key != command.observed_tenant_key
        or account.tenant_id != channel.tenant_id
        or account.provider != channel.channel
        or account.provider_tenant_key != command.observed_tenant_key
        or account.provider_account_key != command.observed_app_id
        or link.tenant_id != channel.tenant_id
        or link.provider != channel.channel
        or link.channel_id != channel.id
        or link.provider_account_id != account.id
        or account.identity_revision < 1
        or account.identity_health_state == IdentityProviderHealthState.DISABLED.value
    )
    await _lock_binding(session, command, channel)
    if authority_invalid:
        raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)
    if channel.channel != account.provider:
        raise DirectoryEventError(DirectoryEventErrorCode.AUTHORITY_INVALID)
    return _LockedAuthority(channel=channel, account=account)


async def _lock_binding(
    session: AsyncSession,
    command: DirectoryEventCommand,
    channel: ChatChannel,
) -> None:
    binding_rows = (
        await session.scalars(
            select(ChannelBinding)
            .where(
                ChannelBinding.id == command.binding_id,
                ChannelBinding.channel_id == channel.id,
            )
            .limit(2)
            .with_for_update()
        )
    ).all()
    if len(binding_rows) != 1:
        raise DirectoryEventError(DirectoryEventErrorCode.BINDING_NOT_FOUND)
    binding = binding_rows[0]
    if not binding.enabled or binding.generation != command.binding_generation:
        raise DirectoryEventError(DirectoryEventErrorCode.BINDING_DISABLED)


async def _claim_receipt(
    session: AsyncSession,
    command: DirectoryEventCommand,
    account: IdentityProviderAccount,
    *,
    event_hash: str,
) -> tuple[IdentityEventReceipt, bool]:
    receipt_id = uuid.uuid4().hex
    claimed_id = await session.scalar(
        pg_insert(IdentityEventReceipt)
        .values(
            id=receipt_id,
            tenant_id=account.tenant_id,
            provider=account.provider,
            provider_tenant_key=account.provider_tenant_key,
            provider_account_key=account.provider_account_key,
            event_type=command.event_type.value,
            event_id=command.event_id,
            event_hash=event_hash,
            processing_state=IdentityEventReceiptState.PROCESSING.value,
            event_at=command.event_at,
            processed_at=None,
            error_code=None,
            external_identity_id=None,
        )
        .on_conflict_do_nothing(
            constraint="uq_identity_event_receipts_provider_event",
        )
        .returning(IdentityEventReceipt.id)
    )
    receipt = await session.scalar(
        select(IdentityEventReceipt)
        .where(
            IdentityEventReceipt.tenant_id == account.tenant_id,
            IdentityEventReceipt.provider == account.provider,
            IdentityEventReceipt.provider_tenant_key == account.provider_tenant_key,
            IdentityEventReceipt.provider_account_key == account.provider_account_key,
            IdentityEventReceipt.event_type == command.event_type.value,
            IdentityEventReceipt.event_id == command.event_id,
        )
        .with_for_update()
    )
    if receipt is None:
        raise _CasConflict
    return receipt, claimed_id == receipt_id


def _existing_receipt_result(
    receipt: IdentityEventReceipt,
    event_hash: str,
    account: IdentityProviderAccount,
) -> DirectoryEventProcessingResult:
    context = _provider_context(
        account,
        revision=account.identity_revision,
        last_scope_change_at=account.last_scope_change_at,
    )
    if receipt.event_hash != event_hash:
        return DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.FAILED,
            context=context,
            error_code=DirectoryEventErrorCode.REPLAY_CONFLICT,
        )
    if receipt.processing_state == IdentityEventReceiptState.SUCCEEDED.value:
        return DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.DUPLICATE,
            context=context,
        )
    if receipt.processing_state == IdentityEventReceiptState.FAILED.value:
        try:
            persisted_error = DirectoryEventErrorCode(receipt.error_code or "")
        except ValueError:
            persisted_error = DirectoryEventErrorCode.RECEIPT_FAILED
        return DirectoryEventProcessingResult(
            outcome=DirectoryEventOutcome.FAILED,
            context=context,
            error_code=persisted_error,
        )
    return DirectoryEventProcessingResult(
        outcome=DirectoryEventOutcome.FAILED,
        context=context,
        error_code=DirectoryEventErrorCode.RECEIPT_PROCESSING,
    )


async def _lock_external_identity(
    session: AsyncSession,
    command: DirectoryEventCommand,
    account: IdentityProviderAccount,
) -> tuple[ExternalIdentity | None, bool]:
    alias_identifiers = [identifier for identifier in command.identifiers if identifier.kind in {ProviderIdentifierKind.OPEN_ID, ProviderIdentifierKind.UNION_ID}]
    aliases: list[ExternalIdentityAlias] = []
    if alias_identifiers:
        aliases = list(
            (
                await session.scalars(
                    select(ExternalIdentityAlias)
                    .where(
                        ExternalIdentityAlias.tenant_id == account.tenant_id,
                        ExternalIdentityAlias.provider == account.provider,
                        ExternalIdentityAlias.provider_tenant_key == account.provider_tenant_key,
                        ExternalIdentityAlias.provider_account_key == account.provider_account_key,
                        or_(
                            *(
                                and_(
                                    ExternalIdentityAlias.alias_type == identifier.kind.value,
                                    ExternalIdentityAlias.alias_value == identifier.value,
                                )
                                for identifier in alias_identifiers
                            )
                        ),
                    )
                    .order_by(
                        ExternalIdentityAlias.alias_type,
                        ExternalIdentityAlias.alias_value,
                    )
                    .with_for_update()
                )
            ).all()
        )

    user_id = next(
        (identifier.value for identifier in command.identifiers if identifier.kind is ProviderIdentifierKind.USER_ID),
        None,
    )
    alias_identity_ids = {alias.external_identity_id for alias in aliases}
    identity_filters: list[ColumnElement[bool]] = []
    if alias_identity_ids:
        identity_filters.append(ExternalIdentity.id.in_(alias_identity_ids))
    if user_id is not None:
        identity_filters.append(
            and_(
                ExternalIdentity.subject_type == ProviderIdentifierKind.USER_ID.value,
                ExternalIdentity.subject_value == user_id,
            )
        )
    if not identity_filters:
        return None, False
    identities = list(
        (
            await session.scalars(
                select(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == account.tenant_id,
                    ExternalIdentity.provider == account.provider,
                    ExternalIdentity.provider_tenant_key == account.provider_tenant_key,
                    or_(*identity_filters),
                )
                .order_by(ExternalIdentity.id)
                .with_for_update()
            )
        ).all()
    )
    candidate_ids = alias_identity_ids | {identity.id for identity in identities}
    if len(candidate_ids) > 1:
        return None, True
    if not candidate_ids:
        return None, False
    identity = next(
        (item for item in identities if item.id in candidate_ids),
        None,
    )
    if identity is None:
        return None, True
    if user_id is not None and identity.subject_value != user_id:
        return None, True
    return identity, False


async def _latest_identity_event_at(
    session: AsyncSession,
    *,
    identity: ExternalIdentity,
) -> datetime | None:
    receipt_event_at = await session.scalar(
        select(func.max(IdentityEventReceipt.event_at)).where(
            IdentityEventReceipt.tenant_id == identity.tenant_id,
            IdentityEventReceipt.external_identity_id == identity.id,
            IdentityEventReceipt.processing_state == IdentityEventReceiptState.SUCCEEDED.value,
            IdentityEventReceipt.event_at.is_not(None),
        )
    )
    markers = [
        marker
        for marker in (
            identity.verified_at,
            identity.last_seen_at,
            receipt_event_at,
        )
        if marker is not None
    ]
    return max(markers) if markers else None


async def _tighten_identity(
    session: AsyncSession,
    identity: ExternalIdentity,
    command: DirectoryEventCommand,
) -> None:
    target_state: ExternalIdentityState | None = None
    if command.event_type is DirectoryEventType.USER_DELETED:
        if identity.state not in {
            ExternalIdentityState.REVOKED.value,
            ExternalIdentityState.CONFLICT.value,
        }:
            target_state = ExternalIdentityState.REVOKED
    elif (
        command.event_type is DirectoryEventType.USER_UPDATED
        and command.directory_status is DirectoryStatus.INACTIVE
        and identity.state
        not in {
            ExternalIdentityState.INACTIVE.value,
            ExternalIdentityState.REVOKED.value,
            ExternalIdentityState.CONFLICT.value,
        }
    ):
        target_state = ExternalIdentityState.INACTIVE
    if target_state is None:
        return
    updated_identity_id = await session.scalar(
        update(ExternalIdentity)
        .where(
            ExternalIdentity.id == identity.id,
            ExternalIdentity.identity_revision == identity.identity_revision,
            ExternalIdentity.state == identity.state,
        )
        .values(
            state=target_state.value,
            identity_revision=ExternalIdentity.identity_revision + 1,
        )
        .returning(ExternalIdentity.id)
    )
    if updated_identity_id is None:
        raise _CasConflict


async def _cas_bump_account(
    session: AsyncSession,
    account: IdentityProviderAccount,
    command: DirectoryEventCommand,
) -> tuple[int, datetime | None]:
    last_directory_event_at = command.event_at
    if account.last_directory_event_at is not None:
        last_directory_event_at = max(last_directory_event_at, account.last_directory_event_at)
    last_scope_change_at = account.last_scope_change_at
    if command.event_type is DirectoryEventType.SCOPE_UPDATED:
        last_scope_change_at = command.event_at if last_scope_change_at is None else max(last_scope_change_at, command.event_at)
    expected_revision = account.identity_revision
    new_revision = await session.scalar(
        update(IdentityProviderAccount)
        .where(
            IdentityProviderAccount.id == account.id,
            IdentityProviderAccount.identity_revision == expected_revision,
        )
        .values(
            identity_revision=IdentityProviderAccount.identity_revision + 1,
            last_directory_event_at=last_directory_event_at,
            last_scope_change_at=last_scope_change_at,
        )
        .returning(IdentityProviderAccount.identity_revision)
    )
    if new_revision is None:
        raise _CasConflict
    return new_revision, last_scope_change_at


def _finish_receipt(
    receipt: IdentityEventReceipt,
    *,
    state: IdentityEventReceiptState,
    processed_at: datetime,
    error_code: DirectoryEventErrorCode | None = None,
) -> None:
    receipt.processing_state = state.value
    receipt.processed_at = processed_at
    receipt.error_code = error_code.value if error_code is not None else None


def _provider_context(
    account: IdentityProviderAccount,
    *,
    revision: int,
    last_scope_change_at: datetime | None,
) -> ProviderContext:
    return ProviderContext(
        tenant_id=account.tenant_id,
        provider=account.provider,
        provider_tenant_key=account.provider_tenant_key,
        provider_account_id=account.id,
        provider_account_key=account.provider_account_key,
        provider_account_revision=revision,
        provider_account_last_scope_change_at=last_scope_change_at,
    )
