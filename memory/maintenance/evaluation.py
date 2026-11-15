"""Synthetic candidate-contract baseline; never a question-answer accuracy metric.

The oracle recognizes whole, explicitly listed Chinese assertions, exact host
patches and necessary source bindings. Paraphrases need human semantic review.
No provider, database, publication or currency conversion is invoked here.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from common.asyncio_utils import finish_owned_work
from memory.maintenance.domain import assemble_candidates
from memory.revisions.domain import RevisionError, RevisionValidationError, content_hash

_BUCKETS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass(frozen=True)
class EvaluationObservation:
    analysis: dict[str, Any] | None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None


BaselineAnalyzer = Callable[[dict[str, Any], str, int], Awaitable[EvaluationObservation]]
ResultRecorder = Callable[[dict[str, Any]], Awaitable[None]]


def load_cases(path: Path) -> list[dict[str, Any]]:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("baseline_fixture_too_large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not 1 <= len(value) <= 8 or any(not isinstance(case, dict) or not isinstance(case.get("case_id"), str) for case in value):
        raise ValueError("invalid_baseline_cases")
    if len({case["case_id"] for case in value}) != len(value):
        raise ValueError("duplicate_baseline_case")
    return value


def case_manifest(case: dict[str, Any], count_tokens: Callable[[str], int]) -> dict[str, Any]:
    sources = case["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError("invalid_baseline_sources")
    inputs = []
    for source in sources:
        inputs.append(
            {
                "input_id": f"source-{source['source_id']}",
                "source_id": source["source_id"],
                "source_revision": 1,
                "source_generation": 1,
                "content_sha256": content_hash(source["content"]),
                "scope": {key: source[key] for key in ("user_id", "agent_id", "session_id")},
                "valid_at": source.get("valid_at", "2026-11-15 00:00:00"),
                "snapshot": {"content": source["content"], "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
                "coverage": "included",
                "defer_reason": None,
            }
        )
    entries = []
    for index, entry in enumerate(case["entries"]):
        anchor = next(item for item in inputs if item["source_id"] == entry["source_ids"][0])
        entries.append(
            {
                "entry_id": entry["entry_id"],
                "revision": 1,
                "generation": 1,
                "message_id": 1000 + index,
                "content": entry["content"],
                "content_sha256": content_hash(entry["content"]),
                "message_type": entry.get("message_type", "semantic"),
                "scope": deepcopy(anchor["scope"]),
                "source_ids": list(entry["source_ids"]),
            }
        )
    if len(entries) > 32:
        raise ValueError("invalid_baseline_targets")
    manifest = {"inputs": inputs, "entries": entries, "estimated_input_tokens": 0}
    manifest["estimated_input_tokens"] = count_tokens(json.dumps({"inputs": inputs, "entries": entries}, ensure_ascii=False, separators=(",", ":")))
    return manifest


def _number(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid_cost_quantity")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid_cost_quantity") from error
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_cost_quantity")
    return result


def _amount(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.00000001")), "f") if value is not None else None


def usage_cost(attempts: list[dict[str, Any]], prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Retain totals without guessing their input/output/cache decomposition."""
    rows: list[dict[str, Any]] = []
    subtotal, complete = Decimal(0), bool(attempts)
    provider_total = 0
    totals_complete = bool(attempts)
    for attempt in attempts:
        model = attempt.get("model_reference")
        quote = (prices or {}).get(model) if isinstance(model, str) else None
        if quote is not None and (not isinstance(quote, dict) or not quote.get("source") or not quote.get("as_of") or not quote.get("currency") or not isinstance(quote.get("per_million"), dict)):
            raise ValueError("invalid_price_quote")
        usage = attempt.get("usage") or {}
        if not isinstance(usage, dict):
            raise ValueError("invalid_usage_tokens")
        buckets = {name: usage.get(name) for name in _BUCKETS}
        if any(value is not None and (type(value) is not int or value < 0) for value in buckets.values()):
            raise ValueError("invalid_usage_tokens")
        total = attempt.get("provider_total_tokens")
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("invalid_usage_total")
        if total is None:
            totals_complete = False
        else:
            provider_total += total
        missing, known = [], Decimal(0)
        for name, tokens in buckets.items():
            rate = _number(quote["per_million"].get(name)) if quote is not None else None
            if tokens is None or rate is None:
                missing.append(name)
            else:
                known += Decimal(tokens) * rate / Decimal(1000000)
        currency = quote["currency"] if quote is not None else None
        # No cross-model/currency total without an explicitly uniform quote.
        currencies = {item["quoted_currency"] for item in rows if item["quoted_currency"] is not None}
        if missing or (currencies and currency not in currencies):
            complete = False
        subtotal += known
        rows.append(
            {
                "model_reference": model,
                "config_reference": attempt.get("config_reference"),
                "state": attempt.get("state", "unknown"),
                "elapsed_seconds": attempt.get("elapsed_seconds"),
                "provider_total_tokens": total,
                "usage": buckets,
                "provider_reported_amount": _amount(_number(attempt.get("provider_reported_amount"))),
                "provider_reported_currency": attempt.get("provider_reported_currency"),
                "quoted_currency": currency,
                "quote_source": quote["source"] if quote is not None else None,
                "quote_as_of": quote["as_of"] if quote is not None else None,
                "token_price_estimate": _amount(known) if not missing else None,
                "unpriced_buckets": missing,
            }
        )
    currencies = {row["quoted_currency"] for row in rows if row["quoted_currency"] is not None}
    return {
        "attempts": rows,
        "attempt_count": len(rows),
        "attempt_count_basis": "caller-observed attempts; unreported retries may exist",
        "provider_total_tokens": provider_total if totals_complete else None,
        "known_provider_total_tokens_subtotal": provider_total if any(row["provider_total_tokens"] is not None for row in rows) else None,
        "token_price_estimate": _amount(subtotal) if complete else None,
        "currency": next(iter(currencies)) if len(currencies) == 1 else None,
        "price_complete": complete,
        "invoice_verified": False,
        "provider_internal_retries_observable": False,
    }


