"""Pure-async SQLAlchemy adapter for the EIM-I3 repository contract."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from typing import ParamSpec, TypeVar

from sqlalchemy import and_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import ExternalIdentityState, IdentityProviderHealthState
from api.db.db_models import (
    EXTERNAL_IDENTITY_ATTRIBUTE_KEYS,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityProviderAccount,
    IdentityProviderTenant,
    Tenant,
    User,
    UserTenant,
    get_utc_now,
)
from api.identity.contracts import (
    CasOutcome,
    CasResult,
    ExternalIdentityAliasInsert,
    ExternalIdentityInsert,
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityResolutionRequest,
    IdentityResolutionSnapshot,
    IdentityStateTransition,
    InsertOutcome,
    InsertResult,
    ProviderAccountHealthCAS,
    ProviderAccountInsertResult,
    ProviderAccountRecord,
    ProviderContext,
    ProviderTenantInsertResult,
    ProviderTenantRecord,
    UserMembershipRecord,
    VerifiedIdentityActivation,
    VerifiedProviderAccountOnboarding,
    VerifiedProviderTenantOnboarding,
)
from api.identity.validation import (
    valid_alias,
    valid_opaque_id,
    valid_provider_context,
    valid_revision,
    valid_text,
    valid_timestamp,
)
from common.constants import StatusEnum

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _redact_database_errors(
    method: Callable[_P, Awaitable[_R]],
) -> Callable[_P, Awaitable[_R]]:
    """Map driver/SQL errors to a stable code without SQL bind parameters."""

    @wraps(method)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await method(*args, **kwargs)
        except IdentityRepositoryError:
            raise
        except SQLAlchemyError:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None

    return wrapped


class IdentityRepositoryError(RuntimeError):
    """Safe repository failure with a stable code and no provider identifiers."""

    def __init__(self, code: IdentityErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


_ORDINARY_TRANSITIONS: Mapping[str, frozenset[str]] = {
    ExternalIdentityState.PENDING_LINK.value: frozenset(
        {
            ExternalIdentityState.INACTIVE.value,
            ExternalIdentityState.CONFLICT.value,
            ExternalIdentityState.REVOKED.value,
        }
    ),
    ExternalIdentityState.ACTIVE.value: frozenset(
        {
            ExternalIdentityState.INACTIVE.value,
            ExternalIdentityState.CONFLICT.value,
            ExternalIdentityState.REVOKED.value,
        }
    ),
    ExternalIdentityState.INACTIVE.value: frozenset(
        {
            ExternalIdentityState.CONFLICT.value,
            ExternalIdentityState.REVOKED.value,
        }
    ),
    ExternalIdentityState.CONFLICT.value: frozenset(),
    ExternalIdentityState.REVOKED.value: frozenset(),
}

_HEALTH_TRANSITIONS: Mapping[str, frozenset[str]] = {
    IdentityProviderHealthState.PENDING.value: frozenset(
        {
            IdentityProviderHealthState.HEALTHY.value,
            IdentityProviderHealthState.ERROR.value,
            IdentityProviderHealthState.DISABLED.value,
        }
    ),
    IdentityProviderHealthState.HEALTHY.value: frozenset(
        {
            IdentityProviderHealthState.DEGRADED.value,
            IdentityProviderHealthState.ERROR.value,
            IdentityProviderHealthState.DISABLED.value,
        }
    ),
    IdentityProviderHealthState.DEGRADED.value: frozenset(
        {
            IdentityProviderHealthState.HEALTHY.value,
            IdentityProviderHealthState.ERROR.value,
            IdentityProviderHealthState.DISABLED.value,
        }
    ),
    IdentityProviderHealthState.ERROR.value: frozenset(
        {
            IdentityProviderHealthState.HEALTHY.value,
            IdentityProviderHealthState.DEGRADED.value,
            IdentityProviderHealthState.DISABLED.value,
        }
    ),
    IdentityProviderHealthState.DISABLED.value: frozenset(),
}


class SqlAlchemyIdentityRepository:
    """Narrow identity repository; the caller owns commit/rollback boundaries."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    @_redact_database_errors
    async def insert_verified_provider_tenant(
        self,
        command: VerifiedProviderTenantOnboarding,
    ) -> ProviderTenantInsertResult:
        _validate_onboarding_scope(
            command.tenant_id,
            command.provider,
            command.provider_tenant_key,
        )
        if not valid_timestamp(command.verified_at):
            raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
        await self._require_active_tenant(command.tenant_id)
        audit = _insert_audit_values()
        statement = (
            insert(IdentityProviderTenant)
            .values(
                id=uuid.uuid4().hex,
                tenant_id=command.tenant_id,
                provider=command.provider,
                provider_tenant_key=command.provider_tenant_key,
                verified_at=command.verified_at,
                **audit,
            )
            .on_conflict_do_nothing(
                constraint="uq_identity_provider_tenants_provider_tenant",
            )
            .returning(IdentityProviderTenant)
        )
        try:
            created = (await self._db.scalars(statement)).one_or_none()
        except IntegrityError:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None
        if created is not None:
            return ProviderTenantInsertResult(InsertOutcome.CREATED, _provider_tenant_record(created))

        existing = await self._db.scalar(
            select(IdentityProviderTenant).where(
                IdentityProviderTenant.provider == command.provider,
                IdentityProviderTenant.provider_tenant_key == command.provider_tenant_key,
            )
        )
        if existing is None:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        if existing.tenant_id != command.tenant_id:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        return ProviderTenantInsertResult(InsertOutcome.EXISTING, _provider_tenant_record(existing))

    @_redact_database_errors
    async def insert_verified_provider_account(
        self,
        command: VerifiedProviderAccountOnboarding,
    ) -> ProviderAccountInsertResult:
        _validate_onboarding_scope(
            command.tenant_id,
            command.provider,
            command.provider_tenant_key,
            provider_account_key=command.provider_account_key,
        )
        await self._require_active_tenant(command.tenant_id)
        provider_tenant = await self._db.scalar(
            select(IdentityProviderTenant).where(
                IdentityProviderTenant.tenant_id == command.tenant_id,
                IdentityProviderTenant.provider == command.provider,
                IdentityProviderTenant.provider_tenant_key == command.provider_tenant_key,
            )
        )
        if provider_tenant is None:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        audit = _insert_audit_values()
        statement = (
            insert(IdentityProviderAccount)
            .values(
                id=uuid.uuid4().hex,
                tenant_id=command.tenant_id,
                provider=command.provider,
                provider_tenant_key=command.provider_tenant_key,
                provider_account_key=command.provider_account_key,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.PENDING.value,
                **audit,
            )
            .on_conflict_do_nothing(
                constraint="uq_identity_provider_accounts_provider_account",
            )
            .returning(IdentityProviderAccount)
        )
        try:
            created = (await self._db.scalars(statement)).one_or_none()
        except IntegrityError:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None
        if created is not None:
            return ProviderAccountInsertResult(InsertOutcome.CREATED, _account_record(created))

        existing = await self._db.scalar(
            select(IdentityProviderAccount).where(
                IdentityProviderAccount.provider == command.provider,
                IdentityProviderAccount.provider_tenant_key == command.provider_tenant_key,
                IdentityProviderAccount.provider_account_key == command.provider_account_key,
            )
        )
        if existing is None:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        if existing.tenant_id != command.tenant_id:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        return ProviderAccountInsertResult(InsertOutcome.EXISTING, _account_record(existing))

    @_redact_database_errors
    async def get_provider_account(
        self,
        context: ProviderContext,
        *,
        for_update: bool = False,
    ) -> ProviderAccountRecord | None:
        if not valid_provider_context(context):
            return None
        statement = (
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
                IdentityProviderAccount.provider == context.provider,
                IdentityProviderAccount.provider_tenant_key == context.provider_tenant_key,
                IdentityProviderAccount.provider_account_key == context.provider_account_key,
                IdentityProviderAccount.identity_revision == context.provider_account_revision,
                IdentityProviderAccount.last_scope_change_at.is_not_distinct_from(context.provider_account_last_scope_change_at),
            )
        )
        if for_update:
            statement = statement.with_for_update()
        model = (await self._db.scalars(statement)).one_or_none()
        return None if model is None else _account_record(model)

    @_redact_database_errors
    async def resolve_identity(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionSnapshot | None:
        context = request.context
        if not valid_provider_context(context) or not valid_alias(
            request.alias.alias_type,
            request.alias.alias_value,
        ):
            return None
        statement = (
            select(
                IdentityProviderAccount,
                ExternalIdentity,
                UserTenant,
                ExternalIdentityAlias.verified_at,
            )
            .join(
                Tenant,
                and_(
                    Tenant.id == IdentityProviderAccount.tenant_id,
                    Tenant.status == StatusEnum.VALID.value,
                ),
            )
            .outerjoin(
                ExternalIdentityAlias,
                and_(
                    ExternalIdentityAlias.tenant_id == IdentityProviderAccount.tenant_id,
                    ExternalIdentityAlias.provider == IdentityProviderAccount.provider,
                    ExternalIdentityAlias.provider_tenant_key == IdentityProviderAccount.provider_tenant_key,
                    ExternalIdentityAlias.provider_account_key == IdentityProviderAccount.provider_account_key,
                    ExternalIdentityAlias.alias_type == request.alias.alias_type,
                    ExternalIdentityAlias.alias_value == request.alias.alias_value,
                ),
            )
            .outerjoin(
                ExternalIdentity,
                and_(
                    ExternalIdentity.id == ExternalIdentityAlias.external_identity_id,
                    ExternalIdentity.tenant_id == ExternalIdentityAlias.tenant_id,
                    ExternalIdentity.provider == ExternalIdentityAlias.provider,
                    ExternalIdentity.provider_tenant_key == ExternalIdentityAlias.provider_tenant_key,
                ),
            )
            .outerjoin(
                User,
                and_(
                    User.id == ExternalIdentity.user_id,
                    User.status == StatusEnum.VALID.value,
                    User.is_active.is_(True),
                    User.is_anonymous.is_(False),
                ),
            )
            .outerjoin(
                UserTenant,
                and_(
                    UserTenant.user_id == User.id,
                    UserTenant.tenant_id == ExternalIdentity.tenant_id,
                    UserTenant.status == StatusEnum.VALID.value,
                ),
            )
            .where(
                IdentityProviderAccount.id == context.provider_account_id,
                IdentityProviderAccount.tenant_id == context.tenant_id,
                IdentityProviderAccount.provider == context.provider,
                IdentityProviderAccount.provider_tenant_key == context.provider_tenant_key,
                IdentityProviderAccount.provider_account_key == context.provider_account_key,
                IdentityProviderAccount.identity_revision == context.provider_account_revision,
                IdentityProviderAccount.last_scope_change_at.is_not_distinct_from(context.provider_account_last_scope_change_at),
            )
        )
        rows = (await self._db.execute(statement)).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)
        account, identity, membership, alias_verified_at = rows[0]
        return IdentityResolutionSnapshot(
            account=_account_record(account),
            identity=None if identity is None else _identity_record(identity),
            membership=None if membership is None else _membership_record(membership),
            alias_verified_at=alias_verified_at,
        )

    @_redact_database_errors
    async def insert_identity(self, command: ExternalIdentityInsert) -> InsertResult:
        _validate_identity_insert(command)
        account = await self._require_provider_account(command.context, require_healthy=True)
        context = _account_record(account).context()
        await self._require_active_membership(context.tenant_id, command.user_id)
        values = {
            "id": uuid.uuid4().hex,
            "tenant_id": context.tenant_id,
            "user_id": command.user_id,
            "provider": context.provider,
            "provider_tenant_key": context.provider_tenant_key,
            "subject_type": command.subject_type,
            "subject_value": command.subject_value,
            "state": ExternalIdentityState.PENDING_LINK.value,
            "verified_at": None,
            "last_seen_at": None,
            "identity_revision": 1,
            "attributes": dict(command.attributes),
            **_insert_audit_values(),
        }
        statement = insert(ExternalIdentity).values(**values).on_conflict_do_nothing().returning(ExternalIdentity)
        try:
            created = (await self._db.scalars(statement)).one_or_none()
        except IntegrityError:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None
        if created is not None:
            return InsertResult(InsertOutcome.CREATED, _identity_record(created))

        existing = await self._identity_by_subject(context, command)
        if existing is not None:
            if not _same_identity(command, existing):
                raise IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)
            return InsertResult(
                InsertOutcome.EXISTING,
                _identity_record(existing),
            )

        reverse = await self._identity_by_user_slot(context, command)
        if reverse is not None:
            raise IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)
        raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)

    @_redact_database_errors
    async def insert_alias(self, command: ExternalIdentityAliasInsert) -> InsertResult:
        if not (
            valid_provider_context(command.context) and valid_alias(command.alias_type, command.alias_value) and valid_opaque_id(command.external_identity_id) and valid_timestamp(command.verified_at)
        ):
            raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
        account = await self._require_provider_account(command.context, require_healthy=True)
        context = _account_record(account).context()
        if context.provider_account_last_scope_change_at is not None and command.verified_at < context.provider_account_last_scope_change_at:
            raise IdentityRepositoryError(IdentityErrorCode.REVISION_CONFLICT)
        parent = await self._identity_for_alias(context, command)
        if parent is None:
            raise IdentityRepositoryError(IdentityErrorCode.NOT_FOUND)
        await self._require_active_membership(context.tenant_id, parent.user_id)
        statement = (
            insert(ExternalIdentityAlias)
            .values(
                id=uuid.uuid4().hex,
                tenant_id=context.tenant_id,
                external_identity_id=command.external_identity_id,
                provider=context.provider,
                provider_tenant_key=context.provider_tenant_key,
                provider_account_key=context.provider_account_key,
                alias_type=command.alias_type,
                alias_value=command.alias_value,
                verified_at=command.verified_at,
                **_insert_audit_values(),
            )
            .on_conflict_do_nothing(
                constraint="uq_external_identity_aliases_tenant_provider_alias",
            )
            .returning(ExternalIdentityAlias.external_identity_id)
        )
        try:
            created_identity_id = await self._db.scalar(statement)
        except IntegrityError:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE) from None
        if created_identity_id is not None:
            return InsertResult(InsertOutcome.CREATED, _identity_record(parent))

        existing_identity_id = await self._db.scalar(
            select(ExternalIdentityAlias.external_identity_id).where(
                ExternalIdentityAlias.tenant_id == context.tenant_id,
                ExternalIdentityAlias.provider == context.provider,
                ExternalIdentityAlias.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentityAlias.provider_account_key == context.provider_account_key,
                ExternalIdentityAlias.alias_type == command.alias_type,
                ExternalIdentityAlias.alias_value == command.alias_value,
            )
        )
        if existing_identity_id != command.external_identity_id:
            raise IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)
        existing_alias = await self._db.scalar(
            select(ExternalIdentityAlias)
            .where(
                ExternalIdentityAlias.tenant_id == context.tenant_id,
                ExternalIdentityAlias.provider == context.provider,
                ExternalIdentityAlias.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentityAlias.provider_account_key == context.provider_account_key,
                ExternalIdentityAlias.alias_type == command.alias_type,
                ExternalIdentityAlias.alias_value == command.alias_value,
            )
            .with_for_update()
        )
        if existing_alias is None:
            raise IdentityRepositoryError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)
        if command.verified_at > existing_alias.verified_at:
            audit = _update_audit_values()
            await self._db.execute(
                update(ExternalIdentityAlias)
                .where(
                    ExternalIdentityAlias.id == existing_alias.id,
                    ExternalIdentityAlias.verified_at == existing_alias.verified_at,
                )
                .values(verified_at=command.verified_at, **audit)
            )
        return InsertResult(InsertOutcome.EXISTING, _identity_record(parent))

    @_redact_database_errors
    async def cas_identity_state(self, command: IdentityStateTransition) -> CasResult:
        if not (
            valid_provider_context(command.context)
            and valid_opaque_id(command.external_identity_id)
            and valid_revision(command.expected_revision)
            and type(command.target_state) is str
            and command.target_state in _ORDINARY_TRANSITIONS
        ):
            raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
        account = await self._require_provider_account(command.context)
        context = _account_record(account).context()
        current = (
            await self._db.execute(
                select(ExternalIdentity.state, ExternalIdentity.identity_revision)
                .where(
                    ExternalIdentity.id == command.external_identity_id,
                    ExternalIdentity.tenant_id == context.tenant_id,
                    ExternalIdentity.provider == context.provider,
                    ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                )
                .with_for_update()
            )
        ).one_or_none()
        if current is None:
            return CasResult(CasOutcome.NOT_FOUND)
        current_state, current_revision = current
        if current_revision != command.expected_revision:
            return CasResult(CasOutcome.REVISION_CONFLICT, current_revision)
        if command.target_state == current_state:
            return CasResult(CasOutcome.APPLIED, current_revision)
        if command.target_state not in _ORDINARY_TRANSITIONS.get(current_state, frozenset()):
            return CasResult(CasOutcome.INVALID_TRANSITION, current_revision)
        audit = _update_audit_values()
        revision = await self._db.scalar(
            update(ExternalIdentity)
            .where(
                ExternalIdentity.id == command.external_identity_id,
                ExternalIdentity.tenant_id == context.tenant_id,
                ExternalIdentity.provider == context.provider,
                ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentity.identity_revision == command.expected_revision,
                ExternalIdentity.state == current_state,
            )
            .values(
                state=command.target_state,
                identity_revision=ExternalIdentity.identity_revision + 1,
                **audit,
            )
            .returning(ExternalIdentity.identity_revision)
        )
        if revision is not None:
            return CasResult(CasOutcome.APPLIED, revision)
        return await self._classify_identity_cas(
            context.tenant_id,
            command.external_identity_id,
            expected_revision=command.expected_revision,
            allowed_states=frozenset({current_state}),
        )

    @_redact_database_errors
    async def activate_verified_identity(
        self,
        command: VerifiedIdentityActivation,
    ) -> CasResult:
        if not (valid_provider_context(command.context) and valid_opaque_id(command.external_identity_id) and valid_revision(command.expected_revision) and valid_timestamp(command.verified_at)):
            raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
        account = await self._require_provider_account(command.context, require_healthy=True)
        context = _account_record(account).context()
        if context.provider_account_last_scope_change_at is not None and command.verified_at < context.provider_account_last_scope_change_at:
            return CasResult(CasOutcome.INVALID_TRANSITION)
        current = (
            await self._db.execute(
                select(
                    ExternalIdentity.user_id,
                    ExternalIdentity.state,
                    ExternalIdentity.identity_revision,
                    ExternalIdentity.verified_at,
                )
                .where(
                    ExternalIdentity.id == command.external_identity_id,
                    ExternalIdentity.tenant_id == context.tenant_id,
                    ExternalIdentity.provider == context.provider,
                    ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                )
                .with_for_update()
            )
        ).one_or_none()
        if current is None:
            return CasResult(CasOutcome.NOT_FOUND)
        user_id, current_state, current_revision, current_verified_at = current
        if current_revision != command.expected_revision:
            return CasResult(CasOutcome.REVISION_CONFLICT, current_revision)
        allowed_states = frozenset(
            {
                ExternalIdentityState.PENDING_LINK.value,
                ExternalIdentityState.INACTIVE.value,
            }
        )
        if current_state not in allowed_states:
            return CasResult(CasOutcome.INVALID_TRANSITION, current_revision)
        if current_verified_at is not None and command.verified_at < current_verified_at:
            return CasResult(CasOutcome.INVALID_TRANSITION, current_revision)
        await self._require_active_membership(context.tenant_id, user_id)
        audit = _update_audit_values()
        revision = await self._db.scalar(
            update(ExternalIdentity)
            .where(
                ExternalIdentity.id == command.external_identity_id,
                ExternalIdentity.tenant_id == context.tenant_id,
                ExternalIdentity.provider == context.provider,
                ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentity.identity_revision == command.expected_revision,
                ExternalIdentity.state == current_state,
                ExternalIdentity.verified_at.is_not_distinct_from(current_verified_at),
            )
            .values(
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=command.verified_at,
                identity_revision=ExternalIdentity.identity_revision + 1,
                **audit,
            )
            .returning(ExternalIdentity.identity_revision)
        )
        if revision is not None:
            return CasResult(CasOutcome.APPLIED, revision)
        return await self._classify_identity_cas(
            context.tenant_id,
            command.external_identity_id,
            expected_revision=command.expected_revision,
            allowed_states=allowed_states,
        )

    @_redact_database_errors
    async def cas_provider_account_health(
        self,
        command: ProviderAccountHealthCAS,
    ) -> CasResult:
        if not (
            valid_provider_context(command.context)
            and _valid_health(command.target_health_state, command.error_code)
            and (command.last_scope_change_at is None or valid_timestamp(command.last_scope_change_at))
            and (command.last_directory_event_at is None or valid_timestamp(command.last_directory_event_at))
        ):
            raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
        account = await self._db.scalar(
            select(IdentityProviderAccount)
            .where(
                IdentityProviderAccount.id == command.context.provider_account_id,
                IdentityProviderAccount.tenant_id == command.context.tenant_id,
            )
            .with_for_update()
        )
        if account is None:
            return CasResult(CasOutcome.NOT_FOUND)
        if account.provider != command.context.provider or account.provider_tenant_key != command.context.provider_tenant_key or account.provider_account_key != command.context.provider_account_key:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        if account.identity_revision != command.context.provider_account_revision or account.last_scope_change_at != command.context.provider_account_last_scope_change_at:
            return CasResult(CasOutcome.REVISION_CONFLICT, account.identity_revision)
        if command.target_health_state != account.identity_health_state and command.target_health_state not in _HEALTH_TRANSITIONS.get(account.identity_health_state, frozenset()):
            return CasResult(CasOutcome.INVALID_TRANSITION, account.identity_revision)
        if command.last_scope_change_at is not None and account.last_scope_change_at is not None and command.last_scope_change_at < account.last_scope_change_at:
            return CasResult(CasOutcome.INVALID_TRANSITION, account.identity_revision)
        if command.last_directory_event_at is not None and account.last_directory_event_at is not None and command.last_directory_event_at < account.last_directory_event_at:
            return CasResult(CasOutcome.INVALID_TRANSITION, account.identity_revision)
        scope_change_at = account.last_scope_change_at if command.last_scope_change_at is None else command.last_scope_change_at
        directory_event_at = account.last_directory_event_at if command.last_directory_event_at is None else command.last_directory_event_at
        audit = _update_audit_values()
        revision = await self._db.scalar(
            update(IdentityProviderAccount)
            .where(
                IdentityProviderAccount.id == command.context.provider_account_id,
                IdentityProviderAccount.tenant_id == command.context.tenant_id,
                IdentityProviderAccount.provider == command.context.provider,
                IdentityProviderAccount.provider_tenant_key == command.context.provider_tenant_key,
                IdentityProviderAccount.provider_account_key == command.context.provider_account_key,
                IdentityProviderAccount.identity_revision == command.context.provider_account_revision,
                IdentityProviderAccount.last_scope_change_at.is_not_distinct_from(
                    command.context.provider_account_last_scope_change_at,
                ),
            )
            .values(
                identity_health_state=command.target_health_state,
                identity_health_error_code=command.error_code,
                last_scope_change_at=scope_change_at,
                last_directory_event_at=directory_event_at,
                identity_revision=IdentityProviderAccount.identity_revision + 1,
                **audit,
            )
            .returning(IdentityProviderAccount.identity_revision)
        )
        if revision is not None:
            return CasResult(CasOutcome.APPLIED, revision)
        row = (
            await self._db.execute(
                select(IdentityProviderAccount.identity_revision).where(
                    IdentityProviderAccount.id == command.context.provider_account_id,
                    IdentityProviderAccount.tenant_id == command.context.tenant_id,
                )
            )
        ).first()
        return CasResult(
            CasOutcome.NOT_FOUND if row is None else CasOutcome.REVISION_CONFLICT,
            None if row is None else row[0],
        )

    async def _identity_by_subject(
        self,
        context: ProviderContext,
        command: ExternalIdentityInsert,
    ) -> ExternalIdentity | None:
        return await self._db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.tenant_id == context.tenant_id,
                ExternalIdentity.provider == context.provider,
                ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentity.subject_type == command.subject_type,
                ExternalIdentity.subject_value == command.subject_value,
            )
        )

    async def _identity_by_user_slot(
        self,
        context: ProviderContext,
        command: ExternalIdentityInsert,
    ) -> ExternalIdentity | None:
        return await self._db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.tenant_id == context.tenant_id,
                ExternalIdentity.user_id == command.user_id,
                ExternalIdentity.provider == context.provider,
                ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentity.subject_type == command.subject_type,
            )
        )

    async def _identity_for_alias(
        self,
        context: ProviderContext,
        command: ExternalIdentityAliasInsert,
    ) -> ExternalIdentity | None:
        return await self._db.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.id == command.external_identity_id,
                ExternalIdentity.tenant_id == context.tenant_id,
                ExternalIdentity.provider == context.provider,
                ExternalIdentity.provider_tenant_key == context.provider_tenant_key,
                ExternalIdentity.state.not_in(
                    (
                        ExternalIdentityState.CONFLICT.value,
                        ExternalIdentityState.REVOKED.value,
                    )
                ),
            )
        )

    async def _require_provider_account(
        self,
        context: ProviderContext,
        *,
        require_healthy: bool = False,
    ) -> IdentityProviderAccount:
        account = await self._db.scalar(
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
            .with_for_update()
        )
        if account is None:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        if account.provider != context.provider or account.provider_tenant_key != context.provider_tenant_key or account.provider_account_key != context.provider_account_key:
            raise IdentityRepositoryError(IdentityErrorCode.OWNERSHIP_CONFLICT)
        if account.identity_revision != context.provider_account_revision or account.last_scope_change_at != context.provider_account_last_scope_change_at:
            raise IdentityRepositoryError(IdentityErrorCode.REVISION_CONFLICT)
        if require_healthy and account.identity_health_state != IdentityProviderHealthState.HEALTHY.value:
            raise IdentityRepositoryError(IdentityErrorCode.INACTIVE)
        return account

    async def _require_active_tenant(self, tenant_id: str) -> None:
        found = await self._db.scalar(
            select(Tenant.id)
            .where(
                Tenant.id == tenant_id,
                Tenant.status == StatusEnum.VALID.value,
            )
            .with_for_update()
        )
        if found is None:
            raise IdentityRepositoryError(IdentityErrorCode.TENANT_MISMATCH)

    async def _require_active_membership(self, tenant_id: str, user_id: str) -> None:
        rows = (
            await self._db.execute(
                select(User.id, UserTenant.role)
                .join(
                    UserTenant,
                    and_(
                        UserTenant.user_id == User.id,
                        UserTenant.tenant_id == tenant_id,
                        UserTenant.status == StatusEnum.VALID.value,
                    ),
                )
                .where(
                    User.id == user_id,
                    User.status == StatusEnum.VALID.value,
                    User.is_active.is_(True),
                    User.is_anonymous.is_(False),
                )
                .with_for_update(of=(User, UserTenant))
            )
        ).all()
        if not rows:
            raise IdentityRepositoryError(IdentityErrorCode.INACTIVE)
        if len(rows) != 1 or rows[0][1] not in {"owner", "admin", "normal"}:
            raise IdentityRepositoryError(IdentityErrorCode.LINK_CONFLICT)

    async def _classify_identity_cas(
        self,
        tenant_id: str,
        identity_id: str,
        *,
        expected_revision: int,
        allowed_states: frozenset[str],
    ) -> CasResult:
        row = (
            await self._db.execute(
                select(ExternalIdentity.state, ExternalIdentity.identity_revision).where(
                    ExternalIdentity.id == identity_id,
                    ExternalIdentity.tenant_id == tenant_id,
                )
            )
        ).first()
        if row is None:
            return CasResult(CasOutcome.NOT_FOUND)
        state, revision = row
        if revision != expected_revision:
            return CasResult(CasOutcome.REVISION_CONFLICT, revision)
        if state not in allowed_states:
            return CasResult(CasOutcome.INVALID_TRANSITION, revision)
        return CasResult(CasOutcome.REVISION_CONFLICT, revision)


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


