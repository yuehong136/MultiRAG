"""
@project: multirag
@Author：龙
@file： guard_dimension_app.py
@date：2025/01/11 18:00
@desc: AI安全护栏维度管理接口
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.guard_dimension_service import GuardDimensionService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response

router = APIRouter()


class CreateDimensionRequest(BaseModel):
    """创建维度请求模型"""
    code: str = Field(..., description="维度代码")
    name: str = Field(..., description="维度名称")
    description: str | None = Field(None, description="维度描述")
    enabled: bool = Field(True, description="是否启用")
    config: dict = Field(default_factory=dict, description="维度配置")
    sort_order: int = Field(0, description="排序")


class UpdateDimensionRequest(BaseModel):
    """更新维度请求模型"""
    dimension_id: str = Field(..., description="维度ID")
    name: str | None = Field(None, description="维度名称")
    description: str | None = Field(None, description="维度描述")
    enabled: bool | None = Field(None, description="是否启用")
    config: dict | None = Field(None, description="维度配置")
    sort_order: int | None = Field(None, description="排序")


@router.post('/create', summary="创建检测维度")
def create_dimension(
    request: CreateDimensionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建检测维度

    Args:
        request: 创建维度请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 创建结果
    """
    try:
        dimension_id = GuardDimensionService.create_dimension(
            db=db,
            code=request.code,
            name=request.name,
            description=request.description,
            tenant_id=user.id,
            created_by=user.id,
            enabled=request.enabled,
            config=request.config,
            sort_order=request.sort_order
        )

        if dimension_id:
            return get_json_result(data={"dimension_id": dimension_id})
        else:
            return get_data_error_result(retmsg="创建维度失败")

    except ValueError as e:
        return get_data_error_result(retmsg=str(e))
    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="获取维度列表")
def list_dimensions(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取维度列表

    Args:
        enabled_only: 是否只返回启用的维度
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 维度列表
    """
    try:
        dimensions = GuardDimensionService.get_dimensions_by_tenant(
            db, user.id, enabled_only
        )

        return get_json_result(data=[dim.to_dict() for dim in dimensions])

    except Exception as e:
        return server_error_response(e)


@router.put('/update', summary="更新检测维度")
def update_dimension(
    request: UpdateDimensionRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新检测维度

    Args:
        request: 更新维度请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 更新结果
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items()
                      if v is not None and k != "dimension_id"}

        success = GuardDimensionService.update_dimension(
            db, request.dimension_id, update_data
        )

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新维度失败")

    except Exception as e:
        return server_error_response(e)


@router.delete('/{dimension_id}', summary="删除检测维度")
def delete_dimension(
    dimension_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除检测维度

    Args:
        dimension_id: 维度ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 删除结果
    """
    try:
        success = GuardDimensionService.delete_dimension(db, dimension_id)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="删除维度失败")

    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取维度统计")
def get_dimension_stats(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取维度统计信息

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 统计信息
    """
    try:
        stats = GuardDimensionService.get_dimension_stats(db, user.id)
        return get_json_result(data=stats)

    except Exception as e:
        return server_error_response(e)


@router.post('/init', summary="初始化默认维度")
def init_default_dimensions(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    初始化默认维度

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 初始化结果
    """
    try:
        dimension_ids = GuardDimensionService.init_default_dimensions(
            db, user.id, user.id
        )

        return get_json_result(data={
            "created_count": len(dimension_ids),
            "dimension_ids": dimension_ids
        })

    except Exception as e:
        return server_error_response(e)
