"""PostgreSQL unit-of-work adapter for EIM-I8 reconciliation.

One checkpoint lease serializes at most one provider call per account.  Target
rows therefore need no independent lease: the checkpoint owner/attempt/expiry
is the sole fence, while the target persists account and identity generations
across the transaction-free provider call.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db import ExternalIdentityState, IdentityProviderHealthState
from api.db.db_models import (
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityProviderAccount,
    IdentityReconciliationCheckpoint,
    IdentityReconciliationTarget,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import ProviderContext
from api.identity.reconciliation.contracts import (
    ReconciliationAdminSnapshot,
    ReconciliationApplyOutcome,
    ReconciliationApplyResult,
    ReconciliationErrorCode,
    ReconciliationLease,
    ReconciliationObservation,
    ReconciliationProviderStatus,
    ReconciliationRepositoryError,
)
from common.constants import StatusEnum

_CLAIM_SEARCH_LIMIT = 256
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_RECONCILABLE_HEALTH = frozenset(
    {
        IdentityProviderHealthState.HEALTHY.value,
        IdentityProviderHealthState.DEGRADED.value,
    }
)
_TIGHTENING_STATUSES = frozenset(
    {
        ReconciliationProviderStatus.INACTIVE,
        ReconciliationProviderStatus.CONFLICT,
    }
)


class SqlAlchemyIdentityReconciliationRepository:
    """Own short claim/apply transactions around external provider I/O."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def seed_checkpoints(self, *, due_at: datetime) -> int:
        """Create checkpoints from local account rows; never enumerate provider users."""

        _require_aware(due_at, "due_at")
        inserted = 0
        try:
            async with self._session_factory.begin() as session:
                accounts = (
                    await session.execute(
                        select(
                            IdentityProviderAccount.id,
                            IdentityProviderAccount.tenant_id,
                            IdentityProviderAccount.provider,
                        )
                        .join(
                            Tenant,
                            and_(
                                Tenant.id == IdentityProviderAccount.tenant_id,
                                Tenant.status == StatusEnum.VALID.value,
                            ),
                        )
                        .where(IdentityProviderAccount.identity_health_state.in_(_RECONCILABLE_HEALTH))
                        .order_by(IdentityProviderAccount.id)
                    )
                ).all()
                for account_id, tenant_id, provider in accounts:
                    result = await session.execute(
                        pg_insert(IdentityReconciliationCheckpoint)
                        .values(
                            id=uuid.uuid4().hex,
                            provider_account_id=account_id,
                            tenant_id=tenant_id,
                            provider=provider,
                            next_run_at=due_at.astimezone(UTC),
                            lease_attempt=0,
                            processed_count=0,
                            tightened_count=0,
                            error_count=0,
                            consecutive_failures=0,
                        )
                        .on_conflict_do_nothing(index_elements=[IdentityReconciliationCheckpoint.provider_account_id])
                        .returning(IdentityReconciliationCheckpoint.id)
                    )
                    if result.scalar_one_or_none() is not None:
                        inserted += 1
            return inserted
        except SQLAlchemyError:
            raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE) from None

    async def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        active_since: datetime,
        cycle_interval_seconds: int,
    ) -> ReconciliationLease | None:
        """Claim one locally-known active identity, fenced at account and identity."""

        _validate_owner(owner)
        _require_positive(lease_seconds, "lease_seconds")
        _require_positive(cycle_interval_seconds, "cycle_interval_seconds")
        _require_aware(active_since, "active_since")
        try:
            checkpoint_ids = await self._candidate_checkpoint_ids()
            for checkpoint_id in checkpoint_ids:
                lease = await self._claim_checkpoint(
                    checkpoint_id=checkpoint_id,
                    owner=owner,
                    lease_seconds=lease_seconds,
                    active_since=active_since.astimezone(UTC),
                    cycle_interval_seconds=cycle_interval_seconds,
                )
                if lease is not None:
                    return lease
            return None
        except ReconciliationRepositoryError:
            raise
        except SQLAlchemyError:
            raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE) from None

    async def apply(
        self,
        *,
        lease: ReconciliationLease,
        observation: ReconciliationObservation,
        unavailable_delay_seconds: int,
        not_found_confirmation_seconds: int,
        cycle_interval_seconds: int,
        degrade_after_failures: int,
        max_tighten_per_cycle: int,
    ) -> ReconciliationApplyResult:
        """Apply one provider observation under the durable lease/generation fence."""

        _require_positive(unavailable_delay_seconds, "unavailable_delay_seconds")
        _require_positive(
            not_found_confirmation_seconds,
            "not_found_confirmation_seconds",
        )
        _require_positive(cycle_interval_seconds, "cycle_interval_seconds")
        _require_positive(degrade_after_failures, "degrade_after_failures")
        _require_positive(max_tighten_per_cycle, "max_tighten_per_cycle")
        try:
            checkpoint_scope = await self._checkpoint_scope(lease.checkpoint_id)
            if checkpoint_scope is None:
                return _fence_result(lease.context.provider_account_revision)
            account_id, tenant_id = checkpoint_scope
            async with self._session_factory.begin() as session:
                tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
                account = await session.scalar(select(IdentityProviderAccount).where(IdentityProviderAccount.id == account_id).with_for_update())
                checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.id == lease.checkpoint_id).with_for_update())
                if tenant is None or account is None or checkpoint is None:
                    return _fence_result(lease.context.provider_account_revision)

                identity = await session.scalar(
                    select(ExternalIdentity)
                    .where(
                        ExternalIdentity.id == lease.external_identity_id,
                        ExternalIdentity.tenant_id == account.tenant_id,
                        ExternalIdentity.provider == account.provider,
                        ExternalIdentity.provider_tenant_key == account.provider_tenant_key,
                    )
                    .with_for_update()
                )
                target = await session.scalar(
                    select(IdentityReconciliationTarget)
                    .where(
                        IdentityReconciliationTarget.id == lease.target_id,
                        IdentityReconciliationTarget.checkpoint_id == checkpoint.id,
                    )
                    .with_for_update()
                )
                # Wall-clock lease validation must happen after every row that
                # can block has been locked.  Otherwise an old owner can wait
                # across its deadline and still apply with a stale timestamp.
                now = await _database_now(session)
                if not _lease_matches(checkpoint, lease, now=now):
                    return _fence_result(account.identity_revision)
                if tenant.status != StatusEnum.VALID.value:
                    _complete_cycle(
                        account,
                        checkpoint,
                        now=now,
                        cycle_interval_seconds=cycle_interval_seconds,
                        allow_health_recovery=False,
                    )
                    return _fence_result(account.identity_revision)
                if identity is None or target is None:
                    _reject_fence(checkpoint, target, now=now)
                    return _fence_result(account.identity_revision)
                if not _account_fence_matches(account, lease.context) or not _target_fence_matches(
                    target,
                    lease,
                ):
                    _reject_fence(checkpoint, target, now=now)
                    return _fence_result(account.identity_revision)
                if (
                    identity.identity_revision != lease.identity_revision
                    or identity.state != ExternalIdentityState.ACTIVE.value
                    or identity.subject_type != "user_id"
                    or identity.subject_value != lease.subject_value
                ):
                    _finish_target(
                        checkpoint,
                        target,
                        identity_id=identity.id,
                        now=now,
                        failed=True,
                        error_code=ReconciliationErrorCode.FENCE_CONFLICT.value,
                    )
                    return _fence_result(account.identity_revision)

                effective = observation
                if observation.observed_at > now + _MAX_FUTURE_SKEW:
                    effective = ReconciliationObservation(
                        status=ReconciliationProviderStatus.INVALID,
                        observed_at=min(observation.observed_at, now),
                        safe_error_code=(ReconciliationErrorCode.PROVIDER_RESULT_INVALID.value),
                    )
                result = _apply_observation(
                    account=account,
                    checkpoint=checkpoint,
                    target=target,
                    identity=identity,
                    observation=effective,
                    now=now,
                    unavailable_delay_seconds=unavailable_delay_seconds,
                    not_found_confirmation_seconds=not_found_confirmation_seconds,
                    cycle_interval_seconds=cycle_interval_seconds,
                    degrade_after_failures=degrade_after_failures,
                    max_tighten_per_cycle=max_tighten_per_cycle,
                )
                await session.flush()
                return result
        except ReconciliationRepositoryError:
            raise
        except SQLAlchemyError:
            raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE) from None

    async def admin_snapshots(
        self,
        *,
        tenant_id: str,
    ) -> tuple[ReconciliationAdminSnapshot, ...]:
        """Return a tenant-filtered, identifier-free operational projection."""

        if not isinstance(tenant_id, str) or not tenant_id or tenant_id != tenant_id.strip() or len(tenant_id) > 64:
            raise ValueError("tenant_id is invalid")
        try:
            async with self._session_factory() as session:
                now = await _database_now(session)
                rows = (
                    await session.execute(
                        select(
                            IdentityReconciliationCheckpoint,
                            IdentityProviderAccount.identity_health_state,
                        )
                        .join(
                            IdentityProviderAccount,
                            and_(
                                IdentityProviderAccount.id == IdentityReconciliationCheckpoint.provider_account_id,
                                IdentityProviderAccount.tenant_id == IdentityReconciliationCheckpoint.tenant_id,
                                IdentityProviderAccount.provider == IdentityReconciliationCheckpoint.provider,
                            ),
                        )
                        .where(IdentityReconciliationCheckpoint.tenant_id == tenant_id)
                        .order_by(
                            IdentityReconciliationCheckpoint.provider,
                            IdentityReconciliationCheckpoint.id,
                        )
                    )
                ).all()
                return tuple(
                    ReconciliationAdminSnapshot(
                        account_ref=_admin_account_ref(
                            checkpoint.tenant_id,
                            checkpoint.provider_account_id,
                        ),
                        provider=checkpoint.provider,
                        health_state=health_state,
                        cycle_active=checkpoint.cycle_started_at is not None,
                        lease_active=(checkpoint.lease_owner is not None and checkpoint.lease_until is not None and checkpoint.lease_until > now),
                        last_completed_at=checkpoint.last_completed_at,
                        last_success_at=checkpoint.last_success_at,
                        next_run_at=checkpoint.next_run_at,
                        processed_count=checkpoint.processed_count,
                        tightened_count=checkpoint.tightened_count,
                        error_count=checkpoint.error_count,
                        consecutive_failures=checkpoint.consecutive_failures,
                        safe_error_code=checkpoint.safe_error_code,
                    )
                    for checkpoint, health_state in rows
                )
        except SQLAlchemyError:
            raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE) from None

    async def _candidate_checkpoint_ids(self) -> tuple[str, ...]:
        async with self._session_factory() as session:
            now = await _database_now(session)
            return tuple(
                await session.scalars(
                    select(IdentityReconciliationCheckpoint.id)
                    .join(
                        IdentityProviderAccount,
                        IdentityProviderAccount.id == IdentityReconciliationCheckpoint.provider_account_id,
                    )
                    .join(
                        Tenant,
                        Tenant.id == IdentityProviderAccount.tenant_id,
                    )
                    .where(
                        or_(
                            and_(
                                Tenant.status == StatusEnum.VALID.value,
                                or_(
                                    and_(
                                        IdentityReconciliationCheckpoint.lease_owner.is_(None),
                                        IdentityReconciliationCheckpoint.next_run_at <= now,
                                    ),
                                    and_(
                                        IdentityReconciliationCheckpoint.lease_owner.is_not(None),
                                        IdentityReconciliationCheckpoint.lease_until <= now,
                                    ),
                                ),
                            ),
                            and_(
                                Tenant.status != StatusEnum.VALID.value,
                                or_(
                                    IdentityReconciliationCheckpoint.cycle_started_at.is_not(None),
                                    IdentityReconciliationCheckpoint.lease_owner.is_not(None),
                                ),
                            ),
                        )
                    )
                    .order_by(
                        IdentityReconciliationCheckpoint.next_run_at,
                        IdentityReconciliationCheckpoint.id,
                    )
                    .limit(_CLAIM_SEARCH_LIMIT)
                )
            )

    async def _claim_checkpoint(
        self,
        *,
        checkpoint_id: str,
        owner: str,
        lease_seconds: int,
        active_since: datetime,
        cycle_interval_seconds: int,
    ) -> ReconciliationLease | None:
        async with self._session_factory.begin() as session:
            tenant_id = await session.scalar(
                select(IdentityReconciliationCheckpoint.tenant_id).where(
                    IdentityReconciliationCheckpoint.id == checkpoint_id,
                )
            )
            if tenant_id is None:
                return None
            tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update(skip_locked=True))
            if tenant is None:
                return None
            account = await session.scalar(
                select(IdentityProviderAccount)
                .join(
                    IdentityReconciliationCheckpoint,
                    IdentityReconciliationCheckpoint.provider_account_id == IdentityProviderAccount.id,
                )
                .where(IdentityReconciliationCheckpoint.id == checkpoint_id)
                .with_for_update(of=IdentityProviderAccount, skip_locked=True)
            )
            if account is None:
                return None
            checkpoint = await session.scalar(select(IdentityReconciliationCheckpoint).where(IdentityReconciliationCheckpoint.id == checkpoint_id).with_for_update(skip_locked=True))
            eligibility_now = await _database_now(session)
            if account is None or checkpoint is None:
                return None
            if tenant.status != StatusEnum.VALID.value:
                _complete_cycle(
                    account,
                    checkpoint,
                    now=eligibility_now,
                    cycle_interval_seconds=cycle_interval_seconds,
                    allow_health_recovery=False,
                )
                return None
            if checkpoint.lease_owner is not None:
                if checkpoint.lease_until is None or checkpoint.lease_until > eligibility_now:
                    return None
            elif checkpoint.next_run_at > eligibility_now:
                return None
            if account.identity_health_state not in _RECONCILABLE_HEALTH:
                _complete_cycle(
                    account,
                    checkpoint,
                    now=eligibility_now,
                    cycle_interval_seconds=cycle_interval_seconds,
                    allow_health_recovery=False,
                )
                return None
            if checkpoint.cycle_started_at is None:
                checkpoint.cycle_started_at = eligibility_now
                checkpoint.cursor_identity_id = None
                checkpoint.processed_count = 0
                checkpoint.tightened_count = 0
                checkpoint.error_count = 0
                checkpoint.safe_error_code = None

            target_hint = await session.execute(
                select(
                    IdentityReconciliationTarget.id,
                    IdentityReconciliationTarget.external_identity_id,
                    IdentityReconciliationTarget.next_attempt_at,
                )
                .where(
                    IdentityReconciliationTarget.checkpoint_id == checkpoint.id,
                    IdentityReconciliationTarget.state == "pending",
                    IdentityReconciliationTarget.next_attempt_at <= eligibility_now,
                )
                .order_by(
                    IdentityReconciliationTarget.next_attempt_at,
                    IdentityReconciliationTarget.id,
                )
                .limit(1)
            )
            target_row = target_hint.one_or_none()
            target: IdentityReconciliationTarget | None = None
            identity: ExternalIdentity | None = None
            if target_row is not None:
                target_id, identity_id, next_attempt_at = target_row
                # The due predicate above is authoritative; retaining the
                # value here keeps malformed database rows fail closed.
                if next_attempt_at > eligibility_now:
                    return None
                identity = await session.scalar(select(ExternalIdentity).where(ExternalIdentity.id == identity_id).with_for_update())
                target = await session.scalar(select(IdentityReconciliationTarget).where(IdentityReconciliationTarget.id == target_id).with_for_update())
                if identity is not None and target is not None and (identity.last_seen_at is None or identity.last_seen_at < active_since):
                    target.state = "completed"
                    target.next_attempt_at = eligibility_now
                    target.safe_error_code = None
                    checkpoint.cursor_identity_id = _max_cursor(
                        checkpoint.cursor_identity_id,
                        identity.id,
                    )
                    checkpoint.next_run_at = eligibility_now
                    _clear_lease(checkpoint)
                    return None
                has_alias = identity is not None and await _has_account_alias(session, identity, account)
                has_membership = identity is not None and await _has_active_membership(
                    session,
                    identity,
                )
                if (
                    identity is None
                    or target is None
                    or not _identity_belongs_to_account(identity, account)
                    or not has_alias
                    or not has_membership
                    or identity.state != ExternalIdentityState.ACTIVE.value
                    or identity.subject_type != "user_id"
                ):
                    if target is not None:
                        target.state = "failed"
                        target.safe_error_code = ReconciliationErrorCode.FENCE_CONFLICT.value
                    if identity is not None:
                        checkpoint.cursor_identity_id = identity.id
                    checkpoint.error_count += 1
                    checkpoint.next_run_at = eligibility_now
                    _clear_lease(checkpoint)
                    return None
                if target.account_revision != account.identity_revision or target.account_scope_change_at != account.last_scope_change_at or target.identity_revision != identity.identity_revision:
                    target.not_found_count = 0
                    target.first_not_found_at = None
                    target.last_not_found_at = None
                    target.last_observed_at = None

            if target is None:
                identity = await session.scalar(
                    select(ExternalIdentity)
                    .where(
                        ExternalIdentity.tenant_id == account.tenant_id,
                        ExternalIdentity.provider == account.provider,
                        ExternalIdentity.provider_tenant_key == account.provider_tenant_key,
                        ExternalIdentity.subject_type == "user_id",
                        ExternalIdentity.state == ExternalIdentityState.ACTIVE.value,
                        ExternalIdentity.last_seen_at.is_not(None),
                        ExternalIdentity.last_seen_at >= active_since,
                        (sa.true() if checkpoint.cursor_identity_id is None else ExternalIdentity.id > checkpoint.cursor_identity_id),
                        exists(
                            select(ExternalIdentityAlias.id).where(
                                ExternalIdentityAlias.external_identity_id == ExternalIdentity.id,
                                ExternalIdentityAlias.tenant_id == account.tenant_id,
                                ExternalIdentityAlias.provider == account.provider,
                                ExternalIdentityAlias.provider_tenant_key == account.provider_tenant_key,
                                ExternalIdentityAlias.provider_account_key == account.provider_account_key,
                            )
                        ),
                        exists(
                            select(User.id)
                            .join(
                                UserTenant,
                                and_(
                                    UserTenant.user_id == User.id,
                                    UserTenant.tenant_id == ExternalIdentity.tenant_id,
                                    UserTenant.status == StatusEnum.VALID.value,
                                    UserTenant.role.in_({"owner", "admin", "normal"}),
                                ),
                            )
                            .where(
                                User.id == ExternalIdentity.user_id,
                                User.status == StatusEnum.VALID.value,
                                User.is_active.is_(True),
                                User.is_anonymous.is_(False),
                            )
                        ),
                        ~exists(
                            select(IdentityReconciliationTarget.id).where(
                                IdentityReconciliationTarget.provider_account_id == account.id,
                                IdentityReconciliationTarget.external_identity_id == ExternalIdentity.id,
                                IdentityReconciliationTarget.state == "pending",
                            )
                        ),
                    )
                    .order_by(ExternalIdentity.id)
                    .limit(1)
                    .with_for_update()
                )
                if identity is None:
                    _complete_cycle(
                        account,
                        checkpoint,
                        now=eligibility_now,
                        cycle_interval_seconds=cycle_interval_seconds,
                        pending_attempt_at=await _next_pending_attempt_at(
                            session,
                            checkpoint.id,
                        ),
                    )
                    return None
                target = await session.scalar(
                    select(IdentityReconciliationTarget)
                    .where(
                        IdentityReconciliationTarget.provider_account_id == account.id,
                        IdentityReconciliationTarget.external_identity_id == identity.id,
                    )
                    .with_for_update()
                )
                if target is None:
                    target = IdentityReconciliationTarget(
                        id=uuid.uuid4().hex,
                        checkpoint_id=checkpoint.id,
                        provider_account_id=account.id,
                        tenant_id=account.tenant_id,
                        provider=account.provider,
                        provider_tenant_key=account.provider_tenant_key,
                        external_identity_id=identity.id,
                        account_revision=account.identity_revision,
                        account_scope_change_at=account.last_scope_change_at,
                        identity_revision=identity.identity_revision,
                        state="pending",
                        next_attempt_at=eligibility_now,
                        not_found_count=0,
                        last_activity_at=identity.last_seen_at,
                    )
                    session.add(target)
                else:
                    target.state = "pending"
                    target.next_attempt_at = eligibility_now
                    target.last_outcome = None
                    target.not_found_count = 0
                    target.first_not_found_at = None
                    target.last_not_found_at = None
                    target.last_observed_at = None
                    target.safe_error_code = None
                    target.last_activity_at = identity.last_seen_at

            if identity is None:
                raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE)
            target.account_revision = account.identity_revision
            target.account_scope_change_at = account.last_scope_change_at
            target.identity_revision = identity.identity_revision
            # Re-read the database wall clock after the identity and target
            # locks: the returned lease must start after lock wait, not before.
            lease_now = await _database_now(session)
            target.next_attempt_at = lease_now
            checkpoint.lease_owner = owner
            checkpoint.lease_attempt += 1
            checkpoint.lease_until = lease_now + timedelta(seconds=lease_seconds)
            checkpoint.next_run_at = lease_now
            await session.flush()
            return ReconciliationLease(
                checkpoint_id=checkpoint.id,
                target_id=target.id,
                owner=owner,
                attempt=checkpoint.lease_attempt,
                context=_provider_context(account),
                external_identity_id=identity.id,
                subject_value=identity.subject_value,
                identity_revision=identity.identity_revision,
                consecutive_failures=checkpoint.consecutive_failures,
                lease_until=checkpoint.lease_until,
            )

    async def _checkpoint_scope(
        self,
        checkpoint_id: str,
    ) -> tuple[str, str] | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        IdentityReconciliationCheckpoint.provider_account_id,
                        IdentityReconciliationCheckpoint.tenant_id,
                    ).where(IdentityReconciliationCheckpoint.id == checkpoint_id)
                )
            ).one_or_none()
            if row is None:
                return None
            return row.provider_account_id, row.tenant_id


