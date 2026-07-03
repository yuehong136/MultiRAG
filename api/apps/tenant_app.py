from enum import StrEnum
from typing import Literal

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, BackgroundTasks, Body, Depends
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from api.apps import manager
from api.db import UserTenantRole
from api.db.db_models import File, Tenant, TenantLLM, User, UserTenant, get_db
from api.db.services.file_service import FileService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserService, UserTenantService
from api.utils.api_utils import BusinessError, get_data_error_result, get_json_result, server_error_response
from api.utils.web_utils import send_invite_email
from common import settings
from common.constants import RetCode, StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import delta_seconds

router = APIRouter()


# ---------------------------------------------------------------------------
# Enums & Schemas
# ---------------------------------------------------------------------------

class InviteResultStatus(StrEnum):
    INVITED = "invited"
    ALREADY_MEMBER = "already_member"
    ALREADY_ADMIN = "already_admin"
    ALREADY_OWNER = "already_owner"
    ALREADY_INVITED = "already_invited"
    USER_NOT_FOUND = "user_not_found"
    INVALID_EMAIL = "invalid_email"


class InviteUserRequest(BaseModel):
    email: EmailStr


class BatchInviteUsersRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)


class UpdateTenantMemberRoleRequest(BaseModel):
    role: Literal["admin", "normal"]


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def _get_membership(
    tenant_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager),
) -> UserTenant:
    membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user.id)
    if not membership:
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return membership


def require_member_manager(
    membership: UserTenant = Depends(_get_membership),
) -> UserTenant:
    if not UserTenantService.can_manage_members(membership.role):
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return membership


def require_role_manager(
    membership: UserTenant = Depends(_get_membership),
) -> UserTenant:
    if not UserTenantService.can_manage_roles(membership.role):
        raise BusinessError(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
    return membership


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def object_as_dict(obj):
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


def _validate_email_safe(email: str) -> bool:
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def _normalize_batch_emails(emails: list[str]) -> list[str]:
    normalized_emails: list[str] = []
    seen: set[str] = set()
    for raw_email in emails:
        email = (raw_email or "").strip()
        if not email:
            continue
        email_key = email.casefold()
        if email_key in seen:
            continue
        seen.add(email_key)
        normalized_emails.append(email)
    return normalized_emails


def _get_inviter_display_name(db: Session, user) -> str:
    inviter_user = UserService.get_by_id(db, user.id)
    if inviter_user and inviter_user.nickname:
        return inviter_user.nickname
    return user.email


_ROLE_TO_INVITE_STATUS: dict[UserTenantRole, InviteResultStatus] = {
    UserTenantRole.NORMAL: InviteResultStatus.ALREADY_MEMBER,
    UserTenantRole.ADMIN: InviteResultStatus.ALREADY_ADMIN,
    UserTenantRole.OWNER: InviteResultStatus.ALREADY_OWNER,
    UserTenantRole.INVITE: InviteResultStatus.ALREADY_INVITED,
}

_INVITE_MESSAGE_TEMPLATES: dict[InviteResultStatus, str] = {
    InviteResultStatus.INVITED: "Invitation sent to {email}.",
    InviteResultStatus.ALREADY_MEMBER: "{email} is already in the team.",
    InviteResultStatus.ALREADY_ADMIN: "{email} is already an admin of the team.",
    InviteResultStatus.ALREADY_OWNER: "{email} is the owner of the team.",
    InviteResultStatus.ALREADY_INVITED: "{email} has already been invited.",
    InviteResultStatus.USER_NOT_FOUND: "User not found.",
    InviteResultStatus.INVALID_EMAIL: "Invalid email address: {email}.",
}


def _build_invite_result(
    email: str,
    status: InviteResultStatus,
    invited_user: User | None = None,
) -> dict:
    result: dict = {
        "email": email,
        "status": status,
        "message": _INVITE_MESSAGE_TEMPLATES[status].format(email=email),
    }
    if invited_user:
        result["user_id"] = invited_user.id
        result["nickname"] = invited_user.nickname
    return result


def _invite_user_to_tenant(
    db: Session,
    tenant_id: str,
    invite_user_email: str,
    inviter_user_id: str,
    inviter_display_name: str,
    background_tasks: BackgroundTasks,
) -> dict:
    invite_user_email = invite_user_email.strip()
    if not _validate_email_safe(invite_user_email):
        return _build_invite_result(invite_user_email, InviteResultStatus.INVALID_EMAIL)

    invite_users = UserService.query(db, email=invite_user_email)
    if not invite_users:
        return _build_invite_result(invite_user_email, InviteResultStatus.USER_NOT_FOUND)

    invited_user = invite_users[0]
    user_tenant = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=invited_user.id)
    if user_tenant:
        invite_status = _ROLE_TO_INVITE_STATUS.get(user_tenant.role, InviteResultStatus.ALREADY_MEMBER)
        return _build_invite_result(invite_user_email, invite_status, invited_user=invited_user)

    UserTenantService.save(
        db=db,
        id=get_uuid(),
        user_id=invited_user.id,
        tenant_id=tenant_id,
        invited_by=inviter_user_id,
        role=UserTenantRole.INVITE,
        status=StatusEnum.VALID.value,
    )
    background_tasks.add_task(
        send_invite_email,
        to_email=invite_user_email,
        invite_url=settings.MAIL_FRONTEND_URL,
        tenant_id=tenant_id,
        inviter=inviter_display_name,
    )
    return _build_invite_result(invite_user_email, InviteResultStatus.INVITED, invited_user=invited_user)


