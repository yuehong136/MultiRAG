"""
@project: multirag
@Author：龙
@file： guard_relationship_app.py
@date：2025/01/11 18:40
@desc: AI安全护栏关系管理接口
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.guard_label_library_service import GuardLabelLibraryService
from api.db.services.guard_service_library_service import GuardServiceLibraryService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response

router = APIRouter()


class BindServiceLibraryRequest(BaseModel):
    """服务词库绑定请求模型"""
    service_id: str = Field(..., description="服务ID")
    library_id: str = Field(..., description="词库ID")
    priority: int = Field(0, description="优先级")
    enabled: bool = Field(True, description="是否启用")
    library_type: str | None = Field(None, description="词库在此服务中的类型: blacklist/whitelist/reply/pattern/custom")


class BindLabelLibraryRequest(BaseModel):
    """标签词库绑定请求模型"""
    label_id: str = Field(..., description="标签ID")
    library_id: str = Field(..., description="词库ID")
    priority: int = Field(0, description="优先级")
    enabled: bool = Field(True, description="是否启用")


class BatchBindRequest(BaseModel):
    """批量绑定请求模型"""
    target_id: str = Field(..., description="目标ID（服务ID或标签ID）")
    library_ids: list[str] = Field(..., description="词库ID列表")
    library_type: str | None = Field(None, description="词库类型: blacklist/whitelist/reply/pattern/custom")


class UpdateBindingRequest(BaseModel):
    """更新绑定请求模型"""
    binding_id: str = Field(..., description="绑定关系ID")
    priority: int | None = Field(None, description="优先级")
    enabled: bool | None = Field(None, description="是否启用")


# 服务词库关系管理
@router.post('/service-library/bind', summary="绑定词库到服务")
def bind_service_library(
    request: BindServiceLibraryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    绑定词库到服务

    Args:
        request: 绑定请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 绑定结果
    """
    try:
        binding_id = GuardServiceLibraryService.bind_library_to_service(
            db=db,
            service_id=request.service_id,
            library_id=request.library_id,
            priority=request.priority,
            enabled=request.enabled,
            library_type=request.library_type,
            tenant_id=user.id,
            created_by=user.id
        )

        if binding_id:
            return get_json_result(data={"binding_id": binding_id})
        else:
            return get_data_error_result(retmsg="绑定失败，可能已存在相同绑定")

    except Exception as e:
        return server_error_response(e)


@router.get('/service-library/service/{service_id}', summary="获取服务绑定的词库")
def get_service_libraries(
    service_id: str,
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取服务绑定的词库列表

    Args:
        service_id: 服务ID
        enabled_only: 是否只返回启用的
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 词库列表
    """
    try:
        libraries = GuardServiceLibraryService.get_libraries_by_service(
            db, service_id, enabled_only
        )

        return get_json_result(data=libraries)

    except Exception as e:
        return server_error_response(e)


@router.get('/service-library/library/{library_id}', summary="获取使用词库的服务")
def get_library_services(
    library_id: str,
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取使用词库的服务列表

    Args:
        library_id: 词库ID
        enabled_only: 是否只返回启用的
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 服务列表
    """
    try:
        services = GuardServiceLibraryService.get_services_by_library(
            db, library_id, enabled_only
        )

        return get_json_result(data=services)

    except Exception as e:
        return server_error_response(e)


@router.post('/service-library/batch-bind', summary="批量绑定词库到服务")
def batch_bind_service_libraries(
    request: BatchBindRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    ### POST `/service-library/batch-bind` 批量绑定词库到服务

    **功能描述**:
    此接口用于批量绑定多个词库到指定服务。
    支持为每个绑定关系指定词库类型，同一个词库在不同服务中可以有不同的类型。
    library_type字段存储在服务-词库关系表中，而不是词库表中，实现更灵活的配置。

    ---
    ### 请求体 (Request Body)
    | 字段           | 类型         | 必填 | 描述                                                    |
    |----------------|-------------|------|--------------------------------------------------------|
    | `target_id`    | `string`    | 是   | 服务ID                                                  |
    | `library_ids`  | `list[string]` | 是   | 词库ID列表                                           |
    | `library_type` | `string`    | 否   | 词库类型: blacklist/whitelist/reply/pattern/custom      |

    **请求示例**:
    ```json
    {
        "target_id": "uuid-service-id-here",
        "library_ids": [
            "uuid-library-id-1",
            "uuid-library-id-2"
        ],
        "library_type": "blacklist"
    }
    ```

    **不更新类型示例**:
    ```json
    {
        "target_id": "uuid-service-id-here",
        "library_ids": [
            "uuid-library-id-1",
            "uuid-library-id-2"
        ]
    }
    ```

    ---
    ### 响应 (Response)
    #### 成功响应（有library_type）(200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "success_count": 2,
            "failed_count": 0,
            "failed_libraries": [],
            "set_library_types_count": 2,
            "set_library_type_ids": [
                "uuid-library-id-1",
                "uuid-library-id-2"
            ]
        }
    }
    ```

    #### 成功响应（无library_type）(200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "success_count": 2,
            "failed_count": 0,
            "failed_libraries": []
        }
    }
    ```
    """
    try:
        result = GuardServiceLibraryService.batch_bind_libraries(
            db, request.target_id, request.library_ids,
            tenant_id=user.id, created_by=user.id, library_type=request.library_type
        )

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.delete('/service-library/unbind', summary="解绑服务词库")
def unbind_service_library(
    service_id: str,
    library_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    解绑服务词库关系

    Args:
        service_id: 服务ID
        library_id: 词库ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 解绑结果
    """
    try:
        success = GuardServiceLibraryService.unbind_library_from_service(
            db, service_id, library_id
        )

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="解绑失败")

    except Exception as e:
        return server_error_response(e)


