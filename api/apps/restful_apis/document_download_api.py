"""SDK binary downloads with dataset and beta-key authorization."""

from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from api.db import FileType
from api.db.db_models import APIToken, Tenant, get_db
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import construct_json_result, get_error_data_result, token_required
from api.utils.web_utils import document_content_type
from common import settings
from common.constants import RetCode, StatusEnum

router = APIRouter()


@router.get("/datasets/{dataset_id}/documents/{document_id}", summary="下载文档")
def download_document(dataset_id: str, document_id: str, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)) -> Response:
    """
    从数据集下载文档

    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        文档文件流
    """
    if not document_id:
        return get_error_data_result(retmsg="Specify document_id please.")

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg=f"You do not own the dataset {dataset_id}.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id, status=StatusEnum.VALID.value)
    if not doc:
        return get_error_data_result(retmsg=f"The dataset not own the document {document_id}.")

    doc = doc[0]
    try:
        bucket, name = File2DocumentService.get_authorized_document_storage_address(db, document_id, tenant_id)
    except PermissionError:
        return get_error_data_result(retmsg="You do not have access to this document.")
    file_stream = settings.STORAGE_IMPL.get(bucket, name)
    if not file_stream:
        return construct_json_result(message="This file is empty.", code=RetCode.DATA_ERROR)

    file = BytesIO(file_stream)
    encoded_filename = quote(doc.name, safe="")
    return StreamingResponse(
        file, media_type=document_content_type(doc.name, visual=doc.type == FileType.VISUAL.value), headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )


@router.get("/documents/{document_id}", summary="按文档ID下载文档")
def download_doc(
    document_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """
    仅通过文档ID下载文档（使用API Token认证）
    """
    token = request.headers.get("Authorization", "").split()
    if len(token) != 2 or token[0].lower() != "bearer":
        return get_error_data_result(retmsg="Authorization is not valid!")
    token = token[1]
    objs = APIToken.query(db, beta=token)
    if not objs:
        return get_error_data_result(retmsg="Authentication error: API key is invalid!")
    if len(objs) != 1:
        return get_error_data_result(retmsg="Authentication error: API key configuration is ambiguous.")
    tenant_id = objs[0].tenant_id
    if not tenant_id or not Tenant.query(db, id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="Authentication error: API key is invalid!")

    if not document_id:
        return get_error_data_result(retmsg="Specify document_id please.")
    doc = DocumentService.query(db, id=document_id, status=StatusEnum.VALID.value)
    if not doc:
        return get_error_data_result(retmsg=f"The dataset not own the document {document_id}.")
    if not KnowledgebaseService.query(db, id=doc[0].kb_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="You do not have access to this document.")
    try:
        bucket, key = File2DocumentService.get_authorized_document_storage_address(db, document_id, tenant_id)
    except PermissionError:
        return get_error_data_result(retmsg="You do not have access to this document.")
    file_stream = settings.STORAGE_IMPL.get(bucket, key)
    if not file_stream:
        return construct_json_result(message="This file is empty.", code=RetCode.DATA_ERROR)
    file = BytesIO(file_stream)
    encoded_filename = quote(doc[0].name, safe="")
    return StreamingResponse(
        file, media_type=document_content_type(doc[0].name, visual=doc[0].type == FileType.VISUAL.value), headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
    )
