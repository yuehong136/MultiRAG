"""PostgreSQL unit-of-work adapter for EIM-I6 identity provisioning."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.db import (
    ExternalIdentityState,
    IdentityProviderHealthState,
    UserAccountKind,
    UserTenantRole,
)
from api.db.db_models import (
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityBindingEvent,
    IdentityLinkCode,
    IdentityProviderAccount,
    IdentityTenantPolicy,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityErrorCode,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
    ProvisioningPolicySnapshot,
    UserMembershipRecord,
)
from api.identity.provisioning_contracts import (
    LinkCodeGrantRecord,
    LinkCodeIssueCommand,
    ProvisioningOutcome,
    ProvisioningPolicyCreate,
    ProvisioningPolicyUpdate,
    ProvisioningPolicyWriteOutcome,
    ProvisioningPolicyWriteResult,
    ProvisioningRepositoryError,
    ProvisioningResult,
    ProvisioningStatus,
    VerifiedProvisioningAlias,
    VerifiedProvisioningCommand,
)
from api.identity.validation import (
    valid_alias,
    valid_alias_key,
    valid_opaque_id,
    valid_provider_context,
    valid_revision,
    valid_text,
    valid_timestamp,
)
from common.constants import StatusEnum

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_ROLES = frozenset(
    {
        UserTenantRole.OWNER.value,
        UserTenantRole.ADMIN.value,
        UserTenantRole.NORMAL.value,
    }
)
_ACTION_MODE = {
    ProvisioningAction.BIND_PREPROVISIONED: ProvisioningMode.PREPROVISIONED,
    ProvisioningAction.REQUIRE_LINK: ProvisioningMode.LINK_ONLY,
    ProvisioningAction.CREATE_NORMAL_MEMBER: ProvisioningMode.JIT,
}
_MAX_PROVIDER_PROOF_AGE = timedelta(minutes=5)


class SqlAlchemyIdentityProvisioningRepository:
    """Own a fresh async transaction for each complete I6 write use case."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_policy(
        self,
        tenant_id: str,
    ) -> ProvisioningPolicySnapshot | None:
        if not valid_opaque_id(tenant_id):
            return None
        try:
            async with self._session_factory() as session:
                model = await session.scalar(
                    select(IdentityTenantPolicy)
                    .join(
                        Tenant,
                        and_(
                            Tenant.id == IdentityTenantPolicy.tenant_id,
                            Tenant.status == StatusEnum.VALID.value,
                        ),
                    )
                    .where(IdentityTenantPolicy.tenant_id == tenant_id)
                )
                return None if model is None else _policy_snapshot(model)
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def create_policy(
        self,
        command: ProvisioningPolicyCreate,
    ) -> ProvisioningPolicyWriteResult:
        _validate_policy_create(command)
        try:
            async with self._session_factory.begin() as session:
                await _require_active_tenant(session, command.tenant_id)
                existing = await session.scalar(select(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id == command.tenant_id).with_for_update())
                if existing is not None:
                    return ProvisioningPolicyWriteResult(
                        ProvisioningPolicyWriteOutcome.EXISTS,
                        _policy_snapshot(existing),
                    )
                model = IdentityTenantPolicy(
                    id=command.tenant_id,
                    tenant_id=command.tenant_id,
                    mode=command.mode.value,
                    revision=1,
                    link_code_ttl_seconds=command.link_code_ttl_seconds,
                    changed_at=command.changed_at,
                )
                session.add(model)
                await session.flush()
                return ProvisioningPolicyWriteResult(
                    ProvisioningPolicyWriteOutcome.CREATED,
                    _policy_snapshot(model),
                )
        except ProvisioningRepositoryError:
            raise
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def cas_policy(
        self,
        command: ProvisioningPolicyUpdate,
    ) -> ProvisioningPolicyWriteResult:
        _validate_policy_update(command)
        try:
            async with self._session_factory.begin() as session:
                await _require_active_tenant(session, command.tenant_id)
                model = await session.scalar(select(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id == command.tenant_id).with_for_update())
                if model is None:
                    return ProvisioningPolicyWriteResult(
                        ProvisioningPolicyWriteOutcome.NOT_FOUND,
                    )
                if model.revision != command.expected_revision:
                    return ProvisioningPolicyWriteResult(
                        ProvisioningPolicyWriteOutcome.REVISION_CONFLICT,
                        _policy_snapshot(model),
                    )
                if command.changed_at < model.changed_at:
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.REVISION_CONFLICT,
                    )
                model.mode = command.mode.value
                model.link_code_ttl_seconds = command.link_code_ttl_seconds
                model.changed_at = command.changed_at
                model.revision += 1
                await session.flush()
                return ProvisioningPolicyWriteResult(
                    ProvisioningPolicyWriteOutcome.APPLIED,
                    _policy_snapshot(model),
                )
        except ProvisioningRepositoryError:
            raise
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def issue_link_code(
        self,
        command: LinkCodeIssueCommand,
    ) -> LinkCodeGrantRecord:
        _validate_link_code_issue(command)
        try:
            async with self._session_factory.begin() as session:
                operation_now = await _database_now(session)
                if command.issued_at > operation_now or command.expires_at <= operation_now:
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.ASSERTION_INVALID,
                    )
                policy = await _policy_for_update(session, command.context.tenant_id)
                if policy is None or policy.mode != ProvisioningMode.LINK_ONLY.value or policy.revision != command.policy_revision:
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.POLICY_UNAVAILABLE,
                    )
                account = await _account_for_update(session, command.context)
                if (
                    command.provider_account_revision != account.identity_revision
                    or command.provider_account_last_scope_change_at != account.last_scope_change_at
                    or command.expires_at != command.issued_at + timedelta(seconds=policy.link_code_ttl_seconds)
                ):
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.REVISION_CONFLICT,
                    )
                await _advisory_lock(
                    session,
                    "target-user",
                    command.context.tenant_id,
                    command.target_user_id,
                )
                await _active_user_and_membership(
                    session,
                    command.context.tenant_id,
                    command.target_user_id,
                    for_update=True,
                )
                operation_now = await _database_now(session)
                if command.issued_at > operation_now or command.expires_at <= operation_now:
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.ASSERTION_INVALID,
                    )
                now = operation_now
                pending_codes = (
                    await session.scalars(
                        select(IdentityLinkCode)
                        .where(
                            IdentityLinkCode.tenant_id == command.context.tenant_id,
                            IdentityLinkCode.provider == command.context.provider,
                            IdentityLinkCode.provider_tenant_key == command.context.provider_tenant_key,
                            IdentityLinkCode.provider_account_key == command.context.provider_account_key,
                            IdentityLinkCode.target_user_id == command.target_user_id,
                            IdentityLinkCode.state == "pending",
                        )
                        .with_for_update()
                    )
                ).all()
                for pending in pending_codes:
                    pending.state = "revoked"
                    pending.revoked_at = max(now, pending.issued_at)
                model = IdentityLinkCode(
                    id=uuid.uuid4().hex,
                    tenant_id=command.context.tenant_id,
                    provider=command.context.provider,
                    provider_tenant_key=command.context.provider_tenant_key,
                    provider_account_key=command.context.provider_account_key,
                    target_user_id=command.target_user_id,
                    digest_key_id=command.digest_key_id,
                    code_digest=command.code_digest,
                    policy_revision=command.policy_revision,
                    provider_account_revision=command.provider_account_revision,
                    provider_account_last_scope_change_at=(command.provider_account_last_scope_change_at),
                    state="pending",
                    issued_at=command.issued_at,
                    expires_at=command.expires_at,
                )
                session.add(model)
                await session.flush()
                return _link_code_record(model)
        except ProvisioningRepositoryError:
            raise
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def provision_verified_identity(
        self,
        command: VerifiedProvisioningCommand,
    ) -> ProvisioningResult:
        _validate_provisioning(command)
        try:
            async with self._session_factory.begin() as session:
                operation_now = await _database_now(session)
                if command.verified_at > operation_now:
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.ASSERTION_INVALID,
                    )
                policy = await _policy_for_update(session, command.context.tenant_id)
                account = await _account_for_update(session, command.context)
                if account.last_scope_change_at is not None and command.verified_at < account.last_scope_change_at:
                    return _rejected(IdentityErrorCode.REVISION_CONFLICT)
                await _advisory_lock(
                    session,
                    "canonical",
                    command.context.tenant_id,
                    command.context.provider,
                    command.context.provider_tenant_key,
                    "user_id",
                    command.subject_value,
                )
                canonical = await session.scalar(
                    select(ExternalIdentity)
                    .where(
                        ExternalIdentity.tenant_id == command.context.tenant_id,
                        ExternalIdentity.provider == command.context.provider,
                        ExternalIdentity.provider_tenant_key == command.context.provider_tenant_key,
                        ExternalIdentity.subject_type == "user_id",
                        ExternalIdentity.subject_value == command.subject_value,
                    )
                    .with_for_update()
                )
                if canonical is not None:
                    return await self._provision_existing(
                        session,
                        command,
                        policy,
                        canonical,
                        operation_now,
                    )
                return await self._provision_missing(
                    session,
                    command,
                    policy,
                    operation_now,
                )
        except ProvisioningRepositoryError:
            raise
        except SQLAlchemyError:
            raise ProvisioningRepositoryError(
                IdentityErrorCode.REPOSITORY_UNAVAILABLE,
            ) from None

    async def _provision_existing(
        self,
        session: AsyncSession,
        command: VerifiedProvisioningCommand,
        policy: IdentityTenantPolicy | None,
        identity: ExternalIdentity,
        operation_now: datetime,
    ) -> ProvisioningResult:
        if identity.state == ExternalIdentityState.ACTIVE.value:
            await _advisory_lock(
                session,
                "target-user",
                command.context.tenant_id,
                identity.user_id,
            )
            _, membership = await _active_user_and_membership(
                session,
                command.context.tenant_id,
                identity.user_id,
                for_update=True,
            )
            grant: IdentityLinkCode | None = None
            if command.link_code_digest is not None:
                grant = await _link_grant_for_update(
                    session,
                    command,
                    policy,
                    allow_consumed=True,
                    operation_now=operation_now,
                )
                if grant.target_user_id != identity.user_id:
                    return _rejected(IdentityErrorCode.LINK_CONFLICT)
                if grant.state == "consumed":
                    if grant.consumed_external_identity_id != identity.id:
                        return _rejected(IdentityErrorCode.LINK_CONFLICT)
                    if not await _same_link_binding_event(
                        session,
                        grant,
                        command,
                        identity.id,
                    ):
                        raise ProvisioningRepositoryError(
                            IdentityErrorCode.LINK_REQUIRED,
                        )
                else:
                    # A canonical identity can have only one initial binding event.
                    # A later code therefore cannot become an auditable binding.
                    raise ProvisioningRepositoryError(
                        IdentityErrorCode.LINK_CONFLICT,
                    )
            operation_now = await _recheck_write_time(
                session,
                command,
                grant,
            )
            await _upsert_aliases(session, command, identity)
            if identity.last_seen_at is None or command.verified_at > identity.last_seen_at:
                identity.last_seen_at = command.verified_at
            if identity.verified_at is None or command.verified_at > identity.verified_at:
                identity.verified_at = command.verified_at
                identity.identity_revision += 1
            return ProvisioningResult(
                status=ProvisioningStatus.RESOLVED,
                outcome=ProvisioningOutcome.ALREADY_BOUND,
                identity=_identity_record(identity),
                membership=_membership_record(membership),
            )

        if identity.state in {
            ExternalIdentityState.CONFLICT.value,
            ExternalIdentityState.REVOKED.value,
        }:
            return _rejected(IdentityErrorCode.LINK_CONFLICT)
        if identity.state == ExternalIdentityState.INACTIVE.value:
            return _rejected(IdentityErrorCode.INACTIVE)
        if identity.state != ExternalIdentityState.PENDING_LINK.value:
            return _rejected(IdentityErrorCode.TRANSITION_INVALID)

        expected_mode = _require_policy_for_action(command, policy)
        if expected_mode is ProvisioningMode.JIT:
            return _rejected(IdentityErrorCode.LINK_CONFLICT)
        if expected_mode is ProvisioningMode.PREPROVISIONED and command.link_code_digest is not None:
            return _rejected(IdentityErrorCode.LINK_CONFLICT)

        await _advisory_lock(
            session,
            "target-user",
            command.context.tenant_id,
            identity.user_id,
        )
        user, membership = await _active_user_and_membership(
            session,
            command.context.tenant_id,
            identity.user_id,
            for_update=True,
        )
        link: IdentityLinkCode | None = None
        method = "preprovisioned"
        outcome = ProvisioningOutcome.PREPROVISIONED_BOUND
        if expected_mode is ProvisioningMode.LINK_ONLY:
            link = await _link_grant_for_update(
                session,
                command,
                policy,
                allow_consumed=False,
                operation_now=operation_now,
            )
            if link.target_user_id != identity.user_id:
                return _rejected(IdentityErrorCode.LINK_CONFLICT)
            method = "link_code"
            outcome = ProvisioningOutcome.LINK_CODE_BOUND
        operation_now = await _recheck_write_time(
            session,
            command,
            link,
        )
        previous_kind, result_kind = _promote_account_kind(user)
        await _upsert_aliases(session, command, identity)
        _activate_identity(identity, command)
        if link is not None:
            _consume_link_grant(link, identity.id, operation_now)
        _append_binding_event(
            session,
            command,
            identity=identity,
            target_user_id=user.id,
            method=method,
            previous_account_kind=previous_kind,
            result_account_kind=result_kind,
            link=link,
            occurred_at=operation_now,
        )
        return ProvisioningResult(
            status=ProvisioningStatus.RESOLVED,
            outcome=outcome,
            identity=_identity_record(identity),
            membership=_membership_record(membership),
        )

    async def _provision_missing(
        self,
        session: AsyncSession,
        command: VerifiedProvisioningCommand,
        policy: IdentityTenantPolicy | None,
        operation_now: datetime,
    ) -> ProvisioningResult:
        expected_mode = _require_policy_for_action(command, policy)
        if expected_mode is ProvisioningMode.PREPROVISIONED:
            return _rejected(IdentityErrorCode.NOT_FOUND)

        link: IdentityLinkCode | None = None
        if expected_mode is ProvisioningMode.LINK_ONLY:
            target_user_id = await _peek_link_target(session, command)
            await _advisory_lock(
                session,
                "target-user",
                command.context.tenant_id,
                target_user_id,
            )
            user, membership = await _active_user_and_membership(
                session,
                command.context.tenant_id,
                target_user_id,
                for_update=True,
            )
            link = await _link_grant_for_update(
                session,
                command,
                policy,
                allow_consumed=False,
                operation_now=operation_now,
            )
            if link.target_user_id != target_user_id:
                raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
            reverse = await _reverse_slot(session, command, target_user_id)
            if reverse is not None:
                return _rejected(IdentityErrorCode.LINK_CONFLICT)
            operation_now = await _recheck_write_time(
                session,
                command,
                link,
            )
            previous_kind, result_kind = _promote_account_kind(user)
            outcome = ProvisioningOutcome.LINK_CODE_BOUND
            method = "link_code"
        else:
            if command.link_code_digest is not None:
                return _rejected(IdentityErrorCode.LINK_CONFLICT)
            operation_now = await _recheck_write_time(session, command)
            user_id = uuid.uuid4().hex
            user = User(
                id=user_id,
                access_token=None,
                nickname=_jit_nickname(command.jit_nickname),
                password=None,
                email=None,
                account_kind=UserAccountKind.EXTERNAL.value,
                login_channel=command.context.provider,
                is_authenticated=True,
                is_active=True,
                is_anonymous=False,
                status=StatusEnum.VALID.value,
                is_superuser=False,
            )
            membership = UserTenant(
                id=uuid.uuid4().hex,
                user_id=user_id,
                tenant_id=command.context.tenant_id,
                role=UserTenantRole.NORMAL.value,
                invited_by=user_id,
                status=StatusEnum.VALID.value,
            )
            session.add_all([user, membership])
            await session.flush()
            previous_kind = None
            result_kind = UserAccountKind.EXTERNAL.value
            outcome = ProvisioningOutcome.JIT_CREATED
            method = "jit"

        identity = ExternalIdentity(
            id=uuid.uuid4().hex,
            tenant_id=command.context.tenant_id,
            user_id=user.id,
            provider=command.context.provider,
            provider_tenant_key=command.context.provider_tenant_key,
            subject_type="user_id",
            subject_value=command.subject_value,
            state=ExternalIdentityState.PENDING_LINK.value,
            verified_at=None,
            last_seen_at=None,
            identity_revision=1,
            attributes={
                "display_name": command.jit_nickname or None,
                "provider_status": "active",
            },
        )
        session.add(identity)
        await session.flush()
        await _upsert_aliases(session, command, identity)
        _activate_identity(identity, command)
        if link is not None:
            _consume_link_grant(link, identity.id, operation_now)
        _append_binding_event(
            session,
            command,
            identity=identity,
            target_user_id=user.id,
            method=method,
            previous_account_kind=previous_kind,
            result_account_kind=result_kind,
            link=link,
            occurred_at=operation_now,
        )
        await session.flush()
        return ProvisioningResult(
            status=ProvisioningStatus.RESOLVED,
            outcome=outcome,
            identity=_identity_record(identity),
            membership=_membership_record(membership),
        )


