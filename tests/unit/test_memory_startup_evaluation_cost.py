"""Partial reference subtotals retain unknown costs instead of inventing zeros."""

from copy import deepcopy
from typing import Any

import pytest

from tests.support.memory_startup_evaluation_cost import known_bucket_cost


def quote(currency: str = "USD") -> dict[str, Any]:
    return {
        "source": "https://provider.example/pricing",
        "as_of": "2026-11-16",
        "currency": currency,
        "per_million": {"input_tokens": "1", "output_tokens": "5", "cache_read_input_tokens": "0.05", "cache_creation_input_tokens": None},
    }


def attempt(*, identity: str = "a", model: str = "chat") -> dict[str, Any]:
    return {
        "attempt_id": identity,
        "model_reference": model,
        "state": "completed",
        "provider_total_tokens": 350,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 200, "cache_creation_input_tokens": None, "reasoning_tokens": 30},
    }


def test_known_buckets_are_useful_while_complete_cost_remains_unknown_and_input_is_immutable() -> None:
    rows = [attempt()]
    before = deepcopy(rows)
    result = known_bucket_cost(rows, {"chat": quote()})
    assert result["known_bucket_subtotal"] == "0.00036000" and result["currency"] == "USD"
    assert result["full_cost"]["token_price_estimate"] is None and not result["full_cost"]["price_complete"]
    assert result["excluded_buckets"] == ["cache_creation_input_tokens"]
    assert result["attempts"][0]["excluded_buckets"] == {"cache_creation_input_tokens": None}
    assert not result["price_complete"] and not result["invoice_verified"] and not result["tco_measured"]
    assert rows == before and result["attempts"][0]["usage"] == before[0]["usage"]


def test_reasoning_already_in_output_is_not_billed_twice_and_unknown_input_is_not_zero() -> None:
    row = attempt()
    row["usage"]["input_tokens"] = None
    result = known_bucket_cost([row], {"chat": quote()})
    assert result["known_bucket_subtotal"] == "0.00026000"
    assert result["by_bucket"]["input_tokens"]["known_token_subtotal"] is None
    assert result["missing_buckets"] == [{"attempt_index": 0, "attempt_id": "a", "bucket": "input_tokens", "reason": "usage_unknown"}]
    assert result["reasoning_tokens_added_separately"] is False


def test_missing_quote_or_all_usage_is_unknown_not_a_free_call() -> None:
    result = known_bucket_cost([attempt()])
    assert result["known_bucket_subtotal"] is None and len(result["missing_buckets"]) == 3
    assert {item["reason"] for item in result["missing_buckets"]} == {"price_unknown"}
    row = attempt()
    row["usage"] = {}
    result = known_bucket_cost([row], {"chat": quote()})
    assert result["known_bucket_subtotal"] is None and result["amount_by_currency"] == {}
    assert result["attempts"][0]["excluded_buckets"]["cache_creation_input_tokens"] is None


def test_failures_are_included_and_decimal_total_is_not_sum_of_rounded_attempts() -> None:
    rows = []
    for index in range(3):
        row = attempt(identity=str(index))
        row["state"] = "failed" if index == 1 else "completed"
        row["usage"] = {"input_tokens": 1, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": None}
        rows.append(row)
    price = quote()
    price["per_million"]["input_tokens"] = "0.004"
    result = known_bucket_cost(rows, {"chat": price})
    assert result["attempt_count"] == 3 and result["attempts"][1]["state"] == "failed"
    assert result["known_bucket_subtotal"] == "0.00000001"
    assert all(row["known_bucket_subtotal"] == "0.00000000" for row in result["attempts"])


def test_mixed_currency_preserves_separate_subtotals_without_exchange_rate() -> None:
    rows = [attempt(), attempt(identity="b", model="other")]
    result = known_bucket_cost(rows, {"chat": quote(), "other": quote("CNY")})
    assert result["currency"] is None and result["known_bucket_subtotal"] is None
    assert result["amount_by_currency"] == {"CNY": "0.00036000", "USD": "0.00036000"}


def test_empty_attempts_remain_unknown_and_known_zero_remains_zero() -> None:
    assert known_bucket_cost([])["known_bucket_subtotal"] is None
    row = attempt()
    row["usage"] = dict.fromkeys(("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"), 0)
    result = known_bucket_cost([row], {"chat": quote()})
    assert result["known_bucket_subtotal"] == "0.00000000"


@pytest.mark.parametrize("currency", ["usd", "", "US", "USDD", 1])
def test_malformed_currency_is_rejected(currency: Any) -> None:
    price = quote()
    price["currency"] = currency
    with pytest.raises(ValueError, match=r"currency|price_quote"):
        known_bucket_cost([attempt()], {"chat": price})


@pytest.mark.parametrize("rate", [True, "NaN", "Infinity", "-0.1"])
def test_invalid_prices_use_existing_validator(rate: Any) -> None:
    price = quote()
    price["per_million"]["input_tokens"] = rate
    with pytest.raises(ValueError, match="invalid_cost_quantity"):
        known_bucket_cost([attempt()], {"chat": price})


@pytest.mark.parametrize("tokens", [True, -1, 1.5])
def test_invalid_usage_is_not_repaired(tokens: Any) -> None:
    row = attempt()
    row["usage"]["input_tokens"] = tokens
    with pytest.raises(ValueError, match="invalid_usage_tokens"):
        known_bucket_cost([row], {"chat": quote()})
