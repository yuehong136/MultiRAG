"""Authorized document ingestion with explicit history, queue and cancellation results."""

import asyncio
import copy
import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, UserCanvas, UserTenant, db_connection
from api.db.services.document_image_lock import image_reference_key, image_write_locks, retire_task_image_reservations
from api.db.services.document_ingest_recovery import RecoveryOwner, StoredRecovery, recovery_owner_active
from api.db.services.document_status_service import finish_status_write
from api.db.services.document_task_service import accounted_tokens, base_task_digest
from api.db.services.document_update_effects import apply_update_sql, apply_update_store, restore_update_effects, sql_row_version
from api.db.services.file2document_service import File2DocumentService
from api.db.services.task_service import _task_queue_payload, prepare_parse_tasks, reuse_prev_task_chunks
from api.utils.document_update_contract import DocumentUpdateError
from common import settings
from common.constants import MAXIMUM_TASK_PAGE_NUMBER, RetCode
from common.doc_store.availability import availability_parent_ids
from common.doc_store.document_history import delete_document_history, document_history, restore_document_history
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN
from core.utils.task_runtime import TASK_CANCEL_MARKER, TASK_RUNTIME_TTL

logger = logging.getLogger(__name__)


class IngestError(ValueError):
    def __init__(self, message: str, code: RetCode = RetCode.DATA_ERROR, result: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


class IngestConflict(IngestError):
    """A resource/revision fence lost its write authority."""


@dataclass(frozen=True)
class Selection:
    document_id: str
    dataset_id: str
    task_ids: frozenset[str]
    revision: int | None


@dataclass(frozen=True)
class DocumentMutationPlan:
    values: dict[str, Any]
    reset: bool
    effects: dict[str, Any]


MutationPrepare = Callable[[Session, Document, Knowledgebase], DocumentMutationPlan]


def _restored_status(doc: Document, data: dict[str, Any], version: str | None = None) -> str:
    original, applied = data["original_doc"], data.get("applied_doc")
    effects = data.get("update_effects")
    return original["status"] if effects and effects["change_status"] and applied and doc.status == applied["status"] and version == data.get("applied_doc_version") else doc.status


def writable_kb(db: Session, dataset_id: str, principal_id: str, *, lock: bool = False, lock_dataset: bool = False) -> Knowledgebase | None:
    query = select(Knowledgebase).where(Knowledgebase.id == dataset_id, Knowledgebase.status == "1")
    if lock_dataset:
        query = query.with_for_update(read=True)
    kb = db.scalar(query.execution_options(populate_existing=True))
    if kb is None or kb.tenant_id == principal_id:
        return kb
    statement = select(UserTenant).where(UserTenant.tenant_id == kb.tenant_id, UserTenant.user_id == principal_id, UserTenant.status == "1")
    if lock:
        statement = statement.with_for_update(read=True)
    member = db.scalar(statement.execution_options(populate_existing=True))
    return kb if member is not None and member.role in {"owner", "admin"} else None


def preflight(db: Session, document_ids: list[str], principal_id: str, dataset_id: str | None = None) -> list[Selection]:
    """Reject the complete unauthorized selection before its first write."""
    selected = []
    for identifier in dict.fromkeys(document_ids):
        doc = db.scalar(select(Document).where(Document.id == identifier))
        if doc is None or (dataset_id is not None and doc.kb_id != dataset_id) or writable_kb(db, doc.kb_id, principal_id) is None:
            raise IngestError("Document selection unavailable or not writable.", RetCode.AUTHENTICATION_ERROR)
        ids = frozenset(db.scalars(select(Task.id).where(Task.doc_id == doc.id)))
        selected.append(Selection(doc.id, doc.kb_id, ids, doc.update_time))
    return selected


def _rows(model: type[Any], rows: list[Any]) -> list[dict[str, Any]]:
    result = [{column.name: copy.deepcopy(getattr(row, column.name)) for column in model.__table__.columns} for row in rows]
    for row in result:
        for key, value in row.items():
            if isinstance(value, datetime) and value.tzinfo is not None:
                row[key] = value.astimezone(UTC).replace(tzinfo=None)
    return result


def _same_document(actual: dict[str, Any], expected: dict[str, Any] | None) -> bool:
    # Retrieval availability is an independent write. It may change without
    # touching update_time, and ingestion must preserve its latest winner.
    return expected is not None and all(actual.get(key) == value for key, value in expected.items() if key != "status")


def _history_for_status(rows: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    restored = copy.deepcopy(rows)
    parents = {identifier for ids in availability_parent_ids(restored).values() for identifier in ids}
    for row in restored:
        if not any(row.get(key) for key in ["raptor_kwd", "knowledge_graph_kwd", "compile_kwd"]):
            row["available_int"] = 0 if status == "0" or row.get("id", row.get("pk")) in parents or row.get("toc_kwd") == "toc" else 1
    return restored


def _recovery_submitted(data: dict[str, Any]) -> bool:
    tasks = data.get("applied_tasks", [])
    if tasks and all((task["progress"] or 0) >= 1 for task in tasks):
        return True
    identifiers = {task["id"] for task in tasks}
    return any(identifier in _queue_task_counts(data.get("queue", settings.get_svr_queue_name(0))) for identifier in identifiers)


def _queue_task_counts(queue: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    cursor: str | bytes = "-"
    while entries := REDIS_CONN.REDIS.xrange(queue, min=cursor, count=1000):
        for _, fields in entries:
            identifier = json.loads(fields.get(b"message", fields.get("message", b"{}"))).get("id")
            if isinstance(identifier, str):
                counts[identifier] += 1
        last = entries[-1][0]
        cursor = "(" + (last.decode() if isinstance(last, bytes) else last)
    return counts


def _existing_submission(tasks: list[Task], planned: list[dict[str, Any]]) -> list[str] | None:
    def range_digest(task: dict[str, Any]) -> tuple[Any, ...]:
        return task["from_page"], task["to_page"], task.get("task_type") or "", base_task_digest(task.get("digest"))

    if (
        not tasks
        or len(tasks) != len(planned)
        or any(
            task.from_page is None
            or task.to_page is None
            or (task.task_type or "") not in {"", "dataflow", "dataflow_rerun"}
            or (task.progress or 0) < 0
            or TASK_CANCEL_MARKER in (task.progress_msg or "")
            or REDIS_CONN.REDIS.get(task.id + "-cancel")
            for task in tasks
        )
    ):
        return None
    if sorted(map(range_digest, _rows(Task, tasks))) != sorted(map(range_digest, planned)):
        return None
    pending = [task.id for task in tasks if (task.progress or 0) < 1]
    if any((task.progress or 0) != 0 or (task.progress_msg or "").strip() for task in tasks if task.id in pending):
        return None
    counts = _queue_task_counts(settings.get_svr_queue_name(0))
    return pending if all(counts[identifier] == 1 for identifier in pending) else None


@contextmanager
def _ingest_connection(document_id: str, outcome: dict[str, Any]) -> Iterator[Session]:
    """Keep effects known before cleanup exceptions cross the per-document boundary."""
    try:
        with db_connection() as db:
            yield db
    except Exception as error:
        entry = {**copy.deepcopy(outcome), "error": "Document ingestion finalization failed; retry to reconcile.", "_code": int(RetCode.SERVER_ERROR)}
        entry["effect"] = "confirmed" if outcome and "error" not in outcome else "partial" if outcome.get("queued_task_ids") else "unknown"
        raise IngestError("Document ingestion was not fully confirmed.", RetCode.SERVER_ERROR, {"results": {document_id: entry}}) from error


def _set_document(db: Session, doc: Document, kb: Knowledgebase, values: dict[str, Any]) -> None:
    chunk_delta = values.get("chunk_num", doc.chunk_num) - doc.chunk_num
    token_delta = values.get("token_num", doc.token_num) - doc.token_num
    # One SQL transaction owns both ledgers; never restore a whole shared KB row.
    changed = db.execute(update(Document).where(Document.id == doc.id, Document.kb_id == kb.id).values(**values))
    updated = db.execute(
        update(Knowledgebase).where(Knowledgebase.id == kb.id, Knowledgebase.status == "1").values(chunk_num=Knowledgebase.chunk_num + chunk_delta, token_num=Knowledgebase.token_num + token_delta)
    )
    if changed.rowcount != 1 or updated.rowcount != 1:
        raise IngestConflict("Document resource changed; retry.")
    db.flush()


def _restore_kb_configuration(db: Session, kb: Knowledgebase, original: dict[str, Any] | None, applied: dict[str, Any] | None) -> None:
    if original is None or applied is None:
        return
    db.refresh(kb, with_for_update=True)
    actual = _rows(Knowledgebase, [kb])[0]
    if any(actual[key] != value for key, value in applied.items()):
        raise RuntimeError("Dataset configuration changed before recovery.")
    result = db.execute(Knowledgebase.__table__.update().where(Knowledgebase.id == kb.id).values(**original))
    if result.rowcount != 1:
        raise RuntimeError("Dataset configuration recovery could not be confirmed.")


def _history_image_keys(rows: list[dict[str, Any]], recovery: StoredRecovery | None = None) -> set[tuple[str, str]]:
    keys = {key for row in rows if (key := image_reference_key(row.get("img_id"))) is not None}
    if recovery is not None:
        keys.update(_history_image_keys(recovery.data["snapshot"]))
        effects = recovery.data.get("update_effects")
        if effects:
            keys.update((item["bucket"], item["key"]) for item in effects["objects"])
    return keys


def _recover_history(db: Session, doc: Document, kb: Knowledgebase, tasks: list[Task]) -> tuple[Document, list[Task], bool]:
    recovery = StoredRecovery.load(doc.id)
    if recovery is None:
        return doc, tasks, False
    with image_write_locks(db.get_bind(), _history_image_keys([], recovery)):
        return _recover_locked_history(db, doc, kb, tasks, recovery)


def _recover_locked_history(db: Session, doc: Document, kb: Knowledgebase, tasks: list[Task], recovery: StoredRecovery) -> tuple[Document, list[Task], bool]:
    data = recovery.data
    actual = _rows(Document, [doc])[0]
    unchanged = _same_document(actual, data["original_doc"]) and _rows(Task, tasks) == data["original_tasks"]
    owned = _same_document(actual, data.get("applied_doc")) and _rows(Task, tasks) == data.get("applied_tasks")
    if not unchanged and not owned:
        # A later generation owns its actual SQL/store state. Old material must
        # not roll it back or permanently block its next authorized request.
        recovery.clear()
        return doc, tasks, False
    if recovery_owner_active(db, data.get("owner_lock")):
        return doc, tasks, False
    if owned and not unchanged and _recovery_submitted(data):
        # A lost journal response cannot roll back a real queue submission.
        recovery.clear()
        return doc, tasks, False
    if data["dataset_id"] != kb.id or data["index_name"] != search.index_name_one(kb.tenant_id, kb.name):
        raise IngestError("Document recovery resource changed; retry after reconciliation.")
    status = _restored_status(doc, data, sql_row_version(db, Document, doc.id) if data.get("update_effects") else None)
    restore_document_history(settings.docStoreConn, data["index_name"], kb.id, doc.id, _history_for_status(data["snapshot"], status))
    if data.get("update_effects"):
        restore_update_effects(db, doc, kb, data["update_effects"], owned_sql=owned)
    if owned and not unchanged:
        _set_document(db, doc, kb, {**{key: value for key, value in data["original_doc"].items() if key not in {"id", "status"}}, "status": status})
        _restore_kb_configuration(db, kb, data.get("original_kb_config"), data.get("applied_kb_config"))
        db.execute(delete(Task).where(Task.doc_id == doc.id))
        if data["original_tasks"]:
            db.execute(Task.__table__.insert(), data["original_tasks"])
    if data.get("flags"):
        _restore_flags(data["flags"])
    db.commit()
    identifier = data["document_id"]
    # A new writer can own Document while waiting for our image guard after
    # COMMIT. Do not wait back on that writer; retain recovery material instead.
    guarded_images = bool(_history_image_keys([], recovery))
    current = db.scalar(select(Document).where(Document.id == identifier).with_for_update(nowait=guarded_images).execution_options(populate_existing=True))
    current_tasks = list(db.scalars(select(Task).where(Task.doc_id == identifier).order_by(Task.id).with_for_update(nowait=guarded_images).execution_options(populate_existing=True)))
    if current is None or not _same_document(_rows(Document, [current])[0], data["original_doc"]) or _rows(Task, current_tasks) != data["original_tasks"]:
        raise IngestConflict("Document work changed after recovery; retry.")
    recovery.clear()
    return current, current_tasks, True


def _restore_flags(flags: list[tuple[str, str, bytes | str | None, int]]) -> None:
    script = """
local current = redis.call('GET', KEYS[1])
if current ~= ARGV[1] then
 if (ARGV[2] == 'absent' and not current) or (ARGV[2] == 'present' and current == ARGV[3]) then return 1 end
 return 0
end
if ARGV[2] == 'absent' then redis.call('DEL', KEYS[1])
elseif tonumber(ARGV[4]) > 0 then redis.call('SET', KEYS[1], ARGV[3], 'PX', ARGV[4])
else redis.call('SET', KEYS[1], ARGV[3]) end
return 1
"""
    for key, nonce, previous, ttl in reversed(flags):
        remaining = ttl - int(datetime.now().timestamp() * 1000) if ttl > 0 else ttl
        absent = previous is None or (ttl > 0 and remaining <= 0)
        if REDIS_CONN.REDIS.eval(script, 1, key, nonce, "absent" if absent else "present", previous or "", remaining) != 1:
            raise RuntimeError("Cancellation recovery could not be confirmed.")


def _cancel_flags(tasks: list[Task], flags: list[tuple[str, str, bytes | str | None, int]], on_record: Callable[[], None] | None = None) -> None:
    for task in tasks:
        if not 0 <= (task.progress or 0) < 1:
            continue
        key, nonce = f"{task.id}-cancel", uuid4().hex
        previous, ttl = REDIS_CONN.REDIS.get(key), REDIS_CONN.REDIS.pttl(key)
        expiry = int(datetime.now().timestamp() * 1000) + ttl if ttl > 0 else ttl
        # Record before sending: a failed response may still have reached Redis.
        flags.append((key, nonce, previous, expiry))
        if on_record is not None:
            on_record()
        if not REDIS_CONN.REDIS.set(key, nonce, ex=TASK_RUNTIME_TTL):
            raise ConnectionError("Failed to submit document cancellation.")


def _plan(db: Session, doc: Document, kb: Knowledgebase, old_tasks: list[Task]) -> tuple[list[dict[str, Any]], list[str]]:
    # Analyzer tasks store JSON in chunk_ids, not document chunk identifiers.
    old_tasks = [task for task in old_tasks if (task.task_type or "") in {"", "dataflow", "dataflow_rerun"}]
    if doc.pipeline_id:
        pipeline = db.scalar(select(UserCanvas).where(UserCanvas.id == doc.pipeline_id, UserCanvas.user_id == kb.tenant_id))
        if pipeline is None:
            raise IngestError("Document pipeline is unavailable.")
        digest = hashlib.sha256(json.dumps([pipeline.dsl, doc.parser_config, doc.name], sort_keys=True, default=str).encode()).hexdigest()
        tasks = [{"digest": digest, "id": uuid4().hex, "doc_id": doc.id, "from_page": 0, "to_page": MAXIMUM_TASK_PAGE_NUMBER, "task_type": "dataflow", "priority": 0, "begin_at": datetime.now()}]
        previous = _rows(Task, old_tasks)
        reuse_prev_task_chunks(tasks[0], previous, {})
        return tasks, list(dict.fromkeys(identifier for task in previous for identifier in (task["chunk_ids"] or "").split()))
    bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc.id)
    tasks, config = prepare_parse_tasks(db, doc.to_dict(), bucket, name, 0)
    if not tasks:
        raise IngestError("Document has no parseable task ranges.")
    previous = _rows(Task, old_tasks)
    for task in tasks:
        reuse_prev_task_chunks(task, previous, config)
    obsolete = list(dict.fromkeys(identifier for task in previous for identifier in (task["chunk_ids"] or "").split()))
    return tasks, obsolete


class EnqueueUncertain(ConnectionError):
    """The transport did not establish whether Redis accepted the task."""


def submit_task(queue: str, payload: dict[str, Any]) -> bool:
    """Issue XADD once; never retry an uncertain enqueue and duplicate work."""
    last = REDIS_CONN.REDIS.xrevrange(queue, count=1)
    boundary = last[0][0] if last else b"0-0"
    try:
        identifier = REDIS_CONN.REDIS.xadd(queue, {"message": json.dumps(payload)})
    except Exception:
        identifier = None
    try:
        entries = REDIS_CONN.REDIS.xrange(queue, min=f"({boundary.decode() if isinstance(boundary, bytes) else boundary}")
        found = [entry_id for entry_id, fields in entries if json.loads(fields.get(b"message", fields.get("message", b"{}"))).get("id") == payload["id"]]
        if len(found) == 1:
            return True
        if found or identifier:
            raise EnqueueUncertain("Document task enqueue could not be reconciled.")
    except EnqueueUncertain:
        raise
    except Exception as exc:
        raise EnqueueUncertain("Document task enqueue readback failed.") from exc
    return False


def _operate(selection: Selection, principal_id: str, run: str, clear: bool, apply_kb: bool, mutation_prepare: MutationPrepare | None = None) -> dict[str, Any]:
    """One document owns its transaction, store snapshot and enqueue acknowledgements."""
    flags: list[tuple[str, str, bytes | str | None, int]] = []
    snapshot: list[dict[str, Any]] | None = None
    original_doc: dict[str, Any] | None = None
    original_tasks: list[dict[str, Any]] = []
    applied_doc: dict[str, Any] | None = None
    applied_tasks: list[dict[str, Any]] = []
    original_kb_config: dict[str, Any] | None = None
    applied_kb_config: dict[str, Any] | None = None
    owned_tasks: list[dict[str, Any]] = []
    changed_store = False
    acknowledged: list[str] = []
    uncertain: list[str] = []
    index_name = ""
    recovery: StoredRecovery | None = None
    recovery_owner: RecoveryOwner | None = None
    outcome: dict[str, Any] = {}
    mutation: DocumentMutationPlan | None = None
    mutation_started = False
    applied_doc_version: str | None = None
    image_guard = ExitStack()
    image_keys: set[tuple[str, str]] = set()
    completed = False
    with _ingest_connection(selection.document_id, outcome) as db:
        try:
            doc = db.scalar(select(Document).where(Document.id == selection.document_id).with_for_update().execution_options(populate_existing=True))
            if doc is None or doc.kb_id != selection.dataset_id:
                if mutation_prepare:
                    raise DocumentUpdateError("Document is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
                raise IngestError("Document resource changed; retry.")
            kb = writable_kb(db, doc.kb_id, principal_id, lock=True)
            if kb is None:
                if mutation_prepare:
                    visible_kb = db.scalar(select(Knowledgebase).where(Knowledgebase.id == doc.kb_id, Knowledgebase.status == "1"))
                    visible = visible_kb is not None and (
                        visible_kb.tenant_id == principal_id
                        or db.scalar(select(UserTenant.id).where(UserTenant.tenant_id == visible_kb.tenant_id, UserTenant.user_id == principal_id, UserTenant.status == "1")) is not None
                    )
                    if not visible:
                        raise DocumentUpdateError("Document is unavailable.", status=404, code="DOCUMENT_UPDATE_UNAVAILABLE")
                    raise DocumentUpdateError("Document is not writable.", status=403, numeric_code=109, code="DOCUMENT_UPDATE_FORBIDDEN")
                raise IngestError("Document selection unavailable or not writable.", RetCode.AUTHENTICATION_ERROR)
            old_tasks = list(db.scalars(select(Task).where(Task.doc_id == doc.id).order_by(Task.id).with_for_update().execution_options(populate_existing=True)))
            if frozenset(task.id for task in old_tasks) != selection.task_ids or doc.update_time != selection.revision:
                if mutation_prepare:
                    raise DocumentUpdateError("Document work changed; retry.", status=409, code="DOCUMENT_UPDATE_CONFLICT")
                raise IngestError("Document work changed; retry.")
            if mutation_prepare:
                # Invalid mixed requests cannot mutate even an older journal.
                mutation_prepare(db, doc, kb)
                pending = StoredRecovery.load(doc.id)
                if pending and recovery_owner_active(db, pending.data.get("owner_lock")):
                    raise DocumentUpdateError("Document work changed; retry.", status=409, code="DOCUMENT_UPDATE_CONFLICT")
                mutation_started = pending is not None
                history = document_history(settings.docStoreConn, search.index_name_one(kb.tenant_id, kb.name), kb.id, doc.id)
                # Keep every reference, including protected/shared keys, stable
                # until reset, SQL save and any compensation have finished.
                image_keys = _history_image_keys(history, pending)
                image_guard.enter_context(image_write_locks(db.get_bind(), image_keys))
            doc, old_tasks, _ = _recover_history(db, doc, kb, old_tasks)
            if mutation_prepare:
                mutation = mutation_prepare(db, doc, kb)
                clear = mutation.reset
                run = "0" if clear else "save"
            original_doc = _rows(Document, [doc])[0]
            original_tasks = _rows(Task, old_tasks)
            index_name = search.index_name_one(kb.tenant_id, kb.name)
            values: dict[str, Any] = {"run": run, "progress": 0, "progress_msg": "", "update_time": max(int(datetime.now().timestamp() * 1000), (doc.update_time or 0) + 1)}
            if mutation:
                if not clear and mutation.values.keys() == {"status"} and mutation.effects["metadata"] is None:
                    # Availability is an independent write. Preserve its
                    # existing timestamp/revision contract, including retries.
                    values = copy.deepcopy(mutation.values)
                else:
                    values = {**mutation.values, "update_time": values["update_time"]}
                if clear:
                    values.update(run="0", progress=0, progress_msg="", process_begin_at=None)
                snapshot = document_history(settings.docStoreConn, index_name, kb.id, doc.id)
                if clear and not snapshot and (doc.chunk_num or doc.token_num or any(task.chunk_ids for task in old_tasks if (task.task_type or "") in {"", "dataflow", "dataflow_rerun"})):
                    raise DocumentUpdateError("Document history is unavailable.", status=500, numeric_code=500, code="DOCUMENT_UPDATE_FAILED")
                recovery_owner = RecoveryOwner(db.get_bind(), uuid4().hex)
                recovery = StoredRecovery.prepare(
                    doc.id,
                    nonce=uuid4().hex,
                    owner_lock=recovery_owner.key,
                    queue=settings.get_svr_queue_name(0),
                    dataset_id=kb.id,
                    index_name=index_name,
                    snapshot=snapshot,
                    original_doc=original_doc,
                    original_tasks=original_tasks,
                    original_kb_config=None,
                    flags=flags,
                    update_effects=mutation.effects,
                    original_doc_version=sql_row_version(db, Document, doc.id),
                )
                mutation_started = True
                recovery.create()
            if run == "2":
                if doc.run not in {"1", "2"} and not any(0 <= (task.progress or 0) < 1 for task in old_tasks):
                    raise IngestError("Document has no active parsing task to cancel.")
                _cancel_flags(old_tasks, flags)
                for task in old_tasks:
                    if 0 <= (task.progress or 0) < 1:
                        task.progress = -1
                        task.progress_msg = (task.progress_msg or "") + "\n" + TASK_CANCEL_MARKER + " Task stopped by user."
            if run == "0":
                _cancel_flags(old_tasks, flags, (lambda: recovery.update(flags=flags)) if recovery is not None else None)
                for task in old_tasks:
                    if (task.progress or 0) < 1:
                        db.delete(task)
            if run == "1" and apply_kb:
                config = copy.deepcopy(doc.parser_config or {})
                config.update(
                    {"llm_id": kb.parser_config.get("llm_id"), "enable_metadata": kb.parser_config.get("enable_metadata", False), "metadata": copy.deepcopy(kb.parser_config.get("metadata", {}))}
                )
                doc.parser_config = config
                values["parser_config"] = config
                db.flush()
            if not mutation and (run == "1" or clear):
                snapshot = document_history(settings.docStoreConn, index_name, kb.id, doc.id)
                if (
                    (run == "1" or clear)
                    and not snapshot
                    and (doc.chunk_num or doc.token_num or any(task.chunk_ids for task in old_tasks if (task.task_type or "") in {"", "dataflow", "dataflow_rerun"}))
                ):
                    raise IngestError("Document history is unavailable; restore the index before retrying.")
            obsolete: list[str] = []
            if run == "1":
                owned_tasks, obsolete = _plan(db, doc, kb, [] if clear else old_tasks)
                if doc.run == "1" and doc.parser_config == original_doc["parser_config"] and (not clear or (not snapshot and not doc.chunk_num and not doc.token_num)):
                    existing = _existing_submission(old_tasks, owned_tasks)
                    if existing is not None:
                        db.rollback()
                        outcome.update(run=run, queued_task_ids=existing)
                        return {"run": run}
                if clear:
                    obsolete = []
                db.execute(delete(Task).where(Task.doc_id == doc.id))
                for task in owned_tasks:
                    db.add(Task(**task))
                all_reused = all(task.get("progress", 0) >= 1 for task in owned_tasks)
                values.update(
                    run="3" if all_reused else "1",
                    progress=1 if all_reused else 0,
                    process_begin_at=datetime.now(),
                    progress_msg="Previous parsing results reused." if all_reused else "Task is queued...",
                )
            if run == "1" and doc.parser_id == "table" and not doc.chunk_num:
                db.refresh(kb, with_for_update=True)
                completed_document = db.scalar(select(Document.id).where(Document.kb_id == kb.id, Document.run == "3").limit(1))
                if completed_document is None:
                    original_kb_config = {key: value for key, value in _rows(Knowledgebase, [kb])[0].items() if key in {"parser_config", "update_time", "update_date"}}
                    table_config = copy.deepcopy(kb.parser_config or {})
                    table_config.pop("field_map", None)
                    kb.parser_config = table_config
            if snapshot and obsolete:
                parents = set(availability_parent_ids(snapshot).get(doc.id, []))
                retained_children = [row for row in snapshot if row.get("id", row.get("pk")) not in [*parents, *obsolete]]
                referenced = {row.get("mom_id") for row in retained_children if row.get("mom_id")}
                obsolete = list(dict.fromkeys([*obsolete, *(identifier for identifier in parents if identifier not in referenced)]))
            if clear or obsolete or mutation:
                if not mutation:
                    image_keys = _history_image_keys(snapshot or [])
                    image_guard.enter_context(image_write_locks(db.get_bind(), image_keys))
                if recovery is None:
                    nonce = uuid4().hex
                    recovery_owner = RecoveryOwner(db.get_bind(), nonce)
                    recovery = StoredRecovery.prepare(
                        doc.id,
                        nonce=nonce,
                        owner_lock=recovery_owner.key,
                        queue=settings.get_svr_queue_name(0),
                        dataset_id=kb.id,
                        index_name=index_name,
                        snapshot=snapshot or [],
                        original_doc=original_doc,
                        original_tasks=original_tasks,
                        original_kb_config=original_kb_config,
                        flags=flags,
                    )
                    recovery.create()
                if clear or obsolete:
                    changed_store = True
                    delete_document_history(settings.docStoreConn, index_name, kb.id, doc.id, ids=None if clear else obsolete)
            if mutation:
                apply_update_sql(db, doc, kb, mutation.effects)
                # The journal precedes every external side effect, including
                # rename/availability/metadata with no generation reset.
                if recovery is not None:
                    recovery.update(update_effects=mutation.effects)
                changed_store = True
                apply_update_store(
                    db,
                    doc,
                    kb,
                    mutation.effects,
                    [] if clear else snapshot or [],
                    values.get("status", doc.status),
                    (lambda: recovery.update(update_effects=mutation.effects)) if recovery is not None else None,
                )
            if run == "1" or clear:
                retained = [] if clear else [row for row in snapshot or [] if row.get("id", row.get("pk")) not in obsolete]
                parent_ids = set(availability_parent_ids(retained).get(doc.id, [])) if retained else set()
                count = sum(row.get("id", row.get("pk")) not in parent_ids for row in retained)
                tokens = doc.token_num if retained else 0
                if retained and obsolete and doc.token_num:
                    removed_tasks = [task for task in old_tasks if set((task.chunk_ids or "").split()) & set(obsolete)]
                    removed_tokens = [accounted_tokens(task.digest) for task in removed_tasks]
                    if any(value is None for value in removed_tokens):
                        raise IngestError("Partial history token ledger unavailable; retry with history clearing.")
                    tokens -= sum(value or 0 for value in removed_tokens)
                    if tokens < 0:
                        raise IngestError("Document history ledger is inconsistent; retry with history clearing.")
                values.update(chunk_num=count, token_num=tokens, process_duration=doc.process_duration if retained else 0)
            if clear:
                if run != "1":
                    db.execute(delete(Task).where(Task.doc_id == doc.id))
                values.update(chunk_num=0, token_num=0, process_duration=0)
            _set_document(db, doc, kb, values)
            applied_doc = copy.deepcopy(dict(db.execute(select(Document.__table__).where(Document.id == doc.id)).mappings().one()))
            if mutation:
                applied_doc_version = sql_row_version(db, Document, doc.id)
            applied_tasks = [copy.deepcopy(dict(row)) for row in db.execute(select(Task.__table__).where(Task.doc_id == doc.id).order_by(Task.id)).mappings()]
            if original_kb_config is not None:
                stored_kb = db.execute(select(Knowledgebase.__table__).where(Knowledgebase.id == kb.id)).mappings().one()
                applied_kb_config = {key: copy.deepcopy(stored_kb[key]) for key in original_kb_config}
            if recovery is not None:
                recovery.update(applied_doc=applied_doc, applied_tasks=applied_tasks, applied_kb_config=applied_kb_config, **({"applied_doc_version": applied_doc_version} if mutation else {}))
            db.commit()
            if run == "1":
                # Rows are committed before Redis can expose the task. Reacquire
                # the same lock order and reject replacement before enqueueing.
                current = db.scalar(select(Document).where(Document.id == doc.id).with_for_update(nowait=bool(image_keys)).execution_options(populate_existing=True))
                current_tasks = list(db.scalars(select(Task).where(Task.doc_id == doc.id).order_by(Task.id).with_for_update(nowait=bool(image_keys)).execution_options(populate_existing=True)))
                # The post-commit queue window performs no ledger upgrade;
                # a shared dataset lock now also holds active status until XADD.
                kb = writable_kb(db, selection.dataset_id, principal_id, lock=True, lock_dataset=True)
                if (
                    current is None
                    or kb is None
                    or not _same_document(_rows(Document, [current])[0], applied_doc)
                    or _rows(Task, current_tasks) != applied_tasks
                    or any(REDIS_CONN.REDIS.get(task.id + "-cancel") for task in current_tasks if (task.progress or 0) < 1)
                ):
                    raise IngestError("Document work changed before enqueue; retry.")
                for task in owned_tasks:
                    if task.get("progress", 0) >= 1:
                        continue
                    payload = _task_queue_payload(task)
                    payload.update(kb_id=kb.id, tenant_id=kb.tenant_id)
                    if doc.pipeline_id:
                        payload.update(kb_id=kb.id, tenant_id=kb.tenant_id, dataflow_id=doc.pipeline_id, file=None)
                    try:
                        submitted = submit_task(settings.get_svr_queue_name(0), payload)
                    except EnqueueUncertain:
                        uncertain.append(task["id"])
                        raise
                    if submitted is not True:
                        raise ConnectionError("Document task enqueue could not be confirmed.")
                    acknowledged.append(task["id"])
                db.commit()
            if mutation:
                from api.db.services.document_service import DocumentService

                db.expire_all()
                current = db.get(Document, selection.document_id)
                serialized = DocumentService.serialize_document(db, current) if current is not None else None
                if serialized is None:
                    raise RuntimeError("Document update readback failed.")
                if recovery is not None:
                    recovery.clear()
                outcome.update(run=run, queued_task_ids=acknowledged)
                completed = True
                return {"document": serialized}
            if recovery is not None:
                recovery.clear()
            outcome.update(run=run, queued_task_ids=acknowledged)
            completed = True
            return {"run": run}
        except Exception as error:
            logger.exception("Document ingest failed: document_id=%s", selection.document_id)
            recovery_failed = False
            try:
                db.rollback()
                current = db.scalar(select(Document).where(Document.id == selection.document_id).with_for_update(nowait=bool(image_keys)).execution_options(populate_existing=True))
                tasks = list(db.scalars(select(Task).where(Task.doc_id == selection.document_id).order_by(Task.id).with_for_update(nowait=bool(image_keys)).execution_options(populate_existing=True)))
                unchanged_sql = current is not None and _same_document(_rows(Document, [current])[0], original_doc) and _rows(Task, tasks) == original_tasks
                owned_sql = current is not None and _same_document(_rows(Document, [current])[0], applied_doc) and _rows(Task, tasks) == applied_tasks
                if original_doc is None:
                    db.rollback()
                elif (acknowledged or uncertain) and owned_sql:
                    for task in tasks:
                        if task.id not in [*acknowledged, *uncertain] and (task.progress or 0) < 1:
                            task.progress = -1
                            task.progress_msg = "Task enqueue failed; retry document ingestion."
                    db.commit()
                elif unchanged_sql or owned_sql:
                    # SQL commit may have failed before or after reaching the
                    # server. The actual current rows, never the exception,
                    # decide whether this operation still owns compensation.
                    status = _restored_status(
                        current,
                        {"original_doc": original_doc, "applied_doc": applied_doc, "update_effects": mutation.effects if mutation else None, "applied_doc_version": applied_doc_version},
                        sql_row_version(db, Document, current.id) if mutation else None,
                    )
                    if changed_store and snapshot is not None:
                        restore_document_history(settings.docStoreConn, index_name, selection.dataset_id, selection.document_id, _history_for_status(snapshot, status))
                    if mutation:
                        kb = db.get(Knowledgebase, selection.dataset_id)
                        if kb is None:
                            raise RuntimeError("Document recovery dataset is unavailable.")
                        restore_update_effects(db, current, kb, mutation.effects, owned_sql=owned_sql)
                    if owned_sql and current is not None:
                        kb = writable_kb(db, current.kb_id, principal_id, lock=True)
                        if kb is None:
                            raise RuntimeError("Document recovery resource unavailable.")
                        _set_document(db, current, kb, {**{key: value for key, value in original_doc.items() if key not in {"id", "status"}}, "status": status})
                        _restore_kb_configuration(db, kb, original_kb_config, applied_kb_config)
                        db.execute(delete(Task).where(Task.doc_id == current.id))
                        if original_tasks:
                            db.execute(Task.__table__.insert(), original_tasks)
                        db.commit()
                    else:
                        db.rollback()
                else:
                    if recovery is not None:
                        recovery.clear()
                        recovery = None
                    raise RuntimeError("A later document operation won recovery.")
                if flags:
                    _restore_flags(flags)
            except Exception:
                logger.exception("Document ingest recovery failed: document_id=%s", selection.document_id)
                recovery_failed = True
            finally:
                db.rollback()
            if recovery is not None:
                try:
                    if recovery_failed and not acknowledged and not uncertain:
                        recovery.update(phase="failed")
                    else:
                        recovery.clear()
                except Exception:
                    logger.exception("Document recovery material could not be finalized: document_id=%s", selection.document_id)
                    recovery_failed = True
            message = (
                "Document ingestion recovery could not be confirmed; retry to reconcile."
                if recovery_failed
                else str(error)
                if isinstance(error, IngestError)
                else "Document ingestion failed; retry to reconcile."
            )
            result: dict[str, Any] = {"error": message, "_code": int(error.code) if isinstance(error, IngestError) and not recovery_failed else int(RetCode.SERVER_ERROR)}
            if acknowledged or uncertain:
                result.update(run=run, queued_task_ids=acknowledged)
            if uncertain:
                result["uncertain_task_ids"] = uncertain
            outcome.update(result)
            if mutation_prepare:
                unknown = recovery_failed or (original_doc is None and mutation_started)
                if isinstance(error, DocumentUpdateError) and not recovery_failed:
                    update_error = error
                elif isinstance(error, IngestConflict) and not unknown:
                    update_error = DocumentUpdateError("Document work changed; retry.", status=409, code="DOCUMENT_UPDATE_CONFLICT")
                else:
                    update_error = DocumentUpdateError(
                        "Document update recovery could not be confirmed; read back before retrying." if unknown else "Document update failed and was restored.",
                        status=500,
                        numeric_code=500,
                        code="DOCUMENT_UPDATE_OUTCOME_UNKNOWN" if unknown else "DOCUMENT_UPDATE_FAILED",
                        outcome="unknown" if unknown else "unchanged",
                    )
                result["_update_error"] = update_error
            return result
        finally:
            image_guard.close()
            if recovery_owner is not None:
                recovery_owner.close()
            if completed:
                # Do not retire during compensation: its original Task may
                # become current again. Confirmed SQL retirement comes first.
                retire_task_image_reservations(db, selection.task_ids)


def ingest_selected(selected: list[Selection], principal_id: str, run: str, clear: bool, apply_kb: bool) -> bool | dict[str, Any]:
    results: dict[str, dict[str, Any]] = {}
    codes: list[int] = []
    for selection in selected:
        try:
            result = _operate(selection, principal_id, run, clear, apply_kb)
        except Exception as error:
            logger.exception("Document ingest boundary failed: document_id=%s", selection.document_id)
            archived = error.result.get("results", {}).get(selection.document_id) if isinstance(error, IngestError) and error.result else None
            result = copy.deepcopy(archived) if isinstance(archived, dict) else {"error": "Document ingestion effect could not be confirmed; retry to reconcile.", "effect": "unknown"}
            result["_code"] = int(RetCode.SERVER_ERROR)
        if not isinstance(result, dict) or ("error" not in result and result.get("run") != run):
            result = {"error": "Document ingestion effect could not be confirmed."}
        if "error" in result:
            codes.append(result.pop("_code", int(RetCode.SERVER_ERROR)))
        results[selection.document_id] = result
    if any("error" in result for result in results.values()):
        code = RetCode(codes[0]) if len(codes) == len(results) and len(set(codes)) == 1 else RetCode.SERVER_ERROR
        raise IngestError("Document ingestion was not fully submitted.", code, {"results": results})
    return True


def ingest_documents_sync(db: Session, document_ids: list[str], principal_id: str, run: str, clear: bool = False, apply_kb: bool = False, dataset_id: str | None = None) -> bool | dict[str, Any]:
    selected = preflight(db, document_ids, principal_id, dataset_id)
    db.rollback()
    return ingest_selected(selected, principal_id, run, clear, apply_kb)


async def ingest_documents(db: AsyncSession, document_ids: list[str], principal_id: str, run: str, clear: bool = False, apply_kb: bool = False) -> bool | dict[str, Any]:
    selected = await db.run_sync(lambda session: preflight(session, document_ids, principal_id))  # TODO(async-phase4): SQL-only preflight.
    await db.rollback()
    return await finish_status_write(asyncio.to_thread(ingest_selected, selected, principal_id, run, clear, apply_kb))