async def _require_active_tenant(session: AsyncSession, tenant_id: str) -> None:
    found = await session.scalar(
        select(Tenant.id)
        .where(
            Tenant.id == tenant_id,
            Tenant.status == StatusEnum.VALID.value,
        )
        .with_for_update()
    )
    if found is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.TENANT_MISMATCH)


async def _database_now(session: AsyncSession) -> datetime:
    # PostgreSQL CURRENT_TIMESTAMP is fixed at transaction start and therefore
    # cannot close expiry/freshness windows after a blocking row/advisory lock.
    value = await session.scalar(select(func.clock_timestamp()))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
    return value


async def _policy_for_update(
    session: AsyncSession,
    tenant_id: str,
) -> IdentityTenantPolicy | None:
    return await session.scalar(select(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id == tenant_id).with_for_update())


async def _account_for_update(
    session: AsyncSession,
    context: ProviderContext,
) -> IdentityProviderAccount:
    account = await session.scalar(
        select(IdentityProviderAccount)
        .join(
            Tenant,
            and_(
                Tenant.id == IdentityProviderAccount.tenant_id,
                Tenant.status == StatusEnum.VALID.value,
            ),
        )
        .where(
            IdentityProviderAccount.id == context.provider_account_id,
            IdentityProviderAccount.tenant_id == context.tenant_id,
        )
        .with_for_update(of=IdentityProviderAccount)
    )
    if account is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
    if account.provider != context.provider or account.provider_tenant_key != context.provider_tenant_key or account.provider_account_key != context.provider_account_key:
        raise ProvisioningRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
    if account.identity_revision != context.provider_account_revision or account.last_scope_change_at != context.provider_account_last_scope_change_at:
        raise ProvisioningRepositoryError(IdentityErrorCode.REVISION_CONFLICT)
    if account.identity_health_state != IdentityProviderHealthState.HEALTHY.value:
        raise ProvisioningRepositoryError(IdentityErrorCode.INACTIVE)
    return account


