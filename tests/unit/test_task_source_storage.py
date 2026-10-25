"""Missing objects are distinct from empty bytes and transport failures."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from core.svr import task_executor


@pytest.mark.parametrize("payload", [b"", b"hello"])
async def test_strict_source_reader_preserves_bytes(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    monkeypatch.setattr(task_executor.settings, "STORAGE_IMPL", SimpleNamespace(get_bytes=lambda *_: payload))
    assert await task_executor.get_storage_binary("bucket", "key") == payload


@pytest.mark.parametrize("missing", [True, False])
async def test_source_reader_missing_and_failed_reads_are_safe(monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    def read(*args: Any) -> None:
        if not missing:
            raise ValueError("https://user:secret@private/bucket/key")
        return None

    monkeypatch.setattr(task_executor.settings, "STORAGE_IMPL", SimpleNamespace(get_bytes=read))
    with pytest.raises(FileNotFoundError if missing else RuntimeError) as failure:
        await task_executor.get_storage_binary("private-bucket", "secret-key")
    assert "private" not in str(failure.value) and "secret" not in str(failure.value)


@pytest.mark.parametrize("missing", [True, False])
async def test_build_missing_source_never_calls_parser_or_outline(db: Session, monkeypatch: pytest.MonkeyPatch, missing: bool) -> None:
    task = {"id": "task", "doc_id": "doc", "name": "doc.pdf", "location": "private", "size": 0, "parser_id": "naive"}
    parser = SimpleNamespace(chunk=lambda *args, **kwargs: pytest.fail("missing source entered parser"))
    monkeypatch.setitem(task_executor.FACTORY, "naive", parser)
    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", lambda *args, **kwargs: ("private", "secret"))
    monkeypatch.setattr(task_executor, "get_storage_binary", AsyncMock(side_effect=FileNotFoundError("Source file not found.") if missing else RuntimeError("Object storage read failed.")))
    monkeypatch.setattr(task_executor, "extract_pdf_outlines", lambda *_: pytest.fail("missing source entered PDF recovery"))
    progress: list[tuple[Any, ...]] = []
    with pytest.raises(FileNotFoundError if missing else RuntimeError):
        await task_executor.build_chunks(task, lambda *args: progress.append(args), db)
    assert len(progress) == 1 and progress[0][0] == -1
    assert "private" not in str(progress) and "secret" not in str(progress)
    assert not await task_executor._persist_pdf_outline_from_storage(db, "doc", "doc.pdf")


@pytest.mark.parametrize("timeout_type", [TimeoutError, task_executor.ProcessTimeoutError])
async def test_storage_timeout_keeps_timeout_semantics(monkeypatch: pytest.MonkeyPatch, timeout_type: type[Exception]) -> None:
    def read(*args: Any) -> None:
        raise timeout_type("https://user:secret@private/key")

    monkeypatch.setattr(task_executor.settings, "STORAGE_IMPL", SimpleNamespace(get_bytes=read))
    with pytest.raises(TimeoutError, match=r"^Object storage read timed out[.]$"):
        await task_executor.get_storage_binary("private", "key")
