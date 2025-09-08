# coding=utf-8
"""
@project: multirag
@Author：龙
@file： xxx.py
@date：2024/7/9 9:00
@desc:
"""
import logging
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Body
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator, ConfigDict, ValidationError
from typing import Annotated, Any

from api.apps.api_app import generate_confirmation_token
from api.db.db_models import APIToken, get_db, DATABASE_TYPE
from api.db.services.api_service import APITokenService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import UserTenantService
from api.utils import current_timestamp, datetime_format
from api import settings
from api.utils.api_utils import get_json_result, get_data_error_result, server_error_response
from api.versions import get_multirag_version
from core.utils.storage_factory import STORAGE_IMPL, STORAGE_IMPL_TYPE
from timeit import default_timer as timer
from core.utils.redis_conn import REDIS_CONN
from api.apps import manager

router = APIRouter()


class TokenCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=20, description="Token名称")]
    description: str | None = Field(None, description="Token描述")

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not v:
            raise ValueError('Token名称不能为空')
        if len(v) > 20:
            raise ValueError('Token名称不能超过20个字符')
        return v

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            return None
        return v


class TokenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    token: str
    name: str
    description: str | None
    beta: str
    create_date: str
    create_time: int
    update_date: str
    update_time: int

@router.get("/version", summary="获取版本", response_description="成功获取版本")
async def version(user=Depends(manager)):
    """
    获取系统版本信息的接口说明文档。

    概要：返回系统当前版本信息。
    响应描述：成功获取系统版本信息，并返回 JSON 格式的数据。

    返回：
    - dict: 包含系统版本信息的 JSON 结果。

    功能：
    1. 从系统中获取当前版本信息。
    2. 返回该版本信息以便前端或用户了解系统的当前版本。

    异常处理：
    - 若出现异常，将返回错误信息。

    注意：
    - 该接口不涉及数据库操作，仅返回静态版本信息。
    """
    return get_json_result(data=get_multirag_version())


@router.get("/status", summary="获取系统状态", response_description="成功获取系统状态")
async def status(db: Session = Depends(get_db), user=Depends(manager)):
    """
    检查系统状态的接口说明文档。

    概要：获取系统的健康状态，包括 Milvus、存储、数据库和 Redis 等组件的状态。
    响应描述：成功获取并返回系统各组件的状态信息。

    返回：
    - dict: 返回包含各个组件健康状态及耗时信息的 JSON 结果。

    功能：
    1. 检查 Milvus 服务的健康状态并记录响应耗时。
    2. 检查存储服务的健康状态并记录响应耗时。
    3. 检查数据库服务的健康状态并记录响应耗时。
    4. 检查 Redis 服务的健康状态并记录响应耗时。
    5. 检查任务执行器的运行状态，并提供详细的延迟时间信息。

    流程：
    1. 尝试获取 Milvus 服务的健康状态，并记录响应耗时。
    2. 检查存储服务是否正常运行，并记录响应耗时。
    3. 尝试访问数据库，以确保数据库连接正常，并记录响应耗时。
    4. 检查 Redis 是否正常连接，并记录响应耗时。
    5. 获取任务执行器的状态信息，并提供详细的任务延迟数据。

    异常处理：
    - 如果在检查任何组件的健康状态过程中发生异常，将在响应结果中返回相应的错误信息。

    注意：
    - 所有组件的健康状态均会在 JSON 结果中返回，便于实时监控系统状态。
    """
    res = {}
    st = timer()
    try:
        res["doc_engine"] = settings.docStoreConn.health()
        res["doc_engine"]["elapsed"] = "{:.1f}".format((timer() - st) * 1000.0)
    except Exception as e:
        res["doc_engine"] = {
            "type": "unknown",
            "status": "red",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
            "error": str(e),
        }

    st = timer()
    try:
        STORAGE_IMPL.health()
        res["storage"] = {
            "storage": STORAGE_IMPL_TYPE.lower(),
            "status": "green",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
        }
    except Exception as e:
        res["storage"] = {
            "storage": STORAGE_IMPL_TYPE.lower(),
            "status": "red",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
            "error": str(e),
        }

    st = timer()
    try:
        KnowledgebaseService.get_by_id(db, "x")
        res["database"] = {
            "database": DATABASE_TYPE.lower(),
            "status": "green",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
        }
    except Exception as e:
        res["database"] = {
            "database": DATABASE_TYPE.lower(),
            "status": "red",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
            "error": str(e),
        }

    st = timer()
    try:
        if not REDIS_CONN.health():
            raise Exception("Lost connection!")
        res["redis"] = {
            "status": "green",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
        }
    except Exception as e:
        res["redis"] = {
            "status": "red",
            "elapsed": "{:.1f}".format((timer() - st) * 1000.0),
            "error": str(e),
        }

    task_executor_heartbeats = {}
    try:
        task_executors = REDIS_CONN.smembers("TASKEXE")
        now = datetime.now().timestamp()
        for task_executor_id in task_executors:
            heartbeats = REDIS_CONN.zrangebyscore(task_executor_id, now - 60*30, now)
            heartbeats = [json.loads(heartbeat) for heartbeat in heartbeats]
            task_executor_heartbeats[task_executor_id] = heartbeats
    except Exception:
        logging.exception("get task executor heartbeats failed!")
    res["task_executor_heartbeats"] = task_executor_heartbeats

    return get_json_result(data=res)


