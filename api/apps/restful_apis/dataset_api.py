"""
@project: multirag
@Author：龙
@file： dataset_api.py
@date：2026/06/04
@desc: Dataset API 网关层 - 路由 + 鉴权 + 参数校验，业务逻辑委托给 service 层。

鉴权：使用统一异步鉴权依赖 async_current_tenant_id（同时接受 web 会话 JWT 与 SDK API-key），
      因此对外 /api/v1/datasets 既服务 web 前端又服务 SDK。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import dataset_api_service
from api.db.db_models import get_async_db
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result
from api.utils.validation_utils import CreateDatasetReq
from common.constants import RetCode

router = APIRouter()
logger = logging.getLogger(__name__)


# ==================== Pydantic Models (V2 风格) ====================


class CreateDatasetRequest(CreateDatasetReq):
    pass


class UpdateDatasetRequest(BaseModel):
    name: str | None = None
    avatar: str | None = None
    description: str | None = None
    embedding_model: str | None = None
    permission: str | None = None
    chunk_method: str | None = None
    pagerank: int | None = None
    language: str | None = None
    connectors: list[dict] | None = None
    parser_config: dict[str, Any] | None = None
    auto_metadata_config: dict[str, Any] | None = None
    # ext：承接前端塞进来的旧 web 扩展参数
    ext: dict[str, Any] = {}


class DeleteDatasetRequest(BaseModel):
    ids: list[str] | None = None
    delete_all: bool = False


class AutoMetadataConfigRequest(BaseModel):
    enabled: bool = True
    fields: list[dict[str, Any]] = []


# ==================== 响应映射 ====================


def _respond(success: bool, result: Any):
    """通用映射：成功 -> get_result(data)，失败 -> get_error_data_result(retmsg)。

    兼容个别底层 helper（如 KnowledgebaseService.create_with_name）失败时仍返回
    已构造好的 HTTP 响应（遗留实现），此处识别 JSONResponse 后原样透传。
    """
    if success:
        return get_result(data=result)
    if isinstance(result, JSONResponse):
        return result
    return get_error_data_result(retmsg=result)


# ==================== API Endpoints ====================


@router.post("/datasets", summary="创建数据集")
async def create_dataset(
    request: CreateDatasetRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.create_dataset(s, tenant_id, request.model_dump()))  # TODO(async-phase4)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/datasets", summary="删除数据集")
async def delete(
    request: DeleteDatasetRequest,
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        # DB 与 doc-store/存储/Redis 深度交错：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.delete_datasets_async(tenant_id, request.ids, request.delete_all)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.put("/datasets/{dataset_id}", summary="更新数据集")
async def update_dataset(
    dataset_id: str,
    request: UpdateDatasetRequest,
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        # pagerank 分支内联 doc-store 同步 HTTP：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.update_dataset_async(tenant_id, dataset_id, request.model_dump(exclude_unset=True))
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets", summary="获取数据集列表")
async def list_datasets(
    id: str | None = Query(None, description="数据集ID过滤"),
    name: str | None = Query(None, description="数据集名称过滤"),
    page: int = Query(1, description="页码"),
    page_size: int = Query(30, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    keywords: str | None = Query(None, description="按名称模糊搜索"),
    parser_id: str | None = Query(None, description="按 chunk_method(parser_id) 过滤"),
    owner_ids: list[str] | None = Query(None, description="按所属租户过滤（承接旧 web owner_ids）"),
    include_parsing_status: bool = Query(False, description="是否包含文档解析状态计数"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    args = {
        "id": id,
        "name": name,
        "page": page,
        "page_size": page_size,
        "orderby": orderby,
        "desc": desc,
        "keywords": keywords,
        "parser_id": parser_id,
        "owner_ids": owner_ids,
        "include_parsing_status": include_parsing_status,
    }
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.list_datasets(s, tenant_id, args))  # TODO(async-phase4)
        if success:
            return get_result(data=result["data"], total=result["total"])
        return get_error_data_result(retmsg=result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/auto_metadata", summary="获取数据集自动元数据配置")
async def get_auto_metadata(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.get_auto_metadata(s, tenant_id, dataset_id))  # TODO(async-phase4)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.put("/datasets/{dataset_id}/auto_metadata", summary="更新数据集自动元数据配置")
async def update_auto_metadata(
    dataset_id: str,
    request: AutoMetadataConfigRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.update_auto_metadata(s, tenant_id, dataset_id, request.model_dump()))  # TODO(async-phase4)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/knowledge_graph", summary="获取数据集知识图谱")
async def get_knowledge_graph(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await dataset_api_service.get_knowledge_graph(db, tenant_id, dataset_id)
        if success:
            return get_result(data=result)
        # 无权限：保持 AUTHENTICATION_ERROR 语义（与历史行为一致）
        return get_result(data=False, retmsg=result, retcode=RetCode.AUTHENTICATION_ERROR)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/datasets/{dataset_id}/knowledge_graph", summary="删除数据集知识图谱")
async def delete_knowledge_graph(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await dataset_api_service.delete_knowledge_graph(db, tenant_id, dataset_id)
        if success:
            return get_result(data=result)
        return get_result(data=False, retmsg=result, retcode=RetCode.AUTHENTICATION_ERROR)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/datasets/{dataset_id}/run_graphrag", summary="运行GraphRAG任务")
async def run_graphrag(
    dataset_id: str,
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        # 任务入队（共享 helper）内 DB 写 + Redis 交错：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.run_graphrag_async(tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/trace_graphrag", summary="追踪GraphRAG任务状态")
async def trace_graphrag(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.trace_graphrag(s, tenant_id, dataset_id))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/datasets/{dataset_id}/run_raptor", summary="运行RAPTOR任务")
async def run_raptor(
    dataset_id: str,
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        # 任务入队（共享 helper）内 DB 写 + Redis 交错：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.run_raptor_async(tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/trace_raptor", summary="追踪RAPTOR任务状态")
async def trace_raptor(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.trace_raptor(s, tenant_id, dataset_id))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")
