# coding=utf-8
"""
@project: multirag
@Author：龙
@file： tenant_app.py
@date：2024/9/13 9:19
@desc:
"""

from fastapi import APIRouter, Depends
from sqlalchemy import inspect

from api.db import UserTenantRole, StatusEnum
from api.db.db_models import UserTenant
from api.settings import RetCode
from api.utils import get_uuid
from api.apps import manager
from api.db.database import get_db
from api.utils.api_utils import server_error_response
from sqlalchemy.orm import Session
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import get_json_result

router = APIRouter()

def object_as_dict(obj):
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}

@router.get("/list", summary="获取租户列表", response_model=dict)
async def tenant_list(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        tenants = TenantService.get_by_user_id(db, user.id)
        return get_json_result(data=tenants)
    except Exception as e:
        return server_error_response(e)


@router.get("/<tenant_id>/user/list", summary="获取租户下用户列表", response_model=dict)
async def user_list(tenant_id, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        users = UserTenantService.get_by_tenant_id(db, tenant_id)
        return get_json_result(data=users)
    except Exception as e:
        return server_error_response(e)


@router.post('/<tenant_id>/user', summary="新增租户下用户", response_model=dict)
def create(tenant_id, user_id, db: Session = Depends(get_db), user=Depends(manager)):
    if not user_id:
        return get_json_result(
            data=False, retmsg='Lack of "USER ID"', retcode=RetCode.ARGUMENT_ERROR)

    try:
        user_tenants = UserTenantService.query(db, user_id=user_id, tenant_id=tenant_id)
        if user_tenants:
            uuid = user_tenants[0].id
            return get_json_result(data={"id": uuid})

        uuid = get_uuid()
        UserTenantService.save(
            db,
            id=uuid,
            user_id=user_id,
            tenant_id=tenant_id,
            role=UserTenantRole.NORMAL.value,
            status=StatusEnum.VALID.value)

        return get_json_result(data={"id": uuid})
    except Exception as e:
        return server_error_response(e)


@router.delete('/<tenant_id>/user/<user_id>', summary="删除租户下用户", response_model=dict)
def rm(tenant_id, user_id, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        UserTenantService.filter_delete(db, [UserTenant.tenant_id == tenant_id, UserTenant.user_id == user_id])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
