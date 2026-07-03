import logging
from pathlib import Path

from sqlalchemy.orm import Session

from api.db.db_models import db_connection
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.misc_utils import get_uuid


def convert_files(db: Session, file_ids: list[str], kb_ids: list[str], user_id: str) -> None:
    """Rebuild file-document links for already validated file/kb IDs."""
    for file_id in file_ids:
        informs = File2DocumentService.get_by_file_id(db, file_id)
        for inform in informs:
            doc_id = inform.document_id
            doc = DocumentService.get_by_id(db, doc_id)
            if not doc:
                logging.warning("Document not found for doc_id=%s, skipping remove_document", doc_id)
                continue
            tenant_id = DocumentService.get_tenant_id(db, doc_id)
            if not tenant_id:
                logging.warning("tenant_id not found for doc_id=%s, skipping remove_document", doc_id)
                continue
            if not DocumentService.remove_document(db, doc, tenant_id):
                logging.warning("remove_document returned false for doc_id=%s", doc_id)
        File2DocumentService.delete_by_file_id(db, file_id)

        file = FileService.get_by_id(db, file_id)
        if not file:
            logging.warning("File not found for file_id=%s, skipping insert", file_id)
            continue

        for kb_id in kb_ids:
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            if not kb:
                logging.warning("Knowledgebase not found for kb_id=%s, skipping insert", kb_id)
                continue

            doc = DocumentService.insert(
                db,
                {
                    "id": get_uuid(),
                    "kb_id": kb.id,
                    "parser_id": FileService.get_parser(file.type, file.name, kb.parser_id),
                    "pipeline_id": kb.pipeline_id,
                    "parser_config": kb.parser_config,
                    "created_by": user_id,
                    "type": file.type,
                    "name": file.name,
                    "suffix": Path(file.name).suffix.lstrip("."),
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


def convert_files_with_new_session(file_ids: list[str], kb_ids: list[str], user_id: str) -> None:
    try:
        with db_connection() as db:
            convert_files(db, file_ids, kb_ids, user_id)
    except Exception:
        logging.exception("convert_files failed")
