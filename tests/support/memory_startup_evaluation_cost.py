"""Known usage-bucket reference subtotals; never a complete bill or TCO.

The existing cost evaluator validates and reports the complete (possibly unknown)
cost. This projection only exposes its known input/output/cache-read buckets.
"""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal
from typing import Any

from memory.maintenance.evaluation import _amount, _number, usage_cost

INCLUDED_BUCKETS = ("input_tokens", "output_tokens", "cache_read_input_tokens")
EXCLUDED_BUCKETS = ("cache_creation_input_tokens",)


def known_bucket_cost(attempts: list[dict[str, Any]], prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Project known buckets while preserving missing counts and quote currencies.

    Reasoning already included in output is not an additional bucket. Cache-write
    retention/rates are explicitly outside this subtotal even if a count exists.
    No exchange rate, default price, inferred zero or provider bill is introduced.
    """
    complete = usage_cost(attempts, prices)
    totals: dict[str, Decimal] = {}
    bucket_totals: dict[str, dict[str, Decimal]] = {bucket: {} for bucket in INCLUDED_BUCKETS}
    known_tokens: dict[str, int | None] = dict.fromkeys(INCLUDED_BUCKETS)
    missing: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for index, (original, normalized) in enumerate(zip(attempts, complete["attempts"], strict=True)):
        currency = normalized["quoted_currency"]
        if currency is not None and (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None):
            raise ValueError("invalid_known_bucket_currency")
        quote = (prices or {}).get(normalized["model_reference"])
        amount = Decimal(0)
        priced: list[str] = []
        absent: list[dict[str, str]] = []
        for bucket in INCLUDED_BUCKETS:
            tokens = normalized["usage"][bucket]
            rate = _number(quote["per_million"].get(bucket)) if quote is not None else None
            if tokens is not None:
                known_tokens[bucket] = (known_tokens[bucket] or 0) + tokens
            if tokens is None or rate is None or currency is None:
                detail = {"bucket": bucket, "reason": "usage_unknown" if tokens is None else "price_unknown"}
                absent.append(detail)
                missing.append({"attempt_index": index, "attempt_id": original.get("attempt_id"), **detail})
                continue
            # The same per-million projection as usage_cost; keep Decimal until
            # final rendering, never sum already-rounded per-attempt amounts.
            value = Decimal(tokens) * rate / Decimal(1000000)
            amount += value
            priced.append(bucket)
            totals[currency] = totals.get(currency, Decimal(0)) + value
            bucket_totals[bucket][currency] = bucket_totals[bucket].get(currency, Decimal(0)) + value
        rows.append(
            {
                "attempt_id": original.get("attempt_id"),
                "state": normalized["state"],
                "model_reference": normalized["model_reference"],
                "currency": currency,
                "quote_source": normalized["quote_source"],
                "quote_as_of": normalized["quote_as_of"],
                "usage": deepcopy(original.get("usage")),
                "priced_buckets": priced,
                "missing_buckets": absent,
                "known_bucket_subtotal": _amount(amount) if priced else None,
                "excluded_buckets": {bucket: normalized["usage"][bucket] for bucket in EXCLUDED_BUCKETS},
            }
        )
    currencies = set(totals)
    uniform = next(iter(currencies)) if len(currencies) == 1 else None
    return {
        "full_cost": complete,
        "attempt_count": len(rows),
        "attempts": rows,
        "currency": uniform,
        "known_bucket_subtotal": _amount(totals[uniform]) if uniform is not None else None,
        "amount_by_currency": {currency: _amount(value) for currency, value in sorted(totals.items())},
        "by_bucket": {
            bucket: {"known_token_subtotal": known_tokens[bucket], "amount_by_currency": {currency: _amount(value) for currency, value in sorted(bucket_totals[bucket].items())}}
            for bucket in INCLUDED_BUCKETS
        },
        "included_buckets": list(INCLUDED_BUCKETS),
        "missing_buckets": missing,
        "excluded_buckets": list(EXCLUDED_BUCKETS),
        "excluded_cache_write_reason": "retention_and_write_pricing_not_assumed",
        "reasoning_tokens_added_separately": False,
        "price_complete": False,
        "reference_subtotal_only": True,
        "invoice_verified": False,
        "tco_measured": False,
        "provider_internal_retries_observable": False,
    }