@router.post('/new_token', summary="创建新访问令牌", response_description="成功创建并返回新令牌", response_model=dict[str, Any])
def new_token(request: TokenCreateRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    创建新的API访问令牌

    - **name**: Token名称（必填，最大20字符）
    - **description**: Token描述（可选）

    返回创建的令牌信息，包括token、创建时间等
    """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        tenant_id = [tenant for tenant in tenants if tenant.role == 'owner'][0].tenant_id
        obj = {
            "tenant_id": tenant_id,
            "token": generate_confirmation_token(tenant_id),
            "beta": generate_confirmation_token(generate_confirmation_token(tenant_id)).replace("multirag-", "")[:32],
            "name": request.name,
            "description": request.description,
            "create_time": current_timestamp(),
            "create_date": datetime_format(datetime.now()),
            "update_time": None,
            "update_date": None,
        }

        if not APITokenService.save(db, **obj):
            return get_data_error_result(retmsg="Fail to new a dialog!")

        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@router.get('/token_list', summary="获取API访问令牌列表", response_description="成功获取并返回令牌列表")
def token_list(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取API访问令牌列表的接口说明文档。

    概要：获取当前用户的所有API访问令牌。
    响应描述：成功获取并返回用户的API访问令牌列表。

    返回：
    - list: 返回包含用户所有API访问令牌的JSON结果列表。

    功能：
    1. 查询当前用户的租户信息，确保用户属于某个租户。
    2. 查询并返回该租户的所有API访问令牌。

    流程：
    1. 使用 UserTenantService 从数据库中查询当前用户的租户信息。
    2. 如果用户不属于任何租户，返回错误信息。
    3. 使用 APITokenService 查询该租户的所有API访问令牌。
    4. 返回包含所有API访问令牌的列表结果。

    异常处理：
    - 如果用户不属于任何租户，返回错误信息。
    - 如果在查询令牌列表过程中发生异常，抛出 HTTP 异常，并返回错误信息。

    注意：
    - 用户必须属于某个租户才能获取API访问令牌列表。
    """
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")

        tenant_id = [tenant for tenant in tenants if tenant.role == 'owner'][0].tenant_id
        objs = APITokenService.query(db, tenant_id=tenant_id)
        objs = [o.to_dict() for o in objs]
        for o in objs:
            if not o["beta"]:
                o["beta"] = generate_confirmation_token(generate_confirmation_token(tenants[0].tenant_id)).replace("multirag-", "")[:32]
                APITokenService.filter_update(db, [APIToken.tenant_id == tenant_id, APIToken.token == o["token"]], o)
        return get_json_result(data=objs)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除API访问令牌", response_description="成功删除指定的令牌")
def rm(
    token: str = Body(..., description="要删除的令牌"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    删除指定的API访问令牌。

    此接口允许用户删除指定的API访问令牌。
    用户需提供要删除的令牌，系统会根据令牌和用户ID从数据库中删除匹配的记录。

    参数:
       - token (str): 要删除的令牌。

    返回:
        JSON 响应，指示删除操作是否成功。
    """
    APITokenService.filter_delete(
        db, [APIToken.tenant_id == user.id, APIToken.token == token]
    )
    return get_json_result(data=True)


@router.get('/config', summary="获取系统配置")  # noqa: F821
def get_config():
    """
    Get system configuration.
    ---
    tags:
        - System
    responses:
        200:
            description: Return system configuration
            schema:
                type: object
                properties:
                    registerEnable:
                        type: integer 0 means disabled, 1 means enabled
                        description: Whether user registration is enabled
    """
    return get_json_result(data={
        "registerEnabled": settings.REGISTER_ENABLED
    })
