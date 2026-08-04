"""Tenant RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``:
    GET    /tenants
    PATCH  /tenants/{tenant_id}
    GET    /tenants/{tenant_id}/users
    POST   /tenants/{tenant_id}/users
    POST   /tenants/{tenant_id}/users/batch
    DELETE /tenants/{tenant_id}/users
    PUT    /tenants/{tenant_id}/users/{user_id}/role

业务逻辑在 ``api/apps/services/tenant_api_service.py``；旧的 ``/v1/tenant/*`` 端点
留在 ``api/apps/tenant_app.py`` 并已标 deprecated（前端过渡期仍在用）。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import tenant_api_service
from api.db.db_models import get_async_db
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import BusinessError, Principal, async_current_user, get_data_error_result, get_json_result, server_error_response
from api.utils.web_utils import send_invite_email
from common import settings
from common.constants import RetCode

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class InviteUserRequest(BaseModel):
    email: EmailStr


class BatchInviteUsersRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)


class RemoveTenantUserRequest(BaseModel):
    user_id: str


class UpdateTenantMemberRoleRequest(BaseModel):
    role: Literal["admin", "normal"]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def _member_role(tenant_id: str, db: AsyncSession, user_id: str) -> str | None:
    return await db.run_sync(lambda s: tenant_api_service.membership_role(s, tenant_id, user_id))  # TODO(async-phase4)


async def require_member_manager(
    tenant_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> str:
    role = await _member_role(tenant_id, db, user.id)
    if not UserTenantService.can_manage_members(role):
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return role


async def require_role_manager(
    tenant_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> str:
    role = await _member_role(tenant_id, db, user.id)
    if not UserTenantService.can_manage_roles(role):
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return role


def _schedule_invite_email(background_tasks: BackgroundTasks, tenant_id: str, to_email: str, inviter: str) -> None:
    background_tasks.add_task(
        send_invite_email,
        to_email=to_email,
        invite_url=settings.MAIL_FRONTEND_URL,
        tenant_id=tenant_id,
        inviter=inviter,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/tenants", summary="获取当前用户加入的租户列表")
async def tenant_list(
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        tenants = await db.run_sync(lambda s: tenant_api_service.list_user_tenants(s, user.id))  # TODO(async-phase4)
        return get_json_result(data=tenants)
    except Exception as e:
        return server_error_response(e)


@router.patch("/tenants/{tenant_id}", summary="同意加入租户")
async def agree(
    tenant_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        await db.run_sync(lambda s: tenant_api_service.agree_tenant_invite(s, tenant_id, user.id))  # TODO(async-phase4)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get("/tenants/{tenant_id}/users", summary="获取租户下用户列表")
async def user_list(
    tenant_id: str,
    db: AsyncSession = Depends(get_async_db),
    _role: str = Depends(require_member_manager),
):
    try:
        users = await db.run_sync(lambda s: tenant_api_service.list_tenant_users(s, tenant_id))  # TODO(async-phase4)
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.post("/tenants/{tenant_id}/users", summary="邀请用户加入租户")
async def create(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    request_body: InviteUserRequest = Body(...),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
    _role: str = Depends(require_member_manager),
):
    def _invite(s):
        inviter = tenant_api_service.inviter_display_name(s, user.id, user.email)
        result = tenant_api_service.invite_user_to_tenant(s, tenant_id, request_body.email, user.id)
        profile = tenant_api_service.invited_user_profile(s, result["user_id"]) if result.get("user_id") else None
        return inviter, result, profile

    inviter, invite_result, profile = await db.run_sync(_invite)  # TODO(async-phase4)
    if invite_result["status"] != tenant_api_service.InviteResultStatus.INVITED:
        return get_data_error_result(retmsg=invite_result["message"])
    if not profile:
        return get_data_error_result(retmsg="User not found.")

    _schedule_invite_email(background_tasks, tenant_id, invite_result["email"], inviter)
    return get_json_result(data=profile)


@router.post("/tenants/{tenant_id}/users/batch", summary="批量邀请用户加入租户")
async def batch_create(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    request_body: BatchInviteUsersRequest = Body(...),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
    _role: str = Depends(require_member_manager),
):
    emails = tenant_api_service.normalize_batch_emails(request_body.emails)

    def _invite_all(s):
        inviter = tenant_api_service.inviter_display_name(s, user.id, user.email)
        return inviter, [tenant_api_service.invite_user_to_tenant(s, tenant_id, email, user.id) for email in emails]

    inviter, results = await db.run_sync(_invite_all)  # TODO(async-phase4)
    for result in results:
        if result["status"] == tenant_api_service.InviteResultStatus.INVITED:
            _schedule_invite_email(background_tasks, tenant_id, result["email"], inviter)

    return get_json_result(data={"results": results, "summary": tenant_api_service.summarize_invite_results(results)})


@router.delete("/tenants/{tenant_id}/users", summary="移除租户下用户")
async def rm(
    tenant_id: str,
    request_body: RemoveTenantUserRequest = Body(...),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:
        success, result, retcode = await db.run_sync(  # TODO(async-phase4)
            lambda s: tenant_api_service.remove_tenant_member(s, tenant_id, request_body.user_id, user.id)
        )
    except Exception as e:
        return server_error_response(e)
    if not success:
        return get_json_result(data=False, retmsg=result, retcode=retcode or RetCode.AUTHENTICATION_ERROR)
    return get_json_result(data=result)


@router.put("/tenants/{tenant_id}/users/{user_id}/role", summary="更新租户成员角色")
async def update_member_role(
    tenant_id: str,
    user_id: str,
    request_body: UpdateTenantMemberRoleRequest = Body(...),
    db: AsyncSession = Depends(get_async_db),
    _role: str = Depends(require_role_manager),
):
    success, result, retcode = await db.run_sync(  # TODO(async-phase4)
        lambda s: tenant_api_service.update_tenant_member_role(s, tenant_id, user_id, request_body.role)
    )
    if not success:
        return get_data_error_result(retcode=retcode or RetCode.DATA_ERROR, retmsg=result)
    return get_json_result(data=result)
