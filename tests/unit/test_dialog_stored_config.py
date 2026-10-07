"""Chats stored by earlier REST writes still reach retrieval and generation.

Before the chats API normalized writes, Studio stored ``search_mode`` in the
documented ``{"type": ...}`` shape and PUT stored ``llm_setting: null``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services import dialog_service
from api.db.services.llm_service import LLMBundle

_MESSAGES = [{"role": "user", "content": "question"}]
_STORED_MODES = [
    pytest.param({"type": "dense"}, {"dense": {}}, id="rest-dense"),
    pytest.param({"type": "hybrid", "weight_dense": 0.6, "weight_sparse": 0.4}, {"hybrid": {"weight_dense": 0.6, "weight_sparse": 0.4}}, id="rest-hybrid"),
    pytest.param({"sparse": {}}, {"sparse": {}}, id="legacy-keyed"),
    pytest.param(None, None, id="unset"),
]


class _GenerationReached(RuntimeError):
    """Stop the stubbed pipeline once retrieval has run."""


class _ChatBundle(LLMBundle):
    """Nominal LLMBundle instance required by the api package's beartype hook."""

    def __init__(self) -> None:
        self.db = None
        self.max_length = 1024

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        raise _GenerationReached

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        raise _GenerationReached


@pytest.fixture
def retrieval_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run both chat pipelines offline up to generation, recording retrieval kwargs."""
    calls: list[dict[str, Any]] = []
    kb = SimpleNamespace(id="kb-1", name="dataset", tenant_id="tenant-unit")
    embedding = SimpleNamespace(db=None)

    async def retrieval(*_args: Any, **kwargs: Any) -> dict[str, list[Any]]:
        calls.append(kwargs)
        return {"chunks": [], "doc_aggs": []}

    monkeypatch.setattr(dialog_service.TenantLLMService, "llm_id2llm_type", staticmethod(lambda _llm_id: "chat"))
    monkeypatch.setattr(dialog_service, "_resolve_dialog_primary_model_config", lambda _db, _dialog: {"max_tokens": 8192})
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", classmethod(lambda _cls, _db, tenant_id: None))
    monkeypatch.setattr(dialog_service, "get_models", lambda _db, _dialog: ([kb], embedding, None, _ChatBundle(), None))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", classmethod(lambda _cls, _db, _kb_ids: {}))
    monkeypatch.setattr(dialog_service, "label_question", lambda *_args: {})
    monkeypatch.setattr(dialog_service.settings, "retriever", SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, _tenant_ids: chunks))
    monkeypatch.setattr(dialog_service, "kb_prompt", lambda _kbinfos, _max_tokens: [])
    monkeypatch.setattr(dialog_service, "message_fit_in", lambda messages, _max_length: (1, messages))
    return calls


def _dialog(search_mode: dict[str, Any] | None) -> SimpleNamespace:
    return SimpleNamespace(
        kb_ids=["kb-1"],
        llm_id="chat-model",
        tenant_id="tenant-unit",
        llm_setting={},
        prompt_config={"system": "Use {knowledge}", "parameters": [{"key": "knowledge", "optional": False}]},
        meta_data_filter=None,
        top_n=6,
        similarity_threshold=0.2,
        vector_similarity_weight=0.3,
        search_mode=search_mode,
    )


@pytest.mark.parametrize(("stored", "expected"), _STORED_MODES)
async def test_async_chat_retrieves_with_keyed_search_mode(
    retrieval_calls: list[dict[str, Any]],
    async_db: AsyncSession,
    stored: dict[str, Any] | None,
    expected: dict[str, Any] | None,
) -> None:
    with pytest.raises(_GenerationReached):
        async for _answer in dialog_service.async_chat(_dialog(stored), _MESSAGES, async_db, stream=False):
            pass

    assert [call["search_mode"] for call in retrieval_calls] == [expected]


@pytest.mark.parametrize(("stored", "expected"), _STORED_MODES)
def test_chat_retrieves_with_keyed_search_mode(
    retrieval_calls: list[dict[str, Any]],
    db: Session,
    stored: dict[str, Any] | None,
    expected: dict[str, Any] | None,
) -> None:
    with pytest.raises(_GenerationReached):
        list(dialog_service.chat(_dialog(stored), _MESSAGES, db, stream=False))

    assert [call["search_mode"] for call in retrieval_calls] == [expected]


async def test_async_chat_rejects_unusable_stored_search_mode(retrieval_calls: list[dict[str, Any]], async_db: AsyncSession) -> None:
    with pytest.raises(ValueError):
        async for _answer in dialog_service.async_chat(_dialog({"type": "keyword"}), _MESSAGES, async_db, stream=False):
            pass

    assert retrieval_calls == []


async def test_async_chat_generates_with_null_llm_setting(retrieval_calls: list[dict[str, Any]], async_db: AsyncSession) -> None:
    dialog = _dialog(None)
    dialog.llm_setting = None

    with pytest.raises(_GenerationReached):
        async for _answer in dialog_service.async_chat(dialog, _MESSAGES, async_db, stream=False):
            pass


def test_chat_generates_with_null_llm_setting(retrieval_calls: list[dict[str, Any]], db: Session) -> None:
    dialog = _dialog(None)
    dialog.llm_setting = None

    with pytest.raises(_GenerationReached):
        list(dialog_service.chat(dialog, _MESSAGES, db, stream=False))
