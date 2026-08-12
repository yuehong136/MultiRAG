"""Legacy Web/API-token adapter for the personal owner tenant.

This module is deliberately separate from ``api.utils.api_utils`` so the
canonical Principal and MultiRAG-specific tenant projection stay outside the
RAGFlow-following authentication utility surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import UserTenantRole
from api.db.db_models import Tenant, UserTenant
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    PrincipalBuildError,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from common.constants import StatusEnum


def _load_personal_owner_tenant_ids(db: Session, user_id: str) -> list[str]:
    stmt = (
        select(Tenant.id)
        .join(UserTenant, Tenant.id == UserTenant.tenant_id)
        .where(
            UserTenant.user_id == user_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role == UserTenantRole.OWNER.value,
            Tenant.status == StatusEnum.VALID.value,
            Tenant.id == user_id,
        )
    )
    return [str(tenant_id) for tenant_id in db.scalars(stmt).all()]


def principal_from_legacy_owner_context(
    db: Session,
    user: Any,
    source: AuthenticationSource,
    *,
    validated_at: datetime | None = None,
) -> Principal | None:
    """Project one live personal-owner membership into a Principal.

    This preserves the legacy RAGFlow Web/API-token behavior. It is not a
    general active-tenant selector: P2/C3 must provide a server-verified
    membership tenant explicitly.
    """

    if user is None or not getattr(user, "id", None):
        return None
    if (
        getattr(user, "status", None) != StatusEnum.VALID.value
        or getattr(user, "is_active", None) is not True
        or getattr(user, "is_authenticated", None) is not True
        or getattr(user, "is_anonymous", None) is not False
    ):
        return None

    owner_tenant_ids = _load_personal_owner_tenant_ids(db, str(user.id))
    if len(owner_tenant_ids) != 1 or owner_tenant_ids[0] != str(user.id):
        return None
    try:
        return build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(
                platform_user_id=str(user.id),
                display_name=str(user.nickname or ""),
            ),
            membership=TenantMembershipEvidence(
                platform_user_id=str(user.id),
                tenant_id=owner_tenant_ids[0],
            ),
            authentication=AuthenticationContext(
                source=source,
                assurance=IdentityAssurance.AUTHENTICATED,
                validated_at=validated_at or datetime.now(UTC),
                # Current tokens prove neither a human auth_time nor
                # directory/enterprise evidence. Never invent them from
                # request time or token expiry.
                authenticated_at=None,
                assurance_verified_at=None,
            ),
        )
    except PrincipalBuildError:
        return None


__all__ = ["principal_from_legacy_owner_context"]
