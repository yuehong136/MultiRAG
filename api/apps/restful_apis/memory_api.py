# coding=utf-8
"""
@project: multirag
@Author：龙
@file： memory_api.py
@date：2026/03/18
@desc: Memory API 网关层 - 负责路由和参数解析，业务逻辑委托给service层
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import get_db
from api.utils.api_utils import get_json_result
from api.constants import MEMORY_NAME_LIMIT, MEMORY_SIZE_LIMIT
from api.apps.services import memory_api_service
from common.constants import RetCode
from common.exceptions import ArgumentException, NotFoundException


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
    embd_id: str | None = Field(None, description="嵌入模型ID")
    memory_type: list[str] | None = Field(None, min_length=1, description="Memory类型列表")
    llm_id: str | None = Field(None, description="LLM模型ID")
    permissions: str | None = Field(None, description="权限范围：me|team")
    memory_size: int | None = Field(None, gt=0, le=MEMORY_SIZE_LIMIT, description="最大Memory大小（字节）")
    forgetting_policy: str | None = Field(None, description="遗忘策略：fifo|lru")
    temperature: float | None = Field(None, ge=0, le=1, description="LLM温度参数")
    system_prompt: str | None = Field(None, description="系统提示词")
    user_prompt: str | None = Field(None, description="用户提示词")


# ==================== API Endpoints ====================

@router.post('/memories', summary="创建Memory", response_description="成功创建Memory")
def create_memory(
    request_body: CreateMemoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    memory_info = {
        "name": request_body.name,
        "memory_type": request_body.memory_type,
        "embd_id": request_body.embd_id,
        "llm_id": request_body.llm_id,
    }
    try:
        success, result = memory_api_service.create_memory(db, user.id, memory_info)
        if success:
            return get_json_result(data=result)
        else:
            return get_json_result(data=False, retmsg=result, retcode=RetCode.SERVER_ERROR)
    except ArgumentException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.ARGUMENT_ERROR)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.put('/memories/{memory_id}', summary="更新Memory", response_description="成功更新Memory")
def update_memory(
    memory_id: str,
    request_body: UpdateMemoryRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    new_settings = {k: v for k, v in request_body.model_dump(exclude_unset=True).items()}
    try:
        success, result = memory_api_service.update_memory(db, memory_id, new_settings)
        if success:
            return get_json_result(data=result)
        else:
            return get_json_result(data=False, retmsg=result, retcode=RetCode.SERVER_ERROR)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except ArgumentException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.ARGUMENT_ERROR)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.delete('/memories/{memory_id}', summary="删除Memory", response_description="成功删除Memory")
def delete_memory(
    memory_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    try:
        memory_api_service.delete_memory(db, memory_id)
        return get_json_result(data=True)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.get('/memories', summary="获取Memory列表", response_description="成功获取Memory列表")
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
    filter_params = {}
    if tenant_id is not None:
        filter_params["tenant_id"] = tenant_id
    if memory_type is not None:
        filter_params["memory_type"] = memory_type
    if storage_type is not None:
        filter_params["storage_type"] = storage_type
    try:
        result = memory_api_service.list_memory(db, user.id, filter_params, keywords or "", page, page_size)
        return get_json_result(data=result)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.get('/memories/{memory_id}/config', summary="获取Memory配置", response_description="成功获取Memory配置")
def get_memory_config(
    memory_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    try:
        result = memory_api_service.get_memory_config(db, memory_id)
        return get_json_result(data=result)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.get('/memories/{memory_id}', summary="获取Memory详情", response_description="成功获取Memory详情及消息列表")
def get_memory_messages(
    memory_id: str,
    agent_id: list[str] | None = Query(None, description="Agent ID列表"),
    keywords: str | None = Query(None, description="搜索关键词"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(50, ge=1, le=200, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    if agent_id and len(agent_id) == 1 and ',' in agent_id[0]:
        agent_id = agent_id[0].split(',')
    try:
        result = memory_api_service.get_memory_messages(
            db, memory_id, agent_id or [], keywords.strip() if keywords else "", page, page_size
        )
        return get_json_result(data=result)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)
