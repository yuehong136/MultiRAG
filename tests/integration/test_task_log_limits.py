"""Real SQL concurrency, UTF-8 capacity, rollback and receipt/projection paths."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, Tenant
from api.db.services import task_service
from api.db.services.document_service import DocumentService
from api.db.services.task_service import TASK_LOG_MAX_BYTES, TaskService
from common.constants import TaskStatus
from core.utils.task_runtime import TASK_CANCEL_MARKER


def evidence(tmp_path: Path, label: str, data: dict[str, Any]) -> None:
    root = Path(os.environ.get("TASK_LOG_EVIDENCE", str(tmp_path)))
    root.mkdir(parents=True, exist_ok=True)
    path = root / (label + "-" + uuid4().hex[:8] + ".json")
    path.write_text(json.dumps(data, indent=2))
    path.chmod(0o600)


@pytest.mark.parametrize("macos", [False, True])
def test_concurrent_writers_refresh_cached_rows_until_commit(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, macos: bool) -> None:
    engine = bootstrapped_engine
    if macos:
        monkeypatch.setenv("MACOS", "1")
    else:
        monkeypatch.delenv("MACOS", raising=False)
    identifier = uuid4().hex
    with Session(engine) as db:
        db.add(Task(id=identifier, doc_id=uuid4().hex, progress_msg="seed", progress=0))
        db.commit()
    gate = threading.Barrier(4)

    def write(index: int) -> None:
        with Session(engine) as db:
            cached = db.get(Task, identifier)
            assert cached is not None
            gate.wait(timeout=15)
            TaskService.update_progress(db, identifier, {"progress_msg": f"writer-{index}", "progress": (index + 1) / 12})
            time.sleep(0.01)
            db.commit()

    try:
        with ThreadPoolExecutor(4) as threads:
            list(threads.map(write, range(12)))
        with Session(engine) as reader:
            task = reader.get(Task, identifier)
            assert task is not None
            assert set(task.progress_msg.splitlines()) == {"seed", *(f"writer-{index}" for index in range(12))}
            assert len(task.progress_msg.splitlines()) == 13 and task.progress == 1
        evidence(tmp_path, "concurrency", {"database": engine.url.database, "macos": macos, "writers": 12, "all_messages_once": True, "progress": 1})
    finally:
        with Session(engine) as db:
            db.execute(sa.delete(Task).where(Task.id == identifier))
            db.commit()


def test_byte_budget_rollback_and_cancel_marker(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine = bootstrapped_engine
    identifier = uuid4().hex
    monkeypatch.setattr(task_service, "TASK_MAX_LOG_LENGTH", 60000)
    with Session(engine) as db:
        db.add(Task(id=identifier, doc_id=uuid4().hex, progress_msg="original", progress=0))
        db.commit()
    try:
        with Session(engine) as writer:
            TaskService.update_progress(writer, identifier, {"progress_msg": "uncommitted"})
            with Session(engine) as reader:
                assert reader.get(Task, identifier).progress_msg == "original"
            writer.rollback()
        text = "中文🚀" * 30000
        with Session(engine) as writer:
            TaskService.update_progress(writer, identifier, {"progress_msg": text, "progress": 0.5})
            writer.commit()
        with Session(engine) as reader:
            stored, byte_count = reader.execute(sa.select(Task.progress_msg, sa.func.octet_length(Task.progress_msg)).where(Task.id == identifier)).one()
            assert byte_count == len(stored.encode("utf-8")) <= TASK_LOG_MAX_BYTES
            assert text.endswith(stored) and stored.endswith("中文🚀")
            reader.execute(sa.update(Task).where(Task.id == identifier).values(progress_msg=TASK_CANCEL_MARKER, progress=-1))
            reader.commit()
        with Session(engine) as writer:
            TaskService.update_progress(writer, identifier, {"progress_msg": "late completion", "progress": 1})
            writer.commit()
        with Session(engine) as reader:
            task = reader.get(Task, identifier)
            assert task.progress_msg == TASK_CANCEL_MARKER and task.progress == -1
        evidence(tmp_path, "utf8", {"database": engine.url.database, "bytes": byte_count, "budget": TASK_LOG_MAX_BYTES, "rollback_unchanged": True, "cancel_unchanged": True})
    finally:
        with Session(engine) as db:
            db.execute(sa.delete(Task).where(Task.id == identifier))
            db.commit()


def test_receipt_and_multi_task_document_projection_share_capacity(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    engine = bootstrapped_engine
    tenant, kb, doc = uuid4().hex, uuid4().hex, uuid4().hex
    tasks = [uuid4().hex for _ in range(3)]
    monkeypatch.setattr(task_service, "TASK_MAX_LOG_LENGTH", 60000)
    with Session(engine) as db:
        db.add(Tenant(id=tenant, name="log acceptance", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
        db.add(Knowledgebase(id=kb, tenant_id=tenant, created_by=tenant, name="logs", embd_id="controlled"))
        db.add(Document(id=doc, kb_id=kb, created_by=tenant, name="logs.txt", type="doc", parser_id="naive", run=TaskStatus.RUNNING.value, progress=0.1))
        db.add_all([Task(id=identifier, doc_id=doc, progress=0.5, progress_msg=("a" if index == 0 else "z") * 40000 + f"-tail-{index}") for index, identifier in enumerate(tasks)])
        db.commit()
        db.execute(sa.update(Task).where(Task.id == tasks[2]).values(progress_msg=None))
        db.commit()
    try:
        with Session(engine) as db:
            assert TaskService.get_task(db, tasks[0]) is not None
        with Session(engine) as db:
            stored = db.get(Task, tasks[0]).progress_msg
            assert "Task has been received." in stored and len(stored.encode()) <= TASK_LOG_MAX_BYTES
            DocumentService.update_progress_immediately(db, [{"id": doc}])
        with Session(engine) as reader:
            projected = reader.get(Document, doc).progress_msg
            assert len(projected.encode()) <= TASK_LOG_MAX_BYTES and projected.endswith("-tail-1")
        evidence(
            tmp_path,
            "projection",
            {"database": engine.url.database, "receipt_bounded": True, "nullable_task_supported": True, "projection_bytes": len(projected.encode()), "task_budget": TASK_LOG_MAX_BYTES},
        )
    finally:
        with Session(engine) as db:
            db.execute(sa.delete(Task).where(Task.id.in_(tasks)))
            db.execute(sa.delete(Document).where(Document.id == doc))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == kb))
            db.execute(sa.delete(Tenant).where(Tenant.id == tenant))
            db.commit()
