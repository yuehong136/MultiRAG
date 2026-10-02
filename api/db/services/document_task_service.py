"""Fence document producer side effects against replacement of their SQL task."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, db_connection
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_image_lock import image_write_locks, reserve_task_image
from common import settings
from common.constants import PipelineTaskType
from common.doc_store.availability import availability_parent_ids
from common.doc_store.document_history import delete_document_history, document_history
from common.metadata_utils import update_metadata_to
from core.nlp import search
from core.utils.task_runtime import TASK_CANCEL_MARKER

TASK_TOKEN_SUFFIX = ":ingest-tokens:"


def base_task_digest(digest: str | None) -> str:
    return (digest or "").partition(TASK_TOKEN_SUFFIX)[0]


def accounted_tokens(digest: str | None) -> int | None:
    _, separator, count = (digest or "").partition(TASK_TOKEN_SUFFIX)
    return int(count) if separator and count.isdecimal() else None


def token_digest(digest: str | None, tokens: int) -> str:
    # The Task row is authoritative. Generated progress messages must never
    # supply or erase token ledger state. Reuse compares only the base hash.
    return f"{base_task_digest(digest)}{TASK_TOKEN_SUFFIX}{tokens}"


class SupersededDocumentTask(NoResultFound):
    """The producer no longer owns this document's current work."""


@contextmanager
def current_document_task(db: Session, task_id: str, document_id: str, dataset_id: str, *, cleanup: bool = False) -> Iterator[Task]:
    """Hold Doc -> Task locks through the protected SQL/store/object operation."""
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.kb_id == dataset_id).with_for_update().execution_options(populate_existing=True))
    task = db.scalar(select(Task).where(Task.id == task_id, Task.doc_id == document_id).with_for_update().execution_options(populate_existing=True))
    if doc is None or task is None:
        raise SupersededDocumentTask("Document task has been replaced.")
    if not cleanup and (doc.run in {"0", "2"} or (task.progress or 0) < 0 or (task.progress or 0) >= 1 or TASK_CANCEL_MARKER in (task.progress_msg or "")):
        raise SupersededDocumentTask("Document task has been canceled.")
    yield task


def reconcile_task_chunk_count(db: Session, document_id: str, dataset_id: str, index_name: str) -> None:
    """Record actual unique source rows, including partial insert/overwrite paths."""
    doc = db.get(Document, document_id)
    if doc is None:
        raise SupersededDocumentTask("Document task resource disappeared.")
    rows = document_history(settings.docStoreConn, index_name, dataset_id, document_id)
    parents = set(availability_parent_ids(rows).get(document_id, []))
    count = sum(row.get("id", row.get("pk")) not in parents for row in rows)
    delta = count - doc.chunk_num
    changed = db.execute(
        update(Document)
        .where(Document.id == document_id, Document.kb_id == dataset_id)
        .values(chunk_num=count, update_time=max(int(datetime.now().timestamp() * 1000), (doc.update_time or 0) + 1), update_date=datetime.now())
    )
    kb_changed = db.execute(update(Knowledgebase).where(Knowledgebase.id == dataset_id).values(chunk_num=Knowledgebase.chunk_num + delta))
    if changed.rowcount != 1 or kb_changed.rowcount != 1:
        raise SupersededDocumentTask("Document task resource disappeared.")


def increment_task_document(bind: Engine | Connection, task_id: str, document_id: str, dataset_id: str, tokens: int, chunks: int, duration: float | int) -> None:
    """Count only a task still present after indexing, including pipeline completion."""
    with Session(bind) as db, current_document_task(db, task_id, document_id, dataset_id) as task:
        kb = db.get(Knowledgebase, dataset_id)
        if kb is None:
            raise SupersededDocumentTask("Document task resource disappeared.")
        reconcile_task_chunk_count(db, document_id, dataset_id, search.index_name_one(kb.tenant_id, kb.name))
        account_task_tokens(db, task, document_id, dataset_id, tokens)
        result = db.execute(update(Document).where(Document.id == document_id, Document.kb_id == dataset_id).values(process_duration=Document.process_duration + duration))
        if result.rowcount != 1:
            raise SupersededDocumentTask("Document task resource disappeared.")
        db.commit()


def account_task_tokens(db: Session, task: Task, document_id: str, dataset_id: str, tokens: int) -> None:
    previous = accounted_tokens(task.digest) or 0
    accounted = max(previous, tokens)
    delta = accounted - previous
    task.digest = token_digest(task.digest, accounted)
    document = db.execute(update(Document).where(Document.id == document_id, Document.kb_id == dataset_id).values(token_num=Document.token_num + delta))
    dataset = db.execute(update(Knowledgebase).where(Knowledgebase.id == dataset_id).values(token_num=Knowledgebase.token_num + delta))
    if document.rowcount != 1 or dataset.rowcount != 1:
        raise SupersededDocumentTask("Document task resource disappeared.")