def _apply_observation(
    *,
    account: IdentityProviderAccount,
    checkpoint: IdentityReconciliationCheckpoint,
    target: IdentityReconciliationTarget,
    identity: ExternalIdentity,
    observation: ReconciliationObservation,
    now: datetime,
    unavailable_delay_seconds: int,
    not_found_confirmation_seconds: int,
    cycle_interval_seconds: int,
    degrade_after_failures: int,
    max_tighten_per_cycle: int,
) -> ReconciliationApplyResult:
    status = observation.status
    # Durable ordering and confirmation windows use the database clock.  The
    # API-process observation clock may differ across replicas.
    target.last_observed_at = now
    target.last_outcome = status.value

    if status is ReconciliationProviderStatus.UNAVAILABLE:
        code = observation.safe_error_code or ReconciliationErrorCode.PROVIDER_UNAVAILABLE.value
        _record_failure(
            account,
            checkpoint,
            target,
            code=code,
            degrade_after_failures=degrade_after_failures,
        )
        retry_at = now + timedelta(seconds=unavailable_delay_seconds)
        target.state = "pending"
        target.next_attempt_at = retry_at
        checkpoint.next_run_at = retry_at
        _clear_lease(checkpoint)
        return ReconciliationApplyResult(
            outcome=ReconciliationApplyOutcome.RETRY_SCHEDULED,
            identity_changed=False,
            account_revision=account.identity_revision,
            next_attempt_at=retry_at,
            safe_error_code=code,
        )

    if status is ReconciliationProviderStatus.INVALID:
        code = observation.safe_error_code or ReconciliationErrorCode.PROVIDER_RESULT_INVALID.value
        _record_failure(
            account,
            checkpoint,
            target,
            code=code,
            degrade_after_failures=degrade_after_failures,
        )
        _finish_target(
            checkpoint,
            target,
            identity_id=identity.id,
            now=now,
            failed=True,
            error_code=code,
        )
        retry_at = now + timedelta(seconds=unavailable_delay_seconds)
        checkpoint.next_run_at = retry_at
        return ReconciliationApplyResult(
            outcome=ReconciliationApplyOutcome.TARGET_REJECTED,
            identity_changed=False,
            account_revision=account.identity_revision,
            next_attempt_at=retry_at,
            safe_error_code=code,
        )

    target.last_success_at = now
    checkpoint.last_success_at = now

    if status is ReconciliationProviderStatus.NOT_FOUND:
        if target.not_found_count == 0:
            _record_success(account, checkpoint, target)
            target.not_found_count = 1
            target.first_not_found_at = now
            target.last_not_found_at = now
            retry_at = now + timedelta(seconds=not_found_confirmation_seconds)
            target.state = "pending"
            target.next_attempt_at = retry_at
            _advance_past_pending_target(
                checkpoint,
                identity_id=identity.id,
                now=now,
            )
            return ReconciliationApplyResult(
                outcome=ReconciliationApplyOutcome.CONFIRMATION_PENDING,
                identity_changed=False,
                account_revision=account.identity_revision,
                next_attempt_at=retry_at,
            )
        last_not_found_at = target.last_not_found_at
        if last_not_found_at is None or now < last_not_found_at + timedelta(
            seconds=not_found_confirmation_seconds,
        ):
            _record_success(account, checkpoint, target)
            retry_at = max(
                now,
                (last_not_found_at or now) + timedelta(seconds=not_found_confirmation_seconds),
            )
            target.state = "pending"
            target.next_attempt_at = retry_at
            _advance_past_pending_target(
                checkpoint,
                identity_id=identity.id,
                now=now,
            )
            return ReconciliationApplyResult(
                outcome=ReconciliationApplyOutcome.CONFIRMATION_PENDING,
                identity_changed=False,
                account_revision=account.identity_revision,
                next_attempt_at=retry_at,
            )
        target.not_found_count += 1
        target.last_not_found_at = now

    would_tighten = status in _TIGHTENING_STATUSES or (status is ReconciliationProviderStatus.NOT_FOUND and _negative_is_current(identity, now))
    if would_tighten and checkpoint.tightened_count >= max_tighten_per_cycle:
        code = ReconciliationErrorCode.TIGHTEN_CIRCUIT_OPEN.value
        checkpoint.error_count += 1
        checkpoint.processed_count += 1
        checkpoint.consecutive_failures += 1
        checkpoint.safe_error_code = code
        target.state = "failed"
        target.safe_error_code = code
        target.next_attempt_at = now
        _set_account_health(account, IdentityProviderHealthState.DEGRADED)
        target.account_revision = account.identity_revision
        _complete_cycle(
            account,
            checkpoint,
            now=now,
            cycle_interval_seconds=cycle_interval_seconds,
            preserve_counts=True,
        )
        return ReconciliationApplyResult(
            outcome=ReconciliationApplyOutcome.CIRCUIT_OPEN,
            identity_changed=False,
            account_revision=account.identity_revision,
            next_attempt_at=checkpoint.next_run_at,
            safe_error_code=code,
        )

    if status is not ReconciliationProviderStatus.CONFLICT:
        _record_success(account, checkpoint, target)

    identity_changed = False
    failed = False
    error_code: str | None = None
    if status is ReconciliationProviderStatus.RESOLVED:
        proof_at = observation.verified_at
        if proof_at is None:
            raise ReconciliationRepositoryError(ReconciliationErrorCode.PROVIDER_RESULT_INVALID)
        updates_target_proof = target.verified_at is None or proof_at >= target.verified_at
        if updates_target_proof:
            target.verified_at = proof_at
        if identity.verified_at is None or proof_at > identity.verified_at:
            identity.verified_at = proof_at
            identity.identity_revision += 1
            identity_changed = True
        if updates_target_proof:
            target.proof_account_revision = account.identity_revision
            target.proof_scope_change_at = account.last_scope_change_at
            target.proof_identity_revision = identity.identity_revision
        target.not_found_count = 0
        target.first_not_found_at = None
        target.last_not_found_at = None
    elif status is ReconciliationProviderStatus.INACTIVE:
        if _negative_is_current(identity, now):
            identity.state = ExternalIdentityState.INACTIVE.value
            identity.identity_revision += 1
            checkpoint.tightened_count += 1
            identity_changed = True
    elif status is ReconciliationProviderStatus.NOT_FOUND:
        if _negative_is_current(identity, now):
            identity.state = ExternalIdentityState.INACTIVE.value
            identity.identity_revision += 1
            checkpoint.tightened_count += 1
            identity_changed = True
    elif status is ReconciliationProviderStatus.NOT_IN_SCOPE:
        # Scope is account-specific.  Never tighten the canonical identity,
        # which may remain valid through another provider account alias.
        pass
    elif status is ReconciliationProviderStatus.CONFLICT:
        identity.state = ExternalIdentityState.CONFLICT.value
        identity.identity_revision += 1
        checkpoint.tightened_count += 1
        checkpoint.error_count += 1
        checkpoint.consecutive_failures += 1
        error_code = ReconciliationErrorCode.PROVIDER_CONFLICT.value
        checkpoint.safe_error_code = error_code
        target.safe_error_code = error_code
        failed = True
        identity_changed = True
        if checkpoint.consecutive_failures >= degrade_after_failures:
            _set_account_health(account, IdentityProviderHealthState.DEGRADED)

    target.identity_revision = identity.identity_revision
    target.account_revision = account.identity_revision
    _finish_target(
        checkpoint,
        target,
        identity_id=identity.id,
        now=now,
        failed=failed,
        error_code=error_code,
    )
    return ReconciliationApplyResult(
        outcome=ReconciliationApplyOutcome.APPLIED,
        identity_changed=identity_changed,
        account_revision=account.identity_revision,
        next_attempt_at=checkpoint.next_run_at,
        safe_error_code=error_code,
    )