async def _active_user_and_membership(
    session: AsyncSession,
    tenant_id: str,
    user_id: str,
    *,
    for_update: bool,
) -> tuple[User, UserTenant]:
    user_stmt = select(User).where(
        User.id == user_id,
        User.status == StatusEnum.VALID.value,
        User.is_active.is_(True),
        User.is_anonymous.is_(False),
    )
    if for_update:
        user_stmt = user_stmt.with_for_update()
    user = await session.scalar(user_stmt)
    if user is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.INACTIVE)
    memberships = (
        await session.scalars(
            select(UserTenant)
            .where(
                UserTenant.user_id == user_id,
                UserTenant.tenant_id == tenant_id,
                UserTenant.status == StatusEnum.VALID.value,
            )
            .with_for_update()
        )
    ).all()
    if len(memberships) != 1 or memberships[0].role not in _MEMBER_ROLES:
        raise ProvisioningRepositoryError(IdentityErrorCode.INACTIVE)
    return user, memberships[0]


async def _advisory_lock(
    session: AsyncSession,
    domain: str,
    *parts: str,
) -> None:
    digest = hashlib.sha256()
    digest.update(b"multirag:eim-i6:advisory:v1\x00")
    digest.update(domain.encode())
    for part in parts:
        raw = part.encode()
        digest.update(len(raw).to_bytes(4, "big"))
        digest.update(raw)
    key = int.from_bytes(digest.digest()[:8], "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": key},
    )


