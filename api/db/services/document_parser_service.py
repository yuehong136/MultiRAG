"""Preflight and atomically save document parser, metadata and presentation fields."""

import asyncio
import copy
import math
from pathlib import PurePath
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.constants import FILE_NAME_LEN_LIMIT
from api.db.db_models import Document, File, File2Document, Knowledgebase, Task, UserCanvas, UserTenant
from api.db.services.document_ingest_service import DocumentMutationPlan, Selection, _operate, writable_kb
from api.db.services.document_status_service import finish_status_write
from api.db.services.document_update_effects import prepare_update_effects
from api.utils.document_parser_config import merge_document_parser_config
from api.utils.document_parser_mode import UNSET, resolve_parser_mode
from api.utils.document_pipeline_validation import validate_document_pipeline
from api.utils.document_update_contract import DocumentUpdateError, DocumentUpdatePatch
from common.constants import FileSource


def _authorize(db: Session, dataset_id: str, document_id: str, principal_id: str) -> tuple[Document, Knowledgebase]:
    kb = db.scalar(select(Knowledgebase).where(Knowledgebase.id == dataset_id, Knowledgebase.status == "1"))
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.kb_id == dataset_id))
    if kb is None or doc is None:
        raise DocumentUpdateError("Document is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
    member = db.scalar(select(UserTenant).where(UserTenant.tenant_id == kb.tenant_id, UserTenant.user_id == principal_id, UserTenant.status == "1"))
    if principal_id != kb.tenant_id and member is None:
        raise DocumentUpdateError("Document is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
    if writable_kb(db, dataset_id, principal_id) is None:
        raise DocumentUpdateError("Document is not writable.", status=403, numeric_code=109, code="DOCUMENT_UPDATE_FORBIDDEN")
    return doc, kb


def _source(db: Session, doc: Document) -> tuple[str, str]:
    file = db.scalar(select(File).join(File2Document, File2Document.file_id == File.id).where(File2Document.document_id == doc.id).order_by(File2Document.id).limit(1))
    if file is None and db.scalar(select(File2Document.id).where(File2Document.document_id == doc.id).limit(1)) is not None:
        raise DocumentUpdateError("Document source is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
    # Suffix/location are saved source metadata. Neither a renamed display name
    # nor the new name in this PATCH can reclassify the uploaded bytes.
    local_file = file is not None and (not file.source_type or file.source_type == FileSource.LOCAL)
    location = file.location if local_file else doc.location
    filename = location if location and PurePath(location).suffix else ("source." + doc.suffix.lstrip(".") if doc.suffix else (file.name if local_file else doc.name or ""))
    return doc.type, filename


def _canvas(db: Session, identifier: str, kb: Knowledgebase, principal_id: str, *, lock: bool) -> None:
    statement = select(UserCanvas).where(UserCanvas.id == identifier)
    if lock:
        statement = statement.with_for_update(read=True)
    canvas = db.scalar(statement.execution_options(populate_existing=True))
    if canvas is None or canvas.user_id != kb.tenant_id or (principal_id != kb.tenant_id and canvas.permission != "team"):
        raise DocumentUpdateError("Document pipeline is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
    if canvas.canvas_category != "dataflow_canvas":
        raise DocumentUpdateError("A document DataFlow is required.")
    try:
        validate_document_pipeline(canvas.dsl)
    except Exception:
        raise DocumentUpdateError("Document pipeline definition is invalid.") from None


def prepare_document_update(db: Session, doc: Document, kb: Knowledgebase, request: DocumentUpdatePatch, principal_id: str, *, lock: bool = False) -> DocumentMutationPlan:
    provided = request.model_dump(exclude_unset=True)
    values: dict[str, Any] = {}
    for public, stored in [("chunk_count", "chunk_num"), ("token_count", "token_num"), ("progress", "progress")]:
        if public in provided and (not math.isclose(provided[public], getattr(doc, stored)) if public == "progress" else provided[public] != getattr(doc, stored)):
            raise DocumentUpdateError(f"Cannot change {public}.")
    if "name" in provided:
        name = provided["name"]
        if len(name.encode("utf-8")) > FILE_NAME_LEN_LIMIT or PurePath(name.lower()).suffix != PurePath((doc.name or "").lower()).suffix:
            raise DocumentUpdateError("Document name or extension is invalid.")
        duplicate = db.scalar(select(Document.id).where(Document.kb_id == kb.id, Document.id != doc.id, Document.name == name).limit(1))
        if duplicate is not None:
            raise DocumentUpdateError("Document name already exists in this dataset.")
        if name != doc.name:
            values["name"] = name
    file_type, filename = _source(db, doc)
    try:
        plan = resolve_parser_mode(
            doc.parser_id, doc.pipeline_id, chunk_method=provided.get("chunk_method", UNSET), pipeline_id=provided.get("pipeline_id", UNSET), file_type=file_type, filename=filename
        )
        if "parser_config" in provided:
            if not isinstance(doc.parser_config, dict):
                raise ValueError("Stored document parser configuration requires repair.")
            if request.parser_config is None:
                raise ValueError("Document parser configuration is required.")
            values["parser_config"] = merge_document_parser_config(doc.parser_config, request.parser_config)
    except ValueError:
        raise DocumentUpdateError("Document parser selection or configuration is invalid.") from None
    if plan.pipeline_id:
        _canvas(db, plan.pipeline_id, kb, principal_id, lock=lock)
    if "chunk_method" in provided:
        values["parser_id"] = plan.parser_id
    if "pipeline_id" in provided or ("chunk_method" in provided and doc.pipeline_id):
        values["pipeline_id"] = plan.pipeline_id
    if "enabled" in provided:
        values["status"] = str(int(provided["enabled"]))
    metadata = provided.get("meta_fields")
    effects = prepare_update_effects(db, doc, kb, values, plan.reset_needed, metadata, principal_id) if lock else {}
    return DocumentMutationPlan(values, plan.reset_needed, effects)


def preflight_document_update(db: Session, dataset_id: str, document_id: str, principal_id: str, request: DocumentUpdatePatch) -> Selection:
    doc, kb = _authorize(db, dataset_id, document_id, principal_id)
    prepare_document_update(db, doc, kb, request, principal_id)
    return Selection(doc.id, doc.kb_id, frozenset(db.scalars(select(Task.id).where(Task.doc_id == doc.id))), doc.update_time)


def save_document_update(selection: Selection, principal_id: str, request: DocumentUpdatePatch) -> dict[str, Any]:
    def prepare(db: Session, doc: Document, kb: Knowledgebase) -> DocumentMutationPlan:
        return prepare_document_update(db, doc, kb, request, principal_id, lock=True)

    try:
        result = _operate(selection, principal_id, "save", False, False, prepare)
    except DocumentUpdateError:
        raise
    except Exception:
        raise DocumentUpdateError(
            "Document update outcome could not be confirmed; read back before retrying.", status=500, numeric_code=500, code="DOCUMENT_UPDATE_OUTCOME_UNKNOWN", outcome="unknown"
        ) from None
    error = result.get("_update_error")
    if isinstance(error, DocumentUpdateError):
        raise error
    document = result.get("document")
    if not isinstance(document, dict):
        raise DocumentUpdateError("Document update could not be confirmed; read back before retrying.", status=500, numeric_code=500, code="DOCUMENT_UPDATE_OUTCOME_UNKNOWN", outcome="unknown")
    return copy.deepcopy(document)


async def update_document_parser(db: AsyncSession, dataset_id: str, document_id: str, principal_id: str, request: DocumentUpdatePatch) -> dict[str, Any]:
    try:
        selection = await db.run_sync(lambda session: preflight_document_update(session, dataset_id, document_id, principal_id, request))  # TODO(async-phase4): SQL-only preflight.
    except DocumentUpdateError:
        raise
    except Exception:
        raise DocumentUpdateError("Document update preflight failed.", status=500, numeric_code=500, code="DOCUMENT_UPDATE_FAILED") from None
    await db.rollback()
    return await finish_status_write(asyncio.to_thread(save_document_update, selection, principal_id, request))