def _record_failure(
    account: IdentityProviderAccount,
    checkpoint: IdentityReconciliationCheckpoint,
    target: IdentityReconciliationTarget,
    *,
    code: str,
    degrade_after_failures: int,
) -> None:
    checkpoint.error_count += 1
    checkpoint.consecutive_failures += 1
    checkpoint.safe_error_code = code
    target.safe_error_code = code
    if checkpoint.consecutive_failures >= degrade_after_failures:
        _set_account_health(account, IdentityProviderHealthState.DEGRADED)
    target.account_revision = account.identity_revision


def _record_success(
    account: IdentityProviderAccount,
    checkpoint: IdentityReconciliationCheckpoint,
    target: IdentityReconciliationTarget,
) -> None:
    checkpoint.consecutive_failures = 0
    checkpoint.safe_error_code = None
    target.safe_error_code = None
    target.account_revision = account.identity_revision


def _finish_target(
    checkpoint: IdentityReconciliationCheckpoint,
    target: IdentityReconciliationTarget | None,
    *,
    identity_id: str,
    now: datetime,
    failed: bool,
    error_code: str | None,
) -> None:
    checkpoint.cursor_identity_id = _max_cursor(
        checkpoint.cursor_identity_id,
        identity_id,
    )
    checkpoint.processed_count += 1
    checkpoint.next_run_at = now
    _clear_lease(checkpoint)
    if target is not None:
        target.state = "failed" if failed else "completed"
        target.next_attempt_at = now
        target.safe_error_code = error_code


