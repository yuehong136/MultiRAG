"""RESTful document API endpoints mounted under /api/v1."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.common.check_team_permission import check_kb_team_permission
from api.constants import FILE_NAME_LEN_LIMIT, IMG_BASE64_PREFIX
from api.db import VALID_FILE_TYPES
from api.db.db_models import get_async_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result, server_error_response
from api.utils.validation_utils import UpdateDocumentReq
from common.constants import RetCode, TaskStatus
from common.metadata_utils import convert_conditions, meta_filter, turn2jsonschema

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


def _parse_object_query_param(raw_value: str | None, name: str) -> tuple[dict[str, Any], Response | None]:
    if not raw_value:
        return {}, None
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}, get_error_data_result(retmsg=f"{name} must be valid JSON.")
    if not isinstance(value, dict):
        return {}, get_error_data_result(retmsg=f"{name} must be an object.")
    return value, None


def _metadata_document_ids(
    db: Session,
    dataset_id: str,
    metadata_condition: dict[str, Any],
    metadata: dict[str, Any],
) -> list[str] | None:
    """Resolve metadata filters, distinguishing no filter (None) from no matches ([])."""
    if not metadata_condition and not metadata:
        return None

    metas = DocMetadataService.get_flatted_meta_by_kbs(db, [dataset_id])
    doc_ids: set[str] | None = None

    if metadata_condition:
        doc_ids = set(meta_filter(metas, convert_conditions(metadata_condition), metadata_condition.get("logic", "and")))
        if metadata_condition.get("conditions") and not doc_ids:
            return []

    if metadata:
        metadata_doc_ids: set[str] | None = None
        for key, raw_values in metadata.items():
            if not raw_values:
                continue
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            normalized_values = [str(value) for value in values if value is not None and str(value).strip()]
            if not normalized_values:
                continue
            key_doc_ids: set[str] = set()
            for value in normalized_values:
                key_doc_ids.update(metas.get(key, {}).get(value, []))
            metadata_doc_ids = key_doc_ids if metadata_doc_ids is None else metadata_doc_ids & key_doc_ids
            if not metadata_doc_ids:
                return []

        if metadata_doc_ids is not None:
            doc_ids = metadata_doc_ids if doc_ids is None else doc_ids & metadata_doc_ids

    return list(doc_ids) if doc_ids is not None else None


@router.get("/datasets/{dataset_id}/documents", summary="获取文档列表")
async def list_documents(
    dataset_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    list_type: str | None = Query(None, alias="type", description="filter=返回过滤面板聚合统计（忽略分页与元数据参数）"),
    keywords: str | None = Query(None),
    document_id: str | None = Query(None, alias="id"),
    document_ids: list[str] | None = Query(None, alias="ids", description="按文档 ID 批量过滤（与 id 互斥）"),
    document_name: str | None = Query(None, alias="name"),
    suffix: list[str] | None = Query(None),
    file_types: list[str] | None = Query(None, alias="types"),
    run: list[str] | None = Query(None),
    run_status: list[str] | None = Query(None),
    create_time_from: int = Query(0),
    create_time_to: int = Query(0),
    metadata_condition: str | None = Query(None, description="元数据复合过滤条件（JSON 对象）"),
    metadata: str | None = Query(None, description="元数据精确过滤条件（JSON 对象）"),
    return_empty_metadata: bool = Query(False, description="仅返回无元数据文档"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
) -> Response:
    """统一 web 会话与 API token 的 RESTful 文档列表入口；``type=filter`` 返回过滤面板聚合统计。"""
    file_types = file_types or []
    invalid_types = {file_type for file_type in file_types if file_type not in VALID_FILE_TYPES}
    if invalid_types:
        invalid = ", ".join(sorted(invalid_types))
        return get_error_data_result(retmsg=f"Invalid filter conditions: {invalid} type{'s' if len(invalid_types) > 1 else ''}")

    status_text_to_numeric = {status.name: status.value for status in TaskStatus}
    raw_statuses = (run or []) + (run_status or [])
    converted_statuses = [status_text_to_numeric.get(status.upper(), status) for status in raw_statuses]
    valid_statuses = set(status_text_to_numeric.values())
    invalid_statuses = {status for status in converted_statuses if status not in valid_statuses}
    if invalid_statuses:
        return get_error_data_result(retmsg=f"Invalid filter run status conditions: {', '.join(sorted(invalid_statuses))}")

    if document_id and document_ids:
        return get_error_data_result(retmsg=f"Should not provide both 'id':{document_id} and 'ids':{document_ids}")

    metadata_condition_value, error = _parse_object_query_param(metadata_condition, "metadata_condition")
    if error is not None:
        return error
    metadata_value, error = _parse_object_query_param(metadata, "metadata")
    if error is not None:
        return error

    if metadata_value.get("empty_metadata"):
        return_empty_metadata = True
        metadata_value = {key: value for key, value in metadata_value.items() if key != "empty_metadata"}
    if return_empty_metadata:
        metadata_condition_value = {}
        metadata_value = {}

    def _list(sync_db: Session) -> Response:
        if not KnowledgebaseService.accessible(sync_db, kb_id=dataset_id, user_id=tenant_id):
            return get_error_data_result(retmsg=f"You don't own the dataset {dataset_id}.")

        if list_type == "filter":
            try:
                docs_filter, filter_total = DocumentService.get_filter_by_kb_id(
                    sync_db,
                    dataset_id,
                    keywords,
                    converted_statuses,
                    file_types,
                    suffix or [],
                )
                return get_result(data={"total": filter_total, "filter": docs_filter})
            except Exception as exc:
                return server_error_response(exc)

        if document_id and not DocumentService.query(sync_db, id=document_id, kb_id=dataset_id):
            return get_error_data_result(retmsg=f"You don't own the document {document_id}.")
        if document_name and not DocumentService.query(sync_db, name=document_name, kb_id=dataset_id):
            return get_error_data_result(retmsg=f"You don't own the document {document_name}.")

        doc_ids = _metadata_document_ids(sync_db, dataset_id, metadata_condition_value, metadata_value)
        if document_id:
            doc_ids = [document_id] if doc_ids is None or document_id in doc_ids else []
        if document_ids:
            doc_ids = list(document_ids) if doc_ids is None else [candidate for candidate in document_ids if candidate in set(doc_ids)]

        try:
            docs, total = DocumentService.get_by_kb_id(
                sync_db,
                dataset_id,
                page,
                page_size,
                orderby,
                desc,
                keywords,
                converted_statuses,
                file_types,
                suffix or [],
                name=document_name,
                doc_ids=doc_ids,
                return_empty_metadata=return_empty_metadata,
            )

            if create_time_from or create_time_to:
                docs = [doc for doc in docs if (create_time_from == 0 or doc.get("create_time", 0) >= create_time_from) and (create_time_to == 0 or doc.get("create_time", 0) <= create_time_to)]

            output_docs = [document_api_service.map_doc_keys(sync_db, doc) for doc in docs]
            for doc in output_docs:
                thumbnail = doc.get("thumbnail")
                if thumbnail and not thumbnail.startswith(IMG_BASE64_PREFIX):
                    doc["thumbnail"] = f"/v1/document/image/{dataset_id}-{thumbnail}"
                if doc.get("source_type"):
                    doc["source_type"] = doc["source_type"].split("/")[0]
                parser_config = doc.get("parser_config") or {}
                if parser_config.get("metadata"):
                    parser_config["metadata"] = turn2jsonschema(parser_config["metadata"])
                    doc["parser_config"] = parser_config

            return get_result(data={"total": total, "docs": output_docs})
        except Exception as exc:
            return server_error_response(exc)

    return await db.run_sync(_list)  # TODO(async-phase4)
