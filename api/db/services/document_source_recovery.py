"""Durable source intent committed before native effects, with a Redis mirror."""

import copy
from dataclasses import dataclass
from typing import Any, Never
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.engine import Connection, Engine

from api.db.db_models import SourceRecoveryRecord, db_connection
from api.db.services.document_ingest_recovery import StoredRecovery, _read_wire, _text, _wire
from core.utils.redis_conn import REDIS_CONN


class SourceRecoveryConflict(RuntimeError):
    outcome = "unknown"


def source_recovery_key(document_id: str, task_id: str) -> str:
    return f"document-source-recovery:{document_id}:{task_id}"


def _engine(bind: Engine | Connection | None) -> Engine:
    if bind is None:
        with db_connection() as db:
            bind = db.get_bind()
    return bind.engine if isinstance(bind, Connection) else bind


def _stored(bind: Engine, document_id: str, task_id: str) -> tuple[str, str] | None:
    with bind.connect() as db:
        row = db.execute(select(SourceRecoveryRecord.id, SourceRecoveryRecord.wire).where(SourceRecoveryRecord.document_id == document_id, SourceRecoveryRecord.task_id == task_id)).first()
    return (row.id, row.wire) if row is not None else None


def _transition(bind: Engine, document_id: str, task_id: str, before: StoredRecovery | None, after: StoredRecovery | None) -> None:
    """A separate transaction survives producer rollback; exact readback handles lost COMMIT replies."""
    table = SourceRecoveryRecord.__table__
    failure: Exception | None = None
    try:
        with bind.begin() as db:
            if before is None:
                if after is None:
                    raise ValueError("A recovery transition needs material.")
                db.execute(table.insert().values(id=after.data["nonce"], document_id=document_id, task_id=task_id, wire=after.wire))
            else:
                owned = (table.c.document_id == document_id, table.c.task_id == task_id, table.c.id == before.data["nonce"], table.c.wire == before.wire)
                statement = delete(table).where(*owned) if after is None else update(table).where(*owned).values(id=after.data["nonce"], wire=after.wire)
                if db.execute(statement).rowcount != 1:
                    raise SourceRecoveryConflict("Source recovery ownership changed.")
    except Exception as error:
        failure = error
    expected = (after.data["nonce"], after.wire) if after is not None else None
    if _stored(bind, document_id, task_id) != expected:
        raise SourceRecoveryConflict("Source recovery material could not be confirmed.") from failure


@dataclass
class SourceRecovery(StoredRecovery):
    bind: Engine

    def mirror(self) -> None:
        raw = _text(REDIS_CONN.REDIS.get(self.key))
        if raw == self.wire:
            return
        if raw is None:
            StoredRecovery(self.key, self.data, self.wire).create()
            return
        previous = _read_wire(raw)
        if any(previous.get(key) != self.data[key] for key in ["document_id", "task_id", "nonce"]):
            raise SourceRecoveryConflict("Source recovery mirror ownership changed.")
        script = "if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end redis.call('SET',KEYS[1],ARGV[2]); return 1"
        try:
            acknowledged = REDIS_CONN.REDIS.eval(script, 1, self.key, raw, self.wire)
        except Exception:
            acknowledged = None
        if acknowledged == 0 or _text(REDIS_CONN.REDIS.get(self.key)) != self.wire:
            raise SourceRecoveryConflict("Source recovery mirror could not be confirmed.")

    def update(self, *, mirror: bool = True, **changes: Any) -> None:
        data = {**copy.deepcopy(self.data), **changes}
        target = StoredRecovery(self.key, data, _wire(data))
        _transition(self.bind, self.data["document_id"], self.data["task_id"], self, target)
        self.data, self.wire = data, target.wire
        if mirror:
            self.mirror()

    def clear_mirror(self) -> None:
        raw = _text(REDIS_CONN.REDIS.get(self.key))
        if raw is None:
            return
        data = _read_wire(raw)
        if any(data.get(key) != self.data[key] for key in ["document_id", "task_id", "nonce"]):
            raise SourceRecoveryConflict("Source recovery mirror ownership changed.")
        try:
            StoredRecovery(self.key, data, raw).clear()
        except Exception:
            if REDIS_CONN.REDIS.exists(self.key):
                raise
        if REDIS_CONN.REDIS.exists(self.key):
            raise SourceRecoveryConflict("Source recovery retirement could not be confirmed.")

    def clear(self) -> None:
        self.clear_mirror()
        _transition(self.bind, self.data["document_id"], self.data["task_id"], self, None)

    def ready(self, identifiers: set[str]) -> None:
        owned = copy.deepcopy((self.data.get("prior_ready") or {}).get("owned_rows", {}))
        owned.update(_native_rows(self.data["applied_native"], identifiers))
        # Until mirror retirement and this final SQL transition are confirmed,
        # the current Task stays blocked, including failures after successful writes.
        self.update(phase="complete", owned_rows=owned)
        self.clear_mirror()
        self.update(phase="ready", prior_ready=None, mirror=False)

    def restored(self) -> None:
        self.clear_mirror()
        prior = self.data.get("prior_ready")
        target = StoredRecovery(self.key, prior, _wire(prior)) if prior is not None else None
        _transition(self.bind, self.data["document_id"], self.data["task_id"], self, target)


