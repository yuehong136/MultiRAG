"""Membership scope shared by chat and search list endpoints."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import UserTenantRole
from api.db.db_models import Tenant, UserTenant
from common.constants import StatusEnum


async def resolve_list_owner_ids(db: AsyncSession, actor_id: str, requested: list[str] | None) -> list[str]:
    """Validate the complete filter before any application rows are read."""
    stmt = (
        select(Tenant.id)
        .join(UserTenant, UserTenant.tenant_id == Tenant.id)
        .where(
            UserTenant.user_id == actor_id,
            UserTenant.status == StatusEnum.VALID.value,
            UserTenant.role.in_([UserTenantRole.OWNER.value, UserTenantRole.ADMIN.value, UserTenantRole.NORMAL.value]),
            Tenant.status == StatusEnum.VALID.value,
        )
    )
    authorized = set((await db.scalars(stmt)).all())
    owners = set(requested) if requested is not None else authorized
    if not owners.issubset(authorized):
        raise PermissionError("Only authorized owner_ids can be queried.")
    return sorted(owners)
