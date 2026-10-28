"""Saved and ad-hoc searches resolve chat defaults within the requesting tenant."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services import dialog_service
from common import settings


@pytest.mark.parametrize(
    ("config", "argument", "selected"),
    [({}, None, None), ({"chat_id": None}, None, None), ({"chat_id": ""}, None, None), ({}, "17", "17"), ({"chat_id": "23"}, "17", "23")],
)
async def test_search_stream_selects_explicit_model_or_tenant_default(monkeypatch: pytest.MonkeyPatch, config: dict[str, Any], argument: str | None, selected: str | None) -> None:
    async def chunks(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        yield "<think>reason</think>Visible answer"

    db = AsyncSession()
    monkeypatch.setattr(db, "run_sync", AsyncMock(side_effect=lambda callback: callback(db.sync_session)))
    kb = SimpleNamespace(id="selected", tenant_id="dataset-owner", name="Selected", parser_id="naive", tenant_embd_id=9, embd_id="")
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", lambda *_: True)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", lambda _db, ids: [kb] if ids == ["selected"] else [])
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "ensure_same_embedding_model", lambda _: None)
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda *_: {"mdl_type": "embedding"})
    default = Mock(return_value={"mdl_type": "chat", "id": 11})
    explicit = Mock(return_value={"mdl_type": "chat", "id": 23})
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", default)
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", explicit)
    chat = SimpleNamespace(max_length=4096, async_chat_streamly_delta=chunks)
    monkeypatch.setattr(dialog_service, "detached_bundle", lambda _db, _tenant, model: chat if model["mdl_type"] == "chat" else object())
    retrieve = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setattr(settings, "retriever", SimpleNamespace(retrieval=retrieve, insert_citations=lambda answer, *_a, **_k: (answer, [])))
    monkeypatch.setattr(dialog_service, "label_question", lambda *_: {})
    monkeypatch.setattr(dialog_service, "enrich_reference_metadata_async", AsyncMock())
    effective = {"kb_ids": ["selected"], "doc_ids": [], "similarity_threshold": 0, "vector_similarity_weight": 0, **config}
    try:
        emitted = [item async for item in dialog_service.async_ask(db, "Question", ["ignored"], "request-tenant", argument, effective)]
    finally:
        await db.close()
    if selected is None:
        default.assert_called_once_with(db.sync_session, "request-tenant", "chat")
        explicit.assert_not_called()
    else:
        explicit.assert_called_once_with(db.sync_session, "request-tenant", "chat", selected)
        default.assert_not_called()
    assert emitted[-1]["final"] is True and emitted[-1]["answer"].endswith("Visible answer")
    assert retrieve.call_args.kwargs["kb_ids"] == ["selected"]
    assert retrieve.call_args.kwargs["doc_ids"] == []
    assert retrieve.call_args.kwargs["similarity_threshold"] == 0
    assert retrieve.call_args.kwargs["vector_similarity_weight"] == 0


async def test_denied_dataset_never_resolves_default_or_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    db = AsyncSession()
    monkeypatch.setattr(db, "run_sync", AsyncMock(side_effect=lambda callback: callback(db.sync_session)))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", lambda *_: False)
    forbidden = Mock(side_effect=AssertionError("Unauthorized search reached a model"))
    for name in ("get_tenant_default_model_by_type", "get_model_config_by_type_and_name", "detached_bundle"):
        monkeypatch.setattr(dialog_service, name, forbidden)
    try:
        with pytest.raises(PermissionError, match="No authorization"):
            await anext(dialog_service.async_ask(db, "Question", ["foreign"], "request-tenant"))
    finally:
        await db.close()
    forbidden.assert_not_called()


@pytest.mark.parametrize("reference", [False, 0, [], {}, 23])
async def test_invalid_saved_model_reference_never_falls_back_or_loads_resources(monkeypatch: pytest.MonkeyPatch, reference: Any) -> None:
    db = AsyncSession()
    forbidden = AsyncMock(side_effect=AssertionError("Invalid saved model reached SQL or a model"))
    monkeypatch.setattr(db, "run_sync", forbidden)
    try:
        with pytest.raises(ValueError, match="string reference"):
            await anext(dialog_service.async_ask(db, "Question", ["selected"], "request-tenant", search_config={"chat_id": reference}))
    finally:
        await db.close()
    forbidden.assert_not_awaited()