def _require_policy_for_action(
    command: VerifiedProvisioningCommand,
    policy: IdentityTenantPolicy | None,
) -> ProvisioningMode:
    if command.action is None or command.policy_revision is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.POLICY_UNAVAILABLE)
    expected = _ACTION_MODE.get(command.action)
    if expected is None or policy is None or policy.revision != command.policy_revision or policy.mode != expected.value:
        raise ProvisioningRepositoryError(IdentityErrorCode.POLICY_UNAVAILABLE)
    return expected


async def _link_grant_for_update(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
    policy: IdentityTenantPolicy | None,
    *,
    allow_consumed: bool,
    operation_now: datetime,
) -> IdentityLinkCode:
    if command.link_code_key_id is None or command.link_code_digest is None or policy is None or policy.mode != ProvisioningMode.LINK_ONLY.value:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    grant = await session.scalar(
        select(IdentityLinkCode)
        .where(
            IdentityLinkCode.tenant_id == command.context.tenant_id,
            IdentityLinkCode.provider == command.context.provider,
            IdentityLinkCode.provider_tenant_key == command.context.provider_tenant_key,
            IdentityLinkCode.provider_account_key == command.context.provider_account_key,
            IdentityLinkCode.digest_key_id == command.link_code_key_id,
            IdentityLinkCode.code_digest == command.link_code_digest,
        )
        .with_for_update()
    )
    allowed_states = {"pending", "consumed"} if allow_consumed else {"pending"}
    if (
        grant is None
        or grant.state not in allowed_states
        or grant.policy_revision != policy.revision
        or grant.provider_account_revision != command.context.provider_account_revision
        or grant.provider_account_last_scope_change_at != command.context.provider_account_last_scope_change_at
        or (grant.state == "pending" and grant.expires_at <= operation_now)
    ):
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    return grant