def load_source_recovery(document_id: str, task_id: str, bind: Engine | Connection | None = None) -> SourceRecovery | None:
    engine = _engine(bind)
    row = _stored(engine, document_id, task_id)
    if row is None:
        if REDIS_CONN.REDIS.exists(source_recovery_key(document_id, task_id)):
            raise SourceRecoveryConflict("Source recovery authority could not be confirmed.")
        return None
    nonce, raw = row
    data = _read_wire(raw)
    if (
        data.get("version") != 1
        or data.get("document_id") != document_id
        or data.get("task_id") != task_id
        or data.get("nonce") != nonce
        or data.get("phase") not in {"intent", "applied", "unknown", "complete", "ready"}
    ):
        raise SourceRecoveryConflict("Source recovery material could not be confirmed.")
    return SourceRecovery(source_recovery_key(document_id, task_id), data, raw, engine)


def require_source_recovery_resolved(document_id: str, task_id: str, bind: Engine | Connection) -> None:
    material = load_source_recovery(document_id, task_id, bind)
    if material is not None and material.data["phase"] != "ready":
        raise SourceRecoveryConflict("Source recovery requires reconciliation before retry.")


def _native_rows(rows: list[dict[str, Any]], identifiers: set[str]) -> dict[str, dict[str, Any]]:
    return {str(row.get("id", row.get("pk"))): row for row in rows if str(row.get("id", row.get("pk"))) in identifiers}


def prepare_source_recovery(bind: Engine | Connection, document_id: str, task_id: str, **data: Any) -> SourceRecovery:
    previous = load_source_recovery(document_id, task_id, bind)
    if previous is not None:
        if previous.data["phase"] != "ready":
            raise SourceRecoveryConflict("Source recovery requires reconciliation before retry.")
        ids = {str(chunk.get("id", chunk.get("pk"))) for chunk in data["chunks"]}
        owned = {key: value for key, value in previous.data["owned_rows"].items() if key in ids}
        current = _native_rows(data["original_native"], set(owned))
        if previous.data["applied_doc"]["status"] != data["original_doc"]["status"]:
            availability = {str(chunk.get("id", chunk.get("pk"))): chunk["available_int"] for chunk in data["chunks"]}
            for key, row in owned.items():
                if key in current and current[key].get("available_int") == availability[key]:
                    row = copy.deepcopy(row)
                    row["available_int"] = current[key].get("available_int")
                    owned[key] = row
        if current != owned:
            preserve_source_recovery(previous, current_native=data["original_native"], current_doc=data["original_doc"], current_task=data["original_task"])
    content = {"version": 1, "phase": "intent", "document_id": document_id, "task_id": task_id, "nonce": uuid4().hex, "prior_ready": previous.data if previous is not None else None, **data}
    material = SourceRecovery(source_recovery_key(document_id, task_id), content, _wire(content), _engine(bind))
    _transition(material.bind, document_id, task_id, previous, material)
    try:
        material.mirror()
    except Exception as error:
        # SQL intent and original material are already authoritative. No native
        # mutation has started, and this exact Task remains blocked.
        raise SourceRecoveryConflict("Source recovery mirror could not be confirmed before native writes.") from error
    return material


def preserve_source_recovery(material: SourceRecovery, **data: Any) -> Never:
    try:
        material.update(phase="unknown", **data)
    except Exception as error:
        raise SourceRecoveryConflict("Source recovery requires reconciliation; durable intent is retained.") from error
    raise SourceRecoveryConflict("Source recovery no longer owns the native rows; reconciliation is required.")


def retire_source_recovery(document_id: str, task_id: str, bind: Engine | Connection) -> None:
    material = load_source_recovery(document_id, task_id, bind)
    if material is not None:
        material.clear()