def _reject_fence(
    checkpoint: IdentityReconciliationCheckpoint,
    target: IdentityReconciliationTarget | None,
    *,
    now: datetime,
) -> None:
    checkpoint.error_count += 1
    checkpoint.safe_error_code = ReconciliationErrorCode.FENCE_CONFLICT.value
    checkpoint.cycle_started_at = None
    checkpoint.cursor_identity_id = None
    checkpoint.next_run_at = now
    _clear_lease(checkpoint)
    if target is not None:
        target.state = "failed"
        target.next_attempt_at = now
        target.safe_error_code = ReconciliationErrorCode.FENCE_CONFLICT.value


def _complete_cycle(
    account: IdentityProviderAccount,
    checkpoint: IdentityReconciliationCheckpoint,
    *,
    now: datetime,
    cycle_interval_seconds: int,
    preserve_counts: bool = True,
    pending_attempt_at: datetime | None = None,
    allow_health_recovery: bool = True,
) -> None:
    if allow_health_recovery and checkpoint.processed_count > 0 and checkpoint.error_count == 0 and checkpoint.consecutive_failures == 0:
        _set_account_health(account, IdentityProviderHealthState.HEALTHY)
    checkpoint.cycle_started_at = None
    checkpoint.cursor_identity_id = None
    checkpoint.last_completed_at = now
    cycle_due_at = now + timedelta(seconds=cycle_interval_seconds)
    checkpoint.next_run_at = min(cycle_due_at, pending_attempt_at) if pending_attempt_at is not None else cycle_due_at
    _clear_lease(checkpoint)
    if not preserve_counts:
        checkpoint.processed_count = 0
        checkpoint.tightened_count = 0
        checkpoint.error_count = 0


