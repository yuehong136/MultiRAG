"""[Deprecated] 旧的 web 团队管理端点 ``/v1/tenant/*``。

团队管理已收编到 ``/api/v1/tenants/*``（``api/apps/restful_apis/tenant_api.py``），
业务逻辑统一在 ``api/apps/services/tenant_api_service.py``。本模块仅为前端等既有
消费方保留，待其全部迁移后移除；新代码请直接打 RESTful 端点。
"""

from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Body, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.apps.services import tenant_api_service
from api.db.db_models import get_db
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import BusinessError, get_data_error_result, get_json_result, server_error_response
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


class UpdateTenantMemberRoleRequest(BaseModel):
    role: Literal["admin", "normal"]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def require_member_manager(
    tenant_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager),
) -> str:
    role = tenant_api_service.membership_role(db, tenant_id, user.id)
    if not UserTenantService.can_manage_members(role):
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return role


def require_role_manager(
    tenant_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager),
) -> str:
    role = tenant_api_service.membership_role(db, tenant_id, user.id)
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
# Routes（全部 deprecated，逐条对应 /api/v1/tenants/*）
# ---------------------------------------------------------------------------


@router.get("/list", summary="[Deprecated] 获取租户列表（请改用 GET /api/v1/tenants）", deprecated=True)
def tenant_list(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        return get_json_result(data=tenant_api_service.list_user_tenants(db, user.id))
    except Exception as e:
        return server_error_response(e)


@router.get(
    "/{tenant_id}/user/list",
    summary="[Deprecated] 获取租户下用户列表（请改用 GET /api/v1/tenants/{tenant_id}/users）",
    deprecated=True,
)
def user_list(
    tenant_id: str,
    db: Session = Depends(get_db),
    _role: str = Depends(require_member_manager),
):
    try:
        return get_json_result(data=tenant_api_service.list_tenant_users(db, tenant_id))
    except Exception as e:
        return server_error_response(e)


@router.post(
    "/{tenant_id}/user",
    summary="[Deprecated] 新增租户下用户（请改用 POST /api/v1/tenants/{tenant_id}/users）",
    deprecated=True,
)
def create(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    request_body: InviteUserRequest = Body(...),
    db: Session = Depends(get_db),
    user=Depends(manager),
    _role: str = Depends(require_member_manager),
):
    inviter = tenant_api_service.inviter_display_name(db, user.id, user.email)
    invite_result = tenant_api_service.invite_user_to_tenant(db, tenant_id, request_body.email, user.id)
    if invite_result["status"] != tenant_api_service.InviteResultStatus.INVITED:
        return get_data_error_result(retmsg=invite_result["message"])

    profile = tenant_api_service.invited_user_profile(db, invite_result["user_id"])
    if not profile:
        return get_data_error_result(retmsg="User not found.")

    _schedule_invite_email(background_tasks, tenant_id, invite_result["email"], inviter)
    return get_json_result(data=profile)


@router.post(
    "/{tenant_id}/user/batch",
    summary="[Deprecated] 批量新增租户下用户（请改用 POST /api/v1/tenants/{tenant_id}/users/batch）",
    deprecated=True,
)
def batch_create(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    request_body: BatchInviteUsersRequest = Body(...),
    db: Session = Depends(get_db),
    user=Depends(manager),
    _role: str = Depends(require_member_manager),
):
    inviter = tenant_api_service.inviter_display_name(db, user.id, user.email)
    results = [tenant_api_service.invite_user_to_tenant(db, tenant_id, email, user.id) for email in tenant_api_service.normalize_batch_emails(request_body.emails)]
    for result in results:
        if result["status"] == tenant_api_service.InviteResultStatus.INVITED:
            _schedule_invite_email(background_tasks, tenant_id, result["email"], inviter)

    return get_json_result(data={"results": results, "summary": tenant_api_service.summarize_invite_results(results)})


@router.delete(
    "/{tenant_id}/user/{user_id}",
    summary="[Deprecated] 删除租户下用户（请改用 DELETE /api/v1/tenants/{tenant_id}/users）",
    deprecated=True,
)
def rm(tenant_id: str, user_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        success, result, retcode = tenant_api_service.remove_tenant_member(db, tenant_id, user_id, user.id)
    except Exception as e:
        return server_error_response(e)
    if not success:
        return get_json_result(data=False, retmsg=result, retcode=retcode or RetCode.AUTHENTICATION_ERROR)
    return get_json_result(data=result)


@router.put(
    "/{tenant_id}/user/{user_id}/role",
    summary="[Deprecated] 更新租户成员角色（请改用 PUT /api/v1/tenants/{tenant_id}/users/{user_id}/role）",
    deprecated=True,
)
def update_member_role(
    tenant_id: str,
    user_id: str,
    request_body: UpdateTenantMemberRoleRequest = Body(...),
    db: Session = Depends(get_db),
    _role: str = Depends(require_role_manager),
):
    success, result, retcode = tenant_api_service.update_tenant_member_role(db, tenant_id, user_id, request_body.role)
    if not success:
        return get_data_error_result(retcode=retcode or RetCode.DATA_ERROR, retmsg=result)
    return get_json_result(data=result)


@router.put(
    "/agree/{tenant_id}",
    summary="[Deprecated] 同意加入租户（请改用 PATCH /api/v1/tenants/{tenant_id}）",
    deprecated=True,
)
def agree(tenant_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        tenant_api_service.agree_tenant_invite(db, tenant_id, user.id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
