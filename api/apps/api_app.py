# coding=utf-8
"""
@project: multirag
@Author：龙
@file： api_app.py
@date：2024/7/22 16:02
@desc: API 管理接口
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, get_db
from api.db.services.api_service import APITokenService, API4ConversationService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import server_error_response, get_data_error_result, get_json_result, \
    generate_confirmation_token
from common.time_utils import current_timestamp, datetime_format

from api.apps import manager


class NewTokenRequest(BaseModel):
    dialog_id: str | None = None
    """对话的唯一标识符。"""
    canvas_id: str | None = None
    """画布的唯一标识符。"""
    tenant_id: str
    """租户的唯一标识符。"""


class RemoveTokenRequest(BaseModel):
    tokens: list[str]
    """要删除的API令牌列表。"""

    tenant_id: str
    """租户的唯一标识符。"""


router = APIRouter()


@router.post('/new_token', summary="生成新的API令牌", response_description="成功生成新的API令牌")
def new_token(request: NewTokenRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    生成新的API令牌

    该接口用于为指定租户生成新的API令牌。

    参数:
    - request: NewTokenRequest对象，包含租户的唯一标识符
        - tenant_id: str 租户的唯一标识符

    返回:
    - 成功时返回包含新API令牌的JSON结果
    - 失败时返回错误信息
    """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        tenant_id = tenants[0].tenant_id
        obj = {"tenant_id": tenant_id, "token": generate_confirmation_token(),
               "dialog_id": request.tenant_id,
               "create_time": current_timestamp(),
               "create_date": datetime_format(datetime.now()),
               "update_time": None,
               "update_date": None
               }
        if request.canvas_id:
            obj["dialog_id"] = request.canvas_id
            obj["source"] = "agent"
        else:
            obj["dialog_id"] = request.dialog_id
        if not APITokenService.save(db, **obj):
            return get_data_error_result(retmsg="Fail to new a dialog!")

        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@router.get('/token_list', summary="获取API令牌列表", response_description="成功获取API令牌列表")
def token_list(
    dialog_id: str | None = Query(None, alias="dialog_id"),
    canvas_id: str | None = Query(None, alias="canvas_id"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    获取API令牌列表

    该接口用于获取指定对话的API令牌列表。

    参数:
    - dialog_id: str 对话的唯一标识符
    - canvas_id: str 画布的唯一标识符

    返回:
    - 成功时返回包含API令牌列表的JSON结果
    - 失败时返回错误信息
    """
    try:
        # 优先使用 dialog_id，如果 dialog_id 不存在，则使用 canvas_id
        id = dialog_id if dialog_id is not None else canvas_id
        if not id:
            raise HTTPException(status_code=400, detail="Either dialog_id or canvas_id must be provided")

        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        objs = APITokenService.query(db, tenant_id=tenants[0].tenant_id, dialog_id=id)
        return get_json_result(data=[o.to_dict() for o in objs])
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除API令牌", response_description="成功删除API令牌")
def rm(request: RemoveTokenRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
   删除API令牌

   该接口用于删除指定租户的API令牌。

   参数:
   - request: RemoveTokenRequest对象，包含要删除的API令牌和租户的唯一标识符
       - tokens: List[str] 要删除的API令牌列表
       - tenant_id: str 租户的唯一标识符

   返回:
   - 成功时返回成功删除的JSON结果
   - 失败时返回错误信息
   """
    try:
        for token in request.tokens:
            APITokenService.filter_delete(
                db,
                [APIToken.tenant_id == request.tenant_id, APIToken.token == token])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取API使用统计", response_description="成功获取API使用统计")
def stats(from_date: str = None, to_date: str = None, canvas_id: str = None, db: Session = Depends(get_db), user=Depends(manager)):
    """
   获取API使用统计

   该接口用于获取API的使用统计信息。

   参数:
   - from_date: str 起始日期，格式为 YYYY-MM-DD，默认为过去7天
   - to_date: str 结束日期，格式为 YYYY-MM-DD，默认为当前日期

   返回:
   - 成功时返回包含API使用统计的JSON结果
   - 失败时返回错误信息
   """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")
        from_date = from_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        to_date = to_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        objs = API4ConversationService.stats(db, tenants[0].tenant_id, from_date, to_date, "agent" if canvas_id else None)

        res = {"pv": [], "uv": [], "speed": [], "tokens": [], "round": [], "thumb_up": []}

        for obj in objs:
            dt = obj["dt"]
            res["pv"].append((dt, obj["pv"]))
            res["uv"].append((dt, obj["uv"]))
            res["speed"].append((dt, float(obj["tokens"]) / (float(obj["duration"]) + 0.1))) # +0.1 to avoid division by zero
            res["tokens"].append((dt, float(obj["tokens"]) / 1000.0)) # convert to thousands
            res["round"].append((dt, obj["round"]))
            res["thumb_up"].append((dt, obj["thumb_up"]))

        return get_json_result(data=res)
    except Exception as e:
        return server_error_response(e)
