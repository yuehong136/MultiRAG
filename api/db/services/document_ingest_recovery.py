"""Owned durable history material for retry after a failed store compensation."""

import base64
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task
from core.utils.redis_conn import REDIS_CONN


def recovery_key(document_id: str) -> str:
    return f"document-ingest-recovery:{document_id}"


class RecoveryOwner:
    """A separate transaction identifies a live writer across its SQL commits."""

    def __init__(self, bind: Engine | Connection, nonce: str) -> None:
        self.key = int.from_bytes(hashlib.sha256(nonce.encode()).digest()[:8], signed=True)
        engine = bind.engine if isinstance(bind, Connection) else bind
        self.connection = engine.connect()
        try:
            self.connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": self.key})
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()


def recovery_owner_active(db: Session, key: int | None) -> bool:
    if key is None:
        return False
    # A successful probe owns the otherwise vacant key until this request's
    # transaction ends, so two recovery attempts cannot race the same material.
    return db.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}) is not True


def _wire(value: dict[str, Any]) -> str:
    types: list[dict[str, Any]] = []

    def encode(item: Any, path: list[str | int]) -> Any:
        if isinstance(item, datetime):
            types.append({"path": path, "type": "datetime"})
            return item.isoformat()
        if isinstance(item, date):
            types.append({"path": path, "type": "date"})
            return item.isoformat()
        if isinstance(item, bytes):
            types.append({"path": path, "type": "bytes"})
            return base64.b64encode(item).decode()
        if isinstance(item, Decimal):
            types.append({"path": path, "type": "decimal"})
            return str(item)
        if hasattr(item, "tolist"):
            return encode(item.tolist(), path)
        if isinstance(item, dict):
            return {key: encode(child, [*path, key]) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [encode(child, [*path, index]) for index, child in enumerate(item)]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError(f"Unknown ingestion recovery value: {type(item).__name__}.")

    payload = encode(value, [])
    return json.dumps({"payload": payload, "types": types}, sort_keys=True)


def _read_wire(raw: str) -> dict[str, Any]:
    document = json.loads(raw)
    data = document["payload"]
    decoders = {"datetime": datetime.fromisoformat, "date": date.fromisoformat, "bytes": base64.b64decode, "decimal": Decimal}
    for item in document["types"]:
        parent = data
        for part in item["path"][:-1]:
            parent = parent[part]
        key = item["path"][-1]
        parent[key] = decoders[item["type"]](parent[key])
    return data


def _text(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


@dataclass
class StoredRecovery:
    key: str
    data: dict[str, Any]
    wire: str

    @classmethod
    def prepare(cls, document_id: str, **data: Any) -> "StoredRecovery":
        content = {"version": 1, "phase": "active", "document_id": document_id, **data}
        return cls(recovery_key(document_id), content, _wire(content))

    @classmethod
    def load(cls, document_id: str) -> "StoredRecovery | None":
        raw = _text(REDIS_CONN.REDIS.get(recovery_key(document_id)))
        if raw is None:
            return None
        data = _read_wire(raw)
        if data.get("version") != 1 or data.get("document_id") != document_id:
            raise RuntimeError("Document recovery material is unavailable.")
        for name, model in [
            ("original_doc", Document),
            ("applied_doc", Document),
            ("original_kb_config", Knowledgebase),
            ("applied_kb_config", Knowledgebase),
            ("original_tasks", Task),
            ("applied_tasks", Task),
        ]:
            rows = data.get(name)
            if rows is None:
                continue
            for row in rows if isinstance(rows, list) else [rows]:
                for column in model.__table__.columns:
                    if isinstance(column.type, DateTime) and isinstance(row.get(column.name), str):
                        row[column.name] = datetime.fromisoformat(row[column.name])
        return cls(recovery_key(document_id), data, raw)

    def create(self) -> None:
        try:
            acknowledged = REDIS_CONN.REDIS.set(self.key, self.wire, nx=True)
        except Exception:
            acknowledged = None
        if _text(REDIS_CONN.REDIS.get(self.key)) != self.wire:
            raise RuntimeError("Document history recovery material could not be confirmed.")
        # The exact readback confirms an accepted response lost in transport.
        if acknowledged is False:
            raise RuntimeError("Document history recovery material already exists.")

    def update(self, **changes: Any) -> None:
        updated = {**copy.deepcopy(self.data), **changes}
        wire = _wire(updated)
        script = "if redis.call('GET',KEYS[1]) ~= ARGV[1] then return 0 end redis.call('SET',KEYS[1],ARGV[2]); return 1"
        acknowledged = REDIS_CONN.REDIS.eval(script, 1, self.key, self.wire, wire)
        if type(acknowledged) is not int or acknowledged != 1 or _text(REDIS_CONN.REDIS.get(self.key)) != wire:
            raise RuntimeError("Document recovery ownership changed.")
        self.data, self.wire = updated, wire

    def clear(self) -> None:
        script = "local current=redis.call('GET',KEYS[1]); if not current then return 1 end if current ~= ARGV[1] then return 0 end redis.call('DEL',KEYS[1]); return 1"
        acknowledged = REDIS_CONN.REDIS.eval(script, 1, self.key, self.wire)
        if type(acknowledged) is not int or acknowledged != 1:
            raise RuntimeError("Document recovery ownership changed.")
