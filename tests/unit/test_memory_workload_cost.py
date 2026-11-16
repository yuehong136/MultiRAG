"""Whole-workload accounting preserves failures, missing stages and currencies."""

from typing import Any

import pytest

from tests.support.memory_workload_cost import STAGES, workload_cost


def attempt(identity: str, stage: str, *, total: int | None = 10, model: str = "chat") -> dict[str, Any]:
    return {
        "attempt_id": identity,
        "stage": stage,
        "state": "completed" if total is not None else "failed",
        "model_reference": model,
        "provider_total_tokens": total,
        "usage": {"input_tokens": 8 if total is not None else None, "output_tokens": 2 if total is not None else None, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
    }


def quote(currency: str = "CNY") -> dict[str, Any]:
    return {
        "source": "https://provider.example/pricing",
        "as_of": "2026-11-16",
        "currency": currency,
        "per_million": dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), 1),
    }


def test_all_stage_usage_includes_every_retry_without_double_counting() -> None:
    rows = [attempt(str(index), stage) for index, stage in enumerate(STAGES)]
    rows.append({**attempt("retry", "analysis"), "state": "failed"})
    result = workload_cost(rows, dict.fromkeys(STAGES, "completed"), {"chat": quote()})
    assert result["total"]["attempt_count"] == 6
    assert result["total"]["provider_total_tokens"] == 60
    assert result["stages"]["analysis"]["provider_total_tokens"] == 20
    assert result["total"]["token_price_estimate"] == "0.00006000"
    assert result["whole_workload_tokens_known"] and result["whole_workload_price_complete"]
    assert result["invoice_verified"] is False and result["provider_internal_retries_observable"] is False
    assert len(result["observed_attempts"]) == len(rows)


def test_unknown_failed_attempt_keeps_total_unknown_and_known_subtotal() -> None:
    rows = [attempt("first", "analysis"), attempt("unknown-retry", "analysis", total=None)]
    result = workload_cost(rows, {**dict.fromkeys(STAGES, "not_run"), "analysis": "failed"}, {"chat": quote()})
    assert result["total"]["provider_total_tokens"] is None
    assert result["total"]["known_provider_total_tokens_subtotal"] == 10
    assert result["whole_workload_price_complete"] is False
    assert result["stages"]["publication_embedding"]["state"] == "not_run"
    assert result["stages"]["publication_embedding"]["provider_total_tokens"] is None


def test_partial_pipeline_must_not_claim_complete_cost_even_with_known_calls() -> None:
    result = workload_cost([attempt("one", "source_embedding")], {**dict.fromkeys(STAGES, "not_run"), "source_embedding": "completed"}, {"chat": quote()})
    assert result["total"]["price_complete"] is True
    assert result["all_stages_complete"] is False
    assert result["whole_workload_tokens_known"] is False
    assert result["whole_workload_price_complete"] is False


def test_mixed_currency_never_becomes_a_single_monetary_total() -> None:
    rows = [attempt("a", "analysis"), attempt("b", "answer", model="other")]
    result = workload_cost(rows, {**dict.fromkeys(STAGES, "not_run"), "analysis": "completed", "answer": "completed"}, {"chat": quote(), "other": quote("USD")})
    assert result["total"]["currency"] is None
    assert result["total"]["token_price_estimate"] is None
    assert result["total"]["provider_total_tokens"] == 20


@pytest.mark.parametrize("mutation", ["missing_stage", "invalid_state", "duplicate_id", "unknown_stage", "not_run_attempt"])
def test_inconsistent_coverage_or_attempt_identity_is_rejected(mutation: str) -> None:
    rows = [attempt("one", "analysis")]
    states = dict.fromkeys(STAGES, "completed")
    if mutation == "missing_stage":
        states.pop("answer")
    elif mutation == "invalid_state":
        states["analysis"] = "free"
    elif mutation == "duplicate_id":
        rows *= 2
    elif mutation == "unknown_stage":
        rows[0]["stage"] = "hidden"
    else:
        states["analysis"] = "not_run"
    with pytest.raises(ValueError):
        workload_cost(rows, states)
