"""Configuration RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.
Legacy ``/v1/system/log_levels`` endpoints stay in ``api/apps/system_app.py``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from api.apps import manager
from api.utils.api_utils import get_data_error_result, get_json_result
from common.log_utils import get_log_levels, set_log_level

router = APIRouter()


class LogLevelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    pkg_name: Annotated[str, Field(min_length=1, description='包名 (如 "core.utils.es_conn")')]
    level: Annotated[str, Field(min_length=1, description="日志级别 (DEBUG, INFO, WARNING, ERROR)")]


@router.get("/config/log", summary="获取日志级别")
def get_logger_levels(user=Depends(manager)):
    return get_json_result(data=get_log_levels())


@router.put("/config/log", summary="设置日志级别")
def set_logger_level(request: LogLevelRequest, user=Depends(manager)):
    success = set_log_level(request.pkg_name, request.level)
    if success:
        return get_json_result(data={"pkg_name": request.pkg_name, "level": request.level})
    return get_data_error_result(retmsg=f"Invalid log level: {request.level}")