def _clear_lease(checkpoint: IdentityReconciliationCheckpoint) -> None:
    checkpoint.lease_owner = None
    checkpoint.lease_until = None


def _advance_past_pending_target(
    checkpoint: IdentityReconciliationCheckpoint,
    *,
    identity_id: str,
    now: datetime,
) -> None:
    """Keep a confirmation target while letting this account scan continue."""

    checkpoint.cursor_identity_id = _max_cursor(
        checkpoint.cursor_identity_id,
        identity_id,
    )
    checkpoint.processed_count += 1
    checkpoint.next_run_at = now
    _clear_lease(checkpoint)


def _max_cursor(current: str | None, candidate: str) -> str:
    if current is None or candidate > current:
        return candidate
    return current


def _set_account_health(
    account: IdentityProviderAccount,
    desired: IdentityProviderHealthState,
) -> bool:
    if account.identity_health_state == desired.value:
        return False
    account.identity_health_state = desired.value
    account.identity_health_error_code = None
    account.identity_revision += 1
    return True


def _negative_is_current(identity: ExternalIdentity, observed_at: datetime) -> bool:
    return identity.verified_at is None or observed_at >= identity.verified_at


def _identity_belongs_to_account(
    identity: ExternalIdentity,
    account: IdentityProviderAccount,
) -> bool:
    return identity.tenant_id == account.tenant_id and identity.provider == account.provider and identity.provider_tenant_key == account.provider_tenant_key


