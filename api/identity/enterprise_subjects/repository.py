"""PostgreSQL persistence for authoritative enterprise-subject resolutions."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db import EnterpriseSubjectState, UserTenantRole
from api.db.db_models import EnterpriseSubjectLink, Tenant, User, UserTenant
from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectErrorCode,
    EnterpriseSubjectRepositoryError,
    EnterpriseSubjectResolution,
    EnterpriseSubjectResolutionStatus,
    VerifiedEnterpriseSubjectCommand,
)
from api.identity.principal import EnterpriseSubject
from common.constants import StatusEnum

_MEMBER_ROLES = frozenset(
    {
        UserTenantRole.OWNER.value,
        UserTenantRole.ADMIN.value,
        UserTenantRole.NORMAL.value,
    }
)


class SqlAlchemyEnterpriseSubjectRepository:
    """Apply each resolver result in its own async transaction.

    PostgreSQL's uniqueness constraints remain the final authority.  Advisory
    locks serialize repository writers by both resolver slot and subject value,
    while row locks protect transitions of links that already exist.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def persist_resolution(
        self,
        command: VerifiedEnterpriseSubjectCommand,
    ) -> EnterpriseSubjectResolution:
        if not isinstance(command, VerifiedEnterpriseSubjectCommand):
            raise EnterpriseSubjectRepositoryError(
                EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
            )
        if command.resolution.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE:
            # An unavailable authority must never weaken or refresh durable
            # evidence.  Keep this path free of even an implicit transaction.
            return command.resolution

        try:
            return await self._apply(command)
        except EnterpriseSubjectRepositoryError:
            raise
        except IntegrityError:
            # A writer outside this repository may not honor our advisory-lock
            # namespace.  The failed transaction has rolled back; retry once in
            # a fresh transaction so the committed unique-key occupant can be
            # read back or quarantined without exposing driver details.
            try:
                return await self._apply(command)
            except EnterpriseSubjectRepositoryError:
                raise
            except SQLAlchemyError:
                raise EnterpriseSubjectRepositoryError(
                    EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE,
                ) from None
        except SQLAlchemyError:
            raise EnterpriseSubjectRepositoryError(
                EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def _apply(
        self,
        command: VerifiedEnterpriseSubjectCommand,
    ) -> EnterpriseSubjectResolution:
        async with self._session_factory.begin() as session:
            await _require_exact_active_scope(session, command)
            await _lock_command(session, command)
            proof_at = command.resolution.verified_at
            if proof_at is None:
                raise EnterpriseSubjectRepositoryError(
                    EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
                )

            if command.resolution.status is EnterpriseSubjectResolutionStatus.RESOLVED:
                return await _persist_resolved(session, command)
            return await _persist_negative(session, command)


async def _persist_resolved(
    session: AsyncSession,
    command: VerifiedEnterpriseSubjectCommand,
) -> EnterpriseSubjectResolution:
    resolution = command.resolution
    subject = resolution.subject
    if subject is None or resolution.verified_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )

    slot_row = await _slot_for_update(session, command)
    subject_row = await session.scalar(
        sa.select(EnterpriseSubjectLink)
        .where(
            EnterpriseSubjectLink.tenant_id == command.tenant_id,
            EnterpriseSubjectLink.subject_type == command.slot.subject_type,
            EnterpriseSubjectLink.subject_value == subject.subject,
        )
        .with_for_update()
    )
    # Read wall time only after every potentially blocking advisory/row lock.
    if resolution.verified_at > await _database_now(session):
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )

    conflicting_rows = _conflicting_rows(
        slot_row=slot_row,
        subject_row=subject_row,
        desired_subject=subject.subject,
    )
    if conflicting_rows:
        rows_to_quarantine = tuple(row for row in conflicting_rows if _proof_is_not_older(row, resolution.verified_at))
        for row in rows_to_quarantine:
            row.state = EnterpriseSubjectState.CONFLICT.value
        if rows_to_quarantine:
            await session.flush()
            return _ambiguous_result(conflicting_rows, fallback=resolution)
        # A stale subject-change result cannot quarantine or replace a newer
        # row, but this resolution still cannot emit the mismatched subject.
        return _ambiguous_result(conflicting_rows, fallback=resolution)

    if slot_row is None:
        row = EnterpriseSubjectLink(
            id=uuid.uuid4().hex,
            tenant_id=command.tenant_id,
            user_id=command.platform_user_id,
            subject_type=command.slot.subject_type,
            subject_value=subject.subject,
            issuer=command.slot.issuer,
            issuer_tenant=command.slot.issuer_tenant,
            state=EnterpriseSubjectState.ACTIVE.value,
            verified_at=resolution.verified_at,
            source_revision=resolution.source_revision,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return _resolution_from_row(row, fallback=resolution)

    row = slot_row
    if row.state == EnterpriseSubjectState.CONFLICT.value:
        if _proof_is_newer(row, resolution.verified_at):
            _advance_proof(row, resolution)
            await session.flush()
            await session.refresh(row)
        return _resolution_from_row(row, fallback=resolution)
    if row.state not in {
        EnterpriseSubjectState.ACTIVE.value,
        EnterpriseSubjectState.INACTIVE.value,
    }:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )

    if row.state == EnterpriseSubjectState.INACTIVE.value:
        # I5 has no controlled recovery transition.  A newer positive proof is
        # still insufficient to reactivate a durably inactive slot.
        return _resolution_from_row(row, fallback=resolution)
    if _proof_is_newer(row, resolution.verified_at):
        _advance_proof(row, resolution)

    await session.flush()
    await session.refresh(row)
    return _resolution_from_row(row, fallback=resolution)