async def _same_link_binding_event(
    session: AsyncSession,
    grant: IdentityLinkCode,
    command: VerifiedProvisioningCommand,
    identity_id: str,
) -> bool:
    event = await session.scalar(select(IdentityBindingEvent).where(IdentityBindingEvent.link_code_id == grant.id).with_for_update())
    return bool(event is not None and event.external_identity_id == identity_id and event.request_digest_key_id == command.request_digest_key_id and event.request_digest == command.request_digest)


async def _recheck_write_time(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
    grant: IdentityLinkCode | None = None,
) -> datetime:
    """Recheck proof and code freshness after all blocking locks are held."""

    operation_now = await _database_now(session)
    if command.verified_at > operation_now or command.verified_at < operation_now - _MAX_PROVIDER_PROOF_AGE:
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    if grant is not None and grant.state == "pending" and grant.expires_at <= operation_now:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    return operation_now


async def _peek_link_target(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
) -> str:
    if command.link_code_key_id is None or command.link_code_digest is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    target_user_id = await session.scalar(
        select(IdentityLinkCode.target_user_id).where(
            IdentityLinkCode.tenant_id == command.context.tenant_id,
            IdentityLinkCode.provider == command.context.provider,
            IdentityLinkCode.provider_tenant_key == command.context.provider_tenant_key,
            IdentityLinkCode.provider_account_key == command.context.provider_account_key,
            IdentityLinkCode.digest_key_id == command.link_code_key_id,
            IdentityLinkCode.code_digest == command.link_code_digest,
        )
    )
    if target_user_id is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    return target_user_id


