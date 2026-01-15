# coding=utf-8
"""
@project: multirag
@Author：龙
@file： memory_app.py
@date：2025/01/14
@desc: Memory数据集管理API - 采用Pydantic V2风格
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.db.services.memory_service import MemoryService
from api.db.services.user_service import UserTenantService
from api.utils.memory_utils import format_ret_data_from_memory, get_memory_type_human
from api.utils.api_utils import get_json_result
from api.constants import MEMORY_NAME_LIMIT, MEMORY_SIZE_LIMIT
from common.constants import MemoryType, TenantPermission, ForgettingPolicy, RetCode

router = APIRouter()
logger = logging.getLogger(__name__)


# ==================== Pydantic Models (V2 风格) ====================

class CreateMemoryRequest(BaseModel):
    """创建Memory请求模型"""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., min_length=1, max_length=MEMORY_NAME_LIMIT, description="Memory名称")
    memory_type: list[str] = Field(..., min_length=1, description="Memory类型列表，如['raw', 'semantic']")
    embd_id: str = Field(..., description="嵌入模型ID")
    llm_id: str = Field(..., description="LLM模型ID")


class UpdateMemoryRequest(BaseModel):
    """更新Memory请求模型"""
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(None, max_length=MEMORY_NAME_LIMIT, description="Memory名称")
    avatar: str | None = Field(None, description="头像base64字符串")
    description: str | None = Field(None, description="描述")
    llm_id: str | None = Field(None, description="LLM模型ID")
    permissions: str | None = Field(None, description="权限范围：me|team")
    memory_size: int | None = Field(None, gt=0, le=MEMORY_SIZE_LIMIT, description="最大Memory大小（字节）")
    forgetting_policy: str | None = Field(None, description="遗忘策略：fifo|lru")
    temperature: float | None = Field(None, ge=0, le=1, description="LLM温度参数")
    system_prompt: str | None = Field(None, description="系统提示词")
    user_prompt: str | None = Field(None, description="用户提示词")


class MemoryListParams(BaseModel):
    """Memory列表查询参数"""
    model_config = ConfigDict(str_strip_whitespace=True)

    tenant_id: list[str] | None = Field(None, description="租户ID列表")
    memory_type: list[str] | None = Field(None, description="Memory类型列表")
    storage_type: str | None = Field(None, description="存储类型：table|graph")
    keywords: str | None = Field(None, description="名称搜索关键词")
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(50, ge=1, le=200, description="每页数量")


# ==================== API Endpoints ====================

@router.post('', summary="创建Memory", response_description="成功创建Memory")
def create_memory(
    request_body: CreateMemoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    创建新的Memory数据集

    概要：创建一个新的Memory数据集，用于存储AI记忆数据。

    参数：
    - **name**: Memory名称，最大128字符
    - **memory_type**: Memory类型列表，支持raw/semantic/episodic/procedural
    - **embd_id**: 嵌入模型ID
    - **llm_id**: LLM模型ID

    返回：
    - dict: 创建的Memory详情

    异常：
    - 名称为空或超长时返回ARGUMENT_ERROR
    - 类型无效时返回ARGUMENT_ERROR
    """
    name = request_body.name.strip()
    if len(name) == 0:
        return get_json_result(
            data=False,
            retmsg="Memory name cannot be empty or whitespace.",
            retcode=RetCode.ARGUMENT_ERROR
        )

    if len(name) > MEMORY_NAME_LIMIT:
        return get_json_result(
            data=False,
            retmsg=f"Memory name '{name}' exceeds limit of {MEMORY_NAME_LIMIT}.",
            retcode=RetCode.ARGUMENT_ERROR
        )

    # 验证memory_type
    memory_type_set = set(request_body.memory_type)
    valid_types = {e.name.lower() for e in MemoryType}
    invalid_types = memory_type_set - valid_types
    if invalid_types:
        return get_json_result(
            data=False,
            retmsg=f"Memory type '{invalid_types}' is not supported.",
            retcode=RetCode.ARGUMENT_ERROR
        )

    try:
        success, result = MemoryService.create_memory(
            db=db,
            tenant_id=user.id,
            name=name,
            memory_type=list(memory_type_set),
            embd_id=request_body.embd_id,
            llm_id=request_body.llm_id
        )

        if success:
            return get_json_result(data=format_ret_data_from_memory(result))
        else:
            return get_json_result(
                data=False,
                retmsg=result,
                retcode=RetCode.SERVER_ERROR
            )

    except Exception as e:
        logger.error(f"创建Memory失败: {e}")
        return get_json_result(
            data=False,
            retmsg=str(e),
            retcode=RetCode.SERVER_ERROR
        )


