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
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import dataset_api_service, dataset_search_service
from api.db.db_models import get_async_db
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result
from api.utils.dataset_search import SearchDatasetRequest
from api.utils.validation_utils import CreateDatasetReq, validate_dataset_raptor_scope
from common.constants import RetCode
from common.metadata_config import MetadataConfig, validate_parser_metadata

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
    auto_metadata_config: MetadataConfig | None = None
    # ext：承接前端塞进来的旧 web 扩展参数
    ext: dict[str, Any] = {}

    @model_validator(mode="after")
    def validate_raptor_scope(self) -> UpdateDatasetRequest:
        # Validate only an explicitly patched scope; retain unknown fields and
        # avoid filling parser defaults into a partial update.
        validate_dataset_raptor_scope(self.ext.get("parser_config", self.parser_config))
        validate_parser_metadata(self.ext.get("parser_config", self.parser_config))
        return self


class DeleteDatasetRequest(BaseModel):
    ids: list[str] | None = None
    delete_all: bool = False


class DeleteTagsRequest(BaseModel):
    tags: list[str]


class RenameTagRequest(BaseModel):
    from_tag: str
    to_tag: str


def _parse_dataset_ids(raw: str | None) -> list[str]:
    return [d.strip() for d in (raw or "").split(",") if d.strip()]


async def _delete_index(tenant_id: str, dataset_id: str, index_type: str) -> Response:
    """DELETE 索引的两种寻址（?type= 与路径段）共用的执行体。"""
    index_type = (index_type or "").lower()
    if index_type not in dataset_api_service.VALID_INDEX_TYPES:
        return get_error_data_result(retmsg=f"Invalid index type '{index_type}'")
    try:
        success, result = await dataset_api_service.delete_index_async(tenant_id, dataset_id, index_type)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# ==================== 响应映射 ====================


def _respond(success: bool, result: Any) -> Response:
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


# 注意：以下静态多段路径必须先于 /datasets/{dataset_id}/... 的变量段路由注册。
# FastAPI 按注册顺序线性匹配，变量段会抢先吞掉同形状的静态路径。