def _provider_tenant_record(model: IdentityProviderTenant) -> ProviderTenantRecord:
    return ProviderTenantRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        provider=model.provider,
        provider_tenant_key=model.provider_tenant_key,
        verified_at=model.verified_at,
    )


def _account_record(model: IdentityProviderAccount) -> ProviderAccountRecord:
    return ProviderAccountRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        provider=model.provider,
        provider_tenant_key=model.provider_tenant_key,
        provider_account_key=model.provider_account_key,
        identity_revision=model.identity_revision,
        identity_health_state=model.identity_health_state,
        identity_health_error_code=model.identity_health_error_code,
        last_scope_change_at=model.last_scope_change_at,
        last_directory_event_at=model.last_directory_event_at,
    )


def _membership_record(model: UserTenant) -> UserMembershipRecord:
    return UserMembershipRecord(
        user_id=model.user_id,
        tenant_id=model.tenant_id,
        role=model.role,
    )


def _same_identity(command: ExternalIdentityInsert, model: ExternalIdentity) -> bool:
    return model.user_id == command.user_id and model.state not in {
        ExternalIdentityState.CONFLICT.value,
        ExternalIdentityState.REVOKED.value,
    }


def _valid_health(state: str, error_code: str | None) -> bool:
    values = {item.value for item in IdentityProviderHealthState}
    return type(state) is str and state in values and (error_code is None or valid_text(error_code, max_length=64)) and ((state == IdentityProviderHealthState.ERROR.value) == (error_code is not None))


