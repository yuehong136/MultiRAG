"""System RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.
Legacy ``/v1/system/*`` endpoints stay in ``api/apps/system_app.py`` for
backward compatibility.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, get_async_db
from api.db.services.api_service import APITokenService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import Principal, async_current_user, generate_confirmation_token, get_data_error_result, get_json_result, server_error_response
from api.utils.health_utils import run_health_checks_async
from common.log_utils import get_log_levels, set_log_level
from common.time_utils import current_timestamp, datetime_format
from common.versions import get_multirag_version

router = APIRouter()


class TokenCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: Annotated[str | None, Field(min_length=1, max_length=20, description="Token名称")] = None
    description: str | None = Field(None, description="Token描述")

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            return None
        return value


def _owner_tenant_id(db: Session, user: Principal) -> str | None:
    tenants = UserTenantService.query(db, user_id=user.id)
    owner = next((tenant for tenant in tenants if tenant.role == "owner"), None)
    return owner.tenant_id if owner else None


def _new_beta_token() -> str:
    return generate_confirmation_token().replace("multirag-", "")[:32]


@router.get("/system/ping", summary="连通测试")
async def ping():
    return PlainTextResponse("pong")


@router.get("/system/version", summary="获取版本", response_description="成功获取版本")
async def version(user: Principal = Depends(async_current_user)):
    """
    获取系统当前版本信息。

    概要：返回系统当前版本信息（RESTful 风格端点）。
    返回：
    - dict: 包含系统版本信息的 JSON 结果。
    """
    return get_json_result(data=get_multirag_version())


@router.get("/system/healthz", summary="健康检查", response_description="返回系统健康状态")
async def healthz(response: Response, db: AsyncSession = Depends(get_async_db)):
    """纯异步示范端点：DB 探针走 AsyncSession 全链路，其余组件检查经线程池执行。"""
    result, all_ok = await run_health_checks_async(db)
    response.status_code = 200 if all_ok else 500
    return result


@router.get("/system/tokens", summary="获取API访问令牌列表", response_description="成功获取并返回令牌列表")
async def token_list(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    try:

        def _list_tokens(s: Session) -> list[dict] | None:
            tenant_id = _owner_tenant_id(s, user)
            if not tenant_id:
                return None
            objs = APITokenService.query(s, tenant_id=tenant_id)
            tokens = [o.to_dict() for o in objs]
            for token_payload in tokens:
                if not token_payload.get("beta"):
                    token_payload["beta"] = _new_beta_token()
                    APITokenService.filter_update(
                        s,
                        [APIToken.tenant_id == tenant_id, APIToken.token == token_payload["token"]],
                        dict(token_payload),
                    )
            return tokens

        tokens = await db.run_sync(_list_tokens)  # TODO(async-phase4)
        if tokens is None:
            return get_data_error_result(retmsg="Tenant not found!")
        return get_json_result(data=tokens)
    except Exception as e:
        return server_error_response(e)


@router.post("/system/tokens", summary="创建新访问令牌", response_description="成功创建并返回新令牌")
async def new_token(
    request: Annotated[TokenCreateRequest | None, Body()] = None,
    name: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    try:

        def _create_token(s: Session) -> tuple[dict | None, str]:
            tenant_id = _owner_tenant_id(s, user)
            if not tenant_id:
                return None, "Tenant not found!"

            current_ts = current_timestamp()
            current_date = datetime_format(datetime.now())
            obj = {
                "tenant_id": tenant_id,
                "token": generate_confirmation_token(),
                "beta": _new_beta_token(),
                "name": (request.name if request and request.name else name) or "API Token",
                "description": request.description if request else None,
                "create_time": current_ts,
                "create_date": current_date,
                "update_time": None,
                "update_date": None,
            }

            if not APITokenService.save(s, **obj):
                return None, "Fail to new a dialog!"
            return obj, ""

        obj, err = await db.run_sync(_create_token)  # TODO(async-phase4)
        if obj is None:
            return get_data_error_result(retmsg=err)
        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@router.delete("/system/tokens/{token}", summary="删除API访问令牌", response_description="成功删除指定的令牌")
async def rm(token: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    try:

        def _delete_token(s: Session) -> bool:
            tenant_id = _owner_tenant_id(s, user)
            if not tenant_id:
                return False
            APITokenService.filter_delete(s, [APIToken.tenant_id == tenant_id, APIToken.token == token])
            return True

        deleted = await db.run_sync(_delete_token)  # TODO(async-phase4)
        if not deleted:
            return get_data_error_result(retmsg="Tenant not found!")
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


class LogLevelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    pkg_name: Annotated[str, Field(min_length=1, description='包名 (如 "core.utils.es_conn")')]
    level: Annotated[str, Field(min_length=1, description="日志级别 (DEBUG, INFO, WARNING, ERROR)")]


@router.get("/system/config/log", summary="获取日志级别")
async def get_logger_levels(user: Principal = Depends(async_current_user)):
    return get_json_result(data=get_log_levels())


@router.put("/system/config/log", summary="设置日志级别")
async def set_logger_level(request: LogLevelRequest, user: Principal = Depends(async_current_user)):
    success = set_log_level(request.pkg_name, request.level)
    if success:
        return get_json_result(data={"pkg_name": request.pkg_name, "level": request.level})
    return get_data_error_result(retmsg=f"Invalid log level: {request.level}")
