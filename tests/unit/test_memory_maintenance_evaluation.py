"""Whole assertion/provenance contracts and honest known/unknown cost accounting."""

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.evaluation import EvaluationObservation, evaluate_case, load_cases, run_baseline, usage_cost


@pytest.fixture
def cases() -> list[dict[str, Any]]:
    return load_cases(Path(__file__).parents[1] / "fixtures/memory_maintenance_baseline.json")


def oracle(case: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return evaluate_case(case, EvaluationObservation({"proposals": case["reference_proposals"]}, **kwargs), count_tokens=len)


def test_eight_authored_cases_separate_changes_abstention_and_mechanism_rejections(cases: list[dict[str, Any]]) -> None:
    reports = [oracle(case) for case in cases]
    assert [item["outcome"] for item in reports] == ["valid_candidates"] * 5 + ["mechanism_rejected"] * 2 + ["explicit_noop"]
    assert all(item["exact_fixture_output_match"] is True for item in reports[:5])
    assert all(item["mechanism_rejection_matched"] is True and item["after"] is None for item in reports[5:7])
    assert all(item["manual_semantic_review_required"] and not item["publication_performed"] for item in reports)
    assert reports[1]["before"]["recognized_fact_count"] == 1
    assert reports[1]["after"]["recognized_fact_count"] == 2
    assert reports[1]["after"]["preserved_sibling_count"] == 1
    assert reports[7]["after"]["gold_fact_count"] == 0
    assert all(item["cost"]["token_price_estimate"] is None for item in reports)


def test_missing_sibling_is_not_hidden_by_recognizing_the_corrected_port(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[1])
    case["reference_proposals"][0]["patch"] = {"old_text": case["entries"][0]["content"], "new_text": "服务监听端口为 8080。"}
    report = oracle(case)
    assert report["outcome"] == "valid_candidates"
    assert report["after"]["recognized_fact_count"] == 1 and report["after"]["preserved_sibling_count"] == 0
    assert report["exact_fixture_output_match"] is False


def test_known_overgeneralized_rule_is_reported_even_with_a_source(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[2])
    case["reference_proposals"][0]["content"] = "所有 CI 失败都应无限重试。"
    report = oracle(case)
    assert report["after"]["known_error_statements"] == ["所有 CI 失败都应无限重试。"]
    assert report["after"]["source_bound_fact_count"] == 0
    assert report["manual_semantic_review_required"] is True


def test_unrecognized_paraphrase_remains_for_human_review(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[0])
    case["reference_proposals"][0]["content"] = "如项目未另行规定，示例代码请首选 Python 3.12。"
    report = oracle(case)
    assert report["exact_fixture_output_match"] is False
    assert report["after"]["known_error_statements"] == []
    assert report["after"]["unrecognized_paraphrases_are_not_proven_errors"] is True


def test_quoted_or_negated_assertion_is_not_credited_by_substring(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[0])
    case["reference_proposals"][0]["content"] = "用户否认了这条旧偏好：“" + cases[0]["reference_proposals"][0]["content"] + "”"
    report = oracle(case)
    assert report["after"]["recognized_fact_count"] == 0 and report["exact_fixture_output_match"] is False
    case = deepcopy(cases[2])
    case["reference_proposals"][0]["content"] = "不能得出“所有 CI 失败都应无限重试。”这一结论。"
    report = oracle(case)
    assert report["after"]["known_error_statements"] == []


def test_exact_fact_with_unrelated_source_does_not_receive_provenance_credit(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[3])
    case["reference_proposals"][0]["source_input_ids"] = ["source-11"]
    report = oracle(case)
    assert report["after"]["recognized_fact_count"] == 2
    assert report["after"]["source_bound_fact_count"] == 1


def test_source_closure_is_checked_by_the_production_host_assembler(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[4])
    case["reference_proposals"][0]["source_input_ids"] = ["source-12"]
    report = oracle(case)
    assert report["outcome"] == "mechanism_rejected" and report["error_code"] == "maintenance_target_evidence_mismatch"
    assert report["after"] is None


def test_empty_response_is_not_counted_as_a_successful_rejection(cases: list[dict[str, Any]]) -> None:
    report = evaluate_case(cases[5], EvaluationObservation({"proposals": []}), count_tokens=len)
    assert report["outcome"] == "no_proposal" and report["mechanism_rejection_matched"] is False
    assert report["exact_fixture_output_match"] is None


@pytest.fixture
def prices() -> dict[str, Any]:
    return {
        "model": {
            "source": "https://example.invalid/synthetic-test-price",
            "as_of": "2026-11-15",
            "currency": "USD",
            "per_million": {"input_tokens": "1", "output_tokens": "2", "cache_read_input_tokens": "0.1", "cache_creation_input_tokens": "1.25"},
        }
    }


def test_total_tokens_are_kept_without_inventing_a_split_or_bill(prices: dict[str, Any]) -> None:
    result = usage_cost([{"model_reference": "model", "state": "completed", "provider_total_tokens": 1234, "elapsed_seconds": 1.2}], prices)
    assert result["provider_total_tokens"] == 1234 and result["token_price_estimate"] is None
    assert all(value is None for value in result["attempts"][0]["usage"].values())
    assert result["invoice_verified"] is False and result["provider_internal_retries_observable"] is False


def test_failed_attempt_cost_is_preserved_and_summed_with_retry(prices: dict[str, Any]) -> None:
    attempts = [
        {"model_reference": "model", "state": state, "provider_total_tokens": 300, "usage": {"input_tokens": 100, "output_tokens": 200, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
        for state in ("failed", "completed")
    ]
    result = usage_cost(attempts, prices)
    assert result["attempt_count"] == 2 and result["provider_total_tokens"] == 600
    assert result["token_price_estimate"] == "0.00100000" and result["price_complete"] is True
    assert result["invoice_verified"] is False


def test_unknown_attempt_makes_the_total_unknown_but_keeps_known_subtotal(prices: dict[str, Any]) -> None:
    result = usage_cost([{"provider_total_tokens": 400}, {"state": "unknown"}], prices)
    assert result["provider_total_tokens"] is None and result["known_provider_total_tokens_subtotal"] == 400
    assert result["token_price_estimate"] is None


@pytest.mark.parametrize("bad", [True, -1, float("nan")])
def test_invalid_usage_cannot_masquerade_as_free_tokens(bad: Any) -> None:
    with pytest.raises(ValueError):
        usage_cost([{"usage": {"input_tokens": bad}}])


async def test_model_failure_and_partial_progress_are_durable_to_the_caller(cases: list[dict[str, Any]]) -> None:
    recorded = []

    async def analyze(manifest: dict[str, Any], policy: str, budget: int) -> EvaluationObservation:
        return EvaluationObservation(None, [{"provider_total_tokens": 77, "state": "failed", "elapsed_seconds": 0.1}], "provider_failed_after_usage")

    async def record(result: dict[str, Any]) -> None:
        recorded.append(result)

    report = await run_baseline(cases[:1], analyze, count_tokens=len, token_counter="unit-character-surrogate", record_result=record)
    assert report["answer_accuracy_measured"] is False and report["outcome_counts"]["analysis_failed"] == 1
    assert recorded[0]["cost"]["provider_total_tokens"] == 77 and recorded[0]["after"] is None
    assert recorded[0]["error_code"] == "provider_failed_after_usage"


def test_two_patches_for_one_target_are_not_silently_applied_last_wins(cases: list[dict[str, Any]]) -> None:
    case = deepcopy(cases[1])
    case["reference_proposals"].append({"action": "patch", "target_entry_id": "service", "patch": {"old_text": "02:00", "new_text": "03:00"}, "source_input_ids": ["source-11"], "reason": "另一候选"})
    report = oracle(case)
    assert report["outcome"] == "mechanism_rejected" and report["error_code"] == "publication_duplicate_target"
    assert report["after"] is None


async def test_cancel_preserves_completed_case_and_marks_interrupted_cost_unknown(cases: list[dict[str, Any]]) -> None:
    recorded = []
    calls = 0

    async def analyze(manifest: dict[str, Any], policy: str, budget: int) -> EvaluationObservation:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return EvaluationObservation({"proposals": cases[0]["reference_proposals"]})

    async def record(result: dict[str, Any]) -> None:
        recorded.append(result)

    with pytest.raises(asyncio.CancelledError):
        await run_baseline(cases[:2], analyze, count_tokens=len, token_counter="unit-surrogate", record_result=record)
    assert len(recorded) == 2 and recorded[0]["outcome"] == "valid_candidates"
    assert recorded[1]["error_code"] == "analysis_canceled_usage_unknown"
    assert recorded[1]["cost"]["provider_total_tokens"] is None
    assert recorded[1]["cost"]["token_price_estimate"] is None
