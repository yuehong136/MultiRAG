import logging
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from core.app import manual, naive
from core.svr import task_executor


def pdf_bytes(*, bookmarked: bool) -> bytes:
    content = BytesIO()
    pdf = canvas.Canvas(content)
    pdf.drawString(72, 720, "Chapter One content for indexing.")
    pdf.save()
    if not bookmarked:
        return content.getvalue()
    writer = PdfWriter()
    writer.append(BytesIO(content.getvalue()))
    parent = writer.add_outline_item("Chapter One", 0)
    writer.add_outline_item("Section One", 0, parent=parent)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.mark.parametrize("bookmarked", [True, False])
def test_naive_parser_passes_raw_pdf_outline_only_when_present(bookmarked: bool) -> None:
    chunks = naive.chunk(
        "outline.pdf",
        binary=pdf_bytes(bookmarked=bookmarked),
        callback=lambda *args, **kwargs: None,
        parser_config={"layout_recognize": "Plain Text", "chunk_token_num": 128, "delimiter": "\n", "analyze_hyperlink": False},
    )

    assert chunks
    expected = [{"title": "Chapter One", "depth": 0}, {"title": "Section One", "depth": 1}] if bookmarked else None
    assert chunks[0].get("__outline__") == expected


def test_manual_parser_passes_three_part_pdf_outline(monkeypatch: pytest.MonkeyPatch) -> None:
    def parse_pdf(**kwargs: Any) -> tuple[list[Any], list[Any], SimpleNamespace]:
        return [("Chapter One content for indexing.", "", [(0, 0.0, 1.0, 0.0, 1.0)])], [], SimpleNamespace()

    def tokenize_chunks(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"content_with_weight": "Chapter One content for indexing."}]

    monkeypatch.setitem(manual.PARSERS, "plain text", parse_pdf)
    monkeypatch.setattr(manual, "tokenize_chunks", tokenize_chunks)
    chunks = manual.chunk(
        "outline.pdf",
        binary=pdf_bytes(bookmarked=True),
        callback=lambda *args, **kwargs: None,
        parser_config={"layout_recognize": "Plain Text", "chunk_token_num": 128},
    )

    assert chunks[0]["__outline__"] == [{"title": "Chapter One", "depth": 0}, {"title": "Section One", "depth": 1}]


async def run_build_chunks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chunks: list[dict[str, Any]],
    extracted: list[tuple[str, int, int]],
    existing: dict[str, Any] | None = None,
    update_result: bool | Exception = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async def get_binary(bucket: str, name: str) -> bytes:
        return b"pdf bytes"

    async def run_chunker(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [dict(chunk) for chunk in chunks]

    def get_address(db: Session, doc_id: str) -> tuple[str, str]:
        return "scratch", "outline.pdf"

    def get_metadata(db: Session, doc_id: str) -> dict[str, Any]:
        return existing or {}

    saved: list[dict[str, Any]] = []

    def update_metadata(db: Session, doc_id: str, metadata: dict[str, Any]) -> bool:
        saved.append(metadata)
        if isinstance(update_result, Exception):
            raise update_result
        return update_result

    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", get_address)
    monkeypatch.setattr(task_executor, "get_storage_binary", get_binary)
    monkeypatch.setattr(task_executor, "thread_pool_exec", run_chunker)
    monkeypatch.setattr(task_executor, "extract_pdf_outlines", lambda binary: extracted)
    monkeypatch.setattr(task_executor.DocMetadataService, "get_document_metadata", get_metadata)
    monkeypatch.setattr(task_executor.DocMetadataService, "update_document_metadata", update_metadata)
    task = {
        "id": "task",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "parser_id": "book",
        "name": "outline.pdf",
        "location": "outline.pdf",
        "size": 9,
        "from_page": 0,
        "to_page": 1,
        "language": "English",
        "parser_config": {},
        "kb_parser_config": {},
    }
    with Session() as db:
        docs = await task_executor.build_chunks(task, lambda *args, **kwargs: None, db)
    return docs, saved


async def test_outline_merges_metadata_and_never_enters_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    outline = [{"title": "Chapter One", "depth": 0}]
    chunks = [
        {"content_with_weight": "one", "__outline__": outline},
        {"content_with_weight": "two", "__outline__": [{"title": "stray", "depth": 0}]},
    ]
    docs, saved = await run_build_chunks(monkeypatch, chunks=chunks, extracted=[], existing={"owner": "Ada", "labels": ["reviewed"]})

    assert len(docs) == 2
    assert all("__outline__" not in doc for doc in docs)
    assert saved == [{"owner": "Ada", "labels": ["reviewed"], "outline": outline}]


async def test_other_pdf_parser_falls_back_to_raw_bookmarks(monkeypatch: pytest.MonkeyPatch) -> None:
    docs, saved = await run_build_chunks(
        monkeypatch,
        chunks=[{"content_with_weight": "chapter"}],
        extracted=[("Chapter One", 0, 1), ("Section One", 1, 1)],
    )

    assert len(docs) == 1
    assert saved == [{"outline": [{"title": "Chapter One", "depth": 0}, {"title": "Section One", "depth": 1}]}]


async def test_no_bookmarks_do_not_write_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    docs, saved = await run_build_chunks(monkeypatch, chunks=[{"content_with_weight": "chapter"}], extracted=[])

    assert len(docs) == 1
    assert saved == []


async def test_dataflow_pdf_path_reads_bookmarks_and_merges_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_binary(bucket: str, name: str) -> bytes:
        return pdf_bytes(bookmarked=True)

    def get_address(db: Session, doc_id: str) -> tuple[str, str]:
        return "scratch", "outline.pdf"

    saved: list[dict[str, Any]] = []

    def update_metadata(db: Session, doc_id: str, metadata: dict[str, Any]) -> bool:
        saved.append(metadata)
        return True

    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", get_address)
    monkeypatch.setattr(task_executor, "get_storage_binary", get_binary)
    monkeypatch.setattr(task_executor.DocMetadataService, "get_document_metadata", lambda db, doc_id: {"source": "dataflow"})
    monkeypatch.setattr(task_executor.DocMetadataService, "update_document_metadata", update_metadata)
    with Session() as db:
        persisted = await task_executor._persist_pdf_outline_from_storage(db, "doc", "outline.pdf")

    assert persisted is True
    assert saved == [{"source": "dataflow", "outline": [{"title": "Chapter One", "depth": 0}, {"title": "Section One", "depth": 1}]}]


@pytest.mark.parametrize("update_result", [False, RuntimeError("storage unavailable")])
async def test_metadata_save_failure_is_not_logged_as_success(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    update_result: bool | Exception,
) -> None:
    with caplog.at_level(logging.INFO):
        docs, saved = await run_build_chunks(
            monkeypatch,
            chunks=[{"content_with_weight": "chapter", "__outline__": [{"title": "Chapter One", "depth": 0}]}],
            extracted=[],
            update_result=update_result,
        )

    assert len(docs) == 1
    assert saved
    assert "Failed to persist PDF outline" in caplog.text
    assert "Persisted PDF outline" not in caplog.text