async def _has_account_alias(
    session: AsyncSession,
    identity: ExternalIdentity,
    account: IdentityProviderAccount,
) -> bool:
    value = await session.scalar(
        select(
            exists(
                select(ExternalIdentityAlias.id).where(
                    ExternalIdentityAlias.external_identity_id == identity.id,
                    ExternalIdentityAlias.tenant_id == account.tenant_id,
                    ExternalIdentityAlias.provider == account.provider,
                    ExternalIdentityAlias.provider_tenant_key == account.provider_tenant_key,
                    ExternalIdentityAlias.provider_account_key == account.provider_account_key,
                )
            )
        )
    )
    return value is True


async def _has_active_membership(
    session: AsyncSession,
    identity: ExternalIdentity,
) -> bool:
    value = await session.scalar(
        select(
            exists(
                select(User.id)
                .join(
                    UserTenant,
                    and_(
                        UserTenant.user_id == User.id,
                        UserTenant.tenant_id == identity.tenant_id,
                        UserTenant.status == StatusEnum.VALID.value,
                        UserTenant.role.in_({"owner", "admin", "normal"}),
                    ),
                )
                .where(
                    User.id == identity.user_id,
                    User.status == StatusEnum.VALID.value,
                    User.is_active.is_(True),
                    User.is_anonymous.is_(False),
                )
            )
        )
    )
    return value is True