def _summarize_invite_results(results: list[dict]) -> dict:
    summary: dict = {"total": len(results)}
    for status in InviteResultStatus:
        summary[status.value] = 0
    for result in results:
        summary[result["status"]] += 1
    return summary


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/list", summary="获取租户列表")
def tenant_list(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        users = UserTenantService.get_tenants_by_user_id(db, user.id)
        for u in users:
            u["delta_seconds"] = delta_seconds(u["update_date"])
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.get("/{tenant_id}/user/list", summary="获取租户下用户列表")
def user_list(
    tenant_id: str,
    db: Session = Depends(get_db),
    _membership: UserTenant = Depends(require_member_manager),
):
    try:
        users = UserTenantService.get_by_tenant_id(db, tenant_id)
        for u in users:
            u["delta_seconds"] = delta_seconds(str(u["update_date"]))
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.post('/{tenant_id}/user', summary="新增租户下用户")
def create(
    tenant_id: str,
    request_body: InviteUserRequest = Body(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    user=Depends(manager),
    _membership: UserTenant = Depends(require_member_manager),
):
    invite_result = _invite_user_to_tenant(
        db=db,
        tenant_id=tenant_id,
        invite_user_email=request_body.email,
        inviter_user_id=user.id,
        inviter_display_name=_get_inviter_display_name(db, user),
        background_tasks=background_tasks,
    )
    if invite_result["status"] != InviteResultStatus.INVITED:
        return get_data_error_result(retmsg=invite_result["message"])

    invited_user = UserService.get_by_id(db, invite_result["user_id"])
    if not invited_user:
        return get_data_error_result(retmsg="User not found.")

    return get_json_result(data={
        "id": invited_user.id,
        "avatar": invited_user.avatar,
        "email": invited_user.email,
        "nickname": invited_user.nickname,
    })


@router.post('/{tenant_id}/user/batch', summary="批量新增租户下用户")
def batch_create(
    tenant_id: str,
    request_body: BatchInviteUsersRequest = Body(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    user=Depends(manager),
    _membership: UserTenant = Depends(require_member_manager),
):
    inviter_display_name = _get_inviter_display_name(db, user)
    normalized_emails = _normalize_batch_emails(request_body.emails)
    results = [
        _invite_user_to_tenant(
            db=db,
            tenant_id=tenant_id,
            invite_user_email=email,
            inviter_user_id=user.id,
            inviter_display_name=inviter_display_name,
            background_tasks=background_tasks,
        )
        for email in normalized_emails
    ]
    return get_json_result(data={
        "results": results,
        "summary": _summarize_invite_results(results),
    })


@router.delete('/{tenant_id}/user/{user_id}', summary="删除租户下用户")
def rm(tenant_id: str, user_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    actor_membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user.id)
    target_membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
    target_role = target_membership.role if target_membership else None
    is_self = user.id == user_id
    is_manager = actor_membership and UserTenantService.can_manage_members(actor_membership.role)
    if not is_self and not is_manager:
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR)
    try:
        if target_role == UserTenantRole.OWNER and not is_self:
            return get_json_result(
                data=False,
                retmsg='Owner cannot be removed by others.',
                retcode=RetCode.AUTHENTICATION_ERROR)
        UserTenantService.filter_delete(db, [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user_id])
        if target_role == UserTenantRole.OWNER:
            UserTenantService.filter_delete(db, [UserTenant.tenant_id == tenant_id])
            TenantLLMService.filter_delete(db, [TenantLLM.tenant_id == tenant_id])
            TenantService.filter_delete(db, [Tenant.id == tenant_id])
            UserService.filter_delete(db, [User.id == user_id])
            FileService.filter_delete(db, [File.tenant_id == tenant_id])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.put('/{tenant_id}/user/{user_id}/role', summary="更新租户成员角色")
def update_member_role(
    tenant_id: str,
    user_id: str,
    request_body: UpdateTenantMemberRoleRequest = Body(...),
    db: Session = Depends(get_db),
    _membership: UserTenant = Depends(require_role_manager),
):
    target_membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
    if not target_membership:
        return get_data_error_result(retmsg="User not found in the team.")

    if target_membership.role == UserTenantRole.OWNER:
        return get_data_error_result(retmsg="Owner role cannot be changed.")

    if target_membership.role == UserTenantRole.INVITE:
        return get_data_error_result(retmsg="Invite role cannot be changed before acceptance.")

    if target_membership.role == request_body.role:
        return get_json_result(data={"user_id": user_id, "role": target_membership.role})

    UserTenantService.filter_update(
        db,
        [UserTenant.id == target_membership.id],
        {"role": request_body.role},
    )
    return get_json_result(data={"user_id": user_id, "role": request_body.role})


@router.put("/agree/{tenant_id}", summary="同意加入租户")
def agree(tenant_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        UserTenantService.filter_update(db, [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user.id], {"role": UserTenantRole.NORMAL})
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