async def _persist_negative(
    session: AsyncSession,
    command: VerifiedEnterpriseSubjectCommand,
) -> EnterpriseSubjectResolution:
    resolution = command.resolution
    proof_at = resolution.verified_at
    if proof_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    row = await _slot_for_update(session, command)
    # PostgreSQL CURRENT_TIMESTAMP is transaction-start time; clock_timestamp
    # closes the future-proof window after a blocking row lock.
    if proof_at > await _database_now(session):
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    if row is None:
        return resolution

    if resolution.status is EnterpriseSubjectResolutionStatus.AMBIGUOUS:
        if _proof_is_not_older(row, proof_at):
            row.state = EnterpriseSubjectState.CONFLICT.value
            if _proof_is_newer(row, proof_at):
                _advance_proof(row, resolution)
    elif resolution.status in {
        EnterpriseSubjectResolutionStatus.NOT_FOUND,
        EnterpriseSubjectResolutionStatus.INACTIVE,
    }:
        if row.state != EnterpriseSubjectState.CONFLICT.value and _proof_is_not_older(row, proof_at):
            row.state = EnterpriseSubjectState.INACTIVE.value
            if _proof_is_newer(row, proof_at):
                _advance_proof(row, resolution)
    else:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )

    await session.flush()
    await session.refresh(row)
    # Physical storage has only inactive/conflict states, while resolver
    # outcomes distinguish NOT_FOUND from INACTIVE.  Preserve that externally
    # meaningful five-state result after applying its fail-closed transition.
    return resolution


def _conflicting_rows(
    *,
    slot_row: EnterpriseSubjectLink | None,
    subject_row: EnterpriseSubjectLink | None,
    desired_subject: str,
) -> tuple[EnterpriseSubjectLink, ...]:
    rows: list[EnterpriseSubjectLink] = []
    if slot_row is not None and slot_row.subject_value != desired_subject:
        rows.append(slot_row)
    if subject_row is not None and (slot_row is None or subject_row.id != slot_row.id):
        rows.append(subject_row)
    return tuple(rows)


async def _slot_for_update(
    session: AsyncSession,
    command: VerifiedEnterpriseSubjectCommand,
) -> EnterpriseSubjectLink | None:
    return await session.scalar(
        sa.select(EnterpriseSubjectLink)
        .where(
            EnterpriseSubjectLink.tenant_id == command.tenant_id,
            EnterpriseSubjectLink.user_id == command.platform_user_id,
            EnterpriseSubjectLink.subject_type == command.slot.subject_type,
            EnterpriseSubjectLink.issuer == command.slot.issuer,
            EnterpriseSubjectLink.issuer_tenant == command.slot.issuer_tenant,
        )
        .with_for_update()
    )


async def _require_exact_active_scope(
    session: AsyncSession,
    command: VerifiedEnterpriseSubjectCommand,
) -> None:
    rows = (
        await session.execute(
            sa.select(User.id, UserTenant.id)
            .join(
                UserTenant,
                sa.and_(
                    UserTenant.user_id == User.id,
                    UserTenant.tenant_id == command.tenant_id,
                    UserTenant.status == StatusEnum.VALID.value,
                    UserTenant.role.in_(_MEMBER_ROLES),
                ),
            )
            .join(
                Tenant,
                sa.and_(
                    Tenant.id == command.tenant_id,
                    Tenant.status == StatusEnum.VALID.value,
                ),
            )
            .where(
                User.id == command.platform_user_id,
                User.status == StatusEnum.VALID.value,
                User.is_active.is_(True),
                User.is_anonymous.is_(False),
            )
            .with_for_update(
                read=True,
                of=(Tenant, User, UserTenant),
            )
        )
    ).all()
    if len(rows) != 1:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )


async def _lock_command(
    session: AsyncSession,
    command: VerifiedEnterpriseSubjectCommand,
) -> None:
    resources = [
        _advisory_key(
            "slot",
            command.tenant_id,
            command.platform_user_id,
            command.slot.subject_type,
            command.slot.issuer,
            command.slot.issuer_tenant,
        )
    ]
    subject = command.resolution.subject
    if subject is not None:
        resources.append(
            _advisory_key(
                "subject",
                command.tenant_id,
                command.slot.subject_type,
                subject.subject,
            )
        )
    for key in sorted(set(resources)):
        await session.execute(
            sa.text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": key},
        )


def _advisory_key(domain: str, *parts: str) -> int:
    digest = hashlib.sha256(b"multirag:eim-i5:advisory:v1\x00")
    for value in (domain, *parts):
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return int.from_bytes(digest.digest()[:8], "big", signed=True)


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(sa.select(sa.func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE,
        )
    return value


def _proof_is_newer(
    row: EnterpriseSubjectLink,
    proof_at: datetime,
) -> bool:
    return row.verified_at is None or proof_at > row.verified_at


def _proof_is_not_older(
    row: EnterpriseSubjectLink,
    proof_at: datetime,
) -> bool:
    return row.verified_at is None or proof_at >= row.verified_at


def _advance_proof(
    row: EnterpriseSubjectLink,
    resolution: EnterpriseSubjectResolution,
) -> None:
    if resolution.verified_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    row.verified_at = resolution.verified_at
    if resolution.source_revision is not None:
        row.source_revision = resolution.source_revision


def _resolution_from_row(
    row: EnterpriseSubjectLink,
    *,
    fallback: EnterpriseSubjectResolution,
) -> EnterpriseSubjectResolution:
    row_verified_at = row.verified_at
    fallback_at = fallback.verified_at
    if row_verified_at is None and fallback_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    if row.state == EnterpriseSubjectState.ACTIVE.value:
        if row_verified_at is None:
            raise EnterpriseSubjectRepositoryError(
                EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
            )
        try:
            subject = EnterpriseSubject(
                subject_type=row.subject_type,
                subject=row.subject_value,
                issuer=row.issuer,
                issuer_tenant=row.issuer_tenant,
                verified_at=row_verified_at,
            )
            return EnterpriseSubjectResolution(
                status=EnterpriseSubjectResolutionStatus.RESOLVED,
                subject=subject,
                verified_at=row_verified_at,
                source_revision=row.source_revision,
            )
        except ValueError:
            raise EnterpriseSubjectRepositoryError(
                EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
            ) from None
    status = {
        EnterpriseSubjectState.INACTIVE.value: EnterpriseSubjectResolutionStatus.INACTIVE,
        EnterpriseSubjectState.CONFLICT.value: EnterpriseSubjectResolutionStatus.AMBIGUOUS,
    }.get(row.state)
    if status is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    if row_verified_at is None or (fallback_at is not None and fallback_at > row_verified_at):
        verified_at = fallback_at
        source_revision = fallback.source_revision
    else:
        verified_at = row_verified_at
        source_revision = row.source_revision
    if verified_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    try:
        return EnterpriseSubjectResolution(
            status=status,
            verified_at=verified_at,
            source_revision=source_revision,
        )
    except ValueError:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        ) from None


def _ambiguous_result(
    rows: Iterable[EnterpriseSubjectLink],
    *,
    fallback: EnterpriseSubjectResolution,
) -> EnterpriseSubjectResolution:
    candidates = tuple(rows)
    if not candidates:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    fallback_at = fallback.verified_at
    if fallback_at is None:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        )
    row = max(
        candidates,
        key=lambda candidate: candidate.verified_at or fallback_at,
    )
    row_at = row.verified_at
    if row_at is None or fallback_at > row_at:
        verified_at = fallback_at
        source_revision = fallback.source_revision
    else:
        verified_at = row_at
        source_revision = row.source_revision
    try:
        return EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.AMBIGUOUS,
            verified_at=verified_at,
            source_revision=source_revision,
        )
    except ValueError:
        raise EnterpriseSubjectRepositoryError(
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        ) from None


__all__ = ["SqlAlchemyEnterpriseSubjectRepository"]
