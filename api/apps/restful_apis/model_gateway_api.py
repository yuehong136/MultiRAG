"""Management API of the model gateway (design: docs/model-gateway/README.md).

The routes require an interactive login: SDK API tokens are refused so that a
calling credential can never mint or change gateway keys. Everything is scoped
to the owner tenant, the same scope the model management routes use.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db
from api.identity.principal import AuthenticationSource
from api.model_gateway import management
from api.model_gateway.schemas import GatewayKeyCreate, GatewayKeyPatch, GatewayModelCreate, GatewayModelPatch
from api.utils.api_utils import Principal, async_current_user
from common.app_config import get_app_config

_DEFAULT_USAGE_WINDOW = timedelta(days=7)


def require_enabled() -> None:
    if not get_app_config().model_gateway.enabled:
        raise HTTPException(status_code=404, detail="Not Found")


def require_manager(user: Principal = Depends(async_current_user)) -> Principal:
    if user.authentication.source is AuthenticationSource.SDK_API_TOKEN:
        raise HTTPException(status_code=403, detail="Model gateway management requires an interactive login")
    return user


router = APIRouter(prefix="/model-gateway", dependencies=[Depends(require_enabled)])


def result(data: Any = None) -> dict[str, Any]:
    return {"code": 0, "message": "success", "data": data}


def failure(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": status, "message": message, "data": None})


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def write(db: AsyncSession, operation: Callable[[], Awaitable[Any]]) -> dict[str, Any] | JSONResponse:
    try:
        data = await operation()
        await db.commit()
        return result(data)
    except management.GatewayNotFound:
        await db.rollback()
        return failure(404, "Model gateway resource not found")
    except management.GatewayConflict as error:
        await db.rollback()
        return failure(409, str(error))
    except ValueError as error:
        await db.rollback()
        return failure(400, str(error))
    except IntegrityError:
        await db.rollback()
        return failure(409, "A model gateway resource with this name already exists")
    except SQLAlchemyError:
        await db.rollback()
        return failure(500, "Model gateway operation failed")


@router.get("/models", summary="列出已发布的公开模型")
async def list_models(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any]:
    return result(await management.list_models(db, user.id))


@router.post("/models", summary="把已登记的模型发布为公开模型", response_model=None)
async def create_model(body: GatewayModelCreate, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.create_model(db, user.id, user.id, body))


@router.patch("/models/{gateway_model_id}", summary="修改公开模型的名称、说明或启停状态", response_model=None)
async def update_model(gateway_model_id: str, body: GatewayModelPatch, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.update_model(db, user.id, user.id, gateway_model_id, body))


@router.delete("/models/{gateway_model_id}", summary="删除公开模型", response_model=None)
async def delete_model(gateway_model_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.delete_model(db, user.id, gateway_model_id))


@router.get("/keys", summary="列出 API Key（不含明文）")
async def list_keys(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any]:
    return result(await management.list_keys(db, user.id))


@router.post("/keys", summary="创建 API Key；明文只在本次响应中返回", response_model=None)
async def create_key(body: GatewayKeyCreate, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.create_key(db, user.id, user.id, body))


@router.patch("/keys/{key_id}", summary="修改 API Key 的元数据、模型范围、有效期或启停状态", response_model=None)
async def update_key(key_id: str, body: GatewayKeyPatch, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.update_key(db, user.id, user.id, key_id, body))


@router.post("/keys/{key_id}/revoke", summary="撤销 API Key；撤销后不可恢复", response_model=None)
async def revoke_key(key_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(require_manager)) -> dict[str, Any] | JSONResponse:
    return await write(db, lambda: management.revoke_key(db, user.id, user.id, key_id))


@router.get("/logs", summary="查询调用记录")
async def query_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    key_id: str | None = Query(None, max_length=32),
    model: str | None = Query(None, max_length=128),
    end_user: str | None = Query(None, max_length=255),
    outcome: Literal["success", "error"] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(require_manager),
) -> dict[str, Any]:
    return result(
        await management.query_logs(
            db,
            user.id,
            page=page,
            page_size=page_size,
            key_id=key_id,
            model=model,
            end_user=end_user,
            outcome=outcome,
            start=_utc(start) if start else None,
            end=_utc(end) if end else None,
        )
    )


@router.get("/usage", summary="按 Key、模型、日期或 end_user 汇总用量")
async def usage_summary(
    group_by: Literal["key", "model", "day", "end_user"] = "key",
    key_id: str | None = Query(None, max_length=32),
    model: str | None = Query(None, max_length=128),
    start: datetime | None = None,
    end: datetime | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(require_manager),
) -> dict[str, Any]:
    window_end = _utc(end) if end else datetime.now(UTC)
    window_start = _utc(start) if start else window_end - _DEFAULT_USAGE_WINDOW
    items = await management.usage_summary(db, user.id, group_by=group_by, start=window_start, end=window_end, key_id=key_id, model=model)
    return result({"group_by": group_by, "start": window_start.isoformat(), "end": window_end.isoformat(), "items": items})
