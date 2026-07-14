import json
from collections.abc import Callable
from typing import Any

import pytest

from common.constants import TAG_FLD
from core.graphrag import search as graphrag_search
from core.prompts import generator


def _raise_once_then(value: Any) -> Callable[[str], Any]:
    calls = 0

    def fake_loads(_payload: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise json.JSONDecodeError("invalid JSON", "", 0)
        return value

    return fake_loads


async def test_query_rewrite_falls_back_on_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompletionLLM:
        llm_name = "fake"
        max_length = 1024

        async def async_chat(
            self,
            _system: str,
            _history: list[dict[str, Any]],
            _gen_conf: dict[str, Any] | None = None,
            **_kwargs: Any,
        ) -> str:
            return ""

    async def fake_entities(_idxnms: list[str], _kb_ids: list[str]) -> dict[str, list[str]]:
        return {}

    async def fake_chat(*_args: Any, **_kwargs: Any) -> str:
        return '{"answer_type_keywords": ["type"], "entities_from_query": ["entity"]}'

    search = object.__new__(graphrag_search.KGSearch)
    monkeypatch.setattr(graphrag_search, "get_entity_type2samples", fake_entities)
    monkeypatch.setattr(search, "_chat", fake_chat)
    monkeypatch.setattr(
        graphrag_search.json_repair,
        "loads",
        _raise_once_then({"answer_type_keywords": ["type"], "entities_from_query": ["entity"]}),
    )

    result = await search.query_rewrite(FakeCompletionLLM(), "question", ["index"], ["kb"])

    assert result == (["type"], ["entity"])


async def test_content_tagging_falls_back_on_json_decode_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeChatModel:
        max_length = 1024

        async def async_chat(self, *_args: Any, **_kwargs: Any) -> str:
            return '{"tag": 1}'

    monkeypatch.setattr(
        generator.json_repair,
        "loads",
        _raise_once_then({"tag": 1}),
    )

    result = await generator.content_tagging(
        FakeChatModel(),
        "content",
        ["tag"],
        [{"content": "example", TAG_FLD: {"tag": 1}}],
    )

    assert result == {"tag": 1}
