"""Credentials and resource binding for Agent runtime file gateways only."""

from fastapi import Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import APIToken, Tenant, User, UserCanvas, get_async_db
from api.db.services.canvas_service import require_agent_access
from api.utils.api_utils import BusinessError, Principal, SDKAuthError, async_beta_token_required, async_current_user
from common.constants import RetCode, StatusEnum


def _bearer_credential(request: Request) -> str:
    headers = request.headers.getlist("authorization")
    parts = headers[0].split() if len(headers) == 1 else []
    if len(parts) != 2 or parts[0].lower() != "bearer" or "," in parts[1]:
        raise SDKAuthError("Provide exactly one Bearer credential.")
    return parts[1]


async def require_agent_file_user(request: Request, db: AsyncSession = Depends(get_async_db)) -> Principal:
    """Downloads accept a single Web/API credential, never caller owner fields."""
    _bearer_credential(request)
    return await async_current_user(request, db)


async def require_agent_upload_owner(request: Request, canvas_id: str, db: AsyncSession = Depends(get_async_db)) -> str:
    """Bind JWT/API callers or a unique active beta tenant to the path canvas.

    Beta is deliberately local to this gateway: it is not a platform Principal.
    Authenticated TEAM callers write in their own runtime namespace, matching
    attachment restoration during their run. Public beta runs use the owner.
    """
    credential = _bearer_credential(request)
    beta_tenant = None
    if credential.count(".") != 2:
        tokens = list(await db.scalars(select(APIToken).where(or_(APIToken.token == credential, APIToken.beta == credential))))
        if len(tokens) != 1 or (tokens[0].token == credential and tokens[0].beta == credential):
            raise SDKAuthError("Authentication error: invalid or ambiguous credentials.")
        if tokens[0].beta == credential:
            beta_tenant = await async_beta_token_required(request, db)

    owner_id = await db.scalar(
        select(UserCanvas.user_id)
        .join(User, User.id == UserCanvas.user_id)
        .join(Tenant, Tenant.id == UserCanvas.user_id)
        .where(UserCanvas.id == canvas_id, User.status == StatusEnum.VALID.value, User.is_active.is_(True), Tenant.status == StatusEnum.VALID.value)
    )
    if owner_id is None:
        raise BusinessError("Make sure you have permission to access the agent.", RetCode.OPERATING_ERROR)
    if beta_tenant is not None:
        if owner_id != beta_tenant:
            raise BusinessError("Make sure you have permission to access the agent.", RetCode.OPERATING_ERROR)
        return beta_tenant

    user = await async_current_user(request, db)
    try:
        await require_agent_access(db, canvas_id, user.id)
    except (LookupError, PermissionError) as error:
        raise BusinessError("Make sure you have permission to access the agent.", RetCode.OPERATING_ERROR) from error
    return user.id
