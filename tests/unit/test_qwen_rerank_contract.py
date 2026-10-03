"""Exercise the registered provider and real SDK without cloud requests."""

from http import HTTPStatus
from types import SimpleNamespace
from typing import Any

import dashscope
import numpy as np
import pytest
from dashscope.api_entities.dashscope_response import DashScopeAPIResponse
from dashscope.client.base_api import BaseApi

from core.llm import RerankModel
from core.llm.rerank import QWenRerank


@pytest.mark.parametrize(
    ("model_name", "expected_model", "return_documents"),
    [
        ("qwen3-rerank", "qwen3-rerank", None),
        ("qwen3-rerank-2026-04", "qwen3-rerank-2026-04", None),
        ("gte-rerank-v2", "gte-rerank-v2", False),
        ("gte-rerank", "gte-rerank", False),
        (None, dashscope.TextReRank.Models.gte_rerank, False),
    ],
)
def test_registered_qwen_rerank_sdk_contract(
    monkeypatch: pytest.MonkeyPatch,
    model_name: str | None,
    expected_model: str,
    return_documents: bool | None,
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_call(cls: type[BaseApi], **kwargs: Any) -> DashScopeAPIResponse:
        calls.append(kwargs)
        return DashScopeAPIResponse(
            status_code=HTTPStatus.OK,
            output={"results": [{"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.3}]},
            usage={"total_tokens": 17},
        )

    # Keep TextReRank.call and ReRankResponse conversion real; stop before IO.
    monkeypatch.setattr(BaseApi, "call", classmethod(fake_call))
    provider = RerankModel["Tongyi-Qianwen"]
    assert provider is QWenRerank
    assert provider.__module__ == "core.llm.rerank"
    model = provider("unit-test-key", model_name, base_url="https://unused.invalid", extra="compatible")
    texts = ["first", "unreturned", "third"]
    scores, tokens = model.similarity("query", texts)

    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == expected_model
    assert call["api_key"] == "unit-test-key"
    assert call["input"] == {"query": "query", "documents": texts}
    assert call["top_n"] == len(texts)
    assert call["task"] == "text-rerank"
    assert "base_url" not in call
    assert "extra" not in call
    if return_documents is None:
        assert "return_documents" not in call
    else:
        assert call["return_documents"] is False
    np.testing.assert_array_equal(scores, [0.3, 0.0, 0.9])
    assert scores.dtype == np.dtype(float)
    assert tokens == 17
    assert texts == ["first", "unreturned", "third"]


def test_qwen_rerank_constructor_keeps_default_and_positional_base_url() -> None:
    provider = RerankModel["Tongyi-Qianwen"]
    assert provider("unit-test-key").model_name == "gte-rerank"
    model = provider("unit-test-key", "qwen3-rerank", "https://unused.invalid")
    assert model.model_name == "qwen3-rerank"


def test_qwen_rerank_provider_failure_is_not_returned_as_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_call(**kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(status_code=HTTPStatus.BAD_REQUEST, text="unsupported parameter")

    monkeypatch.setattr(dashscope.TextReRank, "call", fake_call)
    model = RerankModel["Tongyi-Qianwen"]("unit-test-key", "qwen3-rerank")
    with pytest.raises(ValueError, match="qwen3-rerank: 400 - unsupported parameter"):
        model.similarity("query", ["document"])
