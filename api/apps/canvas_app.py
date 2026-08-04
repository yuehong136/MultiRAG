"""Legacy canvas surface retained only for task cancellation."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.utils.api_utils import get_json_result
from core.utils.redis_conn import REDIS_CONN

router = APIRouter()


@router.put("/cancel/{task_id}", summary="取消任务", response_description="成功取消任务")
def cancel(task_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        REDIS_CONN.set(f"{task_id}-cancel", "x")
    except Exception as error:
        logging.exception(error)
    return get_json_result(data=True)
