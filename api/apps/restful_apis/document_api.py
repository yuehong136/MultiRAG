"""RESTful document API endpoints mounted under /api/v1."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.db_models import get_async_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result, server_error_response
from api.utils.validation_utils import UpdateDocumentReq
from common.constants import RetCode

router = APIRouter()
logger = logging.getLogger(__name__)


class UpdateDocumentRequest(UpdateDocumentReq):
    pass


@router.put("/datasets/{dataset_id}/documents/{document_id}", summary="更新文档")
async def update_document(
    dataset_id: str,
    document_id: str,
    request: UpdateDocumentRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    req = request.model_dump(exclude_unset=True)

    def _update(s: Session) -> Response:
        kb = KnowledgebaseService.get_by_id(s, dataset_id)
        if not kb:
            return get_error_data_result(retmsg="Can't find this dataset!")
        if not document_api_service.can_update_dataset(s, tenant_id, kb):
            return get_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

        doc = DocumentService.query(s, kb_id=dataset_id, id=document_id)
        if not doc:
            return get_error_data_result(retmsg="The dataset doesn't own the document.")
        doc = doc[0]

        error_msg, error_code = document_api_service.validate_document_update_fields(s, request, doc, req)
        if error_msg:
            return get_error_data_result(retmsg=error_msg, retcode=error_code)

        if "meta_fields" in req:
            if not DocMetadataService.update_document_metadata(s, document_id, req["meta_fields"]):
                return get_error_data_result(retmsg="Failed to update metadata")

        if "name" in req and req["name"] != doc.name:
            if error := document_api_service.update_document_name_only(s, document_id, req["name"]):
                return error

        if "parser_config" in req:
            DocumentService.update_parser_config(s, doc.id, req["parser_config"])

        if "chunk_method" in req and req["chunk_method"] is not None:
            if error := document_api_service.update_chunk_method_only(s, req, doc, dataset_id, tenant_id):
                return error

        if "enabled" in req and req["enabled"] is not None:
            status = int(req["enabled"])
            if error := document_api_service.update_document_status_only(s, status, doc, kb):
                return error

        try:
            doc = DocumentService.get_by_id(s, doc.id)
            if not doc:
                return get_error_data_result(retmsg="Document update failed")
        except OperationalError as e:
            logger.exception(e)
            return get_error_data_result(retmsg="Database operation failed")

        return get_result(data=document_api_service.rename_doc_key(s, doc))

    return await db.run_sync(_update)  # TODO(async-phase4)


@router.get("/datasets/{dataset_id}/metadata/summary", summary="获取元数据汇总")
async def metadata_summary(
    dataset_id: str,
    doc_ids: str = Query("", description="逗号分隔的文档 ID，按文档过滤汇总"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """获取数据集中文档元数据的汇总统计。

    返回每个元数据键下各值的出现次数，按次数降序排列，
    格式: {"summary": {key: [[value, count], ...], ...}}
    """
    doc_id_list = doc_ids.split(",") if doc_ids else None

    def _summary(s: Session) -> Response:
        if not KnowledgebaseService.accessible(s, kb_id=dataset_id, user_id=tenant_id):
            return get_error_data_result(retmsg=f"You don't own the dataset {dataset_id}.")
        try:
            summary = DocMetadataService.get_metadata_summary(s, dataset_id, doc_id_list)
            return get_result(data={"summary": summary})
        except Exception as e:
            return server_error_response(e)

    return await db.run_sync(_summary)  # TODO(async-phase4)
