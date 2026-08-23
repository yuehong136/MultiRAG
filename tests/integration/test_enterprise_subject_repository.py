"""Real PostgreSQL invariants for the EIM-I5 subject repository."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import EnterpriseSubjectState, UserAccountKind, UserTenantRole
from api.db.db_models import EnterpriseSubjectLink, Tenant, User, UserTenant
from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectErrorCode,
    EnterpriseSubjectRepositoryError,
    EnterpriseSubjectResolution,
    EnterpriseSubjectResolutionStatus,
    EnterpriseSubjectSlot,
    VerifiedEnterpriseSubjectCommand,
)
from api.identity.enterprise_subjects.feishu_employee_number import (
    FeishuEmployeeNumberResolver,
)
from api.identity.enterprise_subjects.repository import (
    SqlAlchemyEnterpriseSubjectRepository,
)
from api.identity.enterprise_subjects.service import EnterpriseSubjectService
from api.identity.principal import EnterpriseSubject
from api.identity.providers.contracts import ProviderDirectoryStatus, ProviderIdentity
from common.constants import StatusEnum


@dataclass(frozen=True, slots=True)
class _Seed:
    tenant_id: str
    other_tenant_id: str
    user_id: str
    other_user_id: str
    cross_tenant_user_id: str


@dataclass(frozen=True, slots=True)
class _Store:
    factory: async_sessionmaker[AsyncSession]
    repository: SqlAlchemyEnterpriseSubjectRepository
    seed: _Seed


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _tenant(tenant_id: str, name: str) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=name,
        llm_id="test-llm",
        embd_id="test-embedding",
        asr_id="test-asr",
        img2txt_id="test-image",
        parser_ids="naive",
    )


def _user(user_id: str) -> User:
    return User(
        id=user_id,
        nickname="Enterprise subject repository user",
        email=None,
        password=None,
        account_kind=UserAccountKind.EXTERNAL.value,
        login_channel="feishu",
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        status=StatusEnum.VALID.value,
    )


def _membership(tenant_id: str, user_id: str) -> UserTenant:
    return UserTenant(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        user_id=user_id,
        role=UserTenantRole.NORMAL.value,
        invited_by=user_id,
        status=StatusEnum.VALID.value,
    )


@pytest.fixture
async def subject_store(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[_Store]:
    seed = _Seed(
        tenant_id=uuid.uuid4().hex,
        other_tenant_id=uuid.uuid4().hex,
        user_id=uuid.uuid4().hex,
        other_user_id=uuid.uuid4().hex,
        cross_tenant_user_id=uuid.uuid4().hex,
    )
    factory = _factory(bootstrapped_async_engine)
    async with factory.begin() as session:
        session.add_all(
            [
                _tenant(seed.tenant_id, "I5 repository tenant"),
                _tenant(seed.other_tenant_id, "I5 repository other tenant"),
                _user(seed.user_id),
                _user(seed.other_user_id),
                _user(seed.cross_tenant_user_id),
            ]
        )
        await session.flush()
        session.add_all(
            [
                _membership(seed.tenant_id, seed.user_id),
                _membership(seed.tenant_id, seed.other_user_id),
                _membership(seed.other_tenant_id, seed.cross_tenant_user_id),
            ]
        )

    try:
        yield _Store(
            factory=factory,
            repository=SqlAlchemyEnterpriseSubjectRepository(factory),
            seed=seed,
        )
    finally:
        async with factory.begin() as session:
            await session.execute(
                sa.delete(EnterpriseSubjectLink).where(
                    EnterpriseSubjectLink.tenant_id.in_(
                        (seed.tenant_id, seed.other_tenant_id),
                    )
                )
            )
            await session.execute(
                sa.delete(UserTenant).where(
                    UserTenant.tenant_id.in_(
                        (seed.tenant_id, seed.other_tenant_id),
                    )
                )
            )
            await session.execute(
                sa.delete(User).where(
                    User.id.in_(
                        (
                            seed.user_id,
                            seed.other_user_id,
                            seed.cross_tenant_user_id,
                        )
                    )
                )
            )
            await session.execute(
                sa.delete(Tenant).where(
                    Tenant.id.in_(
                        (seed.tenant_id, seed.other_tenant_id),
                    )
                )
            )


def _slot(
    tenant_id: str,
    *,
    issuer: str = "hr",
    issuer_tenant: str | None = None,
) -> EnterpriseSubjectSlot:
    return EnterpriseSubjectSlot(
        subject_type="employee_no",
        issuer=issuer,
        issuer_tenant=issuer_tenant or f"authority-{tenant_id}",
    )


def _resolved_command(
    *,
    tenant_id: str,
    user_id: str,
    subject_value: str,
    verified_at: datetime,
    source_revision: str | None,
    slot: EnterpriseSubjectSlot | None = None,
) -> VerifiedEnterpriseSubjectCommand:
    authority = slot or _slot(tenant_id)
    subject = EnterpriseSubject(
        subject_type=authority.subject_type,
        subject=subject_value,
        issuer=authority.issuer,
        issuer_tenant=authority.issuer_tenant,
        verified_at=verified_at,
    )
    return VerifiedEnterpriseSubjectCommand(
        platform_user_id=user_id,
        tenant_id=tenant_id,
        slot=authority,
        resolution=EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            subject=subject,
            verified_at=verified_at,
            source_revision=source_revision,
        ),
    )


def _negative_command(
    *,
    tenant_id: str,
    user_id: str,
    status: EnterpriseSubjectResolutionStatus,
    verified_at: datetime,
    source_revision: str,
    slot: EnterpriseSubjectSlot | None = None,
) -> VerifiedEnterpriseSubjectCommand:
    return VerifiedEnterpriseSubjectCommand(
        platform_user_id=user_id,
        tenant_id=tenant_id,
        slot=slot or _slot(tenant_id),
        resolution=EnterpriseSubjectResolution(
            status=status,
            verified_at=verified_at,
            source_revision=source_revision,
        ),
    )


def _unavailable_command(
    *,
    tenant_id: str,
    user_id: str,
) -> VerifiedEnterpriseSubjectCommand:
    return VerifiedEnterpriseSubjectCommand(
        platform_user_id=user_id,
        tenant_id=tenant_id,
        slot=_slot(tenant_id),
        resolution=EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
        ),
    )


async def _links(store: _Store) -> list[EnterpriseSubjectLink]:
    async with store.factory() as session:
        return list(
            (
                await session.scalars(
                    sa.select(EnterpriseSubjectLink).order_by(
                        EnterpriseSubjectLink.tenant_id,
                        EnterpriseSubjectLink.user_id,
                    )
                )
            ).all()
        )


async def test_resolved_upsert_is_idempotent_and_proof_is_monotonic(
    subject_store: _Store,
) -> None:
    store = subject_store
    proof_1 = datetime.now(UTC) - timedelta(minutes=2)
    command_1 = _resolved_command(
        tenant_id=store.seed.tenant_id,
        user_id=store.seed.user_id,
        subject_value="employee-1001",
        verified_at=proof_1,
        source_revision="revision-1",
    )

    first = await store.repository.persist_resolution(command_1)
    replay = await store.repository.persist_resolution(command_1)

    assert first == replay
    assert first.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert first.subject is not None
    assert first.subject.subject == "employee-1001"

    proof_2 = proof_1 + timedelta(seconds=30)
    advanced = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-1001",
            verified_at=proof_2,
            source_revision="revision-2",
        )
    )
    proof_3 = proof_2 + timedelta(seconds=10)
    without_revision = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-1001",
            verified_at=proof_3,
            source_revision=None,
        )
    )
    stale = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-1001",
            verified_at=proof_1 + timedelta(seconds=10),
            source_revision="revision-999",
        )
    )

    assert advanced.verified_at == proof_2
    assert advanced.source_revision == "revision-2"
    assert without_revision.verified_at == proof_3
    assert without_revision.source_revision == "revision-2"
    assert stale.verified_at == proof_3
    assert stale.source_revision == "revision-2"
    rows = await _links(store)
    assert len(rows) == 1
    assert rows[0].state == EnterpriseSubjectState.ACTIVE.value
    assert rows[0].verified_at == proof_3
    assert rows[0].source_revision == "revision-2"


@pytest.mark.parametrize(
    "status",
    [
        EnterpriseSubjectResolutionStatus.NOT_FOUND,
        EnterpriseSubjectResolutionStatus.INACTIVE,
    ],
)
async def test_negative_result_tightens_only_with_non_stale_proof(
    subject_store: _Store,
    status: EnterpriseSubjectResolutionStatus,
) -> None:
    store = subject_store
    active_at = datetime.now(UTC) - timedelta(minutes=3)
    await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-2001",
            verified_at=active_at,
            source_revision="active-1",
        )
    )

    stale_result = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=status,
            verified_at=active_at - timedelta(seconds=1),
            source_revision="negative-stale",
        )
    )
    assert stale_result.status is status
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.ACTIVE.value
    assert row.verified_at == active_at

    inactive_at = active_at + timedelta(seconds=30)
    fresh_result = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=status,
            verified_at=inactive_at,
            source_revision="negative-fresh",
        )
    )
    assert fresh_result.status is status
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.INACTIVE.value
    assert row.verified_at == inactive_at
    assert row.source_revision == "negative-fresh"

    stale_positive = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-2001",
            verified_at=active_at + timedelta(seconds=10),
            source_revision="positive-stale",
        )
    )
    assert stale_positive.status is EnterpriseSubjectResolutionStatus.INACTIVE

    newer_positive_at = inactive_at + timedelta(seconds=30)
    still_inactive = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-2001",
            verified_at=newer_positive_at,
            source_revision="positive-fresh",
        )
    )
    assert still_inactive.status is EnterpriseSubjectResolutionStatus.INACTIVE
    assert still_inactive.verified_at == newer_positive_at
    assert still_inactive.source_revision == "positive-fresh"
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.INACTIVE.value
    assert row.verified_at == inactive_at
    assert row.source_revision == "negative-fresh"


async def test_stale_ambiguous_proof_cannot_quarantine_newer_active_link(
    subject_store: _Store,
) -> None:
    store = subject_store
    active_at = datetime.now(UTC) - timedelta(minutes=2)
    await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-3001",
            verified_at=active_at,
            source_revision="active",
        )
    )

    stale = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=EnterpriseSubjectResolutionStatus.AMBIGUOUS,
            verified_at=active_at - timedelta(seconds=1),
            source_revision="ambiguous-stale",
        )
    )
    assert stale.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.ACTIVE.value
    assert row.verified_at == active_at

    quarantined = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=EnterpriseSubjectResolutionStatus.AMBIGUOUS,
            verified_at=active_at,
            source_revision="ambiguous-equal",
        )
    )
    assert quarantined.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.CONFLICT.value

    still_quarantined = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-3001",
            verified_at=active_at + timedelta(seconds=30),
            source_revision="positive-newer",
        )
    )
    assert still_quarantined.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    assert still_quarantined.subject is None
    assert still_quarantined.verified_at == active_at + timedelta(seconds=30)
    assert still_quarantined.source_revision == "positive-newer"
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.CONFLICT.value
    assert row.verified_at == active_at + timedelta(seconds=30)
    assert row.source_revision == "positive-newer"

    directory_miss = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
            verified_at=active_at + timedelta(seconds=40),
            source_revision="directory-miss",
        )
    )
    assert directory_miss.status is EnterpriseSubjectResolutionStatus.NOT_FOUND
    assert (await _links(store))[0].state == EnterpriseSubjectState.CONFLICT.value


async def test_same_slot_subject_change_is_quarantined_not_replaced(
    subject_store: _Store,
) -> None:
    store = subject_store
    original_at = datetime.now(UTC) - timedelta(minutes=2)
    await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-original",
            verified_at=original_at,
            source_revision="original",
        )
    )

    stale_change = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-replacement",
            verified_at=original_at - timedelta(seconds=1),
            source_revision="replacement-stale",
        )
    )
    assert stale_change.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    assert stale_change.subject is None
    stale_row = (await _links(store))[0]
    assert stale_row.state == EnterpriseSubjectState.ACTIVE.value
    assert stale_row.subject_value == "employee-original"
    assert stale_row.verified_at == original_at

    fresh_change = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-replacement",
            verified_at=original_at + timedelta(seconds=30),
            source_revision="replacement-fresh",
        )
    )
    assert fresh_change.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    assert fresh_change.subject is None
    assert fresh_change.verified_at == original_at + timedelta(seconds=30)
    assert fresh_change.source_revision == "replacement-fresh"
    rows = await _links(store)
    assert len(rows) == 1
    assert rows[0].subject_value == "employee-original"
    assert rows[0].state == EnterpriseSubjectState.CONFLICT.value
    assert rows[0].verified_at == original_at
    assert rows[0].source_revision == "original"


async def test_concurrent_subject_collision_is_quarantined_on_discovery(
    subject_store: _Store,
) -> None:
    store = subject_store
    proof_at = datetime.now(UTC) - timedelta(minutes=1)
    commands = [
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=user_id,
            subject_value="employee-shared",
            verified_at=proof_at,
            source_revision="shared-1",
        )
        for user_id in (store.seed.user_id, store.seed.other_user_id)
    ]

    first_results = await asyncio.gather(*(store.repository.persist_resolution(command) for command in commands))

    # The first commit can be observed before the competing claim discovers
    # it; the discovering transaction must quarantine the sole durable row.
    assert {result.status for result in first_results} == {
        EnterpriseSubjectResolutionStatus.RESOLVED,
        EnterpriseSubjectResolutionStatus.AMBIGUOUS,
    }
    rows = await _links(store)
    assert len(rows) == 1
    assert rows[0].state == EnterpriseSubjectState.CONFLICT.value

    follow_up_at = proof_at + timedelta(seconds=30)
    follow_ups = await asyncio.gather(
        *(
            store.repository.persist_resolution(
                _resolved_command(
                    tenant_id=command.tenant_id,
                    user_id=command.platform_user_id,
                    subject_value="employee-shared",
                    verified_at=follow_up_at,
                    source_revision="shared-2",
                )
            )
            for command in commands
        )
    )
    assert all(result.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS and result.subject is None for result in follow_ups)


async def test_service_treats_fresh_subject_change_as_normal_ambiguous(
    subject_store: _Store,
) -> None:
    store = subject_store
    original_at = datetime.now(UTC) - timedelta(minutes=2)
    resolver_slot = EnterpriseSubjectSlot(
        subject_type="employee_no",
        issuer="feishu_contact",
        issuer_tenant="feishu-enterprise",
    )
    await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-service-original",
            verified_at=original_at,
            source_revision="service-original",
            slot=resolver_slot,
        )
    )
    changed_at = original_at + timedelta(seconds=30)
    service = EnterpriseSubjectService(
        FeishuEmployeeNumberResolver(),
        store.repository,
        now=lambda: changed_at + timedelta(seconds=1),
    )

    result = await service.resolve(
        platform_user_id=store.seed.user_id,
        tenant_id=store.seed.tenant_id,
        provider_identity=ProviderIdentity(
            provider="feishu",
            provider_tenant_key="feishu-enterprise",
            provider_account_id="provider-account",
            provider_user_id="provider-user",
            verified_at=changed_at,
            employee_no="employee-service-replacement",
            provider_status=ProviderDirectoryStatus.ACTIVE,
        ),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS
    assert result.evidence is None
    assert result.error_code is None
    assert not result.fatal
    row = (await _links(store))[0]
    assert row.state == EnterpriseSubjectState.CONFLICT.value
    assert row.subject_value == "employee-service-original"
    assert row.verified_at == original_at
    assert row.source_revision == "service-original"


async def test_authority_and_tenant_coordinates_are_exact_and_unavailable_is_zero_write(
    subject_store: _Store,
) -> None:
    store = subject_store
    proof_at = datetime.now(UTC) - timedelta(minutes=1)
    await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            subject_value="employee-tenant-local",
            verified_at=proof_at,
            source_revision="tenant-a",
        )
    )

    other_authority = _slot(
        store.seed.tenant_id,
        issuer="oa",
        issuer_tenant="separate-namespace",
    )
    missing = await store.repository.persist_resolution(
        _negative_command(
            tenant_id=store.seed.tenant_id,
            user_id=store.seed.user_id,
            status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
            verified_at=proof_at + timedelta(seconds=10),
            source_revision="other-authority",
            slot=other_authority,
        )
    )
    assert missing.status is EnterpriseSubjectResolutionStatus.NOT_FOUND
    assert (await _links(store))[0].state == EnterpriseSubjectState.ACTIVE.value

    other_tenant_result = await store.repository.persist_resolution(
        _resolved_command(
            tenant_id=store.seed.other_tenant_id,
            user_id=store.seed.cross_tenant_user_id,
            subject_value="employee-tenant-local",
            verified_at=proof_at,
            source_revision="tenant-b",
        )
    )
    assert other_tenant_result.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert len(await _links(store)) == 2

    cross_tenant_command = _resolved_command(
        tenant_id=store.seed.other_tenant_id,
        user_id=store.seed.user_id,
        subject_value="employee-cross-tenant",
        verified_at=proof_at,
        source_revision="cross-tenant",
    )
    with pytest.raises(EnterpriseSubjectRepositoryError) as caught:
        await store.repository.persist_resolution(cross_tenant_command)
    assert caught.value.code is EnterpriseSubjectErrorCode.PERSISTENCE_INVALID
    assert store.seed.user_id not in str(caught.value)
    assert "employee-cross-tenant" not in repr(caught.value)
    assert len(await _links(store)) == 2

    future_command = _resolved_command(
        tenant_id=store.seed.tenant_id,
        user_id=store.seed.other_user_id,
        subject_value="employee-future-proof",
        verified_at=datetime.now(UTC) + timedelta(minutes=5),
        source_revision="future",
    )
    with pytest.raises(EnterpriseSubjectRepositoryError) as future_error:
        await store.repository.persist_resolution(future_command)
    assert future_error.value.code is EnterpriseSubjectErrorCode.PERSISTENCE_INVALID
    assert len(await _links(store)) == 2

    unavailable = await store.repository.persist_resolution(
        _unavailable_command(
            tenant_id=uuid.uuid4().hex,
            user_id=uuid.uuid4().hex,
        )
    )
    assert unavailable.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE
    assert len(await _links(store)) == 2


async def test_database_failures_are_reduced_to_a_non_identifying_code(
    subject_store: _Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = subject_store
    sensitive_subject = "employee-never-in-error"
    command = _resolved_command(
        tenant_id=store.seed.tenant_id,
        user_id=store.seed.user_id,
        subject_value=sensitive_subject,
        verified_at=datetime.now(UTC) - timedelta(minutes=1),
        source_revision="error-path",
    )

    async def fail_with_database_detail(
        _command: VerifiedEnterpriseSubjectCommand,
    ) -> EnterpriseSubjectResolution:
        raise sa.exc.OperationalError(
            f"SELECT '{sensitive_subject}'",
            {"subject": sensitive_subject},
            RuntimeError("driver included sensitive bind material"),
        )

    monkeypatch.setattr(store.repository, "_apply", fail_with_database_detail)
    with pytest.raises(EnterpriseSubjectRepositoryError) as caught:
        await store.repository.persist_resolution(command)

    assert caught.value.code is EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE
    assert sensitive_subject not in str(caught.value)
    assert sensitive_subject not in repr(caught.value)
