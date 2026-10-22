"""Column roles separate indexed text from exact metadata across backends."""

from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.utils.validation_utils import ParserConfig
from core.app import table
from core.svr import task_executor
from core.utils.table_metadata import TABLE_METADATA, TABLE_ROW_KEY, collect_table_metadata, merge_table_parser_config_from_kb, replace_owned_metadata


@pytest.fixture
def parser(monkeypatch: pytest.MonkeyPatch, db: Session) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []

    @contextmanager
    def connection() -> Iterator[Session]:
        yield db

    monkeypatch.setattr(table, "db_connection", connection)
    monkeypatch.setattr(table.KnowledgebaseService, "update_table_parser_fields", lambda db, kb, fields, columns: updates.append({"field_map": fields, "table_column_names": columns}))
    return updates


@pytest.mark.parametrize("engine", ["milvus", "infinity", "oceanbase", "elasticsearch"])
@pytest.mark.parametrize("mode", [None, "auto", "manual"])
def test_roles_store_raw_values_and_only_index_requested_columns(parser: Any, monkeypatch: pytest.MonkeyPatch, engine: str, mode: str | None) -> None:
    monkeypatch.setattr(table.settings, "DOC_ENGINE", engine)
    config = {"table_column_mode": mode, "table_column_roles": {"title": "indexing", "country": "metadata", "score": "both"}}
    rows = table.chunk("news.csv", binary=b"title,country,score\nStory,Brazil,0\nOther,,2\n", table_metadata_carrier=True, callback=lambda *a: None, parser_config=config, kb_id="kb")
    assert len(rows) == 2
    assert "Story" in rows[0]["content_with_weight"]
    assert ("Brazil" in rows[0]["content_with_weight"]) is (mode != "manual")
    assert ("title" in rows[0][TABLE_METADATA]) is (mode != "manual")
    assert rows[0][TABLE_METADATA]["score"] == 0
    assert "country" not in rows[1][TABLE_METADATA]
    if engine in {"milvus", "infinity", "oceanbase"}:
        assert rows[0]["chunk_data"] == rows[0][TABLE_METADATA]
    else:
        assert rows[0]["table_country_raw"] == "Brazil"
        assert ("table_title_tks" in parser[-1]["field_map"]) is (mode != "manual")
    assert parser[-1]["table_column_names"] == ["title", "country", "score"]
    assert ("title" in parser[-1]["field_map"].values()) is (mode != "manual")
    metadata = collect_table_metadata(rows)
    assert metadata["score"] == [0, 2]
    assert metadata["country"] == ["Brazil"]
    assert all(TABLE_METADATA not in row for row in rows)


def test_multisheet_union_lossless_mixed_types_and_single_update(parser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table.settings, "DOC_ENGINE", "elasticsearch")
    book = Workbook()
    first = book.active
    assert first is not None
    first.append(["title", "value"])
    first.append(["One", 2])
    second = book.create_sheet("Second")
    second.append(["value", "notes"])
    second.append(["not a number", "Keep, entire; cell"])
    binary = BytesIO()
    book.save(binary)
    rows = table.chunk("sheets.xlsx", binary=binary.getvalue(), table_metadata_carrier=True, callback=lambda *a: None, kb_id="kb")
    assert len(rows) == 2 and len(parser) == 1
    assert parser[0]["table_column_names"] == ["title", "value", "notes"]
    assert parser[0]["field_map"] == {"table_title_tks": "title", "table_value_tks": "value", "table_notes_tks": "notes"}
    assert collect_table_metadata(rows) == {"title": ["One"], "value": ["2", "not a number"], "notes": ["Keep, entire; cell"]}


def test_duplicate_and_normalized_colliding_columns_fail(parser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table.settings, "DOC_ENGINE", "elasticsearch")
    for csv in [b"name,name\na,b\n", b"name/a,name/b\na,b\n"]:
        with pytest.raises(ValueError):
            table.chunk("collision.csv", binary=csv, table_metadata_carrier=True, callback=lambda *a: None, kb_id="kb")
    assert parser == []


def test_legacy_roles_and_invalid_values() -> None:
    assert ParserConfig(table_column_roles={"title": "vectorize"}).table_column_roles == {"title": "indexing"}
    for value in ["unknown", 1, False, None]:
        with pytest.raises(ValidationError):
            ParserConfig(table_column_roles={"title": value})
    assert merge_table_parser_config_from_kb({"parser_id": "table", "parser_config": {"auto_keywords": 3, "table_column_mode": "auto"}, "kb_parser_config": {"table_column_mode": "manual"}}) == {
        "auto_keywords": 3,
        "table_column_mode": "manual",
    }


def test_owned_values_only_removed_and_exact_json_types_preserved() -> None:
    result, owned = replace_owned_metadata(
        {"owner": "Ada", "country": ["manual", "Brazil"], "edited": "changed", "number": [0]}, {"country": ["Brazil"], "edited": ["old"]}, [{"country": ["Japan"], "number": [False, 0]}]
    )
    assert result == {"owner": "Ada", "country": ["manual", "Japan"], "edited": "changed", "number": [0, False]}
    assert owned == {"country": ["Japan"], "number": [False]}


