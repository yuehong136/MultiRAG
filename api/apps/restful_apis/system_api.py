# coding=utf-8
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
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import APIToken, get_db
from api.db.services.api_service import APITokenService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import get_data_error_result, get_json_result, generate_confirmation_token, server_error_response
from api.utils.health_utils import run_health_checks
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


def _owner_tenant_id(db: Session, user) -> str | None:
    tenants = UserTenantService.query(db, user_id=user.id)
    owner = next((tenant for tenant in tenants if tenant.role == "owner"), None)
    return owner.tenant_id if owner else None


def _new_beta_token() -> str:
    return generate_confirmation_token().replace("multirag-", "")[:32]


@router.get("/system/ping", summary="连通测试")
async def ping():
    return PlainTextResponse("pong")


@router.get("/system/version", summary="获取版本", response_description="成功获取版本")
def version(user=Depends(manager)):
    """
    获取系统当前版本信息。

    概要：返回系统当前版本信息（RESTful 风格端点）。
    返回：
    - dict: 包含系统版本信息的 JSON 结果。
    """
    return get_json_result(data=get_multirag_version())


@router.get("/system/healthz", summary="健康检查", response_description="返回系统健康状态")
def healthz(response: Response):
    result, all_ok = run_health_checks()
    response.status_code = 200 if all_ok else 500
    return result


@router.get("/system/tokens", summary="获取API访问令牌列表", response_description="成功获取并返回令牌列表")
def token_list(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        tenant_id = _owner_tenant_id(db, user)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        objs = APITokenService.query(db, tenant_id=tenant_id)
        tokens = [o.to_dict() for o in objs]
        for token_payload in tokens:
            if not token_payload.get("beta"):
                token_payload["beta"] = _new_beta_token()
                APITokenService.filter_update(
                    db,
                    [APIToken.tenant_id == tenant_id, APIToken.token == token_payload["token"]],
                    dict(token_payload),
                )
        return get_json_result(data=tokens)
    except Exception as e:
        return server_error_response(e)


@router.post("/system/tokens", summary="创建新访问令牌", response_description="成功创建并返回新令牌")
def new_token(
    request: Annotated[TokenCreateRequest | None, Body()] = None,
    name: Annotated[str | None, Query(min_length=1, max_length=20)] = None,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        tenant_id = _owner_tenant_id(db, user)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

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

        if not APITokenService.save(db, **obj):
            return get_data_error_result(retmsg="Fail to new a dialog!")

        return get_json_result(data=obj)
    except Exception as e:
        return server_error_response(e)


@router.delete("/system/tokens/{token}", summary="删除API访问令牌", response_description="成功删除指定的令牌")
def rm(token: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        tenant_id = _owner_tenant_id(db, user)
        if not tenant_id:
            return get_data_error_result(retmsg="Tenant not found!")

        APITokenService.filter_delete(db, [APIToken.tenant_id == tenant_id, APIToken.token == token])
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
