"""Shared read-only Message recovery consumer and side-effect evidence."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Base, Memory, Task
from core.utils.redis_conn import REDIS_CONN
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def memory_effects(env: dict[str, Any]) -> dict[str, Any]:
    """Read durable rows independently, including timestamps and recovery state."""
    mid = env["memory_id"]
    with Session(env["engine"]) as reader:
        reader.execute(sa.text("SET TRANSACTION READ ONLY"))
        tables = [table for table in Base.metadata.sorted_tables if table.schema == "usr_ai" and "memory_id" in table.c]
        rows = {table.fullname: [dict(row) for row in reader.execute(sa.select(table).where(table.c.memory_id == mid)).mappings()] for table in tables}
        rows["memory"] = [dict(row) for row in reader.execute(sa.select(Memory.__table__).where(Memory.id == mid)).mappings()]
        rows["tasks"] = [dict(row) for row in reader.execute(sa.select(Task.__table__).where(Task.doc_id == mid)).mappings()]
    canonical = {name: sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in values) for name, values in rows.items()}
    return {
        "sql_sha256": hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "embedding_calls": len(env["embedding_calls"]),
        "chat_calls": len(env["chat_calls"]),
        "queue_length": REDIS_CONN.REDIS.xlen(env["queue"]),
        "counter": REDIS_CONN.REDIS.get(env["counter"]),
    }


@contextmanager
def no_sql_mutations() -> Iterator[list[str]]:
    writes: list[str] = []

    def observe(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if any(getattr(context, flag, False) for flag in ("isinsert", "isupdate", "isdelete")):
            writes.append(statement.split()[0])

    sa.event.listen(sa.engine.Engine, "before_cursor_execute", observe)
    try:
        yield writes
    finally:
        sa.event.remove(sa.engine.Engine, "before_cursor_execute", observe)


def recovery_consumer(env: dict[str, Any], tmp_path: Path, source: dict[str, Any], operation_id: str, *, suffix: str) -> dict[str, Any]:
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("native-message-recovery-web")
    config, returned = tmp_path / f"recovery-{suffix}.private.json", tmp_path / f"recovery-{suffix}.result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            config,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "output": source["output"],
                    "traceItem": source["trace_item"],
                    "expected": [{"memoryId": env["memory_id"], "operationId": operation_id}],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(returned),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(returned, "", journal, owned)
        journal.event("reserve-message-recovery-consumer", web_sha=web_sha, memory_id=env["memory_id"], operation_id=operation_id, input_is_actual_Message_output=True, process_group=os.getpgrp())
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-message-recovery.ts", str(config)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        value = json.loads(returned.read_text())
        assert value["passed"] is True and value["readOnly"] is True and len(value["operations"]) == 1
        operation = value["operations"][0]
        assert operation["memoryId"] == env["memory_id"] and operation["operationId"] == operation_id
        assert "/operations?operation=" in operation["href"]
        journal.event("message-recovery-web-readback", web_sha=web_sha, operation_id=operation_id, receipt=value)
        return value
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("message-recovery-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
