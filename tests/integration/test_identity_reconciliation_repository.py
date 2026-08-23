"""Real PostgreSQL behavior tests for EIM-I8 reconciliation durability."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import (
    ExternalIdentityState,
    IdentityProviderHealthState,
    UserAccountKind,
)
from api.db.db_models import (
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityProviderAccount,
    IdentityProviderTenant,
    IdentityReconciliationCheckpoint,
    IdentityReconciliationTarget,
    Tenant,
    User,
    UserTenant,
)
from api.identity.reconciliation.contracts import (
    ReconciliationApplyOutcome,
    ReconciliationObservation,
    ReconciliationProviderStatus,
)
from api.identity.reconciliation.repository import (
    SqlAlchemyIdentityReconciliationRepository,
)
from common.constants import StatusEnum


@dataclass(frozen=True, slots=True)
class _Graph:
    factory: async_sessionmaker[AsyncSession]
    tenant_id: str
    account_id: str
    provider_tenant_id: str
    provider_tenant_key: str
    account_key: str
    user_id: str
    identity_id: str
    subject_value: str
    last_seen_at: datetime
    initial_verified_at: datetime

    @property
    def repository(self) -> SqlAlchemyIdentityReconciliationRepository:
        return SqlAlchemyIdentityReconciliationRepository(self.factory)


@pytest.fixture
async def graph(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[_Graph]:
    suffix = uuid.uuid4().hex
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    now = datetime.now(UTC) - timedelta(seconds=10)
    value = _Graph(
        factory=factory,
        tenant_id=uuid.uuid4().hex,
        account_id=uuid.uuid4().hex,
        provider_tenant_id=uuid.uuid4().hex,
        provider_tenant_key=f"tenant-{suffix}",
        account_key=f"account-{suffix}",
        user_id=uuid.uuid4().hex,
        identity_id=uuid.uuid4().hex,
        subject_value=f"user-{suffix}",
        last_seen_at=now,
        initial_verified_at=now - timedelta(minutes=1),
    )
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=value.tenant_id,
                    name="I8 reconciliation tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                User(
                    id=value.user_id,
                    nickname="Reconciliation member",
                    email=f"{value.user_id}@example.test",
                    password=None,
                    account_kind=UserAccountKind.EXTERNAL.value,
                    login_channel="feishu",
                    is_authenticated=True,
                    is_active=True,
                    is_anonymous=False,
                    status=StatusEnum.VALID.value,
                    is_superuser=False,
                ),
            ]
        )
        await session.flush()
        session.add(
            UserTenant(
                id=uuid.uuid4().hex,
                user_id=value.user_id,
                tenant_id=value.tenant_id,
                role="normal",
                invited_by=value.user_id,
                status=StatusEnum.VALID.value,
            )
        )
        session.add(
            IdentityProviderTenant(
                id=value.provider_tenant_id,
                tenant_id=value.tenant_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                verified_at=now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=value.account_id,
                tenant_id=value.tenant_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                provider_account_key=value.account_key,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        session.add(
            ExternalIdentity(
                id=value.identity_id,
                tenant_id=value.tenant_id,
                user_id=value.user_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                subject_type="user_id",
                subject_value=value.subject_value,
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=value.initial_verified_at,
                last_seen_at=value.last_seen_at,
                identity_revision=1,
                attributes={},
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityAlias(
                id=uuid.uuid4().hex,
                tenant_id=value.tenant_id,
                external_identity_id=value.identity_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                provider_account_key=value.account_key,
                alias_type="open_id",
                alias_value=f"open-{suffix}",
                verified_at=now,
            )
        )
    try:
        yield value
    finally:
        async with factory.begin() as session:
            user_ids = tuple(await session.scalars(select(ExternalIdentity.user_id).where(ExternalIdentity.tenant_id == value.tenant_id)))
            for model in (
                IdentityReconciliationTarget,
                IdentityReconciliationCheckpoint,
                ExternalIdentityAlias,
                ExternalIdentity,
                UserTenant,
                IdentityProviderAccount,
                IdentityProviderTenant,
            ):
                await session.execute(delete(model).where(model.tenant_id == value.tenant_id))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(Tenant).where(Tenant.id == value.tenant_id))


async def _seed_and_claim(graph: _Graph):
    inserted = await graph.repository.seed_checkpoints(due_at=datetime.now(UTC) - timedelta(seconds=1))
    assert inserted == 1
    lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert lease is not None
    return lease


async def _add_active_member_identity(
    graph: _Graph,
    *,
    identity_id: str,
) -> str:
    user_id = uuid.uuid4().hex
    subject_value = f"member-{uuid.uuid4().hex}"
    async with graph.factory.begin() as session:
        session.add(
            User(
                id=user_id,
                nickname="Additional reconciliation member",
                email=f"{user_id}@example.test",
                password=None,
                account_kind=UserAccountKind.EXTERNAL.value,
                login_channel="feishu",
                is_authenticated=True,
                is_active=True,
                is_anonymous=False,
                status=StatusEnum.VALID.value,
                is_superuser=False,
            )
        )
        await session.flush()
        session.add(
            UserTenant(
                id=uuid.uuid4().hex,
                user_id=user_id,
                tenant_id=graph.tenant_id,
                role="normal",
                invited_by=user_id,
                status=StatusEnum.VALID.value,
            )
        )
        session.add(
            ExternalIdentity(
                id=identity_id,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value=subject_value,
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=graph.initial_verified_at,
                last_seen_at=graph.last_seen_at,
                identity_revision=1,
                attributes={},
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityAlias(
                id=uuid.uuid4().hex,
                tenant_id=graph.tenant_id,
                external_identity_id=identity_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                provider_account_key=graph.account_key,
                alias_type="open_id",
                alias_value=f"open-{uuid.uuid4().hex}",
                verified_at=graph.initial_verified_at,
            )
        )
    return subject_value


def _apply_kwargs() -> dict[str, int]:
    return {
        "unavailable_delay_seconds": 5,
        "not_found_confirmation_seconds": 60,
        "cycle_interval_seconds": 300,
        "degrade_after_failures": 2,
        "max_tighten_per_cycle": 5,
    }


async def test_resolved_proof_is_account_durable_without_touching_activity(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    assert graph.identity_id not in repr(lease)
    assert graph.subject_value not in repr(lease)

    observed_at = datetime.now(UTC)
    proof_at = observed_at - timedelta(seconds=1)
    result = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.RESOLVED,
            observed_at=observed_at,
            verified_at=proof_at,
        ),
        **_apply_kwargs(),
    )
    assert result.outcome is ReconciliationApplyOutcome.APPLIED
    assert result.identity_changed is True

    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        target = await session.scalar(select(IdentityReconciliationTarget).where(IdentityReconciliationTarget.external_identity_id == graph.identity_id))
        checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id))
        assert identity is not None and target is not None and checkpoint is not None
        assert identity.verified_at == proof_at
        assert identity.last_seen_at == graph.last_seen_at
        assert identity.identity_revision == 2
        assert target.verified_at == proof_at
        assert target.proof_account_revision == 1
        assert target.proof_scope_change_at is None
        assert target.proof_identity_revision == 2
        assert target.last_success_at is not None
        assert target.last_activity_at == graph.last_seen_at
        assert checkpoint.last_success_at is not None

    snapshots = await graph.repository.admin_snapshots(tenant_id=graph.tenant_id)
    assert len(snapshots) == 1
    assert snapshots[0].provider == "feishu"
    assert snapshots[0].last_success_at is not None
    assert snapshots[0].account_ref != graph.account_id
    assert len(snapshots[0].account_ref) == 32
    assert graph.account_id not in repr(snapshots[0])
    assert graph.identity_id not in repr(snapshots[0])


async def test_concurrent_claims_serialize_to_one_account_lease(graph: _Graph) -> None:
    assert await graph.repository.seed_checkpoints(due_at=datetime.now(UTC) - timedelta(seconds=1)) == 1

    async def _claim(owner: str):
        return await graph.repository.claim_next(
            owner=owner,
            lease_seconds=30,
            active_since=graph.last_seen_at - timedelta(minutes=1),
            cycle_interval_seconds=300,
        )

    claims = await asyncio.gather(_claim("worker-a"), _claim("worker-b"))
    leased = [claim for claim in claims if claim is not None]
    assert len(leased) == 1
    assert leased[0].attempt == 1


async def test_claim_lease_clock_starts_after_identity_lock_wait(
    graph: _Graph,
) -> None:
    assert (
        await graph.repository.seed_checkpoints(
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        == 1
    )
    async with graph.factory.begin() as blocker:
        await blocker.scalar(select(ExternalIdentity).where(ExternalIdentity.id == graph.identity_id).with_for_update())
        claim_task = asyncio.create_task(
            graph.repository.claim_next(
                owner="integration-worker",
                lease_seconds=1,
                active_since=graph.last_seen_at - timedelta(minutes=1),
                cycle_interval_seconds=300,
            )
        )
        await asyncio.sleep(1.1)

    lease = await claim_task
    assert lease is not None
    async with graph.factory() as session:
        database_now = await session.scalar(select(sa.func.clock_timestamp()))
    assert isinstance(database_now, datetime)
    assert lease.lease_until > database_now


async def test_apply_rejects_lease_that_expires_while_waiting_for_target_lock(
    graph: _Graph,
) -> None:
    assert (
        await graph.repository.seed_checkpoints(
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        == 1
    )
    lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=1,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert lease is not None
    async with graph.factory.begin() as blocker:
        await blocker.scalar(select(IdentityReconciliationTarget).where(IdentityReconciliationTarget.id == lease.target_id).with_for_update())
        apply_task = asyncio.create_task(
            graph.repository.apply(
                lease=lease,
                observation=ReconciliationObservation(
                    status=ReconciliationProviderStatus.INACTIVE,
                    observed_at=datetime.now(UTC),
                ),
                **_apply_kwargs(),
            )
        )
        await asyncio.sleep(1.1)

    result = await apply_task
    assert result.outcome is ReconciliationApplyOutcome.FENCE_REJECTED
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        assert identity is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value


async def test_not_found_requires_separated_confirmation_before_tightening(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    first_at = datetime.now(UTC)
    first = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=first_at,
        ),
        **_apply_kwargs(),
    )
    assert first.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        assert identity is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value
        assert identity.identity_revision == 1

    aged_at = first_at - timedelta(seconds=61)
    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationTarget)
            .where(IdentityReconciliationTarget.external_identity_id == graph.identity_id)
            .values(
                first_not_found_at=aged_at,
                last_not_found_at=aged_at,
                last_observed_at=aged_at,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.execute(
            update(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id).values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert second_lease is not None
    second = await graph.repository.apply(
        lease=second_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert second.outcome is ReconciliationApplyOutcome.APPLIED
    assert second.identity_changed is True
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        assert identity is not None
        assert identity.state == ExternalIdentityState.INACTIVE.value
        assert identity.identity_revision == 2
        assert identity.last_seen_at == graph.last_seen_at


async def test_not_found_confirmation_does_not_block_other_account_members(
    graph: _Graph,
) -> None:
    second_identity_id = "f" * 32
    await _add_active_member_identity(
        graph,
        identity_id=second_identity_id,
    )
    first_lease = await _seed_and_claim(graph)
    assert first_lease.external_identity_id == graph.identity_id

    first = await graph.repository.apply(
        lease=first_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert first.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING

    next_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert next_lease is not None
    assert next_lease.external_identity_id == second_identity_id


async def test_long_not_found_confirmation_survives_shorter_cycles(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    first_at = datetime.now(UTC)
    kwargs = {
        **_apply_kwargs(),
        "not_found_confirmation_seconds": 600,
        "cycle_interval_seconds": 60,
    }
    first = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=first_at,
        ),
        **kwargs,
    )
    assert first.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING

    # Simulate the shorter cycle becoming due while the target confirmation is
    # still in the future.  A fresh scan must not reset its durable evidence.
    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationCheckpoint)
            .where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
            .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (
        await graph.repository.claim_next(
            owner="integration-worker",
            lease_seconds=30,
            active_since=graph.last_seen_at - timedelta(minutes=1),
            cycle_interval_seconds=60,
        )
        is None
    )
    async with graph.factory() as session:
        target = await session.scalar(
            select(IdentityReconciliationTarget).where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
        )
        assert target is not None
        assert target.not_found_count == 1
        assert target.first_not_found_at is not None
        assert target.first_not_found_at == target.last_not_found_at
        assert first_at <= target.first_not_found_at <= datetime.now(UTC)

    aged_at = first_at - timedelta(seconds=601)
    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationTarget)
            .where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
            .values(
                first_not_found_at=aged_at,
                last_not_found_at=aged_at,
                last_observed_at=aged_at,
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await session.execute(
            update(IdentityReconciliationCheckpoint)
            .where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
            .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=60,
    )
    assert second_lease is not None
    second = await graph.repository.apply(
        lease=second_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=datetime.now(UTC),
        ),
        **kwargs,
    )
    assert second.outcome is ReconciliationApplyOutcome.APPLIED
    assert second.identity_changed is True


async def test_not_found_confirmation_uses_database_not_replica_clock(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    replica_clock_behind = datetime.now(UTC) - timedelta(hours=1)
    kwargs = {
        **_apply_kwargs(),
        "not_found_confirmation_seconds": 600,
        "cycle_interval_seconds": 60,
    }
    first = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=replica_clock_behind,
        ),
        **kwargs,
    )
    assert first.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING

    # Force an early retry. Under the old replica-clock comparison, the
    # apparent one-hour gap would satisfy a ten-minute confirmation.
    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationTarget)
            .where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.execute(
            update(IdentityReconciliationCheckpoint)
            .where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
            .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    second_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=60,
    )
    assert second_lease is not None
    second = await graph.repository.apply(
        lease=second_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=datetime.now(UTC),
        ),
        **kwargs,
    )
    assert second.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING
    assert second.identity_changed is False
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        target = await session.scalar(
            select(IdentityReconciliationTarget).where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
        )
        assert identity is not None and target is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value
        assert target.not_found_count == 1


async def test_pending_confirmation_stops_when_activity_window_expires(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    first = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_FOUND,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert first.outcome is ReconciliationApplyOutcome.CONFIRMATION_PENDING

    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationTarget)
            .where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.execute(
            update(IdentityReconciliationCheckpoint)
            .where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
            .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert (
        await graph.repository.claim_next(
            owner="integration-worker",
            lease_seconds=30,
            active_since=graph.last_seen_at + timedelta(seconds=1),
            cycle_interval_seconds=300,
        )
        is None
    )
    async with graph.factory() as session:
        target = await session.scalar(
            select(IdentityReconciliationTarget).where(
                IdentityReconciliationTarget.external_identity_id == graph.identity_id,
            )
        )
        identity = await session.get(ExternalIdentity, graph.identity_id)
        assert target is not None and identity is not None
        assert target.state == "completed"
        assert identity.state == ExternalIdentityState.ACTIVE.value


async def test_claim_requires_live_user_membership(graph: _Graph) -> None:
    assert (
        await graph.repository.seed_checkpoints(
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        == 1
    )
    async with graph.factory.begin() as session:
        await session.execute(
            update(UserTenant)
            .where(
                UserTenant.tenant_id == graph.tenant_id,
                UserTenant.user_id == graph.user_id,
            )
            .values(status=StatusEnum.INVALID.value)
        )

    lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert lease is None


async def test_account_health_recovers_only_after_a_clean_completed_cycle(
    graph: _Graph,
) -> None:
    async with graph.factory.begin() as session:
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert account is not None
        account.identity_health_state = IdentityProviderHealthState.DEGRADED.value
        account.identity_revision = 2

    lease = await _seed_and_claim(graph)
    result = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_IN_SCOPE,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert result.outcome is ReconciliationApplyOutcome.APPLIED
    async with graph.factory() as session:
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert account is not None
        assert account.identity_health_state == IdentityProviderHealthState.DEGRADED.value
        assert account.identity_revision == 2

    assert (
        await graph.repository.claim_next(
            owner="integration-worker",
            lease_seconds=30,
            active_since=graph.last_seen_at - timedelta(minutes=1),
            cycle_interval_seconds=300,
        )
        is None
    )
    async with graph.factory() as session:
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert account is not None
        assert account.identity_health_state == IdentityProviderHealthState.HEALTHY.value
        assert account.identity_revision == 3


async def test_unavailable_is_zero_identity_write_and_health_bump_is_not_repeated(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    kwargs = {**_apply_kwargs(), "degrade_after_failures": 1}
    first = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.UNAVAILABLE,
            observed_at=datetime.now(UTC),
            safe_error_code="PROVIDER_TEMPORARY",
        ),
        **kwargs,
    )
    assert first.outcome is ReconciliationApplyOutcome.RETRY_SCHEDULED
    assert first.account_revision == 2

    async with graph.factory.begin() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert identity is not None and account is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value
        assert identity.identity_revision == 1
        assert identity.verified_at == graph.initial_verified_at
        assert identity.last_seen_at == graph.last_seen_at
        assert account.identity_health_state == IdentityProviderHealthState.DEGRADED.value
        assert account.identity_revision == 2
        await session.execute(
            update(IdentityReconciliationTarget).where(IdentityReconciliationTarget.external_identity_id == graph.identity_id).values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.execute(
            update(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id).values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    second_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert second_lease is not None
    assert second_lease.context.provider_account_revision == 2
    second = await graph.repository.apply(
        lease=second_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.UNAVAILABLE,
            observed_at=datetime.now(UTC),
            safe_error_code="PROVIDER_TEMPORARY",
        ),
        **kwargs,
    )
    assert second.account_revision == 2
    async with graph.factory() as session:
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert account is not None
        assert account.identity_revision == 2


async def test_stale_lease_and_not_in_scope_never_tighten_canonical_identity(
    graph: _Graph,
) -> None:
    lease = await _seed_and_claim(graph)
    async with graph.factory.begin() as session:
        checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id))
        assert checkpoint is not None
        checkpoint.lease_attempt += 1
    stale = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.INACTIVE,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert stale.outcome is ReconciliationApplyOutcome.FENCE_REJECTED
    async with graph.factory.begin() as session:
        checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id))
        assert checkpoint is not None
        checkpoint.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    fresh_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert fresh_lease is not None
    result = await graph.repository.apply(
        lease=fresh_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.NOT_IN_SCOPE,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert result.outcome is ReconciliationApplyOutcome.APPLIED
    assert result.identity_changed is False
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        assert identity is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value
        assert identity.identity_revision == 1


async def test_per_cycle_tightening_limit_opens_safe_circuit(graph: _Graph) -> None:
    second_user_id = uuid.uuid4().hex
    second_identity_id = "f" * 32
    async with graph.factory.begin() as session:
        session.add(
            User(
                id=second_user_id,
                nickname="Second reconciliation member",
                email=f"{second_user_id}@example.test",
                password=None,
                account_kind=UserAccountKind.EXTERNAL.value,
                login_channel="feishu",
                is_authenticated=True,
                is_active=True,
                is_anonymous=False,
                status=StatusEnum.VALID.value,
                is_superuser=False,
            )
        )
        await session.flush()
        session.add(
            UserTenant(
                id=uuid.uuid4().hex,
                user_id=second_user_id,
                tenant_id=graph.tenant_id,
                role="normal",
                invited_by=second_user_id,
                status=StatusEnum.VALID.value,
            )
        )
        session.add(
            ExternalIdentity(
                id=second_identity_id,
                tenant_id=graph.tenant_id,
                user_id=second_user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value=f"second-{uuid.uuid4().hex}",
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=graph.initial_verified_at,
                last_seen_at=graph.last_seen_at,
                identity_revision=1,
                attributes={},
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityAlias(
                id=uuid.uuid4().hex,
                tenant_id=graph.tenant_id,
                external_identity_id=second_identity_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                provider_account_key=graph.account_key,
                alias_type="open_id",
                alias_value=f"open-{uuid.uuid4().hex}",
                verified_at=graph.initial_verified_at,
            )
        )

    first_lease = await _seed_and_claim(graph)
    first = await graph.repository.apply(
        lease=first_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.INACTIVE,
            observed_at=datetime.now(UTC),
        ),
        **{**_apply_kwargs(), "max_tighten_per_cycle": 1},
    )
    assert first.identity_changed is True
    second_lease = await graph.repository.claim_next(
        owner="integration-worker",
        lease_seconds=30,
        active_since=graph.last_seen_at - timedelta(minutes=1),
        cycle_interval_seconds=300,
    )
    assert second_lease is not None
    assert second_lease.external_identity_id == second_identity_id
    blocked = await graph.repository.apply(
        lease=second_lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.INACTIVE,
            observed_at=datetime.now(UTC),
        ),
        **{**_apply_kwargs(), "max_tighten_per_cycle": 1},
    )
    assert blocked.outcome is ReconciliationApplyOutcome.CIRCUIT_OPEN
    assert blocked.identity_changed is False
    async with graph.factory() as session:
        second_identity = await session.get(ExternalIdentity, second_identity_id)
        account = await session.get(IdentityProviderAccount, graph.account_id)
        checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.provider_account_id == graph.account_id))
        assert second_identity is not None and account is not None and checkpoint is not None
        assert second_identity.state == ExternalIdentityState.ACTIVE.value
        assert second_identity.identity_revision == 1
        assert account.identity_health_state == IdentityProviderHealthState.DEGRADED.value
        assert checkpoint.tightened_count == 1
        assert checkpoint.cycle_started_at is None


def test_schema_uses_restrict_and_per_account_serialization(
    bootstrapped_engine: sa.Engine,
) -> None:
    inspector = sa.inspect(bootstrapped_engine)
    checkpoint_unique = {constraint["name"]: tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(IdentityReconciliationCheckpoint.__tablename__, schema="usr_ai")}
    assert checkpoint_unique["uq_identity_reconciliation_checkpoints_account"] == ("provider_account_id",)
    for table in (
        IdentityReconciliationCheckpoint.__tablename__,
        IdentityReconciliationTarget.__tablename__,
    ):
        foreign_keys = inspector.get_foreign_keys(table, schema="usr_ai")
        assert foreign_keys
        assert all(foreign_key["options"].get("ondelete") == "RESTRICT" for foreign_key in foreign_keys)


async def test_migration_downgrade_refuses_nonempty_durable_state(
    graph: _Graph,
    bootstrapped_async_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration_path = Path(__file__).resolve().parents[2] / "configs" / "alembic" / "versions" / "e1f3a5c7b9d0_add_identity_reconciliation_state.py"
    spec = importlib.util.spec_from_file_location(
        "test_identity_reconciliation_migration",
        migration_path,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("migration module could not be loaded")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert isinstance(migration, ModuleType)

    assert await graph.repository.seed_checkpoints(due_at=datetime.now(UTC) - timedelta(seconds=1)) == 1

    def _downgrade(connection: Connection) -> None:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.downgrade()

    async with bootstrapped_async_engine.begin() as connection:
        with pytest.raises(RuntimeError, match="refusing to drop non-empty"):
            await connection.run_sync(_downgrade)


async def test_claim_stops_after_tenant_is_disabled(graph: _Graph) -> None:
    assert (
        await graph.repository.seed_checkpoints(
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        == 1
    )
    first_lease = await graph.repository.claim_next(
        owner="worker-before-disable",
        lease_seconds=60,
        active_since=datetime.now(UTC) - timedelta(days=1),
        cycle_interval_seconds=300,
    )
    assert first_lease is not None
    async with graph.factory.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == graph.tenant_id).values(status=StatusEnum.INVALID.value))
        await session.execute(
            update(IdentityProviderAccount)
            .where(IdentityProviderAccount.id == graph.account_id)
            .values(
                identity_health_state=IdentityProviderHealthState.DEGRADED.value,
            )
        )

    assert (
        await graph.repository.claim_next(
            owner="worker-disabled-tenant",
            lease_seconds=60,
            active_since=datetime.now(UTC) - timedelta(days=1),
            cycle_interval_seconds=300,
        )
        is None
    )
    async with graph.factory() as session:
        checkpoint = await session.scalar(
            select(IdentityReconciliationCheckpoint).where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
        )
        account = await session.get(IdentityProviderAccount, graph.account_id)
        assert checkpoint is not None and account is not None
        assert checkpoint.cycle_started_at is None
        assert checkpoint.lease_owner is None
        assert checkpoint.lease_until is None
        assert account.identity_health_state == IdentityProviderHealthState.DEGRADED.value


async def test_apply_rejects_observation_after_tenant_is_disabled(
    graph: _Graph,
) -> None:
    assert (
        await graph.repository.seed_checkpoints(
            due_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        == 1
    )
    lease = await graph.repository.claim_next(
        owner="worker-tenant-disabled-during-probe",
        lease_seconds=60,
        active_since=datetime.now(UTC) - timedelta(days=1),
        cycle_interval_seconds=300,
    )
    assert lease is not None
    async with graph.factory.begin() as session:
        await session.execute(update(Tenant).where(Tenant.id == graph.tenant_id).values(status=StatusEnum.INVALID.value))

    result = await graph.repository.apply(
        lease=lease,
        observation=ReconciliationObservation(
            status=ReconciliationProviderStatus.INACTIVE,
            observed_at=datetime.now(UTC),
        ),
        **_apply_kwargs(),
    )
    assert result.outcome is ReconciliationApplyOutcome.FENCE_REJECTED
    assert result.identity_changed is False
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, graph.identity_id)
        checkpoint = await session.scalar(
            select(IdentityReconciliationCheckpoint).where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
        )
        assert identity is not None and checkpoint is not None
        assert identity.state == ExternalIdentityState.ACTIVE.value
        assert identity.identity_revision == 1
        assert checkpoint.cycle_started_at is None
        assert checkpoint.lease_owner is None
        assert checkpoint.lease_until is None
