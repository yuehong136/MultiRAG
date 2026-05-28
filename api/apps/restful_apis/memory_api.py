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
from api.utils.api_utils import get_json_result, get_error_argument_result
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from api.constants import MEMORY_NAME_LIMIT, MEMORY_SIZE_LIMIT
from api.apps.services import memory_api_service
from common.constants import RetCode
from common.exceptions import ArgumentException, NotFoundException


router = APIRouter()
logger = logging.getLogger(__name__)
MEMORY_LIST_MAX_PAGE_SIZE = 1000


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
    tenant_llm_id: int | None = Field(None, description="租户LLM模型关联ID")
    tenant_embd_id: int | None = Field(None, description="租户嵌入模型关联ID")


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
    memory_info = ensure_tenant_model_id_for_params(db, user.id, memory_info)
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
    page_size: int = Query(50, ge=1, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    effective_page_size = min(page_size, MEMORY_LIST_MAX_PAGE_SIZE)
    filter_params = {}
    if tenant_id is not None:
        filter_params["tenant_id"] = tenant_id
    if memory_type is not None:
        filter_params["memory_type"] = memory_type
    if storage_type is not None:
        filter_params["storage_type"] = storage_type
    try:
        result = memory_api_service.list_memory(db, user.id, filter_params, keywords or "", page, effective_page_size)
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
    page_size: int = Query(50, ge=1, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    effective_page_size = min(page_size, MEMORY_LIST_MAX_PAGE_SIZE)
    if agent_id and len(agent_id) == 1 and ',' in agent_id[0]:
        agent_id = agent_id[0].split(',')
    try:
        result = memory_api_service.get_memory_messages(
            db, memory_id, agent_id or [], keywords.strip() if keywords else "", page, effective_page_size
        )
        return get_json_result(data=result)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


# ==================== Message Endpoints ====================

class AddMessageRequest(BaseModel):
    """添加消息请求模型"""
    memory_id: str | list[str] = Field(..., description="Memory ID or list of Memory IDs")
    agent_id: str = Field(..., description="Agent ID")
    session_id: str = Field(..., description="Session ID")
    user_input: str = Field(..., description="User input message")
    agent_response: str = Field(..., description="Agent response message")
    user_id: str = Field(default="", description="Optional User ID")


class UpdateMessageRequest(BaseModel):
    """更新消息状态请求模型"""
    status: bool = Field(..., description="Message status (True=active, False=inactive)")


@router.post('/messages', summary="添加消息到记忆")
async def add_message(
    request_body: AddMessageRequest,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    memory_ids = request_body.memory_id
    if isinstance(memory_ids, str):
        memory_ids = [memory_ids]

    message_dict = {
        "user_id": request_body.user_id,
        "agent_id": request_body.agent_id,
        "session_id": request_body.session_id,
        "user_input": request_body.user_input,
        "agent_response": request_body.agent_response,
    }

    res, msg = await memory_api_service.add_message(db, memory_ids, message_dict)
    if res:
        return get_json_result(retmsg=msg)
    return get_json_result(retcode=RetCode.SERVER_ERROR, retmsg="Some messages failed to add. Detail:" + msg)


@router.delete('/messages/{memory_id}:{message_id}', summary="忘记（软删除）消息")
def forget_message(
    memory_id: str,
    message_id: int,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        memory_api_service.forget_message(db, memory_id, message_id)
        return get_json_result(data=True)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.put('/messages/{memory_id}:{message_id}', summary="更新消息状态")
def update_message(
    memory_id: str,
    message_id: int,
    request_body: UpdateMessageRequest,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    if not isinstance(request_body.status, bool):
        return get_error_argument_result("Status must be a boolean.")
    try:
        update_succeed = memory_api_service.update_message_status(db, memory_id, message_id, request_body.status)
        if update_succeed:
            return get_json_result(data=update_succeed)
        else:
            return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg=f"Failed to set status for message '{message_id}' in memory '{memory_id}'.")
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.get('/messages/search', summary="搜索记忆中的消息")
def search_message(
    memory_id: list[str] = Query(..., description="List of Memory IDs"),
    query: str = Query(..., description="Search query"),
    similarity_threshold: float = Query(default=0.2, description="Minimum similarity threshold"),
    keywords_similarity_weight: float = Query(default=0.7, description="Weight for keyword similarity"),
    top_n: int = Query(default=5, description="Maximum number of results"),
    agent_id: str = Query(default="", description="Optional Agent ID filter"),
    session_id: str = Query(default="", description="Optional Session ID filter"),
    user_id: str = Query(default="", description="Optional User ID filter"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    if len(memory_id) == 1 and ',' in memory_id[0]:
        memory_id = memory_id[0].split(',')
    filter_dict = {"memory_id": memory_id, "agent_id": agent_id, "session_id": session_id, "user_id": user_id}
    params = {
        "query": query,
        "similarity_threshold": similarity_threshold,
        "keywords_similarity_weight": keywords_similarity_weight,
        "top_n": top_n,
    }
    res = memory_api_service.search_message(db, filter_dict, params)
    return get_json_result(data=res)


@router.get('/messages', summary="获取最近的消息")
def get_messages(
    memory_id: list[str] = Query(..., description="List of Memory IDs"),
    agent_id: str = Query(default="", description="Agent ID"),
    session_id: str = Query(default="", description="Session ID"),
    limit: int = Query(default=10, description="Maximum number of messages"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    if len(memory_id) == 1 and ',' in memory_id[0]:
        memory_id = memory_id[0].split(',')
    if not memory_id:
        return get_error_argument_result("memory_ids is required.")
    try:
        res = memory_api_service.get_messages(db, memory_id, agent_id, session_id, limit)
        return get_json_result(data=res)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)


@router.get('/messages/{memory_id}:{message_id}/content', summary="获取消息内容")
def get_message_content(
    memory_id: str,
    message_id: int,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        res = memory_api_service.get_message_content(db, memory_id, message_id)
        return get_json_result(data=res)
    except NotFoundException as e:
        logger.error(e)
        return get_json_result(data=False, retmsg=str(e.msg), retcode=RetCode.NOT_FOUND)
    except Exception as e:
        logger.error(e)
        return get_json_result(data=False, retmsg="Internal server error", retcode=RetCode.SERVER_ERROR)