@router.get("/datasets/tags/aggregation", summary="跨数据集聚合标签")
async def aggregate_tags(
    dataset_ids: str | None = Query(None, description="数据集ID列表，逗号分隔"),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    ids = _parse_dataset_ids(dataset_ids)
    if not ids:
        return get_error_data_result(retmsg="Lack of dataset_ids in query parameters")
    try:
        success, result = await dataset_api_service.aggregate_tags_async(tenant_id, ids)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/metadata/keys", summary="List document metadata keys across datasets")
async def get_metadata_keys(
    dataset_ids: str = Query(...),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    from api.db.services.doc_metadata_service import DocMetadataService
    from api.db.services.knowledgebase_service import KnowledgebaseService

    ids = _parse_dataset_ids(dataset_ids)
    if not ids:
        return get_error_data_result(retmsg="Lack of dataset_ids in query parameters")
    for kb_id in ids:
        if not await KnowledgebaseService.accessible_async(db, kb_id, tenant_id):
            return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg="No authorization.")
    try:
        keys: set[str] = set()
        for kb_id in ids:
            metadata = await DocMetadataService.get_metadata_for_documents_async(db, None, kb_id)
            for fields in metadata.values():
                keys.update(fields)
        return get_result(data=sorted(keys))
    except Exception:
        logger.exception("Unable to read document metadata keys")
        return get_error_data_result(retcode=RetCode.SERVER_ERROR, retmsg="Unable to read document metadata keys.")


@router.get("/datasets/metadata/flattened", summary="跨数据集获取拉平的文档元数据")
async def get_flattened_metadata(
    dataset_ids: str | None = Query(None, description="数据集ID列表，逗号分隔"),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    ids = _parse_dataset_ids(dataset_ids)
    if not ids:
        return get_error_data_result(retmsg="Lack of dataset_ids in query parameters")
    try:
        success, result = await dataset_api_service.get_flattened_metadata_async(tenant_id, ids)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}", summary="获取数据集详情")
async def get_dataset(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.get_dataset(db, tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/ingestions/summary", summary="获取数据集摄取概览")
async def get_ingestion_summary(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.get_ingestion_summary(db, tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/ingestions", summary="列出数据集摄取日志")
async def list_ingestion_logs(
    dataset_id: str,
    page: int = Query(0, ge=0, description="页码"),
    page_size: int = Query(0, ge=0, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    operation_status: list[str] | None = Query(None, description="按操作状态过滤，可重复"),
    create_date_from: datetime | None = Query(None, description="创建日期起始"),
    create_date_to: datetime | None = Query(None, description="创建日期结束"),
    log_type: str = Query("dataset", description="日志类别：file 或 dataset"),
    keywords: str | None = Query(None, description="文件或任务名称关键词"),
    types: list[str] | None = Query(None, description="文件类型，可重复"),
    suffix: list[str] | None = Query(None, description="文件后缀，可重复"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.list_ingestion_logs(
            db,
            tenant_id,
            dataset_id,
            page,
            page_size,
            orderby,
            desc,
            operation_status,
            create_date_from,
            create_date_to,
            log_type,
            keywords,
            types,
            suffix,
        )
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# 必须晚于 /ingestions/summary 注册，否则 {log_id} 会把 "summary" 当成日志 ID。
@router.get("/datasets/{dataset_id}/ingestions/{log_id}", summary="获取单条摄取日志")
async def get_ingestion_log(
    dataset_id: str,
    log_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.get_ingestion_log(db, tenant_id, dataset_id, log_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/tags", summary="获取数据集标签")
async def list_tags(
    dataset_id: str,
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.list_tags_async(tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/datasets/{dataset_id}/tags", summary="删除数据集标签")
async def delete_tags(
    dataset_id: str,
    request: DeleteTagsRequest,
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.delete_tags_async(tenant_id, dataset_id, request.tags)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.put("/datasets/{dataset_id}/tags", summary="重命名数据集标签")
async def rename_tag(
    dataset_id: str,
    request: RenameTagRequest,
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    if not request.from_tag.strip() or not request.to_tag.strip():
        return get_error_data_result(retmsg="from_tag and to_tag must not be empty")
    try:
        success, result = await dataset_api_service.rename_tag_async(tenant_id, dataset_id, request.from_tag, request.to_tag)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/datasets/{dataset_id}/index", summary="运行索引任务（graph/raptor/mindmap）")
async def run_index(
    dataset_id: str,
    type: str = Query("", description="索引类型：graph / raptor / mindmap"),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.run_index_async(tenant_id, dataset_id, type.lower())
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/index", summary="追踪索引任务状态")
async def trace_index(
    dataset_id: str,
    type: str = Query("", description="索引类型：graph / raptor / mindmap"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.trace_index(db, tenant_id, dataset_id, type.lower())
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/datasets/{dataset_id}/index", summary="删除索引任务及其产物")
async def delete_index_by_query(
    dataset_id: str,
    type: str = Query("", description="索引类型：graph / raptor / mindmap"),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    return await _delete_index(tenant_id, dataset_id, type)


@router.get("/datasets/{dataset_id}/metadata/config", summary="获取数据集自动元数据配置")
async def get_metadata_config(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.get_auto_metadata(s, tenant_id, dataset_id))  # TODO(async-phase4)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.put("/datasets/{dataset_id}/metadata/config", summary="更新数据集自动元数据配置")
async def update_metadata_config(
    dataset_id: str,
    request: MetadataConfig,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await db.run_sync(lambda s: dataset_api_service.update_auto_metadata(s, tenant_id, dataset_id, request.model_dump(exclude_unset=True)))  # TODO(async-phase4)
        return _respond(success, result)
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/datasets/{dataset_id}/search", summary="数据集检索测试")
async def search_dataset(
    dataset_id: str,
    request: SearchDatasetRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result, code = await dataset_search_service.search_dataset(db, tenant_id, dataset_id, request)
        return get_result(data=result) if success else get_error_data_result(retcode=code, retmsg=result)
    except Exception as exc:
        logger.exception(exc)
        message = "No chunk found! Check the chunk status please!" if "not_found" in str(exc) else "Internal server error"
        return get_error_data_result(retmsg=message)


@router.get("/datasets/{dataset_id}/graph", summary="获取数据集或文档知识图谱")
async def get_graph(
    dataset_id: str,
    doc_id: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        if doc_id is None:
            success, result = await dataset_api_service.get_knowledge_graph(db, tenant_id, dataset_id)
        else:
            success, result = await dataset_search_service.get_document_graph(db, tenant_id, dataset_id, doc_id)
        if success:
            return get_result(data=result)
        code = RetCode.AUTHENTICATION_ERROR if result == "No authorization." else RetCode.DATA_ERROR
        return get_error_data_result(retcode=code, retmsg=result)
    except Exception as exc:
        logger.exception(exc)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/datasets/{dataset_id}/graph/search", summary="获取数据集知识图谱")
@router.get("/datasets/{dataset_id}/knowledge_graph", summary="获取数据集知识图谱", deprecated=True)
async def get_knowledge_graph(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.get_knowledge_graph(db, tenant_id, dataset_id)
        if success:
            return get_result(data=result)
        # 无权限：保持 AUTHENTICATION_ERROR 语义（与历史行为一致）
        return get_result(data=False, retmsg=result, retcode=RetCode.AUTHENTICATION_ERROR)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/datasets/{dataset_id}/knowledge_graph", summary="删除数据集知识图谱", deprecated=True)
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


# @deprecated —— 由 POST /datasets/{dataset_id}/index?type=graph 取代；生产前端仍在调用。
@router.post("/datasets/{dataset_id}/run_graphrag", summary="运行GraphRAG任务", deprecated=True)
async def run_graphrag(
    dataset_id: str,
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        # 任务入队（共享 helper）内 DB 写 + Redis 交错：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.run_graphrag_async(tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# @deprecated —— 由 GET /datasets/{dataset_id}/index?type=graph 取代；生产前端仍在调用。
@router.get("/datasets/{dataset_id}/trace_graphrag", summary="追踪GraphRAG任务状态", deprecated=True)
async def trace_graphrag(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.trace_graphrag(db, tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# @deprecated —— 由 POST /datasets/{dataset_id}/index?type=raptor 取代；生产前端仍在调用。
@router.post("/datasets/{dataset_id}/run_raptor", summary="运行RAPTOR任务", deprecated=True)
async def run_raptor(
    dataset_id: str,
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        # 任务入队（共享 helper）内 DB 写 + Redis 交错：整块在工作线程 + 自开短会话执行
        success, result = await dataset_api_service.run_raptor_async(tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# @deprecated —— 由 GET /datasets/{dataset_id}/index?type=raptor 取代；生产前端仍在调用。
@router.get("/datasets/{dataset_id}/trace_raptor", summary="追踪RAPTOR任务状态", deprecated=True)
async def trace_raptor(
    dataset_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    try:
        success, result = await dataset_api_service.trace_raptor(db, tenant_id, dataset_id)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# Explicit compatibility paths must not match another router's /documents endpoint.
@router.delete("/datasets/{dataset_id}/graph", summary="删除图索引任务及产物")
async def delete_graph_index(dataset_id: str, tenant_id: str = Depends(async_current_tenant_id)) -> Response:
    return await _delete_index(tenant_id, dataset_id, "graph")


@router.delete("/datasets/{dataset_id}/raptor", summary="删除 RAPTOR 索引任务及产物")
async def delete_raptor_index(dataset_id: str, tenant_id: str = Depends(async_current_tenant_id)) -> Response:
    return await _delete_index(tenant_id, dataset_id, "raptor")


@router.delete("/datasets/{dataset_id}/mindmap", summary="删除思维导图索引任务")
async def delete_mindmap_index(dataset_id: str, tenant_id: str = Depends(async_current_tenant_id)) -> Response:
    return await _delete_index(tenant_id, dataset_id, "mindmap")
