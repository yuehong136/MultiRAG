"""
@project: multirag
@Author：龙
@file： api_app.py
@date：2024/7/22 16:02
@desc: API 管理接口
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.api_service import API4ConversationService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response

router = APIRouter()


@router.get("/stats", summary="获取API使用统计", response_description="成功获取API使用统计")
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
            res["speed"].append((dt, float(obj["tokens"]) / (float(obj["duration"]) + 0.1)))  # +0.1 to avoid division by zero
            res["tokens"].append((dt, float(obj["tokens"]) / 1000.0))  # convert to thousands
            res["round"].append((dt, obj["round"]))
            res["thumb_up"].append((dt, obj["thumb_up"]))

        return get_json_result(data=res)
    except Exception as e:
        return server_error_response(e)
