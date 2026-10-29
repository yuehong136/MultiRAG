"""Translation failures retain the original query without leaking reasoning."""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.db.joint_services import tenant_model_service
from api.db.services import llm_service
from core.prompts import generator


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("**ERROR** provider unavailable", "original query"),
        ("<think>**ERROR** provider unavailable</think>Output: unsafe fallback", "original query"),
        ("<think>private reasoning</think>Output: 中文===English", " 中文\nEnglish"),
    ],
)
async def test_cross_languages_detects_provider_error_before_reasoning_cleanup(monkeypatch: pytest.MonkeyPatch, answer: str, expected: str) -> None:
    @contextmanager
    def connection() -> Iterator[object]:
        yield object()

    async_chat = AsyncMock(return_value=answer)
    monkeypatch.setattr(generator, "db_connection", connection)
    monkeypatch.setattr(tenant_model_service, "get_tenant_default_model_by_type", lambda *_: {})
    monkeypatch.setattr(llm_service, "LLMBundle", lambda *_args, **_kwargs: SimpleNamespace(async_chat=async_chat))
    result = await generator.cross_languages("tenant", None, "original query", ["English"])
    assert result == expected
    assert "private reasoning" not in result
    assert async_chat.call_count == 1
