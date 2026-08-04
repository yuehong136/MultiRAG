"""RESTful Langfuse credential endpoints mounted under ``/api/v1``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import langfuse_api_service
from api.db.db_models import get_async_db
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_json_result, server_error_response

router = APIRouter()


class LangfuseKeysRequest(BaseModel):
    secret_key: str
    public_key: str
    host: str


@router.post("/langfuse/api-key", summary="设置 Langfuse API 密钥")
@router.put("/langfuse/api-key", summary="更新 Langfuse API 密钥")
async def set_api_key(
    request: LangfuseKeysRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Any:
    try:
        credentials = await langfuse_api_service.set_credentials(
            db,
            tenant_id,
            request.secret_key,
            request.public_key,
            request.host,
        )
        return get_json_result(data=credentials)
    except langfuse_api_service.LangfuseCredentialError as exc:
        return get_error_data_result(retmsg=str(exc))
    except Exception as exc:
        await db.rollback()
        return server_error_response(exc)


@router.get("/langfuse/api-key", summary="获取 Langfuse API 密钥")
async def get_api_key(
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Any:
    try:
        credentials = await langfuse_api_service.get_credentials(db, tenant_id)
        return get_json_result(data=credentials)
    except langfuse_api_service.LangfuseCredentialError as exc:
        return get_error_data_result(retmsg=str(exc))
    except langfuse_api_service.LangfuseRemoteError as exc:
        return get_json_result(retmsg=str(exc))
    except Exception as exc:
        return server_error_response(exc)


@router.delete("/langfuse/api-key", summary="删除 Langfuse API 密钥")
async def delete_api_key(
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Any:
    try:
        return get_json_result(data=await langfuse_api_service.delete_credentials(db, tenant_id))
    except langfuse_api_service.LangfuseCredentialError as exc:
        return get_error_data_result(retmsg=str(exc))
    except Exception as exc:
        await db.rollback()
        return server_error_response(exc)