async def _next_pending_attempt_at(
    session: AsyncSession,
    checkpoint_id: str,
) -> datetime | None:
    return await session.scalar(
        select(func.min(IdentityReconciliationTarget.next_attempt_at)).where(
            IdentityReconciliationTarget.checkpoint_id == checkpoint_id,
            IdentityReconciliationTarget.state == "pending",
        )
    )


def _lease_matches(
    checkpoint: IdentityReconciliationCheckpoint,
    lease: ReconciliationLease,
    *,
    now: datetime,
) -> bool:
    return (
        checkpoint.lease_owner == lease.owner
        and checkpoint.lease_attempt == lease.attempt
        and checkpoint.lease_until is not None
        and checkpoint.lease_until == lease.lease_until
        and checkpoint.lease_until > now
    )


def _account_fence_matches(
    account: IdentityProviderAccount,
    context: ProviderContext,
) -> bool:
    return (
        account.id == context.provider_account_id
        and account.tenant_id == context.tenant_id
        and account.provider == context.provider
        and account.provider_tenant_key == context.provider_tenant_key
        and account.provider_account_key == context.provider_account_key
        and account.identity_revision == context.provider_account_revision
        and account.last_scope_change_at == context.provider_account_last_scope_change_at
    )


def _target_fence_matches(
    target: IdentityReconciliationTarget,
    lease: ReconciliationLease,
) -> bool:
    return (
        target.id == lease.target_id
        and target.state == "pending"
        and target.external_identity_id == lease.external_identity_id
        and target.account_revision == lease.context.provider_account_revision
        and target.account_scope_change_at == lease.context.provider_account_last_scope_change_at
        and target.identity_revision == lease.identity_revision
    )


