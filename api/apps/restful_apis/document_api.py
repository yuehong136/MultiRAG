"""RESTful document API endpoints mounted under /api/v1."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.db_models import get_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import current_tenant_id, get_error_data_result, get_result
from api.utils.validation_utils import UpdateDocumentReq
from common.constants import RetCode

router = APIRouter()
logger = logging.getLogger(__name__)


class UpdateDocumentRequest(UpdateDocumentReq):
    pass


@router.put("/datasets/{dataset_id}/documents/{document_id}", summary="更新文档")
def update_document(
    dataset_id: str,
    document_id: str,
    request: UpdateDocumentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    req = request.model_dump(exclude_unset=True)

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return get_error_data_result(retmsg="Can't find this dataset!")
    if not document_api_service.can_update_dataset(db, tenant_id, kb):
        return get_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="The dataset doesn't own the document.")
    doc = doc[0]

    error_msg, error_code = document_api_service.validate_document_update_fields(db, request, doc, req)
    if error_msg:
        return get_error_data_result(retmsg=error_msg, retcode=error_code)

    if "meta_fields" in req:
        if not DocMetadataService.update_document_metadata(db, document_id, req["meta_fields"]):
            return get_error_data_result(retmsg="Failed to update metadata")

    if "name" in req and req["name"] != doc.name:
        if error := document_api_service.update_document_name_only(db, document_id, req["name"]):
            return error

    if "parser_config" in req:
        DocumentService.update_parser_config(db, doc.id, req["parser_config"])

    if "chunk_method" in req and req["chunk_method"] is not None:
        if error := document_api_service.update_chunk_method_only(db, req, doc, dataset_id, tenant_id):
            return error

    if "enabled" in req and req["enabled"] is not None:
        status = int(req["enabled"])
        if error := document_api_service.update_document_status_only(db, status, doc, kb):
            return error

    try:
        doc = DocumentService.get_by_id(db, doc.id)
        if not doc:
            return get_error_data_result(retmsg="Document update failed")
    except OperationalError as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Database operation failed")

    return get_result(data=document_api_service.rename_doc_key(db, doc))
