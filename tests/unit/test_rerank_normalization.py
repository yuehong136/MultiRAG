"""Public reranker scores retain calibration and share a bounded hybrid scale."""

import asyncio
from typing import Any

import numpy as np
import pytest

from core.llm import RerankModel
from core.llm import rerank as rerank_module
from core.llm.rerank import Base


class _RawRerank(Base):
    def __init__(self, rank: Any, tokens: int = 17, error: BaseException | None = None) -> None:
        self.rank = rank
        self.tokens = tokens
        self.error = error
        self.calls = 0

    def _compute_rank(self, query: str, texts: list[str]) -> tuple[np.ndarray, int]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rank, self.tokens


@pytest.mark.parametrize(
    ("factory", "raw", "expected", "expected_tokens"),
    [
        ("NVIDIA", [8.0, -4.0, 1.0], [1.0, 0.0, 5.0 / 12.0], 4),
        ("Jina", [10.0, 0.0, -10.0], [1.0, 0.5, 0.0], 17),
        ("Jina", [0.8, 0.2, 0.5], [0.8, 0.2, 0.5], 17),
    ],
)
def test_registered_provider_scores_share_public_scale(
    monkeypatch: pytest.MonkeyPatch,
    factory: str,
    raw: list[float],
    expected: list[float],
    expected_tokens: int,
) -> None:
    requests: list[dict[str, Any]] = []
    score_field = "logit" if factory == "NVIDIA" else "relevance_score"

    def post_json(url: str, *, headers: dict[str, str], json: dict[str, Any]) -> dict[str, Any]:
        requests.append(json)
        results = [{"index": index, score_field: score} for index, score in reversed(list(enumerate(raw)))]
        return {"rankings" if factory == "NVIDIA" else "results": results, "usage": {"total_tokens": 17}}

    def count_tokens(text: str) -> int:
        return 1

    monkeypatch.setattr(rerank_module, "_post_json", post_json)
    monkeypatch.setattr(rerank_module, "num_tokens_from_string", count_tokens)
    provider = RerankModel[factory]("unit-test-key", "model", base_url="http://provider.invalid/v1")
    texts = ["first", "second", "third"]

    scores, tokens = provider.similarity("query", texts)

    np.testing.assert_allclose(scores, expected)
    assert tokens == expected_tokens
    assert len(requests) == 1
    assert texts == ["first", "second", "third"]


@pytest.mark.parametrize("raw", [[0.0, 0.5, 0.9], [0.8, 0.8], [0.9], [0.0]])
def test_calibrated_scores_preserve_threshold_decisions(raw: list[float]) -> None:
    scores, tokens = _RawRerank(raw).similarity("query", ["document"] * len(raw))

    np.testing.assert_array_equal(scores, raw)
    np.testing.assert_array_equal(scores >= 0.7, np.asarray(raw) >= 0.7)
    assert tokens == 17


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([-1e308, 0.0, 1e308], [0.0, 0.5, 1.0]),
        ([1e8, 1e8 + 1.0], [0.0, 1.0]),
        ([5.0], [1.0]),
        ([-3.0], [0.0]),
        ([5.0, 5.0], [1.0, 1.0]),
        ([-2.0, -2.0], [0.0, 0.0]),
    ],
)
def test_finite_extremes_and_spreadless_scores_stay_bounded(raw: list[float], expected: list[float]) -> None:
    with np.errstate(over="raise", invalid="raise", divide="raise"):
        scores, _ = _RawRerank(raw).similarity("query", ["document"] * len(raw))

    np.testing.assert_allclose(scores, expected)
    assert np.all(np.isfinite(scores))
    assert np.all((scores >= 0.0) & (scores <= 1.0))


@pytest.mark.parametrize(
    "raw",
    [0.5, [0.5], [0.1, 0.2, 0.3], [[0.1, 0.2]], [[0.1], [0.2]], [float("nan"), 0.2], [float("inf"), 0.2], [float("-inf"), 0.2]],
)
def test_public_entrypoint_rejects_wrong_shape_length_and_nonfinite_scores(raw: Any) -> None:
    provider = _RawRerank(raw)

    with pytest.raises(ValueError, match="Rerank response"):
        provider.similarity("query", ["first", "second"])

    assert provider.calls == 1


@pytest.mark.parametrize("error", [RuntimeError("provider failed"), asyncio.CancelledError("provider cancelled")])
def test_provider_errors_and_cancellation_propagate_unchanged(error: BaseException) -> None:
    provider = _RawRerank([0.9], error=error)

    with pytest.raises(type(error)) as caught:
        provider.similarity("query", ["document"])

    assert caught.value is error
    assert provider.calls == 1


@pytest.mark.parametrize("tokens", [0, 17])
def test_public_entrypoint_preserves_provider_token_count(tokens: int) -> None:
    provider = _RawRerank([-4.0, 8.0], tokens=tokens)

    scores, used_tokens = provider.similarity("query", ["first", "second"])

    np.testing.assert_array_equal(scores, [0.0, 1.0])
    assert used_tokens == tokens
    assert provider.calls == 1
