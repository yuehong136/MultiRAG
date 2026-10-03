"""Serialize exact history-image keys across documents and SQL commit windows."""

import hashlib
import json
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from core.utils.redis_conn import REDIS_CONN

_HELD = threading.local()


def task_image_reservation_key(task_id: str) -> str:
    return f"document-task-images:{task_id}"


def reserve_task_image(task_id: str, key: tuple[str, str]) -> None:
    """The caller holds the current Doc/Task and exact image locks before put."""
    reservation = task_image_reservation_key(task_id)
    member = json.dumps(key, separators=(",", ":"))
    acknowledged = REDIS_CONN.REDIS.sadd(reservation, member)
    confirmed = REDIS_CONN.REDIS.sismember(reservation, member)
    if type(acknowledged) is not int or acknowledged not in {0, 1} or type(confirmed) not in {bool, int} or confirmed != 1:
        raise RuntimeError("Task image reservation could not be confirmed.")


def release_task_images(task_id: str, keys: Collection[tuple[str, str]] | None = None) -> None:
    """Release confirmed native references, or an authoritatively retired task."""
    reservation = task_image_reservation_key(task_id)
    if keys is None:
        acknowledged = REDIS_CONN.REDIS.delete(reservation)
        remaining = REDIS_CONN.REDIS.exists(reservation)
        if type(acknowledged) is not int or acknowledged not in {0, 1} or type(remaining) not in {bool, int} or remaining != 0:
            raise RuntimeError("Task image retirement could not be confirmed.")
        return
    members = [json.dumps(key, separators=(",", ":")) for key in sorted(set(keys))]
    if not members:
        return
    acknowledged = REDIS_CONN.REDIS.srem(reservation, *members)
    remaining = [REDIS_CONN.REDIS.sismember(reservation, member) for member in members]
    if type(acknowledged) is not int or not 0 <= acknowledged <= len(members) or any(type(value) not in {bool, int} or value != 0 for value in remaining):
        raise RuntimeError("Task image reference registration could not be confirmed.")


def pending_task_image_references(db: Session, document_id: str, candidates: set[str]) -> set[str]:
    """Only trusted reservations bound to another current SQL Task protect bytes."""
    from sqlalchemy import select

    from api.db.db_models import Document, Task
    from core.utils.task_runtime import TASK_CANCEL_MARKER

    protected: set[str] = set()
    rows = db.execute(select(Task.id, Task.progress, Task.progress_msg, Document.run).join(Document, Document.id == Task.doc_id).where(Document.id != document_id))
    for task_id, progress, message, run in rows:
        members = REDIS_CONN.REDIS.smembers(task_image_reservation_key(task_id))
        if not members:
            continue
        if run in {"0", "2"} or not 0 <= (progress or 0) < 1 or TASK_CANCEL_MARKER in (message or ""):
            continue
        for member in members:
            key = json.loads(member)
            if not isinstance(key, list) or len(key) != 2 or any(not isinstance(part, str) or not part for part in key):
                raise RuntimeError("Task image reservation is unavailable.")
            identifier = f"{key[0]}-{key[1]}"
            if identifier in candidates:
                protected.add(identifier)
    return protected


def retire_task_image_reservations(db: Session, task_ids: Collection[str], *, document_id: str | None = None) -> None:
    """SQL retirement is authoritative; never retire a still-current producer."""
    from sqlalchemy import select

    from api.db.db_models import Document, Task
    from api.db.services.document_source_recovery import retire_source_recovery
    from core.utils.task_runtime import TASK_CANCEL_MARKER

    for task_id in task_ids:
        row = db.execute(select(Task.progress, Task.progress_msg, Document.run).join(Document, Document.id == Task.doc_id).where(Task.id == task_id)).first()
        if row is None or row.run in {"0", "2"} or not 0 <= (row.progress or 0) < 1 or TASK_CANCEL_MARKER in (row.progress_msg or ""):
            release_task_images(task_id)
        if row is None and document_id is not None:
            retire_source_recovery(document_id, task_id, db.get_bind())


def image_reference_key(identifier: Any) -> tuple[str, str] | None:
    if not isinstance(identifier, str):
        return None
    bucket, separator, key = identifier.partition("-")
    return (bucket, key) if separator and bucket and key else None


@contextmanager
def image_write_locks(bind: Engine | Connection, keys: Collection[tuple[str, str]]) -> Iterator[None]:
    """A dedicated transaction keeps locks across a caller's commit/rollback."""
    ordered = sorted(set(keys))
    if not ordered:
        yield
        return
    held = getattr(_HELD, "keys", frozenset())
    if set(ordered) <= held:
        yield
        return
    if held:
        raise RuntimeError("An image operation cannot extend its acquired key set.")
    engine = bind.engine if isinstance(bind, Connection) else bind
    with engine.connect() as connection, connection.begin():
        for bucket, key in ordered:
            lock = int.from_bytes(hashlib.sha256(("document-image\0" + bucket + "\0" + key).encode()).digest()[:8], signed=True)
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
        _HELD.keys = frozenset(ordered)
        try:
            yield
        finally:
            _HELD.keys = frozenset()
