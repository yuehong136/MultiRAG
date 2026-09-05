"""Document API business logic for RESTful document update endpoints."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import Document, Task, db_connection
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService, cancel_all_task_of
from api.db.services.user_service import UserTenantService
from api.utils import validation_utils
from api.utils.api_utils import get_error_data_result, get_parser_config, server_error_response
from api.utils.validation_utils import UpdateDocumentReq
from common import settings
from common.constants import TaskStatus
from common.metadata_utils import convert_conditions, meta_filter
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


def update_document_status_only(db: Session, status: int, doc: Document, kb):
    current_status = None if doc.status is None else int(doc.status)
    if current_status == status:
        return None

    try:
        if not DocumentService.update_by_id(db, doc.id, {"status": str(status)}):
            return get_error_data_result(retmsg="Database error (Document update)!")
        settings.docStoreConn.update({"doc_id": doc.id}, {"available_int": status}, search.index_name(kb.tenant_id, [kb.name]), doc.kb_id)
    except Exception as e:
        return server_error_response(e)
    return None


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


def parse_dataset_documents(db: Session, dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """(Re)queue parsing for documents in a dataset.

    Re-parsing a finished document drops its previous chunks first, so the run starts clean.
    """
    valid_ids, missing_ids = _partition_dataset_documents(db, dataset_id, document_ids)
    if missing_ids and not valid_ids:
        raise DocumentParseError(f"Documents not found: {missing_ids}")

    kb_table_num_map: dict[str, Any] = {}
    success_count = 0
    for document_id in valid_ids:
        doc = DocumentService.get_by_id(db, document_id)
        if not doc:
            errors.append(f"Document not found: {document_id}")
            continue

        info: dict[str, Any] = {"run": str(TaskStatus.RUNNING.value), "progress": 0}
        if str(doc.run) == TaskStatus.DONE.value:
            DocumentService.clear_chunk_num_when_rerun(db, doc.id)
            info["progress_msg"] = ""
            info["chunk_num"] = 0
            info["token_num"] = 0

        DocumentService.update_by_id(db, document_id, info)
        TaskService.filter_delete(db, [Task.doc_id == document_id])
        _drop_document_from_doc_store(db, doc.id, doc.kb_id)

        DocumentService.run(db, tenant_id, doc.to_dict(), kb_table_num_map)
        success_count += 1

    if missing_ids:
        errors.append(f"Documents not found: {missing_ids}")

    result: dict[str, Any] = {"success_count": success_count}
    if errors:
        result["errors"] = errors
    if missing_ids:
        raise DocumentParseError(f"Documents not found: {missing_ids}", result=result)
    return result


def _drop_document_from_doc_store(db: Session, doc_id: str, kb_id: str) -> None:
    """Clear a document's chunks from whichever doc-store backend is configured.

    Milvus addresses one collection per dataset and has no index_exist; the other
    backends probe the index first. Mirrors the legacy /document/run branch.
    """
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return
    collection_name = search.index_name_one(kb.tenant_id, kb.name)
    if settings.docStoreConn.db_type() == "milvus":
        if settings.docStoreConn.has_collection(collection_name):
            settings.docStoreConn.delete(condition={"doc_id": doc_id}, index_name=collection_name, dataset_id=kb.id)
        return
    if settings.docStoreConn.index_exist(collection_name, kb_id):
        settings.docStoreConn.delete({"doc_id": doc_id}, collection_name, kb_id)


def stop_dataset_documents(db: Session, dataset_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """Cancel in-flight parsing for documents in a dataset."""
    valid_ids, missing_ids = _partition_dataset_documents(db, dataset_id, document_ids)
    if missing_ids:
        raise DocumentParseError(f"Documents not found: {missing_ids}")

    success_count = 0
    for document_id in valid_ids:
        doc = DocumentService.get_by_id(db, document_id)
        if not doc:
            errors.append(f"Document not found: {document_id}")
            continue

        tasks = list(TaskService.query(db, doc_id=document_id))
        has_unfinished_task = any((task.progress or 0) < 1 for task in tasks)
        if str(doc.run) not in [TaskStatus.RUNNING.value, TaskStatus.CANCEL.value] and not has_unfinished_task:
            errors.append("Can't stop parsing document that has not started or already completed")
            continue

        cancel_all_task_of(db, document_id)
        DocumentService.update_by_id(db, document_id, {"run": str(TaskStatus.CANCEL.value)})
        success_count += 1

    result: dict[str, Any] = {"success_count": success_count}
    if errors:
        result["errors"] = errors
    return result


async def parse_dataset_documents_async(dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """Legacy parsing unit of work: its session and blocking IO stay in one worker.

    Do not pass an AsyncSession facade into this unit: queue_tasks mixes database,
    object storage, PDF parsing and Redis operations. TODO(async-phase4): migrate
    that shared queue workflow before replacing this boundary.
    """

    def _run() -> dict[str, Any]:
        with db_connection() as db:
            if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
                raise DocumentParseError(f"You don't own the dataset {dataset_id}.")
            return parse_dataset_documents(db, dataset_id, tenant_id, document_ids, errors)

    return await asyncio.to_thread(_run)


async def stop_dataset_documents_async(dataset_id: str, tenant_id: str, document_ids: list[str], errors: list[str]) -> dict[str, Any]:
    """Keep the legacy SQL/Redis cancellation unit on a single worker-owned session."""

    def _run() -> dict[str, Any]:
        with db_connection() as db:
            if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
                raise DocumentParseError(f"You don't own the dataset {dataset_id}.")
            return stop_dataset_documents(db, dataset_id, document_ids, errors)

    return await asyncio.to_thread(_run)
