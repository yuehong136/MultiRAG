"""Legacy canvas surface retained only for task cancellation."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.task_api import TaskID, cancel_response
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user

router = APIRouter()


# Active web caller: cancelCanvas/cancelDataflow. Delete after its Task REST
# migration is accepted; authorization and errors are identical to the new API.
@router.put("/cancel/{task_id}", summary="提交任务取消请求", deprecated=True)
async def cancel(task_id: TaskID, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    return await cancel_response(db, task_id, user)