def _records(manifest: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [{"entry_id": item["entry_id"], "content": item["content"], "source_ids": list(item["source_ids"])} for item in manifest["entries"]]
    for candidate in candidates:
        if candidate["action"] == "noop":
            continue
        record = {"entry_id": candidate["target_entry_id"], "content": candidate["content"], "source_ids": [item["source_id"] for item in candidate["sources"]]}
        if candidate["action"] == "patch":
            result = [item for item in result if item["entry_id"] != candidate["target_entry_id"]]
        result.append(record)
    return result


def _metrics(records: list[dict[str, Any]], gold: dict[str, Any]) -> dict[str, Any]:
    facts = []
    recognized = [(record, oracle) for record in records for oracle in gold["recognized_records"] if record["content"] == oracle["content"]]
    for fact in gold["facts"]:
        matches = [record for record, oracle in recognized if fact["fact_id"] in oracle["fact_ids"]]
        facts.append(
            {"fact_id": fact["fact_id"], "oracle_assertion_recognized": bool(matches), "necessary_sources_present": any(set(fact["source_ids"]) <= set(record["source_ids"]) for record in matches)}
        )
    siblings = [
        {"entry_id": item["entry_id"], "preserved": any(record["entry_id"] == item["entry_id"] and item["statement"] in oracle["preserved_sibling_statements"] for record, oracle in recognized)}
        for item in gold.get("siblings", [])
    ]
    errors = list(dict.fromkeys(error for record in records for oracle in gold["known_incorrect_records"] if record["content"] == oracle["content"] for error in oracle["errors"]))
    return {
        "facts": facts,
        "gold_fact_count": len(facts),
        "recognized_fact_count": sum(item["oracle_assertion_recognized"] for item in facts),
        "source_bound_fact_count": sum(item["necessary_sources_present"] for item in facts),
        "siblings": siblings,
        "preserved_sibling_count": sum(item["preserved"] for item in siblings),
        "known_error_statements": errors,
        "unrecognized_paraphrases_are_not_proven_errors": True,
        "recognition_basis": "exact whole-record oracle with authored fact IDs; no keyword or answer-accuracy score",
    }


def evaluate_case(case: dict[str, Any], observation: EvaluationObservation, *, count_tokens: Callable[[str], int], prices: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest = case_manifest(case, count_tokens)
    gold = case["gold"]
    before = _records(manifest, [])
    report = {
        "case_id": case["case_id"],
        "title": case["title"],
        "before": _metrics(before, gold),
        "after": None,
        "outcome": "analysis_failed",
        "error_code": observation.error_code,
        "expected_mechanism_rejection": gold.get("expected_rejection"),
        "mechanism_rejection_matched": False,
        "exact_fixture_output_match": None,
        "manual_semantic_review_required": True,
        "publication_performed": False,
        "cost": usage_cost(observation.attempts, prices),
        "input_payload_estimate": manifest["estimated_input_tokens"],
        "estimate_scope": "manifest only; system prompt and provider framing excluded",
    }
    if observation.error_code is not None or observation.analysis is None:
        return report
    try:
        proposals = observation.analysis["proposals"]
        candidates = assemble_candidates(proposals, manifest, count_tokens, case.get("output_budget_tokens", 2048))
        targets = [item["target_entry_id"] for item in candidates if item["action"] == "patch"]
        if len(targets) != len(set(targets)):
            raise RevisionValidationError("publication_duplicate_target")
    except (RevisionError, KeyError, TypeError, ValueError) as error:
        code = error.code if isinstance(error, RevisionError) else "invalid_baseline_analysis"
        report.update(outcome="mechanism_rejected", error_code=code, mechanism_rejection_matched=code == gold.get("expected_rejection"))
        return report
    after = _records(manifest, candidates)
    changes = [item for item in candidates if item["action"] != "noop"]
    outcome = "valid_candidates" if changes else "explicit_noop" if candidates else "no_proposal"
    expected = gold.get("expected_records")
    report.update(
        outcome=outcome,
        after=_metrics(after, gold),
        candidate_count=len(candidates),
        proposed_change_count=len(changes),
        assessment={"kind": "hypothesis", "verification_status": "unverified"},
        exact_fixture_output_match=after == expected if expected is not None and candidates else None,
        before_curated_bytes=sum(len(item["content"].encode("utf-8")) for item in before),
        after_curated_bytes=sum(len(item["content"].encode("utf-8")) for item in after),
    )
    return report


async def run_baseline(
    cases: list[dict[str, Any]],
    analyzer: BaselineAnalyzer,
    *,
    count_tokens: Callable[[str], int],
    token_counter: str,
    prices: dict[str, Any] | None = None,
    record_result: ResultRecorder | None = None,
) -> dict[str, Any]:
    if not 1 <= len(cases) <= 8:
        raise ValueError("invalid_baseline_case_count")
    results = []
    for case in cases:
        started = time.monotonic()
        try:
            observation = await analyzer(case_manifest(case, count_tokens), case.get("policy", "general"), case.get("output_budget_tokens", 2048))
        except asyncio.CancelledError:
            canceled = evaluate_case(case, EvaluationObservation(None, error_code="analysis_canceled_usage_unknown"), count_tokens=count_tokens, prices=prices)
            canceled["elapsed_seconds"] = time.monotonic() - started
            if record_result is not None:
                await finish_owned_work(record_result(canceled))
            raise
        except Exception:
            # A callback should return failure usage in EvaluationObservation.
            # If it cannot, absence is explicitly unknown, never zero-priced.
            observation = EvaluationObservation(None, error_code="analysis_callback_failed_usage_unknown")
        result = evaluate_case(case, observation, count_tokens=count_tokens, prices=prices)
        result["elapsed_seconds"] = time.monotonic() - started
        results.append(result)
        if record_result is not None:
            await record_result(deepcopy(result))
    return {
        "benchmark": "synthetic_candidate_contract",
        "case_count": len(results),
        "token_counter": token_counter,
        "before_scope": "existing curated entries; raw evidence remains available but retrieval/answers are not evaluated",
        "after_scope": "in-memory simulation of valid candidate application; no publication",
        "outcome_counts": {state: sum(item["outcome"] == state for item in results) for state in ("valid_candidates", "explicit_noop", "no_proposal", "mechanism_rejected", "analysis_failed")},
        "answer_accuracy_measured": False,
        "mechanism_expectation_scope": "Authored reference challenges; safe real-model abstention/separate-user proposals are not mechanism failures",
        "manual_semantic_review_required": True,
        "results": results,
    }
