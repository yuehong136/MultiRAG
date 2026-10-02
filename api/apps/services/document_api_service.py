"""Document API business logic for RESTful document update endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.responses import Response
from sqlalchemy.orm import Session

from api.common.check_team_permission import check_kb_team_permission
from api.db import FileType
from api.db.db_models import Document, Knowledgebase, db_connection
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_ingest_service import IngestError, ingest_documents_sync
from api.db.services.document_service import DocumentService
from api.db.services.document_status_service import change_document_status_sync, finish_status_write
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import UserTenantService
from api.utils import validation_utils
from api.utils.api_utils import get_error_data_result, get_parser_config
from api.utils.validation_utils import UpdateDocumentReq
from api.utils.web_utils import html2pdf, is_valid_url
from common import settings
from common.constants import RetCode, TaskStatus
from common.metadata_utils import convert_conditions, meta_filter
from common.misc_utils import get_uuid
from core.nlp import rag_tokenizer, search


class MetadataBatchUpdateError(ValueError):
    """Raised when a document metadata batch request is invalid or unauthorized."""


def batch_update_document_metadata(
    db: Session,
    dataset_id: str,
    user_id: str,
    selector: Any,
    updates: Any,
    deletes: Any,
) -> dict[str, int]:
    if not KnowledgebaseService.accessible(db, kb_id=dataset_id, user_id=user_id):
        raise MetadataBatchUpdateError(f"You don't own the dataset {dataset_id}.")
    if not isinstance(selector, dict):
        raise MetadataBatchUpdateError("selector must be an object.")
    if not isinstance(updates, list) or not isinstance(deletes, list):
        raise MetadataBatchUpdateError("updates and deletes must be lists.")

    metadata_condition = selector.get("metadata_condition") or {}
    if metadata_condition and not isinstance(metadata_condition, dict):
        raise MetadataBatchUpdateError("metadata_condition must be an object.")

    raw_document_ids = selector.get("document_ids")
    if raw_document_ids is not None and not isinstance(raw_document_ids, list):
        raise MetadataBatchUpdateError("document_ids must be a list.")
    for update in updates:
        if not isinstance(update, dict) or not update.get("key") or "value" not in update:
            raise MetadataBatchUpdateError("Each update requires key and value.")
    for delete in deletes:
        if not isinstance(delete, dict) or not delete.get("key"):
            raise MetadataBatchUpdateError("Each delete requires key.")

    dataset_document_ids = set(KnowledgebaseService.list_documents_by_ids(db, [dataset_id]))
    if raw_document_ids is None:
        target_document_ids = dataset_document_ids
    else:
        requested_document_ids = set(raw_document_ids)
        invalid_ids = requested_document_ids - dataset_document_ids
        if invalid_ids:
            invalid_list = ", ".join(sorted(invalid_ids))
            raise MetadataBatchUpdateError(f"These documents do not belong to dataset {dataset_id}: {invalid_list}")
        target_document_ids = requested_document_ids

    if metadata_condition:
        metadata = DocMetadataService.get_flatted_meta_by_kbs(db, [dataset_id])
        filtered_ids = set(meta_filter(metadata, convert_conditions(metadata_condition), metadata_condition.get("logic", "and")))
        target_document_ids &= filtered_ids

    document_ids = sorted(target_document_ids)
    updated = DocMetadataService.batch_update_metadata(db, dataset_id, document_ids, updates, deletes)
    return {"updated": updated, "matched_docs": len(document_ids)}


def can_update_dataset(db: Session, user_id: str, kb) -> bool:
    role = UserTenantService.get_role_in_tenant(db, user_id=user_id, tenant_id=kb.tenant_id)
    return UserTenantService.can_update_tenant_resources(role)


def update_document_name_only(db: Session, document_id: str, req_doc_name: str):
    if not DocumentService.update_by_id(db, document_id, {"name": req_doc_name}):
        return get_error_data_result(retmsg="Database error (Document rename)!")

    informs = File2DocumentService.get_by_document_id(db, document_id)
    if informs:
        file = FileService.get_by_id(db, informs[0].file_id)
        if file:
            FileService.update_by_id(db, file.id, {"name": req_doc_name})

    tenant_id = DocumentService.get_tenant_id(db, document_id)
    doc = DocumentService.get_by_id(db, document_id)
    if not doc:
        return get_error_data_result(retmsg=f"Not able to find document by id:{document_id}")
    kb = KnowledgebaseService.get_by_id(db, doc.kb_id)
    if not kb:
        return get_error_data_result(retmsg=f"Can't find the dataset with ID {doc.kb_id}!")
    title_tks = rag_tokenizer.tokenize(req_doc_name)
    doc_store_body = {
        "docnm_kwd": req_doc_name,
        "title_tks": title_tks,
        "title_sm_tks": rag_tokenizer.fine_grained_tokenize(title_tks),
    }
    index_name = search.index_name_one(tenant_id, kb.name)
    if settings.docStoreConn.index_exist(index_name, doc.kb_id):
        settings.docStoreConn.update({"doc_id": document_id}, doc_store_body, index_name, doc.kb_id)
    return None


def update_chunk_method_only(db: Session, req: dict[str, Any], doc: Document, dataset_id: str, tenant_id: str):
    if str(doc.parser_id).lower() != req["chunk_method"].lower():
        updated = DocumentService.update_by_id(
            db,
            doc.id,
            {
                "parser_id": req["chunk_method"],
                "progress": 0,
                "progress_msg": "",
                "run": TaskStatus.UNSTART.value,
            },
        )
        if not updated:
            return get_error_data_result(retmsg="Document not found!")

    if not req.get("parser_config"):
        req["parser_config"] = get_parser_config(req["chunk_method"], req.get("parser_config"))
        DocumentService.update_parser_config(db, doc.id, req["parser_config"])

    if doc.token_num > 0:
        updated = DocumentService.increment_chunk_num(
            db,
            doc.id,
            doc.kb_id,
            doc.token_num * -1,
            doc.chunk_num * -1,
            doc.process_duration * -1,
        )
        if not updated:
            return get_error_data_result(retmsg="Document not found!")
        settings.docStoreConn.delete({"doc_id": doc.id}, search.index_name(tenant_id), dataset_id)
    return None


def update_document_status_only(db: Session, status: int, doc: Document, kb: Knowledgebase) -> Response | None:
    error = change_document_status_sync(db, doc, kb, str(status))
    return get_error_data_result(retmsg=error, retcode=RetCode.SERVER_ERROR) if error else None


def validate_document_update_fields(db: Session, update_doc_req: UpdateDocumentReq, doc: Document, req: dict[str, Any]):
    error_msg, error_code = validation_utils.validate_immutable_fields(update_doc_req, doc)
    if error_msg:
        return error_msg, error_code

    if "name" in req and req["name"] != doc.name:
        docs_from_name = DocumentService.query(db, name=req["name"], kb_id=doc.kb_id)
        error_msg, error_code = validation_utils.validate_document_name(req["name"], doc, docs_from_name)
        if error_msg:
            return error_msg, error_code

    if "chunk_method" in req and req["chunk_method"] is not None:
        error_msg, error_code = validation_utils.validate_chunk_method(doc, req["chunk_method"])
        if error_msg:
            return error_msg, error_code

    return None, None


def map_doc_keys(db: Session, doc: Document | dict[str, Any]) -> dict[str, Any]:
    """将文档 model/dict 的内部字段名映射为 RESTful API 响应字段名。"""
    serialized_doc = dict(doc) if isinstance(doc, dict) else DocumentService.serialize_document(db, doc)
    if serialized_doc is None:
        return {}
    renamed_doc = _process_key_mappings(serialized_doc)
    if "run" in renamed_doc:
        renamed_doc = _process_run_mapping(renamed_doc, renamed_doc["run"])
    return renamed_doc


def map_doc_keys_with_run_status(doc: dict[str, Any], run_status: str) -> dict[str, Any]:
    """dict 输入版本：上传路径的文档来自 FileService（dict 而非 model），run 状态由调用方显式给定。"""
    renamed_doc = _process_key_mappings(doc)
    return _process_run_mapping(renamed_doc, run_status)


def _process_key_mappings(doc: dict[str, Any]) -> dict[str, Any]:
    key_mapping = {
        "chunk_num": "chunk_count",
        "kb_id": "dataset_id",
        "token_num": "token_count",
        "parser_id": "chunk_method",
    }
    return {key_mapping.get(key, key): value for key, value in doc.items()}


def _process_run_mapping(doc: dict[str, Any], run_status: Any) -> dict[str, Any]:
    run_mapping = {
        "0": "UNSTART",
        "1": "RUNNING",
        "2": "CANCEL",
        "3": "DONE",
        "4": "FAIL",
    }
    # 未知 run 值原样透出（不强制归 UNSTART），避免丢失状态信息。
    doc["run"] = run_mapping.get(str(run_status), str(run_status))
    return doc


class DocumentCreationError(ValueError):
    """A document creation failure with the public business error code."""

    def __init__(self, message: str, retcode: RetCode = RetCode.DATA_ERROR) -> None:
        super().__init__(message)
        self.retcode = retcode


def _dataset_for_creation(db: Session, dataset_id: str, tenant_id: str) -> Knowledgebase:
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if kb is None:
        raise DocumentCreationError(f"Can't find the dataset with ID {dataset_id}!")
    if not check_kb_team_permission(db, kb, tenant_id):
        raise DocumentCreationError("No authorization.", RetCode.AUTHENTICATION_ERROR)
    return kb


def create_empty_document(db: Session, dataset_id: str, tenant_id: str, name: str) -> dict[str, Any]:
    """Create a virtual document and its file association in one request session."""
    kb = _dataset_for_creation(db, dataset_id, tenant_id)
    if DocumentService.query(db, name=name, kb_id=dataset_id):
        raise DocumentCreationError("Duplicated document name in the same dataset.")

    kb_root_folder = FileService.get_kb_folder(db, kb.tenant_id)
    if not kb_root_folder:
        raise DocumentCreationError("Cannot find the root folder.")
    kb_folder = FileService.new_a_file_from_kb(db, kb.tenant_id, kb.name, kb_root_folder["id"])
    if not kb_folder:
        raise DocumentCreationError("Cannot find the kb folder for this file.")

    doc = {
        "id": get_uuid(),
        "kb_id": kb.id,
        "parser_id": kb.parser_id,
        "pipeline_id": kb.pipeline_id,
        "parser_config": kb.parser_config,
        "created_by": tenant_id,
        "type": FileType.VIRTUAL,
        "name": name,
        "suffix": Path(name).suffix.lstrip("."),
        "location": "",
        "size": 0,
    }
    DocumentService.insert(db, doc)
    FileService.add_file_from_kb(db, doc, kb_folder["id"], kb.tenant_id)
    persisted = DocumentService.get_by_id(db, doc["id"])
    if persisted is None:
        raise RuntimeError("Created document could not be read back.")
    return map_doc_keys(db, persisted)


def create_web_document(dataset_id: str, tenant_id: str, name: str, url: str) -> dict[str, Any]:
    """Crawl and upload in a worker with self-owned short database sessions."""
    with db_connection() as db:
        _dataset_for_creation(db, dataset_id, tenant_id)

    if not is_valid_url(url):
        raise DocumentCreationError("The URL format is invalid", RetCode.ARGUMENT_ERROR)
    blob = html2pdf(url)
    if not blob:
        raise DocumentCreationError("Download failure.", RetCode.SERVER_ERROR)

    with db_connection() as db:
        kb = _dataset_for_creation(db, dataset_id, tenant_id)
        errors, uploaded = FileService.upload_document(db, kb, [(blob, f"{name}.pdf")], tenant_id)
        if errors:
            raise DocumentCreationError("\n".join(errors), RetCode.SERVER_ERROR)
        if not uploaded:
            raise DocumentCreationError("There seems to be an issue with your file format. Please verify it is correct and not corrupted.")
        return map_doc_keys_with_run_status(uploaded[0][0], "0")


async def create_web_document_async(dataset_id: str, tenant_id: str, name: str, url: str) -> dict[str, Any]:
    return await asyncio.to_thread(create_web_document, dataset_id, tenant_id, name, url)


class DocumentParseError(ValueError):
    """Invalid document selection; partial parse results remain available to callers."""

    def __init__(self, message: str, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.result = result


def _partition_dataset_documents(db: Session, dataset_id: str, document_ids: list[str]) -> tuple[list[str], list[str]]:
    """Split requested ids into those the dataset actually holds and those it does not."""
    valid_ids = []
    missing_ids = []
    for document_id in document_ids:
        if DocumentService.query(db, kb_id=dataset_id, id=document_id):
            valid_ids.append(document_id)
        else:
            missing_ids.append(document_id)
    return valid_ids, missing_ids


def _canonical_result(result: bool | dict[str, Any], document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    if result is not True:
        raise DocumentParseError("Parsing request was not submitted.")
    response: dict[str, Any] = {"success_count": len(document_ids)}
    if errors:
        response["errors"] = errors
    return response


def parse_dataset_documents(db: Session, dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """Retain the published clear-first default through shared reliable ingestion."""
    valid_ids, missing_ids = _partition_dataset_documents(db, dataset_id, document_ids)
    if missing_ids and not valid_ids:
        raise DocumentParseError(f"Documents not found: {missing_ids}")
    try:
        submitted = ingest_documents_sync(db, valid_ids, tenant_id, "1", clear=True, dataset_id=dataset_id)
        result = _canonical_result(submitted, valid_ids, errors)
    except IngestError as exc:
        partial = exc.result.get("results", {}) if exc.result else {}
        success_count = sum("error" not in item for item in partial.values())
        raise DocumentParseError(str(exc), {"success_count": success_count, "errors": [str(exc)], **(exc.result or {})}) from exc
    if missing_ids:
        result.setdefault("errors", []).append(f"Documents not found: {missing_ids}")
        raise DocumentParseError(f"Documents not found: {missing_ids}", result=result)
    return result


def stop_dataset_documents(db: Session, dataset_id: str, document_ids: list[str], errors: list[str], *, principal_id: str) -> dict[str, Any]:
    """Cancel submission keeps partial chunks/counters by default."""
    valid_ids, missing_ids = _partition_dataset_documents(db, dataset_id, document_ids)
    if missing_ids:
        raise DocumentParseError(f"Documents not found: {missing_ids}")
    try:
        submitted = ingest_documents_sync(db, valid_ids, principal_id, "2", dataset_id=dataset_id)
        return _canonical_result(submitted, valid_ids, errors)
    except IngestError as exc:
        partial = exc.result.get("results", {}) if exc.result else {}
        success_count = sum("error" not in item for item in partial.values())
        raise DocumentParseError(str(exc), {"success_count": success_count, "errors": [str(exc)], **(exc.result or {})}) from exc


async def parse_dataset_documents_async(dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """Blocking PDF/object/queue work and its sync session share one owned worker."""

    def worker() -> dict[str, Any]:
        with db_connection() as db:
            return parse_dataset_documents(db, dataset_id, tenant_id, document_ids, errors)

    return await finish_status_write(asyncio.to_thread(worker))


async def stop_dataset_documents_async(dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    def worker() -> dict[str, Any]:
        with db_connection() as db:
            return stop_dataset_documents(db, dataset_id, document_ids, errors, principal_id=tenant_id)

    return await finish_status_write(asyncio.to_thread(worker))
