# coding=utf-8
"""System RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``:
    GET    /system/version

The legacy ``/v1/system/version`` endpoint stays in ``api/apps/system_app.py``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from api.apps import manager
from api.utils.api_utils import get_json_result
from common.versions import get_multirag_version

router = APIRouter()


@router.get("/system/version", summary="获取版本", response_description="成功获取版本")
def version(user=Depends(manager)):
    """
    获取系统当前版本信息。

    概要：返回系统当前版本信息（RESTful 风格端点）。
    返回：
    - dict: 包含系统版本信息的 JSON 结果。
    """
    return get_json_result(data=get_multirag_version())