def _consume_link_grant(
    grant: IdentityLinkCode,
    identity_id: str,
    consumed_at: datetime,
) -> None:
    if grant.state != "pending" or consumed_at >= grant.expires_at:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_REQUIRED)
    grant.state = "consumed"
    grant.consumed_at = consumed_at
    grant.consumed_external_identity_id = identity_id


async def _reverse_slot(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
    user_id: str,
) -> ExternalIdentity | None:
    return await session.scalar(
        select(ExternalIdentity)
        .where(
            ExternalIdentity.tenant_id == command.context.tenant_id,
            ExternalIdentity.user_id == user_id,
            ExternalIdentity.provider == command.context.provider,
            ExternalIdentity.provider_tenant_key == command.context.provider_tenant_key,
            ExternalIdentity.subject_type == "user_id",
        )
        .with_for_update()
    )


async def _upsert_aliases(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
    identity: ExternalIdentity,
) -> None:
    for alias in command.aliases:
        existing = await session.scalar(
            select(ExternalIdentityAlias)
            .where(
                ExternalIdentityAlias.tenant_id == command.context.tenant_id,
                ExternalIdentityAlias.provider == command.context.provider,
                ExternalIdentityAlias.provider_tenant_key == command.context.provider_tenant_key,
                ExternalIdentityAlias.provider_account_key == command.context.provider_account_key,
                ExternalIdentityAlias.alias_type == alias.alias_type.value,
                ExternalIdentityAlias.alias_value == alias.alias_value,
            )
            .with_for_update()
        )
        if existing is None:
            session.add(
                ExternalIdentityAlias(
                    id=uuid.uuid4().hex,
                    tenant_id=command.context.tenant_id,
                    external_identity_id=identity.id,
                    provider=command.context.provider,
                    provider_tenant_key=command.context.provider_tenant_key,
                    provider_account_key=command.context.provider_account_key,
                    alias_type=alias.alias_type.value,
                    alias_value=alias.alias_value,
                    verified_at=command.verified_at,
                )
            )
        elif existing.external_identity_id != identity.id:
            raise ProvisioningRepositoryError(IdentityErrorCode.LINK_CONFLICT)
        elif command.verified_at > existing.verified_at:
            existing.verified_at = command.verified_at
    await session.flush()


