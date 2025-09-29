# coding=utf-8
"""
@project: multirag
@Author：龙
@file： tenant_app.py
@date：2024/10/17 14:24
@desc: 用于处理租户用户操作的路由，包括添加、获取、删除和更新租户成员。
"""

from fastapi import APIRouter, Depends
from sqlalchemy import inspect

from api import settings
from api.db import UserTenantRole, StatusEnum
from api.db.db_models import UserTenant, TenantLLM, Tenant, File, User, get_db
from api.db.services.file_service import FileService
from api.utils import get_uuid, delta_seconds
from api.apps import manager
from api.utils.api_utils import server_error_response, get_data_error_result
from sqlalchemy.orm import Session
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import UserTenantService, UserService, TenantService
from api.utils.api_utils import get_json_result

router = APIRouter()


def object_as_dict(obj):
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


@router.get("/list", summary="获取租户列表", response_model=dict)
def tenant_list(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取当前用户关联的租户列表。

    此接口返回用户为成员的租户列表，以及距离上次更新的时间差。

    返回:
        JSON 响应，其中包含租户列表以及每个租户距离上次更新的时间差（秒）。
    """
    try:
        users = UserTenantService.get_tenants_by_user_id(db, user.id)
        for u in users:
            u["delta_seconds"] = delta_seconds(u["update_date"])
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.get("/<tenant_id>/user/list", summary="获取租户下用户列表", response_model=dict)
def user_list(tenant_id, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取特定租户下的用户列表。

    参数:
       - tenant_id (str): 租户 ID。

    返回:
        JSON 响应，其中包含租户下的用户列表，以及每个用户距离上次更新的时间差（秒）。
    """
    if user.id != tenant_id:
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR)
    try:
        users = UserTenantService.get_by_tenant_id(db, tenant_id)
        for u in users:
            u["delta_seconds"] = delta_seconds(str(u["update_date"]))
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.post('/<tenant_id>/user', summary="新增租户下用户", response_model=dict)
def create(tenant_id, email, db: Session = Depends(get_db), user=Depends(manager)):
    """
    添加新用户到指定租户。

    此接口根据用户邮箱添加用户到租户中。如果用户未找到或已是团队成员，将返回相应提示信息。

    参数:
       - tenant_id (str): 租户 ID。
       - email (str): 要添加的用户邮箱。

    返回:
        JSON 响应，其中包含成功添加的用户信息。
    """
    if user.id != tenant_id:
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR)
    invite_user_email = email
    invite_users = UserService.query(db, email=invite_user_email)
    if not invite_users:
        return get_data_error_result(retmsg="User not found.")

    user_id_to_invite = invite_users[0].id
    user_tenants = UserTenantService.query(db, user_id=user_id_to_invite, tenant_id=tenant_id)
    if user_tenants:
        user_tenant_role = user_tenants[0].role
        if user_tenant_role == UserTenantRole.NORMAL:
            return get_data_error_result(retmsg=f"{invite_user_email} is already in the team.")
        if user_tenant_role == UserTenantRole.OWNER:
            return get_data_error_result(retmsg=f"{invite_user_email} is the owner of the team.")
        return get_data_error_result(
            retmsg=f"{invite_user_email} is in the team, but the role: {user_tenant_role} is invalid.")

    UserTenantService.save(
        db=db,
        id=get_uuid(),
        user_id=user_id_to_invite,
        tenant_id=tenant_id,
        role=UserTenantRole.INVITE,
        invited_by=user.id,  # 默认当前操作的用户是邀请人
        status=StatusEnum.VALID.value)

    # usr = list(usrs.dicts())[0]
    # usr = {k: v for k, v in usr.items() if k in ["id", "avatar", "email", "nickname"]}
    usr = {
        "id": invite_users[0].id,
        "avatar": invite_users[0].avatar,
        "email": invite_users[0].email,
        "nickname": invite_users[0].nickname,
    }

    return get_json_result(data=usr)


@router.delete('/<tenant_id>/user/<user_id>', summary="删除租户下用户", response_model=dict)
def rm(tenant_id, user_id, db: Session = Depends(get_db), user=Depends(manager)):
    """
    从租户中删除指定用户。

    此接口从指定租户中删除指定用户。

    参数:
       - tenant_id (str): 租户 ID。
       - user_id (str): 要删除的用户 ID。

    返回:
        JSON 响应，指示操作是否成功。
    """
    actor_membership = UserTenantService.filter_by_tenant_and_user_id(db, tenant_id=tenant_id, user_id=user.id)
    target_membership = UserTenantService.filter_by_tenant_and_user_id(db, tenant_id=tenant_id, user_id=user_id)
    target_role = target_membership.role if target_membership else None
    is_self = user.id == user_id
    is_manager = actor_membership and actor_membership.role in {UserTenantRole.OWNER, UserTenantRole.ADMIN}
    if not is_self and not is_manager:
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR)
    try:
        if target_role == UserTenantRole.OWNER and not is_self:
            return get_json_result(
                data=False,
                retmsg='Owner cannot be removed by others.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR)
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


@router.put("/agree/<tenant_id>", summary="同意加入租户", response_model=dict)
def agree(tenant_id, db: Session = Depends(get_db), user=Depends(manager)):
    """
    同意加入租户。

    此接口用于用户接受加入租户的邀请。

    参数:
       - tenant_id (str): 要加入的租户 ID。

    返回:
        JSON 响应，指示操作是否成功。
    """
    try:
        UserTenantService.filter_update(db, [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user.id], {"role": UserTenantRole.NORMAL})
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)