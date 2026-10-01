"""Task cancellation. Task inspection is intentionally not a public API."""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db
from api.db.services.task_cancellation_service import cancel_task
from api.utils.api_utils import Principal, async_current_user, get_json_result
from common.constants import RetCode

router = APIRouter()
TaskID = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


class StopTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["stop"]


async def cancel_response(db: AsyncSession, task_id: str, user: Principal) -> JSONResponse:
    try:
        await cancel_task(db, task_id, user.id)
        return get_json_result(data=True)
    except PermissionError as error:
        return get_json_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error), data=False)
    except Exception:
        logging.exception("Task cancellation failed: task_id=%s", task_id)
        return get_json_result(retcode=RetCode.EXCEPTION_ERROR, retmsg="Failed to submit task cancellation.", data=False)


@router.post("/tasks/{task_id}/cancel", summary="提交任务取消请求")
async def cancel(task_id: TaskID, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    return await cancel_response(db, task_id, user)


@router.patch("/tasks/{task_id}", summary="停止任务")
async def update(task_id: TaskID, body: StopTaskRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    return await cancel_response(db, task_id, user)
