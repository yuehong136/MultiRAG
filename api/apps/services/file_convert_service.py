import logging
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import Session

from api.db.db_models import db_connection
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.misc_utils import get_uuid


def convert_files(db: Session, file_ids: list[str], kb_ids: list[str], user_id: str, mode: Literal["replace", "add"] = "replace") -> None:
    """Keep selected documents, add missing pairs and optionally remove deselected pairs."""
    targets = list(dict.fromkeys(kb_ids))
    for file_id in dict.fromkeys(file_ids):
        file = FileService.get_by_id(db, file_id)
        if not file:
            raise LookupError("File not found during conversion.")
        existing_kb_ids: set[str] = set()
        informs = File2DocumentService.get_by_file_id(db, file_id)
        for inform in informs:
            doc_id = inform.document_id
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                logging.warning("Document not found for doc_id=%s, skipping remove_document", doc_id)
                continue
            existing_kb_ids.add(doc.kb_id)
            if mode == "replace" and doc.kb_id not in targets:
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if not tenant_id:
                    raise LookupError("Dataset tenant not found during conversion.")
                if not DocumentService.remove_document(db, doc, tenant_id, strict=True):
                    raise RuntimeError("Document removal failed during conversion.")
                File2DocumentService.delete_by_document_id(db, doc_id)

        def existing_name(**filters: Any) -> Any:
            return DocumentService.query(db, **filters)

        for kb_id in targets:
            if kb_id in existing_kb_ids:
                continue
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            if not kb:
                raise LookupError("Dataset not found during conversion.")
            filename = duplicate_name(existing_name, name=file.name, kb_id=kb_id)

            doc = DocumentService.insert(
                db,
                {
                    "id": get_uuid(),
                    "kb_id": kb.id,
                    "parser_id": FileService.get_parser(file.type, filename, kb.parser_id),
                    "pipeline_id": kb.pipeline_id,
                    "parser_config": kb.parser_config,
                    "created_by": user_id,
                    "type": file.type,
                    "name": filename,
                    "suffix": Path(filename).suffix.lstrip("."),
                    "location": file.location,
                    "size": file.size,
                },
            )
            File2DocumentService.insert(
                db,
                {
                    "id": get_uuid(),
                    "file_id": file_id,
                    "document_id": doc.id,
                },
            )


def convert_files_with_new_session(file_ids: list[str], kb_ids: list[str], user_id: str, mode: Literal["replace", "add"] = "replace") -> None:
    try:
        with db_connection() as db:
            convert_files(db, file_ids, kb_ids, user_id, mode)
    except Exception:
        logging.exception("convert_files failed")
