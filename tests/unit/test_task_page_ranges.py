"""Task boundaries retain every selected PDF page."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from api.db import FileType
from api.db.services import task_service
from common.constants import MAXIMUM_TASK_PAGE_NUMBER


def _queued_pdf_ranges(monkeypatch: Any, *, page_count: int, selected_pages: list[tuple[int, int]] | None = None) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    monkeypatch.setattr(task_service.PdfParser, "total_page_number", lambda _name, _blob: page_count)
    monkeypatch.setattr(
        task_service,
        "settings",
        SimpleNamespace(STORAGE_IMPL=SimpleNamespace(get=lambda _bucket, _name: b"pdf"), get_svr_queue_name=lambda _priority: "test-queue"),
    )
    monkeypatch.setattr(task_service.DocumentService, "get_chunking_config", lambda _db, _doc_id: {"tenant_id": "tenant", "name": "kb", "kb_id": "kb"})
    monkeypatch.setattr(task_service.TaskService, "get_tasks", lambda _db, _doc_id: [])
    monkeypatch.setattr(task_service.DocumentService, "update_by_id", lambda *_args: None)
    monkeypatch.setattr(task_service.DocumentService, "begin2parse", lambda *_args: None)
    monkeypatch.setattr(task_service, "bulk_insert_into_db", lambda _db, _model, rows, _replace: tasks.extend(deepcopy(rows)))
    monkeypatch.setattr(task_service, "REDIS_CONN", SimpleNamespace(queue_product=lambda *_args, **_kwargs: True))

    parser_config: dict[str, Any] = {"layout_recognize": "DeepDOC"}
    if selected_pages is not None:
        parser_config["pages"] = selected_pages
    document = {"id": "doc", "name": "document.pdf", "type": FileType.PDF.value, "parser_id": "naive", "parser_config": parser_config}
    with Session() as db:
        task_service.queue_tasks(db, document, "bucket", "document.pdf", 0)
    return tasks


def test_default_task_ranges_reach_page_302(monkeypatch: Any) -> None:
    tasks = _queued_pdf_ranges(monkeypatch, page_count=302)
    assert tasks[0]["from_page"] == 0
    assert tasks[-1]["from_page"] == 300
    assert tasks[-1]["to_page"] == 302
    assert all(task["to_page"] - task["from_page"] <= 12 for task in tasks)


def test_explicit_page_range_and_short_document(monkeypatch: Any) -> None:
    selected = _queued_pdf_ranges(monkeypatch, page_count=302, selected_pages=[(301, 303)])
    assert [(task["from_page"], task["to_page"]) for task in selected] == [(300, 302)]

    short = _queued_pdf_ranges(monkeypatch, page_count=2)
    assert [(task["from_page"], task["to_page"]) for task in short] == [(0, 2)]


def test_non_page_task_default_uses_distinct_marker() -> None:
    assert task_service.Task.__table__.c.to_page.default.arg == MAXIMUM_TASK_PAGE_NUMBER
