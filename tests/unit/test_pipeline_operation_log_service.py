"""Pipeline operation log regression tests."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import PipelineOperationLog
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID, TaskService
from common.constants import PipelineTaskType, TaskStatus


def _document(*, progress: float = 1) -> SimpleNamespace:
    return SimpleNamespace(
        kb_id="kb-1",
        parser_id="naive",
        thumbnail="avatar",
        name="source.pdf",
        run=TaskStatus.DONE,
        progress=progress,
        progress_msg="document progress",
        process_begin_at=datetime(2026, 7, 17, 9, 0),
        process_duration=3.5,
        suffix="pdf",
        type="pdf",
        source_type="local/file",
        to_dict=lambda: {"id": "doc-1"},
    )


def _stub_log_storage(monkeypatch: pytest.MonkeyPatch) -> tuple[Session, list[dict]]:
    db = Session()
    query = MagicMock()
    query.return_value.filter.return_value.scalar.return_value = 0
    monkeypatch.setattr(db, "query", query)
    saved: list[dict] = []

    def fake_save(_db: Session, **kwargs) -> PipelineOperationLog:
        saved.append(kwargs)
        return PipelineOperationLog(**kwargs)

    monkeypatch.setattr(PipelineOperationLogService, "save", fake_save)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", lambda _db, _kb_id: SimpleNamespace(tenant_id="tenant-1"))
    return db, saved


def test_special_task_log_uses_task_progress_and_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    db, saved = _stub_log_storage(monkeypatch)
    document = _document(progress=0.25)
    task_begin_at = datetime(2026, 7, 17, 10, 30)
    task = SimpleNamespace(
        progress=1,
        progress_msg="GraphRAG done",
        begin_at=task_begin_at,
        process_duration=42.0,
    )
    document_updates: list[list[dict]] = []
    kb_updates: list[tuple[str, dict]] = []

    monkeypatch.setattr(DocumentService, "get_by_id", lambda _db, document_id: document if document_id == "doc-1" else None)
    monkeypatch.setattr(DocumentService, "update_progress_immediately", lambda _db, documents: document_updates.append(documents))
    monkeypatch.setattr(TaskService, "get_by_id", lambda _db, task_id: task if task_id == "task-1" else None)
    monkeypatch.setattr(KnowledgebaseService, "update_by_id", lambda _db, kb_id, values: kb_updates.append((kb_id, values)))

    PipelineOperationLogService.create(
        db,
        document_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        pipeline_id="",
        task_type=PipelineTaskType.GRAPH_RAG,
        task_id="task-1",
        referred_document_id="doc-1",
    )

    assert document_updates == []
    assert len(saved) == 1
    assert saved[0]["document_id"] == GRAPH_RAPTOR_FAKE_DOC_ID
    assert saved[0]["pipeline_title"] == PipelineTaskType.GRAPH_RAG
    assert saved[0]["document_name"] == PipelineTaskType.GRAPH_RAG
    assert saved[0]["operation_status"] == TaskStatus.DONE
    assert saved[0]["progress"] == 1
    assert saved[0]["progress_msg"] == "GraphRAG done"
    assert saved[0]["process_begin_at"] == task_begin_at
    assert saved[0]["process_duration"] == 42.0
    assert kb_updates == [("kb-1", {"graphrag_task_finish_at": datetime(2026, 7, 17, 10, 30, 42)})]


def test_regular_task_log_is_created_after_progress_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    db, saved = _stub_log_storage(monkeypatch)
    document = _document(progress=0.5)
    document_updates: list[list[dict]] = []

    monkeypatch.setattr(DocumentService, "get_by_id", lambda _db, _document_id: document)
    monkeypatch.setattr(DocumentService, "update_progress_immediately", lambda _db, documents: document_updates.append(documents))

    PipelineOperationLogService.create(
        db,
        document_id="doc-1",
        pipeline_id="",
        task_type=PipelineTaskType.PARSE,
    )

    assert document_updates == [[{"id": "doc-1"}]]
    assert len(saved) == 1
    assert saved[0]["progress"] == 0.5
    assert saved[0]["document_name"] == "source.pdf"