def _activate_identity(
    identity: ExternalIdentity,
    command: VerifiedProvisioningCommand,
) -> None:
    identity.state = ExternalIdentityState.ACTIVE.value
    identity.verified_at = command.verified_at
    identity.last_seen_at = command.verified_at
    identity.identity_revision += 1
    identity.attributes = {
        "display_name": command.jit_nickname or None,
        "provider_status": "active",
    }


def _append_binding_event(
    session: AsyncSession,
    command: VerifiedProvisioningCommand,
    *,
    identity: ExternalIdentity,
    target_user_id: str,
    method: str,
    previous_account_kind: str | None,
    result_account_kind: str,
    link: IdentityLinkCode | None,
    occurred_at: datetime,
) -> None:
    if command.policy_revision is None:
        raise ProvisioningRepositoryError(IdentityErrorCode.POLICY_UNAVAILABLE)
    session.add(
        IdentityBindingEvent(
            id=uuid.uuid4().hex,
            tenant_id=command.context.tenant_id,
            provider=command.context.provider,
            provider_tenant_key=command.context.provider_tenant_key,
            provider_account_key=command.context.provider_account_key,
            external_identity_id=identity.id,
            target_user_id=target_user_id,
            actor_user_id=target_user_id if method == "link_code" else None,
            link_code_id=None if link is None else link.id,
            binding_method=method,
            previous_account_kind=previous_account_kind,
            result_account_kind=result_account_kind,
            policy_revision=command.policy_revision,
            provider_verified_at=command.verified_at,
            occurred_at=occurred_at,
            request_digest_key_id=command.request_digest_key_id,
            request_digest=command.request_digest,
        )
    )


def _promote_account_kind(user: User) -> tuple[str, str]:
    previous = user.account_kind
    if previous == UserAccountKind.LOCAL.value:
        user.account_kind = UserAccountKind.HYBRID.value
    elif previous not in {
        UserAccountKind.EXTERNAL.value,
        UserAccountKind.HYBRID.value,
    }:
        raise ProvisioningRepositoryError(IdentityErrorCode.LINK_CONFLICT)
    return previous, user.account_kind


def _identity_record(model: ExternalIdentity) -> ExternalIdentityRecord:
    return ExternalIdentityRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        user_id=model.user_id,
        provider=model.provider,
        provider_tenant_key=model.provider_tenant_key,
        subject_type=model.subject_type,
        subject_value=model.subject_value,
        state=model.state,
        verified_at=model.verified_at,
        last_seen_at=model.last_seen_at,
        identity_revision=model.identity_revision,
        attributes=tuple(sorted(model.attributes.items())),
    )


def _membership_record(model: UserTenant) -> UserMembershipRecord:
    return UserMembershipRecord(
        user_id=model.user_id,
        tenant_id=model.tenant_id,
        role=model.role,
    )


def _policy_snapshot(model: IdentityTenantPolicy) -> ProvisioningPolicySnapshot:
    try:
        mode = ProvisioningMode(model.mode)
    except ValueError:
        raise ProvisioningRepositoryError(
            IdentityErrorCode.POLICY_UNAVAILABLE,
        ) from None
    return ProvisioningPolicySnapshot(
        tenant_id=model.tenant_id,
        mode=mode,
        revision=model.revision,
        link_code_ttl_seconds=model.link_code_ttl_seconds,
        changed_at=model.changed_at,
    )


