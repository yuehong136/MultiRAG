# coding=utf-8
"""
@project: multirag
@Author：龙
@file： tenant_app.py
@date：2024/9/13 9:19
@desc:
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import inspect

from api.utils.api_utils import server_error_response
from api.apps import manager
from api.db.database import get_db
from api.utils.api_utils import get_json_result, server_error_response, validate_request, get_data_error_result
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
