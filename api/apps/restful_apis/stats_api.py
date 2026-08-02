from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import get_async_db
from api.db.services.api_service import API4ConversationService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, server_error_response

router = APIRouter()


def _stats(db: Session, user_id: str, from_date: str | None, to_date: str | None, canvas_id: str | None) -> JSONResponse:
    tenants = UserTenantService.query(db, user_id=user_id)
    if not tenants:
        return get_data_error_result(retmsg="Tenant not found!")
    start = from_date or (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
    end = to_date or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = API4ConversationService.stats(db, tenants[0].tenant_id, start, end, "agent" if canvas_id else None)
    result: dict[str, list[Any]] = {"pv": [], "uv": [], "speed": [], "tokens": [], "round": [], "thumb_up": []}
    for row in rows:
        date = row["dt"]
        result["pv"].append((date, row["pv"]))
        result["uv"].append((date, row["uv"]))
        result["speed"].append((date, float(row["tokens"]) / (float(row["duration"]) + 0.1)))
        result["tokens"].append((date, float(row["tokens"]) / 1000.0))
        result["round"].append((date, row["round"]))
        result["thumb_up"].append((date, row["thumb_up"]))
    return get_json_result(data=result)


@router.get("/system/stats", summary="获取API使用统计", response_description="成功获取API使用统计")
async def stats(
    from_date: str | None = None,
    to_date: str | None = None,
    canvas_id: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    try:
        return await db.run_sync(lambda session: _stats(session, user.id, from_date, to_date, canvas_id))  # TODO(async-phase4)
    except Exception as exc:
        return server_error_response(exc)