def _link_code_record(model: IdentityLinkCode) -> LinkCodeGrantRecord:
    return LinkCodeGrantRecord(
        id=model.id,
        issued_at=model.issued_at,
        expires_at=model.expires_at,
        policy_revision=model.policy_revision,
        provider_account_revision=model.provider_account_revision,
        provider_account_last_scope_change_at=(model.provider_account_last_scope_change_at),
    )


def _rejected(code: IdentityErrorCode) -> ProvisioningResult:
    return ProvisioningResult(
        status=ProvisioningStatus.REJECTED,
        error_code=code,
    )


def _jit_nickname(value: str) -> str:
    normalized = value.strip()
    return normalized[:100] if normalized else "Enterprise user"


def _valid_digest(value: object) -> bool:
    return type(value) is str and _HASH_RE.fullmatch(value) is not None


def _valid_ttl(value: object) -> bool:
    return type(value) is int and 60 <= value <= 900


def _validate_policy_create(command: ProvisioningPolicyCreate) -> None:
    if not (
        isinstance(command, ProvisioningPolicyCreate)
        and valid_opaque_id(command.tenant_id)
        and isinstance(command.mode, ProvisioningMode)
        and _valid_ttl(command.link_code_ttl_seconds)
        and valid_timestamp(command.changed_at)
    ):
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)


def _validate_policy_update(command: ProvisioningPolicyUpdate) -> None:
    if not (
        isinstance(command, ProvisioningPolicyUpdate)
        and valid_opaque_id(command.tenant_id)
        and valid_revision(command.expected_revision)
        and isinstance(command.mode, ProvisioningMode)
        and _valid_ttl(command.link_code_ttl_seconds)
        and valid_timestamp(command.changed_at)
    ):
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)


def _validate_link_code_issue(command: LinkCodeIssueCommand) -> None:
    if not (
        isinstance(command, LinkCodeIssueCommand)
        and valid_provider_context(command.context)
        and valid_opaque_id(command.target_user_id)
        and valid_text(command.digest_key_id, max_length=64)
        and _valid_digest(command.code_digest)
        and valid_revision(command.policy_revision)
        and valid_revision(command.provider_account_revision)
        and (command.provider_account_last_scope_change_at is None or valid_timestamp(command.provider_account_last_scope_change_at))
        and valid_timestamp(command.issued_at)
        and valid_timestamp(command.expires_at)
        and command.issued_at < command.expires_at
        and command.expires_at <= command.issued_at + timedelta(minutes=15)
    ):
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)


def _validate_provisioning(command: VerifiedProvisioningCommand) -> None:
    if not isinstance(command, VerifiedProvisioningCommand):
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    aliases = command.aliases
    aliases_valid = bool(
        isinstance(aliases, tuple) and 1 <= len(aliases) <= 2 and all(isinstance(alias, VerifiedProvisioningAlias) and valid_alias(alias.alias_type, alias.alias_value) for alias in aliases)
    )
    alias_keys = {(alias.alias_type, alias.alias_value) for alias in aliases} if aliases_valid else set()
    action_pair_valid = (command.action is None and command.policy_revision is None) or (
        isinstance(command.action, ProvisioningAction) and valid_revision(command.policy_revision)  # type: ignore[arg-type]
    )
    link_pair_valid = (command.link_code_key_id is None and command.link_code_digest is None) or (valid_text(command.link_code_key_id, max_length=64) and _valid_digest(command.link_code_digest))
    if not (
        valid_provider_context(command.context)
        and valid_alias_key(command.asserted_alias)
        and action_pair_valid
        and valid_text(command.subject_value, max_length=255)
        and aliases_valid
        and len(alias_keys) == len(aliases)
        and (
            command.asserted_alias.alias_type,
            command.asserted_alias.alias_value,
        )
        in alias_keys
        and valid_timestamp(command.verified_at)
        and type(command.jit_nickname) is str
        and len(command.jit_nickname) <= 512
        and link_pair_valid
        and valid_text(command.request_digest_key_id, max_length=64)
        and _valid_digest(command.request_digest)
    ):
        raise ProvisioningRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