@pytest.mark.parametrize("cached", [True, False])
async def test_real_build_chunks_merges_kb_config_consumes_carriers_and_cached_keywords(parser: Any, monkeypatch: pytest.MonkeyPatch, db: Session, cached: bool) -> None:
    binary = b"title,country\nFirst,Brazil\nSecond,Japan\n"
    task: dict[str, Any] = {
        "id": "task",
        "doc_id": "doc",
        "kb_id": "kb",
        "tenant_id": "tenant",
        "name": "news.csv",
        "location": "news.csv",
        "size": len(binary),
        "from_page": 0,
        "to_page": 10,
        "language": "English",
        "parser_id": "table",
        "llm_id": "fake",
        "parser_config": {"auto_keywords": 2},
        "kb_parser_config": {"table_column_mode": "manual", "table_column_roles": {"title": "metadata", "country": "metadata"}},
    }

    async def read(*a: Any) -> bytes:
        return binary

    async def keywords(*a: Any) -> str:
        assert not cached
        return " 功能，配置; 参数；问题、排查\r\n, ; "

    monkeypatch.setattr(task_executor.File2DocumentService, "get_storage_address", lambda *a, **kw: ("scratch", "news.csv"))
    monkeypatch.setattr(task_executor, "get_storage_binary", read)
    monkeypatch.setattr(task_executor, "has_canceled", lambda *a: False)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *a: {})
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *a, **kw: type("Model", (), {"llm_name": "controlled"})())
    monkeypatch.setattr(task_executor, "get_llm_cache", lambda *a: " 功能，配置; 参数；问题、排查\r\n, ; " if cached else None)
    monkeypatch.setattr(task_executor, "set_llm_cache", lambda *a: None)
    monkeypatch.setattr(task_executor, "keyword_extraction", keywords)
    rows = await task_executor.build_chunks(task, lambda *a, **kw: None, db)
    assert len({row["pk"] for row in rows}) == 2
    assert all(row["content_with_weight"] == "" for row in rows)
    assert all(row["important_kwd"] == ["功能", "配置", "参数", "问题", "排查"] for row in rows)
    assert all(TABLE_METADATA not in row and TABLE_ROW_KEY not in row for row in rows)
    assert task["table_metadata"] == {"title": ["First", "Second"], "country": ["Brazil", "Japan"]}


async def test_dataflow_keywords_use_the_same_delimiters_and_whitespace_filter(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api.db.services.canvas_service import UserCanvasService
    from core.flow import pipeline as pipeline_module

    class Pipeline:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def run(self) -> dict[str, Any]:
            return {"chunks": [{"text": "Pipeline body", "keywords": " 功能，配置; 参数；问题、排查\r\n, ; ", "q_768_vec": [0.2] * 768}], "embedding_token_consumption": 0}

    inserted: list[dict[str, Any]] = []

    async def insert(*args: Any) -> bool:
        inserted.extend(args[4])
        return True

    monkeypatch.setattr(pipeline_module, "Pipeline", Pipeline)
    monkeypatch.setattr(UserCanvasService, "get_by_id", lambda *a: SimpleNamespace(dsl="{}"))
    monkeypatch.setattr(task_executor, "set_progress", lambda *a, **kw: None)
    monkeypatch.setattr(task_executor, "get_schema", AsyncMock(return_value={}))
    monkeypatch.setattr(task_executor, "insert_chunks", insert)
    monkeypatch.setattr(task_executor, "increment_task_document", lambda *a: None)
    monkeypatch.setattr(task_executor, "record_task_pipeline", lambda *a: None)
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *a: SimpleNamespace(name="scratch"))
    monkeypatch.setattr(db, "get_bind", lambda: None)
    task = {"id": "task", "doc_id": "doc", "kb_id": "kb", "tenant_id": "tenant", "dataflow_id": "pipeline", "task_type": "dataflow", "name": "pipeline.txt"}
    await task_executor.run_dataflow(db, task)
    assert len(inserted) == 1 and inserted[0]["important_kwd"] == ["功能", "配置", "参数", "问题", "排查"]
    assert inserted[0]["important_tks"] == task_executor.rag_tokenizer.tokenize("功能 配置 参数 问题 排查")
    assert "keywords" not in inserted[0]


def test_es_metadata_title_cannot_overwrite_search_title(parser: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(table.settings, "DOC_ENGINE", "elasticsearch")
    rows = table.chunk(
        "source.csv",
        binary=b"title,content\nSecret metadata,Public body\n",
        table_metadata_carrier=True,
        callback=lambda *a: None,
        parser_config={"table_column_mode": "manual", "table_column_roles": {"title": "metadata", "content": "indexing"}},
        kb_id="kb",
    )
    assert rows[0]["title_tks"] == table.rag_tokenizer.tokenize("source")
    assert rows[0]["table_title_raw"] == "Secret metadata"
    assert "Secret metadata" not in rows[0]["content_with_weight"]


@pytest.mark.parametrize(
    "column, expected",
    [
        (["001", "002"], (["001", "002"], "text")),
        (["0", "2"], ([0, 2], "int")),
        (["9223372036854775808"], (["9223372036854775808"], "text")),
        (["true", "false"], (["yes", "no"], "bool")),
        (["2026-01-01", "2026-01-02"], (["2026-01-01 00:00:00", "2026-01-02 00:00:00"], "datetime")),
    ],
)
def test_column_types_preserve_codes_falsy_values_and_large_integers(column: list[str], expected: Any) -> None:
    assert table.column_data_type(column) == expected


def test_direct_preview_does_not_expose_worker_metadata_carriers(parser: Any) -> None:
    rows = table.chunk("preview.csv", binary=b"title,country\nFirst,Brazil\n", callback=lambda *a: None)
    assert TABLE_METADATA not in rows[0] and TABLE_ROW_KEY not in rows[0]
