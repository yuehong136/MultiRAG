"""Document availability writes, with per-document SQL serialization."""

import asyncio
import logging
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, UserTenant
from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from core.nlp import search

logger = logging.getLogger(__name__)
STATUS_ERROR = "Failed to update document status; retry to reconcile."
COMPENSATION_ERROR = "Document status recovery could not be confirmed; retry to reconcile."


@dataclass(frozen=True)
class StatusWrite:
    document_id: str
    dataset_id: str
    index_name: str
    old_status: str
    chunk_num: int

    def needs_index(self) -> bool:
        if self.chunk_num > 0:
            return True
        store = settings.docStoreConn
        if not store.index_exist(self.index_name, self.dataset_id):
            return False
        counter = getattr(store, "document_chunk_count", None)
        if counter is not None:
            return counter(self.document_id, self.index_name, self.dataset_id) > 0
        # The insert/count boundary can leave real rows while SQL chunk_num is
        # still zero. Never infer absence solely from the SQL counter.
        result = store.search(["id"], [], {"doc_id": self.document_id}, [], OrderByExpr(), 0, 1, [self.index_name], [self.dataset_id])
        return store.get_total(result) > 0

    def index(self, status: str) -> None:
        if not settings.docStoreConn.update({"doc_id": self.document_id}, {"available_int": int(status)}, self.index_name, self.dataset_id):
            raise RuntimeError("Document store table missing or update failed.")


def status_write(doc: Document, kb: Knowledgebase) -> StatusWrite:
    return StatusWrite(doc.id, kb.id, search.index_name_one(kb.tenant_id, kb.name), "0" if doc.status == "0" else "1", doc.chunk_num or 0)


async def writable_dataset(db: AsyncSession, dataset_id: str, principal_id: str) -> Knowledgebase | None:
    kb = await db.scalar(select(Knowledgebase).where(Knowledgebase.id == dataset_id, Knowledgebase.status == "1"))
    if kb is None:
        return None
    if kb.tenant_id == principal_id:
        return kb
    role = await db.scalar(select(UserTenant.role).where(UserTenant.tenant_id == kb.tenant_id, UserTenant.user_id == principal_id, UserTenant.status == "1"))
    return kb if role in {"owner", "admin"} else None


