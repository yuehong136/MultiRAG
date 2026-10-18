"""Run generated scope SQL through Dealer against an owned PostgreSQL table.

This verifies portable SQL and independent state readback. It does not claim a
live Infinity/ES/OceanBase transport run; only the storage transport is adapted.
"""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.services import dialog_service
from common import resources
from core.nlp.search import Dealer


@pytest.fixture
def sql_scope(pg_scratch_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    kb_ids = [uuid4().hex, str(uuid4()), uuid4().hex]
    table = "sql_scope_" + uuid4().hex
    infinity_table = f"{table}_{kb_ids[0]}"
    statements: list[str] = []
    with pg_scratch_engine.begin() as connection:
        for name, ids in [(table, kb_ids), (infinity_table, kb_ids[:1])]:
            connection.execute(sa.text(f"CREATE TABLE {name} (doc_id text, docnm_kwd text, docnm text, kb_id text, value integer)"))
            connection.execute(
                sa.text(f"INSERT INTO {name} VALUES (:doc, :title, :title, :kb, :value)"),
                [{"doc": f"doc-{index}", "title": f"source-{index}", "kb": kb, "value": index + 1} for index, kb in enumerate(ids)],
            )

    def execute(sql: str, fetch_size: int, format: str) -> dict[str, Any]:
        assert format == "json"
        statements.append(sql)
        with pg_scratch_engine.begin() as connection:
            result = connection.execute(sa.text(sql))
            return {"columns": [{"name": name} for name in result.keys()], "rows": [list(row) for row in result.fetchmany(fetch_size)]}

    retriever = object.__new__(Dealer)
    retriever.dataStore = SimpleNamespace(sql=execute)
    monkeypatch.setitem(resources._state, "retriever", retriever)
    monkeypatch.setattr(dialog_service, "index_name", lambda *a: table)
    try:
        yield SimpleNamespace(engine=pg_scratch_engine, kb_ids=kb_ids, table=table, infinity_table=infinity_table, statements=statements)
    finally:
        with pg_scratch_engine.begin() as connection:
            for name in [table, infinity_table]:
                connection.execute(sa.text(f"DROP TABLE {name}"))
        with pg_scratch_engine.connect() as reader:
            assert reader.scalar(sa.text("SELECT to_regclass(:name)"), {"name": table}) is None
            assert reader.scalar(sa.text("SELECT to_regclass(:name)"), {"name": infinity_table}) is None


def read_rows(env: SimpleNamespace) -> list[tuple[Any, ...]]:
    with env.engine.connect() as reader:
        return [tuple(row) for row in reader.execute(sa.text(f"SELECT * FROM {env.table} ORDER BY doc_id"))]


@pytest.mark.parametrize("engine", ["infinity", "elasticsearch", "oceanbase"])
@pytest.mark.parametrize("mixed", [False, True])
async def test_rejected_scope_never_executes_sql_and_preserves_rows(sql_scope: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str, mixed: bool) -> None:
    env = sql_scope
    before = read_rows(env)
    attack = env.kb_ids[0] + "' OR '1'='1'; DELETE FROM " + env.table + "; --"
    model = SimpleNamespace(async_chat=AsyncMock(return_value=f"SELECT doc_id, docnm_kwd, value FROM {env.table}"))
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    with pytest.raises(ValueError, match="Invalid kb_id format"):
        await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["dataset"], model, kb_ids=[env.kb_ids[0], attack] if mixed else [attack])
    model.async_chat.assert_not_awaited()
    assert env.statements == []
    assert read_rows(env) == before


@pytest.mark.parametrize("engine", ["infinity", "elasticsearch", "oceanbase"])
@pytest.mark.parametrize("aggregate", [False, True])
async def test_valid_scope_executes_and_retains_source_identity(sql_scope: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str, aggregate: bool) -> None:
    env = sql_scope
    before = read_rows(env)
    table = env.infinity_table if engine == "infinity" else env.table
    name_column = "docnm" if engine == "infinity" else "docnm_kwd"
    query = f"SELECT COUNT(*) AS rows FROM {table}" if aggregate else f"SELECT doc_id, {name_column}, value FROM {table}"
    model = SimpleNamespace(async_chat=AsyncMock(return_value=query))
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    answer = await dialog_service.use_sql("how many rows in dataset" if aggregate else "show values", {"value": "Value"}, "tenant", ["dataset"], model, kb_ids=env.kb_ids[:1])
    assert answer is not None and answer["answer"]
    assert env.statements
    if not aggregate or engine != "infinity":
        assert answer["reference"]["chunks"] == [{"doc_id": "doc-0", "docnm_kwd": "source-0", "kb_id": env.kb_ids[0]}]
    assert read_rows(env) == before


@pytest.mark.parametrize("engine", ["elasticsearch", "oceanbase"])
async def test_multiple_uuid_representations_exclude_foreign_scope(sql_scope: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str) -> None:
    env = sql_scope
    before = read_rows(env)
    model = SimpleNamespace(async_chat=AsyncMock(return_value=f"SELECT doc_id, docnm_kwd, kb_id, value FROM {env.table}"))
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    answer = await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["one", "two"], model, kb_ids=env.kb_ids[:2])
    assert answer is not None
    assert {chunk["kb_id"] for chunk in answer["reference"]["chunks"]} == set(env.kb_ids[:2])
    assert {chunk["doc_id"] for chunk in answer["reference"]["chunks"]} == {"doc-0", "doc-1"}
    assert read_rows(env) == before
