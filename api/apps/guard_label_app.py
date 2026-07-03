"""
@project: multirag
@Author：龙
@file： guard_label_app.py
@date：2025/01/11 18:20
@desc: AI安全护栏标签管理接口
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.guard_dimension_service import GuardDimensionService
from api.db.services.guard_label_service import GuardLabelService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response

router = APIRouter()


class CreateLabelRequest(BaseModel):
    """创建标签请求模型"""
    dimension_id: str = Field(..., description="维度ID")
    code: str = Field(..., description="标签代码")
    name: str = Field(..., description="标签名称")
    description: str | None = Field(None, description="标签描述")
    label_type: str = Field("keyword", description="标签类型: keyword/regex/custom")
    risk_level: str = Field("medium", description="风险等级: low/medium/high")
    config: dict = Field(default_factory=dict, description="标签配置")
    enabled: bool = Field(True, description="是否启用")
    sort_order: int = Field(0, description="排序")


class UpdateLabelRequest(BaseModel):
    """更新标签请求模型"""
    label_id: str = Field(..., description="标签ID")
    name: str | None = Field(None, description="标签名称")
    description: str | None = Field(None, description="标签描述")
    label_type: str | None = Field(None, description="标签类型")
    risk_level: str | None = Field(None, description="风险等级")
    config: dict | None = Field(None, description="标签配置")
    enabled: bool | None = Field(None, description="是否启用")
    sort_order: int | None = Field(None, description="排序")


@router.post('/create', summary="创建检测标签")
def create_label(
    request: CreateLabelRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    创建检测标签

    Args:
        request: 创建标签请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 创建结果
    """
    try:
        # 验证维度是否存在
        dimension = GuardDimensionService.get_by_id(db, request.dimension_id)
        if not dimension:
            return get_data_error_result(retmsg="维度不存在")

        label_id = GuardLabelService.create_label(
            db=db,
            dimension_id=request.dimension_id,
            code=request.code,
            name=request.name,
            description=request.description,
            label_type=request.label_type,
            risk_level=request.risk_level,
            tenant_id=user.id,
            created_by=user.id,
            config=request.config,
            enabled=request.enabled,
            sort_order=request.sort_order
        )

        if label_id:
            return get_json_result(data={"label_id": label_id})
        else:
            return get_data_error_result(retmsg="创建标签失败")

    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="获取标签列表")
def list_labels(
    dimension_id: str | None = None,
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取标签列表

    Args:
        dimension_id: 维度ID过滤
        enabled_only: 是否只返回启用的标签
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 标签列表
    """
    try:
        if dimension_id:
            labels = GuardLabelService.get_labels_by_dimension(
                db, dimension_id, enabled_only
            )
        else:
            labels = GuardLabelService.get_labels_by_tenant(
                db, user.id, enabled_only
            )

        return get_json_result(data=[label.to_dict() for label in labels])

    except Exception as e:
        return server_error_response(e)


@router.get('/dimensions', summary="获取维度列表")
def get_dimensions_for_labels(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取可用维度列表（用于标签创建）

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 维度列表
    """
    try:
        dimensions = GuardDimensionService.get_dimensions_by_tenant(
            db, user.id, enabled_only=True
        )

        return get_json_result(data=[dim.to_dict() for dim in dimensions])

    except Exception as e:
        return server_error_response(e)


@router.put('/update', summary="更新检测标签")
def update_label(
    request: UpdateLabelRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新检测标签

    Args:
        request: 更新标签请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 更新结果
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items()
                      if v is not None and k != "label_id"}

        success = GuardLabelService.update_label(
            db, request.label_id, update_data
        )

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新标签失败")

    except Exception as e:
        return server_error_response(e)


@router.delete('/{label_id}', summary="删除检测标签")
def delete_label(
    label_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    删除检测标签

    Args:
        label_id: 标签ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 删除结果
    """
    try:
        success = GuardLabelService.delete_label(db, label_id)

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="删除标签失败")

    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取标签统计")
def get_label_stats(
    dimension_id: str | None = None,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取标签统计信息

    Args:
        dimension_id: 维度ID过滤
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 统计信息
    """
    try:
        stats = GuardLabelService.get_label_stats(db, user.id, dimension_id)
        return get_json_result(data=stats)

    except Exception as e:
        return server_error_response(e)


@router.post('/init', summary="初始化默认标签")
def init_default_labels(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    初始化默认标签

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 初始化结果
    """
    try:
        # 获取维度映射
        dimensions = GuardDimensionService.get_dimensions_by_tenant(db, user.id)
        dimension_configs = {dim.code: dim.id for dim in dimensions}

        if not dimension_configs:
            return get_data_error_result(retmsg="请先初始化维度")

        label_ids = GuardLabelService.init_default_labels(
            db, dimension_configs, user.id, user.id
        )

        return get_json_result(data={
            "created_count": len(label_ids),
            "label_ids": label_ids
        })

    except Exception as e:
        return server_error_response(e)


@router.get('/{label_id}', summary="获取标签详情")
def get_label_detail(
    label_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取标签详情

    Args:
        label_id: 标签ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 标签详情
    """
    try:
        label = GuardLabelService.get_by_id(db, label_id)

        if not label:
            return get_data_error_result(retmsg="标签不存在")

        # 获取维度信息
        dimension = GuardDimensionService.get_by_id(db, label.dimension_id)

        label_dict = label.to_dict()
        if dimension:
            label_dict["dimension"] = dimension.to_dict()

        return get_json_result(data=label_dict)

    except Exception as e:
        return server_error_response(e)


@router.post('/batch/enable', summary="批量启用标签")
def batch_enable_labels(
    label_ids: list[str],
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    批量启用标签

    Args:
        label_ids: 标签ID列表
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 操作结果
    """
    try:
        success_count = 0
        failed_count = 0

        for label_id in label_ids:
            if GuardLabelService.update_label(db, label_id, {"enabled": True}):
                success_count += 1
            else:
                failed_count += 1

        return get_json_result(data={
            "success_count": success_count,
            "failed_count": failed_count
        })

    except Exception as e:
        return server_error_response(e)


@router.post('/batch/disable', summary="批量禁用标签")
def batch_disable_labels(
    label_ids: list[str],
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    批量禁用标签

    Args:
        label_ids: 标签ID列表
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 操作结果
    """
    try:
        success_count = 0
        failed_count = 0

        for label_id in label_ids:
            if GuardLabelService.update_label(db, label_id, {"enabled": False}):
                success_count += 1
            else:
                failed_count += 1

        return get_json_result(data={
            "success_count": success_count,
            "failed_count": failed_count
        })

    except Exception as e:
        return server_error_response(e)
