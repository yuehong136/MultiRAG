"""Missing task language falls back at both direct chunk and worker entry."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from core.app import naive
from core.svr import task_executor


@pytest.mark.parametrize("language", [None, "missing", "", "English", "Chinese"])
async def test_direct_worker_chunk_language(db: Session, monkeypatch: pytest.MonkeyPatch, language: str | None) -> None:
    captured: list[str] = []

    def chunk(*args: Any, lang: str, **kwargs: Any) -> list[Any]:
        captured.append(lang)
        return []

    monkeypatch.setitem(task_executor.FACTORY, "naive", SimpleNamespace(chunk=chunk))
    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", lambda *args, **kwargs: ("bucket", "key"))
    monkeypatch.setattr(task_executor, "get_storage_binary", AsyncMock(return_value=b"text"))
    task = {
        "id": "task",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "size": 4,
        "parser_id": "naive",
        "location": "key",
        "name": "source.txt",
        "from_page": 0,
        "to_page": 1,
        "parser_config": {},
        "kb_parser_config": {},
    }
    if language != "missing":
        task["language"] = language
    assert await task_executor.build_chunks(task, lambda *args, **kwargs: None, db) == []
    assert captured == ["Chinese" if language in (None, "missing") else language]


@pytest.mark.parametrize("language,english", [(None, False), ("", False), ("English", True), ("Chinese", False)])
def test_naive_chunk_accepts_missing_language(monkeypatch: pytest.MonkeyPatch, language: str | None, english: bool) -> None:
    captured: list[bool] = []

    def tokenize(chunks: Any, doc: Any, is_english: bool, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        captured.append(is_english)
        return [{"content_with_weight": "source"}]

    monkeypatch.setattr(naive, "tokenize_chunks", tokenize)
    chunks = naive.chunk("source.txt", binary=b"source", lang=language, callback=lambda *args, **kwargs: None)
    assert chunks and captured == [english]