async def finish_status_write(work: Awaitable[Any]) -> Any:
    """Drain owned work before cancellation releases its SQL locks/session."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def change_document_status(db: AsyncSession, document_id: str, status: str, principal_id: str, dataset_id: str | None = None) -> str | None:
    return await finish_status_write(asyncio.create_task(_change_document_status(db, document_id, status, principal_id, dataset_id)))


async def _change_document_status(db: AsyncSession, document_id: str, status: str, principal_id: str, dataset_id: str | None) -> str | None:
    operation: StatusWrite | None = None
    index_attempted = False
    try:
        doc = await db.scalar(select(Document).where(Document.id == document_id).with_for_update().execution_options(populate_existing=True))
        if doc is None or (dataset_id is not None and doc.kb_id != dataset_id):
            await db.rollback()
            return "Document not found in this dataset."
        kb = await writable_dataset(db, doc.kb_id, principal_id)
        if kb is None:
            await db.rollback()
            return "No authorization."
        operation = status_write(doc, kb)
        index_attempted = await asyncio.to_thread(operation.needs_index)
        if index_attempted:
            await asyncio.to_thread(operation.index, status)
        # No early return for the same status: real indexed rows still need
        # repair after an earlier failed/uncertain store operation.
        result = await db.execute(update(Document).where(Document.id == document_id, Document.kb_id == kb.id).values(status=status))
        if result.rowcount != 1:
            raise RuntimeError("Document disappeared during status update.")
        await db.commit()
        return None
    except Exception:
        logger.exception("Document status update failed: document_id=%s", document_id)
        try:
            await db.rollback()
            if operation is not None and index_attempted:
                # Rollback/commit failures release locks. Reacquire and restore
                # the current SQL winner, never overwrite a later status write.
                current = await db.scalar(select(Document).where(Document.id == document_id).with_for_update().execution_options(populate_existing=True))
                if current is None:
                    raise RuntimeError("Document unavailable during recovery.")
                await asyncio.to_thread(operation.index, "0" if current.status == "0" else "1")
            await db.rollback()
        except Exception:
            logger.exception("Document status recovery failed: document_id=%s", document_id)
            try:
                await db.rollback()
            except Exception:
                logger.exception("Document status recovery transaction could not be released: document_id=%s", document_id)
            return COMPENSATION_ERROR
        return STATUS_ERROR


def change_document_status_sync(db: Session, doc: Document, kb: Knowledgebase, status: str) -> str | None:
    """Keep the existing synchronous PATCH transaction chain local."""
    operation: StatusWrite | None = None
    index_attempted = False
    try:
        current = db.scalar(select(Document).where(Document.id == doc.id, Document.kb_id == kb.id).with_for_update().execution_options(populate_existing=True))
        if current is None:
            db.rollback()
            return "Document not found in this dataset."
        operation = status_write(current, kb)
        index_attempted = operation.needs_index()
        if index_attempted:
            operation.index(status)
        result = db.execute(update(Document).where(Document.id == operation.document_id, Document.kb_id == kb.id).values(status=status))
        if result.rowcount != 1:
            raise RuntimeError("Document disappeared during status update.")
        db.commit()
        return None
    except Exception:
        logger.exception("Document status update failed")
        try:
            db.rollback()
            if operation is not None and index_attempted:
                winner = db.scalar(select(Document).where(Document.id == operation.document_id).with_for_update().execution_options(populate_existing=True))
                if winner is None:
                    raise RuntimeError("Document unavailable during recovery.")
                operation.index("0" if winner.status == "0" else "1")
            db.rollback()
        except Exception:
            logger.exception("Document status recovery failed")
            try:
                db.rollback()
            except Exception:
                logger.exception("Document status recovery transaction could not be released")
            return COMPENSATION_ERROR
        return STATUS_ERROR


async def batch_document_status(db: AsyncSession, document_ids: list[str], status: str, principal_id: str, dataset_id: str | None = None) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for document_id in dict.fromkeys(document_ids):
        error = await change_document_status(db, document_id, status, principal_id, dataset_id)
        result[document_id] = {"error": error} if error else {"status": status}
    return result


@contextmanager
def source_document_availability(bind: Engine | Connection, chunks: list[dict[str, Any]], dataset_id: str) -> Iterator[None]:
    """Lock fresh source rows only across one store insertion, not callbacks."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        if chunk.get("raptor_kwd") or chunk.get("knowledge_graph_kwd") or chunk.get("compile_kwd") or chunk.get("doc_id") == "graph_raptor_x":
            continue
        identifier = chunk.get("doc_id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Source document ID unavailable.")
        groups.setdefault(identifier, []).append(chunk)
    if not groups:
        yield
        return
    # An independent read transaction keeps callbacks on the caller's Session
    # from releasing these locks. Never pass either Session to a worker thread.
    with Session(bind) as reader:
        docs = list(reader.scalars(select(Document).where(Document.id.in_(groups), Document.kb_id == dataset_id).order_by(Document.id).with_for_update()))
        if {doc.id for doc in docs} != groups.keys():
            raise ValueError("Source document status unavailable.")
        for doc in docs:
            for chunk in groups[doc.id]:
                parent = bool(chunk.get("mom_id")) and chunk.get("mom_id") == chunk.get("id", chunk.get("pk"))
                chunk["available_int"] = 0 if parent or doc.status == "0" else 1
        yield
        reader.commit()


def insert_source_chunks(bind: Engine | Connection, chunks: list[dict[str, Any]], index_name: str | list[str], dataset_id: str) -> list[str]:
    """Read/lock status and perform the store write in the same worker thread."""
    with source_document_availability(bind, chunks, dataset_id):
        errors = settings.docStoreConn.insert(chunks, index_name, dataset_id)
        if errors:
            raise RuntimeError("Source chunk insertion failed.")
        return errors