# 标签词库关系管理
@router.post('/label-library/bind', summary="绑定词库到标签")
def bind_label_library(
    request: BindLabelLibraryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    绑定词库到标签

    Args:
        request: 绑定请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 绑定结果
    """
    try:
        binding_id = GuardLabelLibraryService.bind_library_to_label(
            db=db,
            label_id=request.label_id,
            library_id=request.library_id,
            priority=request.priority,
            enabled=request.enabled,
            tenant_id=user.id,
            created_by=user.id
        )

        if binding_id:
            return get_json_result(data={"binding_id": binding_id})
        else:
            return get_data_error_result(retmsg="绑定失败，可能已存在相同绑定")

    except Exception as e:
        return server_error_response(e)


@router.get('/label-library/label/{label_id}', summary="获取标签绑定的词库")
def get_label_libraries(
    label_id: str,
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取标签绑定的词库列表

    Args:
        label_id: 标签ID
        enabled_only: 是否只返回启用的
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 词库列表
    """
    try:
        libraries = GuardLabelLibraryService.get_libraries_by_label(
            db, label_id, enabled_only
        )

        return get_json_result(data=libraries)

    except Exception as e:
        return server_error_response(e)


@router.get('/label-library/library/{library_id}', summary="获取使用词库的标签")
def get_library_labels(
    library_id: str,
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取使用词库的标签列表

    Args:
        library_id: 词库ID
        enabled_only: 是否只返回启用的
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 标签列表
    """
    try:
        labels = GuardLabelLibraryService.get_labels_by_library(
            db, library_id, enabled_only
        )

        return get_json_result(data=labels)

    except Exception as e:
        return server_error_response(e)


@router.post('/label-library/batch-bind', summary="批量绑定词库到标签")
def batch_bind_label_libraries(
    request: BatchBindRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    批量绑定词库到标签

    Args:
        request: 批量绑定请求参数
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 绑定结果
    """
    try:
        result = GuardLabelLibraryService.batch_bind_libraries(
            db, request.target_id, request.library_ids,
            tenant_id=user.id, created_by=user.id
        )

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)


@router.delete('/label-library/unbind', summary="解绑标签词库")
def unbind_label_library(
    label_id: str,
    library_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    解绑标签词库关系

    Args:
        label_id: 标签ID
        library_id: 词库ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 解绑结果
    """
    try:
        success = GuardLabelLibraryService.unbind_library_from_label(
            db, label_id, library_id
        )

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="解绑失败")

    except Exception as e:
        return server_error_response(e)


# 通用绑定管理
@router.put('/binding/update', summary="更新绑定关系")
def update_binding(
    request: UpdateBindingRequest,
    binding_type: str,  # service-library 或 label-library
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    更新绑定关系

    Args:
        request: 更新请求参数
        binding_type: 绑定类型
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 更新结果
    """
    try:
        update_data = {k: v for k, v in request.model_dump().items()
                      if v is not None and k != "binding_id"}

        if binding_type == "service-library":
            success = GuardServiceLibraryService.update_binding(
                db, request.binding_id, update_data
            )
        elif binding_type == "label-library":
            success = GuardLabelLibraryService.update_binding(
                db, request.binding_id, update_data
            )
        else:
            return get_data_error_result(retmsg="无效的绑定类型")

        if success:
            return get_json_result(data=True)
        else:
            return get_data_error_result(retmsg="更新失败")

    except Exception as e:
        return server_error_response(e)


@router.get('/stats', summary="获取关系统计")
def get_relationship_stats(
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取关系统计信息

    Args:
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 统计信息
    """
    try:
        service_stats = GuardServiceLibraryService.get_binding_stats(db, user.id)
        label_stats = GuardLabelLibraryService.get_binding_stats(db, user.id)

        return get_json_result(data={
            "service_library_stats": service_stats,
            "label_library_stats": label_stats
        })

    except Exception as e:
        return server_error_response(e)


@router.get('/library/{library_id}/usage', summary="获取词库使用情况")
def get_library_usage(
    library_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    获取词库使用情况

    Args:
        library_id: 词库ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 使用情况
    """
    try:
        # 获取使用此词库的服务
        services = GuardServiceLibraryService.get_services_by_library(
            db, library_id, enabled_only=False
        )

        # 获取使用此词库的标签
        labels = GuardLabelLibraryService.get_labels_by_library(
            db, library_id, enabled_only=False
        )

        # 获取词库在各维度的使用统计
        dimension_usage = GuardLabelLibraryService.get_library_usage_by_dimensions(
            db, library_id, user.id
        )

        return get_json_result(data={
            "library_id": library_id,
            "services": services,
            "labels": labels,
            "dimension_usage": dimension_usage,
            "summary": {
                "total_services": len(services),
                "total_labels": len(labels),
                "enabled_services": len([s for s in services if s.get("binding", {}).get("enabled", False)]),
                "enabled_labels": len([l for l in labels if l.get("binding", {}).get("enabled", False)])
            }
        })

    except Exception as e:
        return server_error_response(e)


@router.post('/label-library/sync-to-dimension', summary="同步词库到维度所有标签")
def sync_library_to_dimension(
    library_id: str,
    dimension_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
) -> dict[str, Any]:
    """
    将词库同步到指定维度的所有标签

    Args:
        library_id: 词库ID
        dimension_id: 维度ID
        db: 数据库会话
        user: 当前用户信息

    Returns:
        dict[str, Any]: 同步结果
    """
    try:
        result = GuardLabelLibraryService.sync_library_to_all_labels_in_dimension(
            db, library_id, dimension_id, user.id, user.id
        )

        return get_json_result(data=result)

    except Exception as e:
        return server_error_response(e)