def _insert_audit_values() -> dict[str, object]:
    now = get_utc_now()
    timestamp = int(now.timestamp() * 1000)
    return {
        "create_date": now,
        "update_date": now,
        "create_time": timestamp,
        "update_time": timestamp,
    }


def _update_audit_values() -> dict[str, object]:
    now = get_utc_now()
    return {
        "update_date": now,
        "update_time": int(now.timestamp() * 1000),
    }


def _validate_onboarding_scope(
    tenant_id: str,
    provider: str,
    provider_tenant_key: str,
    *,
    provider_account_key: str | None = None,
) -> None:
    if not (
        valid_text(tenant_id, max_length=32)
        and valid_text(provider, max_length=64)
        and valid_text(provider_tenant_key, max_length=255)
        and (provider_account_key is None or valid_text(provider_account_key, max_length=255))
    ):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)


def _validate_identity_insert(command: ExternalIdentityInsert) -> None:
    if not valid_provider_context(command.context):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    if not (valid_opaque_id(command.user_id) and valid_text(command.subject_type, max_length=64) and valid_text(command.subject_value, max_length=255)):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    try:
        attributes = dict(command.attributes)
    except (TypeError, ValueError):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID) from None
    if len(attributes) != len(command.attributes):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    if set(attributes) - EXTERNAL_IDENTITY_ATTRIBUTE_KEYS:
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
    if any(value is not None and (not isinstance(value, str) or len(value) > 512) for value in attributes.values()):
        raise IdentityRepositoryError(IdentityErrorCode.ASSERTION_INVALID)
