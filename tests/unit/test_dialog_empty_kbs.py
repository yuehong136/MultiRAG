"""Empty selections terminate before model or retrieval work; access stays strict."""

from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services import dialog_service


@pytest.fixture
def no_models(monkeypatch: pytest.MonkeyPatch) -> Mock:
    unexpected = Mock(side_effect=AssertionError("Empty or unauthorized selections cannot construct models"))
    for name in ["_resolve_model_config", "get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "detached_bundle", "label_question"]:
        monkeypatch.setattr(dialog_service, name, unexpected)
    return unexpected


@pytest.mark.parametrize(("selected", "message"), [([], "No KB selected"), (["stale"], "The selected KB is not valid")])
async def test_async_ask_empty_or_stale_selection_has_one_final_event(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, no_models: Mock, selected: list[str], message: str) -> None:
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", classmethod(lambda *args: True))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", classmethod(lambda *args: []))
    events = [event async for event in dialog_service.async_ask(async_db, "question", selected, "owner")]
    assert events == [{"answer": "**ERROR**: " + message, "reference": {}, "final": True}]
    no_models.assert_not_called()


async def test_effective_empty_search_selection_overrides_request_kbs(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, no_models: Mock) -> None:
    accesses: list[str] = []

    def accessible(cls: Any, db: Any, kb_id: str, owner: str) -> bool:
        accesses.append(kb_id)
        return True

    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", classmethod(accessible))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", classmethod(lambda *args: []))
    events = [event async for event in dialog_service.async_ask(async_db, "question", ["ignored"], "owner", search_config={"kb_ids": []})]
    assert events == [{"answer": "**ERROR**: No KB selected", "reference": {}, "final": True}]
    assert accesses == []
    no_models.assert_not_called()


@pytest.mark.parametrize("selected", [["foreign"], ["owned", "foreign"], ["foreign", "owned"]])
async def test_unauthorized_selection_fails_before_loading_any_kb(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, no_models: Mock, selected: list[str]) -> None:
    read = Mock(side_effect=AssertionError("Authorization must precede dataset loading"))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", classmethod(lambda cls, db, identifier, owner: identifier == "owned"))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", read)
    with pytest.raises(PermissionError, match="No authorization"):
        _ = [event async for event in dialog_service.async_ask(async_db, "question", selected, "owner")]
    read.assert_not_called()
    no_models.assert_not_called()
