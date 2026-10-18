"""SQL scope validation must precede every model and storage side effect."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from beartype.roar import BeartypeCallHintParamViolation

from api.db.services import dialog_service
from common import resources

KB_ID = "abcdef0123456789abcdef0123456789"
ENGINES = ["infinity", "elasticsearch", "oceanbase", "milvus"]


@pytest.fixture
def sql_boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    table = {"columns": [{"name": "doc_id"}, {"name": "docnm_kwd"}, {"name": "value"}], "rows": [["doc", "title", 7]]}
    retrieve = Mock(return_value=table)
    model = SimpleNamespace(async_chat=AsyncMock(return_value="SELECT doc_id, docnm_kwd, value FROM chunks"))
    index = Mock(return_value="chunks")
    monkeypatch.setattr(dialog_service, "index_name", index)
    monkeypatch.setitem(resources._state, "retriever", SimpleNamespace(sql_retrieval=retrieve))
    return SimpleNamespace(retrieve=retrieve, model=model, index=index)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("invalid", ["", "not-a-uuid", KB_ID + "' OR '1'='1", KB_ID + "; DROP TABLE chunks", " " + KB_ID, None, 1])
@pytest.mark.parametrize("mixed", [False, True])
async def test_invalid_scope_fails_before_model_or_sql(sql_boundary: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str, invalid: Any, mixed: bool) -> None:
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    kb_ids = [KB_ID, invalid] if mixed else [invalid]
    # beartype may reject a non-string at the public boundary before the body.
    errors = ValueError if isinstance(invalid, str) else (ValueError, BeartypeCallHintParamViolation)
    with pytest.raises(errors) as error:
        await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["dataset"], sql_boundary.model, kb_ids=kb_ids)
    if isinstance(error.value, ValueError):
        assert str(error.value) == "Invalid kb_id format: expected a UUID string"
    sql_boundary.index.assert_not_called()
    sql_boundary.model.async_chat.assert_not_awaited()
    sql_boundary.retrieve.assert_not_called()


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("kb_id", [KB_ID, KB_ID.upper(), "abcdef01-2345-6789-abcd-ef0123456789", "{abcdef01-2345-6789-abcd-ef0123456789}", "urn:uuid:abcdef01-2345-6789-abcd-ef0123456789"])
async def test_uuid_representations_preserve_stored_identity(sql_boundary: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str, kb_id: str) -> None:
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    result = await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["dataset"], sql_boundary.model, kb_ids=[kb_id])
    assert result is not None
    assert result["reference"]["chunks"][0]["kb_id"] == kb_id
    if engine == "infinity":
        prompt = sql_boundary.model.async_chat.call_args.args[1][0]["content"]
        assert f"Table: chunks_{kb_id}" in prompt
    else:
        assert f"WHERE kb_id = '{kb_id}'" in sql_boundary.retrieve.call_args.args[0]


@pytest.mark.parametrize("engine", ENGINES)
async def test_scope_is_snapshotted_before_model_await(sql_boundary: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, engine: str) -> None:
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", engine)
    kb_ids = [KB_ID]

    async def chat(*args: Any, **kwargs: Any) -> str:
        kb_ids[0] = "invalid' injected"
        return "SELECT doc_id, docnm_kwd, value FROM chunks"

    sql_boundary.model.async_chat.side_effect = chat
    result = await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["dataset"], sql_boundary.model, kb_ids=kb_ids)
    assert result is not None
    assert result["reference"]["chunks"][0]["kb_id"] == KB_ID
    assert "injected" not in sql_boundary.retrieve.call_args.args[0]


@pytest.mark.parametrize("kb_ids", [None, []])
async def test_absent_scope_keeps_existing_behavior(sql_boundary: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, kb_ids: list[str] | None) -> None:
    monkeypatch.setattr(dialog_service.settings, "DOC_ENGINE", "elasticsearch")
    result = await dialog_service.use_sql("show values", {"value": "Value"}, "tenant", ["dataset"], sql_boundary.model, kb_ids=kb_ids)
    assert result is not None
    assert "WHERE kb_id" not in sql_boundary.retrieve.call_args.args[0]
