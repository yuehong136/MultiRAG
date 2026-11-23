"""Discover durable memory work without requiring a live bank configuration."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import _command_error, _result
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user

router = APIRouter()


@router.get("/memories/{memory_id}/operations", summary="Discover authorized pending memory work or operation history")
async def operation_inbox(
    memory_id: str,
    view: Literal["pending", "all"] = "pending",
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1024),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    from api.apps.services.memory_operation_service import list_operations

    try:
        return _result(await list_operations(db, memory_id, actor_id=user.id, view=view, limit=limit, cursor=cursor))
    except Exception as error:
        return _command_error(error)
