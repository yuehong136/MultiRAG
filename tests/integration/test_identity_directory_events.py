"""Real PostgreSQL tests for EIM-I7 directory-event transactions."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import (
    ExternalIdentityState,
    IdentityEventReceiptState,
    IdentityProviderHealthState,
    UserAccountKind,
)
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
    User,
)
from api.identity import directory_event_repository as repository_module
from api.identity.directory_event_repository import SqlAlchemyDirectoryEventRepository
from api.identity.directory_events import (
    DirectoryEventCommand,
    DirectoryEventError,
    DirectoryEventErrorCode,
    DirectoryEventIdentifier,
    DirectoryEventOutcome,
    DirectoryEventType,
    DirectoryStatus,
)
from api.identity.providers.contracts import ProviderIdentifierKind
from common.constants import StatusEnum

_TEST_TENANT_NAME = "Directory event integration tenant"


@pytest.fixture(autouse=True)
def _isolate_directory_event_graphs(
    bootstrapped_engine: Engine,
) -> Iterator[None]:
    """Remove committed I7 seed graphs before the shared scratch DB moves on."""

    yield

    with bootstrapped_engine.begin() as connection:
        tenant_ids = tuple(connection.scalars(select(Tenant.id).where(Tenant.name == _TEST_TENANT_NAME)))
        if not tenant_ids:
            return

        channel_ids = tuple(connection.scalars(select(ChatChannel.id).where(ChatChannel.tenant_id.in_(tenant_ids))))
        user_ids = tuple(connection.scalars(select(ExternalIdentity.user_id).where(ExternalIdentity.tenant_id.in_(tenant_ids))))

        for model in (
            IdentityEventReceipt,
            ExternalIdentityAlias,
            ExternalIdentity,
            IdentityProviderChannelLink,
            IdentityProviderAccount,
            IdentityProviderTenant,
        ):
            connection.execute(sa.delete(model).where(model.tenant_id.in_(tenant_ids)))
        if channel_ids:
            connection.execute(sa.delete(ChannelBinding).where(ChannelBinding.channel_id.in_(channel_ids)))
            connection.execute(sa.delete(ChatChannel).where(ChatChannel.id.in_(channel_ids)))
        if user_ids:
            connection.execute(sa.delete(User).where(User.id.in_(user_ids)))
        connection.execute(sa.delete(Tenant).where(Tenant.id.in_(tenant_ids)))


@dataclass(frozen=True, slots=True)
class _Seed:
    tenant_id: str
    channel_id: str
    binding_id: str
    provider_tenant_key: str
    account_id: str
    app_id: str
    first_identity_id: str
    first_user_id: str
    first_open_id: str
    second_identity_id: str
    second_user_id: str
    second_open_id: str


def _id() -> str:
    return uuid.uuid4().hex


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _user(user_id: str) -> User:
    return User(
        id=user_id,
        nickname="Directory event integration user",
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
    *,
    with_link: bool = True,
    proof_at: datetime | None = None,
) -> _Seed:
    now = datetime.now(UTC)
    seed = _Seed(
        tenant_id=_id(),
        channel_id=_id(),
        binding_id=_id(),
        provider_tenant_key=f"tenant-{_id()}",
        account_id=_id(),
        app_id=f"cli_{_id()}",
        first_identity_id=_id(),
        first_user_id=_id(),
        first_open_id=f"ou_{_id()}",
        second_identity_id=_id(),
        second_user_id=_id(),
        second_open_id=f"ou_{_id()}",
    )
    verified_at = proof_at or now - timedelta(hours=1)
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=seed.tenant_id,
                    name=_TEST_TENANT_NAME,
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                    status=StatusEnum.VALID.value,
                ),
                _user(seed.first_user_id),
                _user(seed.second_user_id),
                ChatChannel(
                    id=seed.channel_id,
                    tenant_id=seed.tenant_id,
                    name="Directory event Feishu",
                    channel="feishu",
                    config={"credential": {"app_id": seed.app_id}},
                    status=1,
                    generation=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            IdentityProviderTenant(
                id=_id(),
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                verified_at=now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=seed.account_id,
                tenant_id=seed.tenant_id,
                provider="feishu",
                provider_tenant_key=seed.provider_tenant_key,
                provider_account_key=seed.app_id,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        session.add(
            ChannelBinding(
                id=seed.binding_id,
                channel_id=seed.channel_id,
                target_type="multirag.dialog",
                target_id=_id(),
                target_revision_id=None,
                policy={},
                enabled=True,
                generation=7,
            )
        )
        await session.flush()
        if with_link:
            session.add(
                IdentityProviderChannelLink(
                    id=_id(),
                    tenant_id=seed.tenant_id,
                    provider="feishu",
                    provider_account_id=seed.account_id,
                    channel_id=seed.channel_id,
                    linked_at=now,
                )
            )
        session.add_all(
            [
                ExternalIdentity(
                    id=seed.first_identity_id,
                    tenant_id=seed.tenant_id,
                    user_id=seed.first_user_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    subject_type="user_id",
                    subject_value=seed.first_user_id,
                    state=ExternalIdentityState.ACTIVE.value,
                    verified_at=verified_at,
                    last_seen_at=verified_at,
                    identity_revision=1,
                    attributes={},
                ),
                ExternalIdentity(
                    id=seed.second_identity_id,
                    tenant_id=seed.tenant_id,
                    user_id=seed.second_user_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    subject_type="user_id",
                    subject_value=seed.second_user_id,
                    state=ExternalIdentityState.ACTIVE.value,
                    verified_at=verified_at,
                    last_seen_at=verified_at,
                    identity_revision=1,
                    attributes={},
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ExternalIdentityAlias(
                    id=_id(),
                    tenant_id=seed.tenant_id,
                    external_identity_id=seed.first_identity_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    provider_account_key=seed.app_id,
                    alias_type="open_id",
                    alias_value=seed.first_open_id,
                    verified_at=verified_at,
                ),
                ExternalIdentityAlias(
                    id=_id(),
                    tenant_id=seed.tenant_id,
                    external_identity_id=seed.second_identity_id,
                    provider="feishu",
                    provider_tenant_key=seed.provider_tenant_key,
                    provider_account_key=seed.app_id,
                    alias_type="open_id",
                    alias_value=seed.second_open_id,
                    verified_at=verified_at,
                ),
            ]
        )
    return seed


def _user_command(
    seed: _Seed,
    *,
    event_id: str,
    event_at: datetime,
    event_type: DirectoryEventType = DirectoryEventType.USER_UPDATED,
    directory_status: DirectoryStatus = DirectoryStatus.INACTIVE,
    second: bool = False,
) -> DirectoryEventCommand:
    user_id = seed.second_user_id if second else seed.first_user_id
    open_id = seed.second_open_id if second else seed.first_open_id
    return DirectoryEventCommand(
        binding_id=seed.binding_id,
        binding_generation=7,
        event_type=event_type,
        event_id=event_id,
        event_at=event_at,
        observed_app_id=seed.app_id,
        observed_tenant_key=seed.provider_tenant_key,
        identifiers=(
            DirectoryEventIdentifier(ProviderIdentifierKind.OPEN_ID, open_id),
            DirectoryEventIdentifier(ProviderIdentifierKind.USER_ID, user_id),
        ),
        directory_status=directory_status,
    )


def _scope_command(
    seed: _Seed,
    *,
    event_id: str,
    event_at: datetime,
) -> DirectoryEventCommand:
    return DirectoryEventCommand(
        binding_id=seed.binding_id,
        binding_generation=7,
        event_type=DirectoryEventType.SCOPE_UPDATED,
        event_id=event_id,
        event_at=event_at,
        observed_app_id=seed.app_id,
        observed_tenant_key=seed.provider_tenant_key,
    )


async def test_concurrent_duplicate_claim_mutates_once(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    command = _user_command(
        seed,
        event_id="duplicate-event",
        event_at=datetime.now(UTC) - timedelta(minutes=1),
    )

    results = await asyncio.gather(
        repository.process(command),
        repository.process(command),
    )

    assert {result.outcome for result in results} == {
        DirectoryEventOutcome.APPLIED,
        DirectoryEventOutcome.DUPLICATE,
    }
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
        receipt_count = await session.scalar(select(func.count()).select_from(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "duplicate-event"))
    assert account is not None and account.identity_revision == 2
    assert identity is not None
    assert identity.state == ExternalIdentityState.INACTIVE.value
    assert identity.identity_revision == 2
    assert receipt_count == 1


async def test_per_identity_order_does_not_use_global_account_timestamp(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    now = datetime.now(UTC)

    first = await repository.process(_user_command(seed, event_id="newer-user-one", event_at=now - timedelta(minutes=1)))
    second = await repository.process(
        _user_command(
            seed,
            event_id="older-user-two",
            event_at=now - timedelta(minutes=2),
            second=True,
        )
    )

    assert first.outcome is DirectoryEventOutcome.APPLIED
    assert second.outcome is DirectoryEventOutcome.APPLIED
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        second_identity = await session.get(ExternalIdentity, seed.second_identity_id)
    assert account is not None and account.identity_revision == 3
    assert second_identity is not None
    assert second_identity.state == ExternalIdentityState.INACTIVE.value


async def test_newer_online_proof_makes_old_delete_stale_without_revision_bump(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    proof_at = datetime.now(UTC) - timedelta(minutes=1)
    seed = await _seed_graph(factory, proof_at=proof_at)
    repository = SqlAlchemyDirectoryEventRepository(factory)

    result = await repository.process(
        _user_command(
            seed,
            event_id="late-delete",
            event_at=proof_at - timedelta(seconds=1),
            event_type=DirectoryEventType.USER_DELETED,
        )
    )

    assert result.outcome is DirectoryEventOutcome.STALE
    assert result.revision_bumped is False
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
    assert account is not None and account.identity_revision == 1
    assert identity is not None and identity.state == ExternalIdentityState.ACTIVE.value


@pytest.mark.parametrize(
    "event_type",
    [DirectoryEventType.USER_CREATED, DirectoryEventType.USER_UPDATED],
)
async def test_unknown_user_event_bumps_negative_cache_fence_without_jit(
    bootstrapped_async_engine: AsyncEngine,
    event_type: DirectoryEventType,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    unknown_user_id = _id()
    command = DirectoryEventCommand(
        binding_id=seed.binding_id,
        binding_generation=7,
        event_type=event_type,
        event_id=f"unknown-{event_type.name.lower()}",
        event_at=datetime.now(UTC) - timedelta(seconds=1),
        observed_app_id=seed.app_id,
        observed_tenant_key=seed.provider_tenant_key,
        identifiers=(
            DirectoryEventIdentifier(
                ProviderIdentifierKind.USER_ID,
                unknown_user_id,
            ),
        ),
        directory_status=DirectoryStatus.ACTIVE,
    )

    result = await repository.process(command)

    assert result.outcome is DirectoryEventOutcome.APPLIED
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        unknown_identity_count = await session.scalar(select(func.count()).select_from(ExternalIdentity).where(ExternalIdentity.subject_value == unknown_user_id))
        unknown_user_count = await session.scalar(select(func.count()).select_from(User).where(User.id == unknown_user_id))
    assert account is not None and account.identity_revision == 2
    assert unknown_identity_count == 0
    assert unknown_user_count == 0


async def test_succeeded_identity_receipt_is_a_per_identity_stale_floor(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    now = datetime.now(UTC)

    applied = await repository.process(
        _user_command(
            seed,
            event_id="identity-marker",
            event_at=now - timedelta(seconds=1),
            directory_status=DirectoryStatus.ACTIVE,
        )
    )
    stale = await repository.process(
        _user_command(
            seed,
            event_id="older-delete",
            event_at=now - timedelta(seconds=2),
            event_type=DirectoryEventType.USER_DELETED,
        )
    )

    assert applied.outcome is DirectoryEventOutcome.APPLIED
    assert stale.outcome is DirectoryEventOutcome.STALE
    assert stale.revision_bumped is False
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
    assert account is not None and account.identity_revision == 2
    assert identity is not None and identity.state == ExternalIdentityState.ACTIVE.value


async def test_stale_scope_and_no_link_are_successful_zero_write_outcomes(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    now = datetime.now(UTC)
    fresh = await repository.process(_scope_command(seed, event_id="scope-fresh", event_at=now - timedelta(minutes=1)))
    stale = await repository.process(_scope_command(seed, event_id="scope-stale", event_at=now - timedelta(minutes=2)))
    no_link_seed = await _seed_graph(factory, with_link=False)
    no_link = await repository.process(
        _user_command(
            no_link_seed,
            event_id="no-link-event",
            event_at=now - timedelta(seconds=1),
        )
    )

    assert fresh.outcome is DirectoryEventOutcome.APPLIED
    assert stale.outcome is DirectoryEventOutcome.STALE
    assert no_link.outcome is DirectoryEventOutcome.NO_LINK
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        no_link_account = await session.get(
            IdentityProviderAccount,
            no_link_seed.account_id,
        )
        no_link_receipts = await session.scalar(select(func.count()).select_from(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "no-link-event"))
    assert account is not None and account.identity_revision == 2
    assert no_link_account is not None and no_link_account.identity_revision == 1
    assert no_link_receipts == 0

    async with factory() as session:
        stale_receipt = await session.scalar(select(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "scope-stale"))
    assert stale_receipt is not None
    assert stale_receipt.processing_state == IdentityEventReceiptState.SUCCEEDED.value


@pytest.mark.parametrize(
    "invalid_change",
    [
        {"binding_generation": 8},
        {"observed_app_id": "wrong-app"},
        {"observed_tenant_key": "wrong-tenant"},
    ],
)
async def test_locked_authority_mismatch_rolls_back_without_receipt(
    bootstrapped_async_engine: AsyncEngine,
    invalid_change: dict[str, object],
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    command = replace(
        _user_command(
            seed,
            event_id="authority-mismatch",
            event_at=datetime.now(UTC) - timedelta(seconds=1),
        ),
        **invalid_change,
    )

    with pytest.raises(DirectoryEventError) as captured:
        await repository.process(command)

    assert captured.value.code in {
        DirectoryEventErrorCode.BINDING_DISABLED,
        DirectoryEventErrorCode.AUTHORITY_INVALID,
    }
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        receipt_count = await session.scalar(select(func.count()).select_from(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "authority-mismatch"))
    assert account is not None and account.identity_revision == 1
    assert receipt_count == 0


async def test_conflicting_identifiers_commit_failed_receipt_and_one_revision_bump(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    command = DirectoryEventCommand(
        binding_id=seed.binding_id,
        binding_generation=7,
        event_type=DirectoryEventType.USER_UPDATED,
        event_id="identity-conflict",
        event_at=datetime.now(UTC) - timedelta(seconds=1),
        observed_app_id=seed.app_id,
        observed_tenant_key=seed.provider_tenant_key,
        identifiers=(
            DirectoryEventIdentifier(
                ProviderIdentifierKind.OPEN_ID,
                seed.first_open_id,
            ),
            DirectoryEventIdentifier(
                ProviderIdentifierKind.USER_ID,
                seed.second_user_id,
            ),
        ),
        directory_status=DirectoryStatus.INACTIVE,
    )

    first = await repository.process(command)
    retry = await repository.process(command)

    assert first.outcome is DirectoryEventOutcome.FAILED
    assert first.error_code is DirectoryEventErrorCode.IDENTITY_CONFLICT
    assert first.revision_bumped is True
    assert retry.outcome is DirectoryEventOutcome.FAILED
    assert retry.error_code is DirectoryEventErrorCode.IDENTITY_CONFLICT
    assert retry.revision_bumped is False
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        first_identity = await session.get(
            ExternalIdentity,
            seed.first_identity_id,
        )
        second_identity = await session.get(
            ExternalIdentity,
            seed.second_identity_id,
        )
        receipt = await session.scalar(select(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "identity-conflict"))
    assert account is not None and account.identity_revision == 2
    assert first_identity is not None
    assert first_identity.state == ExternalIdentityState.ACTIVE.value
    assert second_identity is not None
    assert second_identity.state == ExternalIdentityState.ACTIVE.value
    assert receipt is not None
    assert receipt.processing_state == IdentityEventReceiptState.FAILED.value
    assert receipt.error_code == DirectoryEventErrorCode.IDENTITY_CONFLICT.value


async def test_future_event_rolls_back_before_receipt(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)

    with pytest.raises(DirectoryEventError) as captured:
        await repository.process(
            _user_command(
                seed,
                event_id="future-event",
                event_at=datetime.now(UTC) + timedelta(minutes=6),
            )
        )

    assert captured.value.code is DirectoryEventErrorCode.INVALID
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        receipt_count = await session.scalar(select(func.count()).select_from(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "future-event"))
    assert account is not None and account.identity_revision == 1
    assert receipt_count == 0


async def test_same_receipt_key_with_different_hash_has_zero_second_side_effects(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    command = _user_command(
        seed,
        event_id="replay-conflict",
        event_at=datetime.now(UTC) - timedelta(seconds=1),
        directory_status=DirectoryStatus.ACTIVE,
    )

    first = await repository.process(command)
    conflict = await repository.process(replace(command, directory_status=DirectoryStatus.INACTIVE))

    assert first.outcome is DirectoryEventOutcome.APPLIED
    assert conflict.outcome is DirectoryEventOutcome.FAILED
    assert conflict.error_code is DirectoryEventErrorCode.REPLAY_CONFLICT
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
    assert account is not None and account.identity_revision == 2
    assert identity is not None and identity.state == ExternalIdentityState.ACTIVE.value


async def test_identity_lifecycle_only_tightens_and_deleted_is_terminal(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)
    now = datetime.now(UTC)

    await repository.process(
        _user_command(
            seed,
            event_id="inactive-first",
            event_at=now - timedelta(seconds=4),
        )
    )
    await repository.process(
        _user_command(
            seed,
            event_id="active-cannot-reactivate",
            event_at=now - timedelta(seconds=3),
            directory_status=DirectoryStatus.ACTIVE,
        )
    )
    await repository.process(
        _user_command(
            seed,
            event_id="deleted-terminal",
            event_at=now - timedelta(seconds=2),
            event_type=DirectoryEventType.USER_DELETED,
        )
    )
    await repository.process(
        _user_command(
            seed,
            event_id="update-cannot-unrevoke",
            event_at=now - timedelta(seconds=1),
        )
    )

    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
    assert account is not None and account.identity_revision == 5
    assert identity is not None
    assert identity.state == ExternalIdentityState.REVOKED.value
    assert identity.identity_revision == 3


async def test_transient_mutation_failure_rolls_back_receipt_and_account(
    bootstrapped_async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _factory(bootstrapped_async_engine)
    seed = await _seed_graph(factory)
    repository = SqlAlchemyDirectoryEventRepository(factory)

    async def fail_mutation(
        _session: AsyncSession,
        _identity: ExternalIdentity,
        _command: DirectoryEventCommand,
    ) -> None:
        raise SQLAlchemyError("injected transient failure")

    monkeypatch.setattr(repository_module, "_tighten_identity", fail_mutation)

    with pytest.raises(DirectoryEventError) as captured:
        await repository.process(
            _user_command(
                seed,
                event_id="rollback-event",
                event_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )

    assert captured.value.code is DirectoryEventErrorCode.REPOSITORY_UNAVAILABLE
    async with factory() as session:
        account = await session.get(IdentityProviderAccount, seed.account_id)
        identity = await session.get(ExternalIdentity, seed.first_identity_id)
        receipt_count = await session.scalar(select(func.count()).select_from(IdentityEventReceipt).where(IdentityEventReceipt.event_id == "rollback-event"))
    assert account is not None and account.identity_revision == 1
    assert identity is not None and identity.state == ExternalIdentityState.ACTIVE.value
    assert receipt_count == 0