def put_task_image(task_id: str, document_id: str, dataset_id: str, tenant_id: str, *, bucket: str, fnm: str, binary: bytes) -> Any:
    """Keep stale parsers from overwriting objects belonging to a replacement."""
    with db_connection() as db, current_document_task(db, task_id, document_id, dataset_id):
        with image_write_locks(db.get_bind(), [(bucket, fnm)]):
            kb = db.get(Knowledgebase, dataset_id)
            if kb is None:
                raise SupersededDocumentTask("Document task resource disappeared.")
            # Bytes precede indexing in the real parser lifecycle. Protect
            # that gap until this task's native reference is confirmed.
            if not any(row.get("img_id") == f"{bucket}-{fnm}" for row in document_history(settings.docStoreConn, search.index_name_one(kb.tenant_id, kb.name), dataset_id, document_id)):
                reserve_task_image(task_id, (bucket, fnm))
            return settings.STORAGE_IMPL.put(bucket=bucket, fnm=fnm, binary=binary, tenant_id=tenant_id)


def task_image_writer(canvas: Any) -> partial:
    document_id = getattr(canvas, "_source_document_id", getattr(canvas, "_doc_id", None))
    if document_id:
        return partial(put_task_image, canvas.task_id, document_id, canvas._kb_id or "", canvas._tenant_id)
    return partial(put_document_image, tenant_id=canvas._tenant_id)


def put_document_image(bucket: str, fnm: str, binary: bytes, tenant_id: str | None = None) -> Any:
    """The non-document pipeline fallback shares the exact image-key guard."""
    with db_connection() as db, image_write_locks(db.get_bind(), [(bucket, fnm)]):
        return settings.STORAGE_IMPL.put(bucket=bucket, fnm=fnm, binary=binary, tenant_id=tenant_id)


def cleanup_task_chunks(bind: Engine | Connection, task_id: str, document_id: str, dataset_id: str, index_name: str, chunk_ids: list[str] | None = None) -> None:
    """An absent old task cannot delete new chunks, even with identical chunk IDs."""
    with Session(bind) as db:
        try:
            with current_document_task(db, task_id, document_id, dataset_id) as task:
                ids = list(dict.fromkeys(chunk_ids if chunk_ids is not None else (task.chunk_ids or "").split()))
                if ids:
                    from api.db.services.document_update_effects import _protected_images

                    with image_write_locks(db.get_bind(), [(dataset_id, identifier) for identifier in ids]):
                        delete_document_history(settings.docStoreConn, index_name, dataset_id, document_id, ids=ids)
                        protected = _protected_images(db, db.get(Document, document_id), {f"{dataset_id}-{identifier}" for identifier in ids})
                        for identifier in ids:
                            if f"{dataset_id}-{identifier}" not in protected:
                                settings.STORAGE_IMPL.delete(dataset_id, identifier)
                db.commit()
        except SupersededDocumentTask:
            # Replacement is authoritative: there is nothing this task may clean.
            db.rollback()


def write_task_metadata(bind: Engine | Connection, task_id: str, document_id: str, dataset_id: str, metadata: dict[str, Any]) -> None:
    """Merge generated metadata while the producer still owns the document."""
    with Session(bind) as db, current_document_task(db, task_id, document_id, dataset_id):
        existing = DocMetadataService.get_document_metadata(db, document_id)
        merged = update_metadata_to(metadata, existing if isinstance(existing, dict) else {})
        if not DocMetadataService.update_document_metadata(db, document_id, merged):
            raise RuntimeError(f"Failed to persist generated metadata for document {document_id}")


def record_task_pipeline(bind: Engine | Connection, task_id: str, document_id: str, dataset_id: str, pipeline_id: str, dsl: str, *, task_type: PipelineTaskType = PipelineTaskType.PARSE) -> None:
    """Legacy log helpers may commit, but cannot release the ownership lock."""
    from api.db.services.pipeline_operation_log_service import PipelineOperationLogService

    with Session(bind) as owner:
        try:
            with current_document_task(owner, task_id, document_id, dataset_id, cleanup=True):
                doc = owner.get(Document, document_id)
                if doc is None or doc.run in {"0", "2"}:
                    return
                # Commits inside the existing helper release only its savepoint.
                # The outer transaction holds Doc -> Task through final commit.
                with Session(bind=owner.connection(), join_transaction_mode="create_savepoint") as writer:
                    if PipelineOperationLogService.create(writer, document_id=document_id, pipeline_id=pipeline_id, task_type=task_type, task_id=task_id, dsl=dsl) is None:
                        raise RuntimeError("Pipeline operation log could not be confirmed.")
                owner.commit()
        except SupersededDocumentTask:
            owner.rollback()
