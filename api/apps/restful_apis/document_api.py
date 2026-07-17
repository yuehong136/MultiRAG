"""RESTful document API endpoints mounted under /api/v1."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.common.check_team_permission import check_kb_team_permission
from api.constants import FILE_NAME_LEN_LIMIT
from api.db.db_models import get_async_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result, server_error_response
from api.utils.validation_utils import UpdateDocumentReq
from common.constants import RetCode

MAXIMUM_OF_UPLOADING_FILES = 256

router = APIRouter()
logger = logging.getLogger(__name__)


class UpdateDocumentRequest(UpdateDocumentReq):
    pass


# PATCH 是本端点的正典方法；PUT 为历史别名（前端 updateDocumentMeta / 旧集成仍在发
# PUT），标注 deprecated 留旧，待消费方全部迁移 PATCH 后移除。
@router.patch("/datasets/{dataset_id}/documents/{document_id}", summary="更新文档")
@router.put("/datasets/{dataset_id}/documents/{document_id}", summary="[Deprecated] 更新文档（请改用 PATCH）", deprecated=True)
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

        return get_result(data=document_api_service.map_doc_keys(s, doc))

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


@router.post("/datasets/{dataset_id}/documents", summary="上传文档")
async def upload_documents(
    dataset_id: str,
    file: list[UploadFile] | None = File(None, description="上传的文件（与 files 等价，至少提供其一）"),
    files: list[UploadFile] | None = File(None, description="上传的文件"),
    parent_path: str | None = Form(None, description="父文件夹下的可选嵌套路径，使用 '/' 分隔"),
    return_raw_files: bool = Query(False, description="跳过文档键名映射，返回原始文档数据"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """上传文档到数据集（web 会话与 API token 统一入口）。

    - multipart 字段 ``file`` 与 ``files`` 等价：``file`` 为正典字段名，
      ``files`` 兼容既有 SDK 消费方，二者可混用、按序合并。
    - ``return_raw_files=true`` 返回原始文档字段（web/admin 消费格式）；
      默认返回映射后的键名（chunk_count/dataset_id/... + run=UNSTART）。
    - 只负责上传和创建文档，不自动启动解析任务。
    """
    file_objs = (file or []) + (files or [])
    if not file_objs:
        return get_error_data_result(retmsg="No file part!", retcode=RetCode.ARGUMENT_ERROR)
    if len(file_objs) > MAXIMUM_OF_UPLOADING_FILES:
        return get_error_data_result(retmsg=f"You try to upload {len(file_objs)} files, which exceeds the maximum number: {MAXIMUM_OF_UPLOADING_FILES}", retcode=RetCode.ARGUMENT_ERROR)

    file_contents: list[tuple[bytes, str]] = []
    for file_obj in file_objs:
        if not file_obj.filename:
            return get_error_data_result(retmsg="No file selected!", retcode=RetCode.ARGUMENT_ERROR)
        if len(file_obj.filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            return get_error_data_result(retmsg=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.", retcode=RetCode.ARGUMENT_ERROR)
        file_contents.append((await file_obj.read(), file_obj.filename))

    def _upload(s: Session) -> Response:
        kb = KnowledgebaseService.get_by_id(s, dataset_id)
        if not kb:
            return get_error_data_result(retmsg=f"Can't find the dataset with ID {dataset_id}!", retcode=RetCode.DATA_ERROR)
        if not check_kb_team_permission(s, kb, tenant_id):
            return get_error_data_result(retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

        err, uploaded = FileService.upload_document(s, kb, file_contents, tenant_id, parent_path=parent_path)
        if err:
            return get_error_data_result(retmsg="\n".join(err), retcode=RetCode.SERVER_ERROR)
        if not uploaded:
            return get_error_data_result(retmsg="There seems to be an issue with your file format. Please verify it is correct and not corrupted.", retcode=RetCode.DATA_ERROR)

        docs = [f[0] for f in uploaded]  # 去掉 blob
        if return_raw_files:
            return get_result(data=docs)
        return get_result(data=[document_api_service.map_doc_keys_with_run_status(doc, "0") for doc in docs])

    return await db.run_sync(_upload)  # TODO(async-phase4)
