"""Validate independently scored four-arm answers and summarize observed usage."""

from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

ARMS = ("A", "B", "C", "D")
PHASES = ("initial", "corrected", "withdrawn")


def read(path: Path) -> Any:
    return json.loads(path.read_bytes())


def review(directory: Path, report_path: Path) -> dict[str, Any]:
    report = read(report_path)
    case = report["case_id"]
    request = directory / f"{case}.four-arm-review.json"
    mapping_path = directory / f"{case}.four-arm-mapping.json"
    decision_path = directory / f"{case}.four-arm-review-decision.json"
    if not request.is_file():
        if report["answers"]:
            raise ValueError(f"{case}: answered but masked review request is missing")
        return {"report": report, "reviewed_by": None, "scores": []}
    pack = read(request)
    mapping = read(mapping_path)
    decision = read(decision_path)
    if not pack["synthetic_only"] or decision["synthetic_only"] is not True or not isinstance(decision["reviewed_by"], str) or not decision["reviewed_by"].strip():
        raise ValueError(f"{case}: invalid independent review scope")
    digest = hashlib.sha256(request.read_bytes()).hexdigest()
    if decision["review_sha256"] != digest:
        raise ValueError(f"{case}: stale review request hash")
    evidence = {row["id"]: row for row in pack["items"]}
    judgments = {row["id"]: row for row in decision["items"]}
    if set(evidence) != set(mapping) or set(evidence) != set(judgments) or len(evidence) != len(pack["items"]) or len(judgments) != len(decision["items"]):
        raise ValueError(f"{case}: incomplete or duplicate independent decisions")
    answers = {(row["arm"], row["phase"]): row["answer"] for row in report["answers"]}
    scores = []
    for identity, judgment in judgments.items():
        arm, phase = mapping[identity]["arm"], mapping[identity]["phase"]
        if arm not in ARMS or phase not in PHASES or phase != evidence[identity]["phase"] or answers.get((arm, phase)) != evidence[identity]["answer"]:
            raise ValueError(f"{case}: invalid unmask mapping")
        values = judgment["criteria"]
        citations = judgment["citations_grounded"]
        if len(values) != len(evidence[identity]["criteria"]) or any(type(value) is not bool for value in values) or (citations is not None and type(citations) is not bool):
            raise ValueError(f"{case}: invalid per-criterion decision")
        scores.append({"arm": arm, "phase": phase, "accepted": all(values), "criteria": values, "citations_grounded": citations, "notes": judgment["notes"]})
    actual = {(row["arm"], row["phase"]) for row in report["answers"]}
    if {(row["arm"], row["phase"]) for row in scores} != actual or len(actual) != len(report["answers"]) or len(scores) != len(actual):
        raise ValueError(f"{case}: review rows do not match actual answered arms and phases")
    return {"report": report, "reviewed_by": decision["reviewed_by"], "request_sha256": digest, "scores": scores}


def known_cost(entries: list[dict[str, Any]]) -> dict[str, Any]:
    amounts: dict[str, Decimal] = {}
    tokens = []
    attempts = 0
    cache_reads = []
    for entry in entries:
        attempts += entry["attempt_count"]
        for currency, amount in entry["amount_by_currency"].items():
            amounts[currency] = amounts.get(currency, Decimal(0)) + Decimal(amount)
        tokens.append(entry["full_cost"]["known_provider_total_tokens_subtotal"])
        cache_reads.append(entry["by_bucket"]["cache_read_input_tokens"]["known_token_subtotal"])
    return {
        "observed_attempts": attempts,
        "known_reference_amount_by_currency": {key: format(amount, "f") for key, amount in amounts.items()},
        "known_provider_tokens": sum(value for value in tokens if value is not None) if any(value is not None for value in tokens) else None,
        "known_cache_read_tokens": sum(value for value in cache_reads if value is not None) if any(value is not None for value in cache_reads) else None,
        "invoice_verified": False,
        "price_complete": False,
    }


def aggregate(directory: Path) -> dict[str, Any]:
    cases = [review(directory, path) for path in sorted(directory.glob("*.four-arm-report.json"))]
    if not cases:
        raise ValueError("No four-arm reports found")
    arms = {}
    for arm in ARMS:
        scores = [item for case in cases for item in case["scores"] if item["arm"] == arm]
        arms[arm] = {
            "answer_quality": {
                phase: {"accepted": sum(row["accepted"] for row in scores if row["phase"] == phase), "scored": sum(row["phase"] == phase for row in scores), "planned": 4} for phase in PHASES
            },
            "citations_grounded": {"accepted": sum(row["citations_grounded"] is True for row in scores), "assessed": sum(row["citations_grounded"] is not None for row in scores)},
            "standalone_path": known_cost([case["report"]["deployment_arm_cost"][arm] for case in cases]),
        }
    return {
        "scope": "sealed synthetic comparison, known usage only",
        "case_count": len(cases),
        "planned_cases": 4,
        "mechanism_complete_cases": sum(case["report"]["mechanism_complete"] for case in cases),
        "independent_reviewed_answers": sum(len(case["scores"]) for case in cases),
        "planned_answers": 48,
        "actual_experiment_cost_counts_shared_preparation_once": known_cost([case["report"]["cost"]["known_bucket_reference"] for case in cases]),
        "arms": arms,
        "cases": [
            {
                "case_id": item["report"]["case_id"],
                "fixture_kind": item["report"]["fixture_kind"],
                "case_sha256": item["report"]["case_sha256"],
                "reviewed_by": item["reviewed_by"],
                "request_sha256": item.get("request_sha256"),
                "scores": item["scores"],
            }
            for item in cases
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path)
    arguments = parser.parse_args()
    print(json.dumps(aggregate(arguments.report_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
