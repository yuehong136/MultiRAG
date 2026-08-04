"""
@project: multirag
@file: tenant_api_service.py
@desc: Tenant API 业务逻辑层 - 团队成员的邀请 / 移除 / 角色管理。

从 ``api/apps/tenant_app.py`` 抽出，供两个网关层共用：
    - ``api/apps/restful_apis/tenant_api.py``（正典 ``/api/v1/tenants/*``）
    - ``api/apps/tenant_app.py``（deprecated ``/v1/tenant/*``，前端过渡期仍在用）

两个路由模块由 ``register_page`` 各自以 ``spec_from_file_location`` 加载，互相 import
会二次加载出两份模块对象，因此共享逻辑只能落在本层。

约定（与 ``file_api_service.py`` 一致）：本层只吃 ``db: Session``、不 import fastapi、
不返回 HTTP 响应对象。可能带不同错误码的操作统一返回三元组
``(success, result_or_message, retcode)``；发信等副作用交由网关层调度。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from email_validator import EmailNotValidError, validate_email
from sqlalchemy.orm import Session

from api.db import UserTenantRole
from api.db.db_models import File, Tenant, TenantLLM, User, UserTenant
from api.db.services.file_service import FileService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserService, UserTenantService
from common.constants import RetCode, StatusEnum
from common.misc_utils import get_uuid
from common.time_utils import delta_seconds


class InviteResultStatus(StrEnum):
    INVITED = "invited"
    ALREADY_MEMBER = "already_member"
    ALREADY_ADMIN = "already_admin"
    ALREADY_OWNER = "already_owner"
    ALREADY_INVITED = "already_invited"
    USER_NOT_FOUND = "user_not_found"
    INVALID_EMAIL = "invalid_email"


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


# ---------------------------------------------------------------------------
# 成员与租户查询
# ---------------------------------------------------------------------------


def membership_role(db: Session, tenant_id: str, user_id: str) -> str | None:
    """用户在租户中的角色；无成员记录返回 None。"""
    membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
    return membership.role if membership else None


def list_user_tenants(db: Session, user_id: str) -> list[dict]:
    tenants = UserTenantService.get_tenants_by_user_id(db, user_id)
    for tenant in tenants:
        tenant["delta_seconds"] = delta_seconds(tenant["update_date"])
    return tenants


def list_tenant_users(db: Session, tenant_id: str) -> list[dict]:
    users = UserTenantService.get_by_tenant_id(db, tenant_id)
    for user in users:
        user["delta_seconds"] = delta_seconds(str(user["update_date"]))
    return users


def invited_user_profile(db: Session, user_id: str) -> dict | None:
    user = UserService.get_by_id(db, user_id)
    if not user:
        return None
    return {"id": user.id, "avatar": user.avatar, "email": user.email, "nickname": user.nickname}


def inviter_display_name(db: Session, user_id: str, fallback_email: str) -> str:
    inviter = UserService.get_by_id(db, user_id)
    if inviter and inviter.nickname:
        return inviter.nickname
    return fallback_email


# ---------------------------------------------------------------------------
# 邀请
# ---------------------------------------------------------------------------


def _is_valid_email(email: str) -> bool:
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def normalize_batch_emails(emails: list[str]) -> list[str]:
    """去空白、去空串、大小写不敏感去重，保持原始顺序。"""
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


def _build_invite_result(email: str, status: InviteResultStatus, *, user_id: str | None = None, nickname: str | None = None) -> dict:
    result: dict = {
        "email": email,
        "status": status,
        "message": _INVITE_MESSAGE_TEMPLATES[status].format(email=email),
    }
    if user_id:
        result["user_id"] = user_id
        result["nickname"] = nickname
    return result


def invite_user_to_tenant(db: Session, tenant_id: str, invite_user_email: str, inviter_user_id: str) -> dict:
    """落库一条 INVITE 成员记录。

    只写库不发信——邀请邮件是网关层在 ``status == INVITED`` 时用 BackgroundTasks 调度的。
    """
    invite_user_email = invite_user_email.strip()
    if not _is_valid_email(invite_user_email):
        return _build_invite_result(invite_user_email, InviteResultStatus.INVALID_EMAIL)

    invite_users = UserService.query(db, email=invite_user_email)
    if not invite_users:
        return _build_invite_result(invite_user_email, InviteResultStatus.USER_NOT_FOUND)

    invited_user = invite_users[0]
    user_tenant = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=invited_user.id)
    if user_tenant:
        invite_status = _ROLE_TO_INVITE_STATUS.get(user_tenant.role, InviteResultStatus.ALREADY_MEMBER)
        return _build_invite_result(invite_user_email, invite_status, user_id=invited_user.id, nickname=invited_user.nickname)

    UserTenantService.save(
        db=db,
        id=get_uuid(),
        user_id=invited_user.id,
        tenant_id=tenant_id,
        invited_by=inviter_user_id,
        role=UserTenantRole.INVITE,
        status=StatusEnum.VALID.value,
    )
    return _build_invite_result(invite_user_email, InviteResultStatus.INVITED, user_id=invited_user.id, nickname=invited_user.nickname)


def summarize_invite_results(results: list[dict]) -> dict:
    summary: dict = {"total": len(results)}
    for status in InviteResultStatus:
        summary[status.value] = 0
    for result in results:
        summary[result["status"]] += 1
    return summary


# ---------------------------------------------------------------------------
# 成员维护
# ---------------------------------------------------------------------------


def remove_tenant_member(db: Session, tenant_id: str, user_id: str, actor_id: str) -> tuple[bool, Any, RetCode | None]:
    """移除成员；本人退出团队亦走此入口。

    被移除者是 owner 且是本人操作时，连带清理该租户的成员、模型配置、租户与文件记录
    （注销团队语义）。
    """
    actor_role = membership_role(db, tenant_id, actor_id)
    target_role = membership_role(db, tenant_id, user_id)
    is_self = actor_id == user_id
    if not is_self and not UserTenantService.can_manage_members(actor_role):
        return False, "No authorization.", RetCode.AUTHENTICATION_ERROR
    if target_role == UserTenantRole.OWNER and not is_self:
        return False, "Owner cannot be removed by others.", RetCode.AUTHENTICATION_ERROR

    UserTenantService.filter_delete(db, [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user_id])
    if target_role == UserTenantRole.OWNER:
        UserTenantService.filter_delete(db, [UserTenant.tenant_id == tenant_id])
        TenantLLMService.filter_delete(db, [TenantLLM.tenant_id == tenant_id])
        TenantService.filter_delete(db, [Tenant.id == tenant_id])
        UserService.filter_delete(db, [User.id == user_id])
        FileService.filter_delete(db, [File.tenant_id == tenant_id])
    return True, True, None


def update_tenant_member_role(db: Session, tenant_id: str, user_id: str, role: str) -> tuple[bool, Any, RetCode | None]:
    target_membership = UserTenantService.get_membership(db, tenant_id=tenant_id, user_id=user_id)
    if not target_membership:
        return False, "User not found in the team.", RetCode.DATA_ERROR
    if target_membership.role == UserTenantRole.OWNER:
        return False, "Owner role cannot be changed.", RetCode.DATA_ERROR
    if target_membership.role == UserTenantRole.INVITE:
        return False, "Invite role cannot be changed before acceptance.", RetCode.DATA_ERROR
    if target_membership.role == role:
        return True, {"user_id": user_id, "role": target_membership.role}, None

    UserTenantService.filter_update(db, [UserTenant.id == target_membership.id], {"role": role})
    return True, {"user_id": user_id, "role": role}, None


def agree_tenant_invite(db: Session, tenant_id: str, user_id: str) -> bool:
    UserTenantService.filter_update(
        db,
        [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user_id],
        {"role": UserTenantRole.NORMAL},
    )
    return True