@router.put('/{memory_id}', summary="更新Memory", response_description="成功更新Memory")
def update_memory(
    memory_id: str,
    request_body: UpdateMemoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    更新Memory配置

    概要：更新指定Memory的配置信息。

    参数：
    - **memory_id**: Memory ID
    - 可更新字段：name, avatar, description, llm_id, permissions, memory_size,
      forgetting_policy, temperature, system_prompt, user_prompt

    返回：
    - dict: 更新后的Memory详情

    注意：
    - 不允许修改：id, tenant_id, memory_type, storage_type, embd_id
    """
    # 检查Memory是否存在
    current_memory = MemoryService.get_by_memory_id(db, memory_id)
    if not current_memory:
        return get_json_result(
            data=False,
            retmsg=f"Memory '{memory_id}' not found.",
            retcode=RetCode.NOT_FOUND
        )

    update_dict = {}

    # 验证并构建更新字典
    if request_body.name is not None:
        name = request_body.name.strip()
        if len(name) == 0:
            return get_json_result(
                data=False,
                retmsg="Memory name cannot be empty or whitespace.",
                retcode=RetCode.ARGUMENT_ERROR
            )
        if len(name) > MEMORY_NAME_LIMIT:
            return get_json_result(
                data=False,
                retmsg=f"Memory name '{name}' exceeds limit of {MEMORY_NAME_LIMIT}.",
                retcode=RetCode.ARGUMENT_ERROR
            )
        update_dict["name"] = name

    if request_body.permissions is not None:
        if request_body.permissions not in [e.value for e in TenantPermission]:
            return get_json_result(
                data=False,
                retmsg=f"Unknown permission '{request_body.permissions}'.",
                retcode=RetCode.ARGUMENT_ERROR
            )
        update_dict["permissions"] = request_body.permissions

    if request_body.llm_id is not None:
        update_dict["llm_id"] = request_body.llm_id

    if request_body.memory_size is not None:
        if not 0 < request_body.memory_size <= MEMORY_SIZE_LIMIT:
            return get_json_result(
                data=False,
                retmsg=f"Memory size should be in range (0, {MEMORY_SIZE_LIMIT}] Bytes.",
                retcode=RetCode.ARGUMENT_ERROR
            )
        update_dict["memory_size"] = request_body.memory_size

    if request_body.forgetting_policy is not None:
        if request_body.forgetting_policy not in [e.value for e in ForgettingPolicy]:
            return get_json_result(
                data=False,
                retmsg=f"Forgetting policy '{request_body.forgetting_policy}' is not supported.",
                retcode=RetCode.ARGUMENT_ERROR
            )
        update_dict["forgetting_policy"] = request_body.forgetting_policy

    if request_body.temperature is not None:
        if not 0 <= request_body.temperature <= 1:
            return get_json_result(
                data=False,
                retmsg="Temperature should be in range [0, 1].",
                retcode=RetCode.ARGUMENT_ERROR
            )
        update_dict["temperature"] = request_body.temperature

    # 允许更新为空的字段
    for field in ["avatar", "description", "system_prompt", "user_prompt"]:
        value = getattr(request_body, field)
        if value is not None:
            update_dict[field] = value

    # 检查是否有变更
    memory_dict = current_memory.to_dict()
    memory_dict["memory_type"] = get_memory_type_human(current_memory.memory_type)

    to_update = {}
    for k, v in update_dict.items():
        if isinstance(v, list) and set(memory_dict.get(k, [])) != set(v):
            to_update[k] = v
        elif memory_dict.get(k) != v:
            to_update[k] = v

    if not to_update:
        return get_json_result(data=memory_dict)

    try:
        MemoryService.update_memory(db, memory_id, to_update)
        updated_memory = MemoryService.get_by_memory_id(db, memory_id)
        return get_json_result(data=format_ret_data_from_memory(updated_memory))

    except Exception as e:
        logger.error(f"更新Memory失败: {e}")
        return get_json_result(
            data=False,
            retmsg=str(e),
            retcode=RetCode.SERVER_ERROR
        )


@router.delete('/{memory_id}', summary="删除Memory", response_description="成功删除Memory")
def delete_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    删除Memory

    概要：删除指定的Memory数据集。

    参数：
    - **memory_id**: Memory ID

    返回：
    - dict: 操作结果

    注意：
    - 删除操作不可逆
    """
    memory = MemoryService.get_by_memory_id(db, memory_id)
    if not memory:
        return get_json_result(
            data=True,
            retcode=RetCode.NOT_FOUND
        )

    try:
        MemoryService.delete_memory(db, memory_id)
        return get_json_result(data=True)

    except Exception as e:
        logger.error(f"删除Memory失败: {e}")
        return get_json_result(
            data=False,
            retmsg=str(e),
            retcode=RetCode.SERVER_ERROR
        )


@router.get('', summary="获取Memory列表", response_description="成功获取Memory列表")
def list_memory(
    tenant_id: list[str] | None = Query(None, description="租户ID列表"),
    memory_type: list[str] | None = Query(None, description="Memory类型列表"),
    storage_type: str | None = Query(None, description="存储类型"),
    keywords: str | None = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    获取Memory列表

    概要：分页获取Memory列表，支持多种过滤条件。

    参数：
    - **tenant_id**: 租户ID列表，不传则查询当前用户可访问的租户
    - **memory_type**: Memory类型列表过滤
    - **storage_type**: 存储类型过滤
    - **keywords**: 名称搜索关键词
    - **page**: 页码
    - **page_size**: 每页数量

    返回：
    - dict: 包含memory_list和total_count
    """
    try:
        # 构建过滤条件
        filter_dict = {
            "memory_type": memory_type,
            "storage_type": storage_type
        }

        if not tenant_id:
            # 限制为当前用户可访问的租户
            user_tenants = UserTenantService.get_user_tenant_relation_by_user_id(db, user.id)
            filter_dict["tenant_id"] = [tenant["tenant_id"] for tenant in user_tenants]
        else:
            filter_dict["tenant_id"] = tenant_id

        memory_list, count = MemoryService.get_by_filter(
            db,
            filter_dict,
            keywords or "",
            page,
            page_size
        )

        # 转换memory_type为人类可读格式
        for memory in memory_list:
            memory["memory_type"] = get_memory_type_human(memory["memory_type"])

        return get_json_result(data={"memory_list": memory_list, "total_count": count})

    except Exception as e:
        logger.error(f"获取Memory列表失败: {e}")
        return get_json_result(
            data=False,
            retmsg=str(e),
            retcode=RetCode.SERVER_ERROR
        )


@router.get('/{memory_id}/config', summary="获取Memory配置", response_description="成功获取Memory配置")
def get_memory_config(
    memory_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    获取Memory详细配置

    概要：获取指定Memory的完整配置信息，包括所有者名称。

    参数：
    - **memory_id**: Memory ID

    返回：
    - dict: Memory完整配置信息
    """
    memory = MemoryService.get_with_owner_name_by_id(db, memory_id)
    if not memory:
        return get_json_result(
            data=False,
            retmsg=f"Memory '{memory_id}' not found.",
            retcode=RetCode.NOT_FOUND
        )

    return get_json_result(data=format_ret_data_from_memory(memory))