def _provider_context(account: IdentityProviderAccount) -> ProviderContext:
    return ProviderContext(
        tenant_id=account.tenant_id,
        provider=account.provider,
        provider_tenant_key=account.provider_tenant_key,
        provider_account_id=account.id,
        provider_account_key=account.provider_account_key,
        provider_account_revision=account.identity_revision,
        provider_account_last_scope_change_at=account.last_scope_change_at,
    )


def _fence_result(account_revision: int) -> ReconciliationApplyResult:
    return ReconciliationApplyResult(
        outcome=ReconciliationApplyOutcome.FENCE_REJECTED,
        identity_changed=False,
        account_revision=account_revision,
        safe_error_code=ReconciliationErrorCode.FENCE_CONFLICT.value,
    )


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReconciliationRepositoryError(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE)
    return value.astimezone(UTC)


def _validate_owner(owner: str) -> None:
    if not isinstance(owner, str) or not owner or owner != owner.strip() or len(owner) > 64:
        raise ValueError("reconciliation lease owner is invalid")


def _admin_account_ref(tenant_id: str, provider_account_id: str) -> str:
    """Return a stable one-way diagnostic reference, never an authority key."""

    payload = (f"multirag.identity-reconciliation.admin-ref:v1\0{tenant_id}\0{provider_account_id}").encode()
    return hashlib.sha256(payload).hexdigest()[:32]


def _require_positive(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = ["SqlAlchemyIdentityReconciliationRepository"]
