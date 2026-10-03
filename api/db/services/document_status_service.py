"""Document availability writes, with per-document SQL serialization."""

import asyncio
import copy
import logging
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, UserTenant
from api.db.services.document_image_lock import image_reference_key, image_write_locks, release_task_images, reserve_task_image
from api.db.services.document_source_recovery import SourceRecovery, prepare_source_recovery, preserve_source_recovery, require_source_recovery_resolved
from api.db.services.document_task_service import account_task_tokens, current_document_task, reconcile_task_chunk_count
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
def source_document_availability(bind: Engine | Connection, chunks: list[dict[str, Any]], dataset_id: str, task_id: str | None = None) -> Iterator[Session | None]:
    """Lock fresh source rows only across one store insertion, not callbacks."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        if chunk.get("raptor_kwd") or chunk.get("knowledge_graph_kwd") or chunk.get("compile_kwd") or chunk.get("doc_id") == "graph_raptor_x":
            if task_id is None or chunk.get("doc_id") == "graph_raptor_x":
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
        if task_id is not None:
            from api.db.db_models import Task

            if reader.scalar(select(Task.doc_id).where(Task.id == task_id)) == "graph_raptor_x" and all(
                chunk.get("raptor_kwd") or chunk.get("knowledge_graph_kwd") or chunk.get("compile_kwd") for chunk in chunks
            ):
                yield None
                reader.commit()
                return
            if len(docs) != 1:
                raise ValueError("A source task must belong to one document.")
            with current_document_task(reader, task_id, docs[0].id, dataset_id):
                require_source_recovery_resolved(docs[0].id, task_id, bind)
        for doc in docs:
            for chunk in groups[doc.id]:
                if chunk.get("raptor_kwd") or chunk.get("knowledge_graph_kwd") or chunk.get("compile_kwd"):
                    continue
                parent = bool(chunk.get("mom_id")) and chunk.get("mom_id") == chunk.get("id", chunk.get("pk"))
                chunk["available_int"] = 0 if parent or chunk.get("toc_kwd") == "toc" or doc.status == "0" else 1
        yield reader
        reader.commit()


def insert_source_chunks(bind: Engine | Connection, chunks: list[dict[str, Any]], index_name: str | list[str], dataset_id: str, task_id: str | None = None) -> list[str]:
    """Keep insertion, task registration and ledger inside its ownership lock."""
    from common.doc_store.document_history import document_history

    with source_document_availability(bind, chunks, dataset_id, task_id) as db:
        index = index_name[0] if isinstance(index_name, list) else index_name
        document_id = chunks[0]["doc_id"]
        snapshot = document_history(settings.docStoreConn, index, dataset_id, document_id) if task_id is not None and db is not None else None
        keys = {key for row in [*chunks, *(snapshot or [])] if (key := image_reference_key(row.get("img_id"))) is not None}
        recovery: dict[str, Any] = {"original_native": snapshot}
        try:
            with image_write_locks(bind, keys):
                result = _insert_locked_source_chunks(db, chunks, index_name, dataset_id, task_id, index, document_id, recovery)
                if task_id is not None and db is not None:
                    release_task_images(task_id, {key for chunk in chunks if (key := image_reference_key(chunk.get("img_id"))) is not None})
                    recovery["material"].ready({str(chunk.get("id", chunk.get("pk"))) for chunk in chunks})
                return result
        except Exception:
            # The dedicated image connection has released its locks before
            # rollback can release Doc/Task. Recovery always reenters in the
            # same Doc -> Task -> image order as a current producer.
            if snapshot is not None and db is not None and task_id is not None and recovery.get("native_started"):
                _recover_source_chunks(db, task_id, document_id, dataset_id, index, chunks, snapshot, keys, recovery)
            raise


def _insert_locked_source_chunks(
    db: Session | None,
    chunks: list[dict[str, Any]],
    index_name: str | list[str],
    dataset_id: str,
    task_id: str | None,
    index: str,
    document_id: str,
    recovery: dict[str, Any],
) -> list[str]:
    from common.doc_store.document_history import confirm_history_visibility, document_history

    original_task = db.get(Task, task_id) if db is not None and task_id is not None else None
    original_doc = db.get(Document, document_id) if original_task is not None else None
    original_task_row = _source_row(original_task) if original_task is not None else None
    original_doc_row = _source_row(original_doc) if original_doc is not None else None
    recovery.update(original_task=original_task_row, original_doc=original_doc_row, applied_task=None, applied_doc=None, applied_native=None)
    if task_id is not None and db is not None:
        recovery["material"] = prepare_source_recovery(db.get_bind(), document_id, task_id, dataset_id=dataset_id, index=index, chunks=chunks, **recovery)
        for chunk in chunks:
            if (key := image_reference_key(chunk.get("img_id"))) is not None:
                reserve_task_image(task_id, key)
    try:
        recovery["native_started"] = True
        errors = settings.docStoreConn.insert(chunks, index_name, dataset_id)
        if not isinstance(errors, list) or errors:
            raise RuntimeError("Source chunk insertion failed.")
        confirm_history_visibility(settings.docStoreConn, index)
    finally:
        # Capture the actual partial or complete write while Doc/Task/image
        # locks still exclude native writers, before SQL can release them.
        if original_task is not None:
            recovery["applied_native"] = document_history(settings.docStoreConn, index, dataset_id, document_id)
            recovery["material"].update(phase="applied", applied_native=recovery["applied_native"])
    if task_id is not None and db is not None:
        task = db.get(Task, task_id)
        if task is None:
            raise RuntimeError("Document task registration unavailable.")
        identifiers = [str(chunk.get("id", chunk.get("pk"))) for chunk in chunks if chunk.get("mom_id") != chunk.get("id", chunk.get("pk"))]
        task.chunk_ids = " ".join(dict.fromkeys([*(task.chunk_ids or "").split(), *identifiers]))
        reconcile_task_chunk_count(db, document_id, dataset_id, index)
        if any("ingest_tokens_int" in chunk for chunk in chunks):
            account_task_tokens(db, task, document_id, dataset_id, sum(chunk.get("ingest_tokens_int", 0) for chunk in chunks))
    if db is not None:
        db.flush()
        if original_task is not None and original_doc is not None:
            recovery.update(
                applied_task=copy.deepcopy(dict(db.execute(select(Task.__table__).where(Task.id == task_id)).mappings().one())),
                applied_doc=copy.deepcopy(dict(db.execute(select(Document.__table__).where(Document.id == document_id)).mappings().one())),
            )
            recovery["material"].update(applied_task=recovery["applied_task"], applied_doc=recovery["applied_doc"])
        db.commit()
    return errors


def _recover_source_chunks(
    db: Session,
    task_id: str,
    document_id: str,
    dataset_id: str,
    index: str,
    chunks: list[dict[str, Any]],
    snapshot: list[dict[str, Any]],
    keys: set[tuple[str, str]],
    recovery: dict[str, Any],
) -> None:
    from common.doc_store.document_history import document_history, restore_document_history

    original_task_row, original_doc_row = recovery["original_task"], recovery["original_doc"]
    applied_task_row, applied_doc_row = recovery["applied_task"], recovery["applied_doc"]
    db.rollback()
    with current_document_task(db, task_id, document_id, dataset_id, cleanup=True) as current:
        with image_write_locks(db.get_bind(), keys):
            doc = db.get(Document, document_id)
            current_task_row = _source_row(current)
            current_doc_row = _source_row(doc) if doc is not None else None
            unchanged = current_task_row == original_task_row and current_doc_row == original_doc_row
            committed = current_task_row == applied_task_row and current_doc_row == applied_doc_row
            if not unchanged and not committed:
                logger.error(
                    "Source recovery ownership changed: task_fields=%s document_fields=%s",
                    [key for key in current_task_row if current_task_row[key] != (applied_task_row or {}).get(key)],
                    [key for key in current_doc_row or {} if current_doc_row[key] != (applied_doc_row or {}).get(key)],
                )
                raise RuntimeError("Source chunk recovery no longer owns the current rows.")
            # A lost reservation-release reply may have removed it even
            # though indexing now needs compensation. Protect any retry's
            # bytes again before removing this attempt's native rows.
            for chunk in chunks:
                if (key := image_reference_key(chunk.get("img_id"))) is not None:
                    reserve_task_image(task_id, key)
            identifiers = {str(chunk.get("id", chunk.get("pk"))) for chunk in chunks}
            material = {
                "dataset_id": dataset_id,
                "index": index,
                "original_native": snapshot,
                "chunks": chunks,
                "current_task": current_task_row,
                "current_doc": current_doc_row,
            }
            try:
                current_native = document_history(settings.docStoreConn, index, dataset_id, document_id)
            except Exception as error:
                preserve_source_recovery(recovery["material"], current_native=None, read_error=type(error).__name__, **material)
            applied_native = recovery["applied_native"]
            if applied_native is None or _source_native_rows(current_native, identifiers) != _source_native_rows(applied_native, identifiers):
                preserve_source_recovery(recovery["material"], current_native=current_native, **material)
            restore_document_history(settings.docStoreConn, index, dataset_id, document_id, snapshot, ids=list(identifiers))
            if committed and not unchanged and original_doc_row is not None and original_task_row is not None and current_doc_row is not None:
                chunk_delta = original_doc_row["chunk_num"] - current_doc_row["chunk_num"]
                token_delta = original_doc_row["token_num"] - current_doc_row["token_num"]
                restored_doc = db.execute(
                    Document.__table__.update().where(Document.id == document_id).values(**{key: original_doc_row[key] for key in ["chunk_num", "token_num", "update_time", "update_date"]})
                )
                restored_task = db.execute(Task.__table__.update().where(Task.id == task_id).values(**{key: value for key, value in original_task_row.items() if key != "id"}))
                restored_kb = db.execute(
                    update(Knowledgebase).where(Knowledgebase.id == dataset_id).values(chunk_num=Knowledgebase.chunk_num + chunk_delta, token_num=Knowledgebase.token_num + token_delta)
                )
                if any(result.rowcount != 1 for result in [restored_doc, restored_task, restored_kb]):
                    raise RuntimeError("Source chunk SQL recovery could not be confirmed.")
                db.commit()
    db.rollback()
    durable: SourceRecovery = recovery["material"]
    durable.restored()


def _source_row(row: Document | Task) -> dict[str, Any]:
    result = {column.name: copy.deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
    for key, value in result.items():
        if isinstance(value, datetime) and value.tzinfo is not None:
            result[key] = value.astimezone(UTC).replace(tzinfo=None)
    return result


def _source_native_rows(rows: list[dict[str, Any]], identifiers: set[str]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id", row.get("pk"))): row for row in rows if str(row.get("id", row.get("pk"))) in identifiers}
