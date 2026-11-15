"""Independent Chinese holdout integrity and bounded oracle interpretation."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.evaluation import EvaluationObservation, case_manifest, evaluate_case, load_cases, run_baseline

_FIXTURE = Path(__file__).parents[1] / "fixtures/memory_maintenance_holdout.json"


@pytest.fixture
def holdout() -> list[dict[str, Any]]:
    return load_cases(_FIXTURE)


def _evaluate(case: dict[str, Any], proposals: list[dict[str, Any]]) -> dict[str, Any]:
    return evaluate_case(case, EvaluationObservation({"proposals": proposals}), count_tokens=len)


def test_holdout_has_eight_distinct_cases_and_explicit_semantic_review(holdout: list[dict[str, Any]]) -> None:
    assert len(holdout) == 8
    assert all(case["evaluation_split"] == "independent_holdout" and case["policy"] == "general" for case in holdout)
    assert len({source["source_id"] for case in holdout for source in case["sources"]}) == sum(len(case["sources"]) for case in holdout)
    for case in holdout:
        review = case["gold"]["semantic_review"]
        assert review["required"] is True and review["criteria"]
        assert review["empty_response"]
        assert set(case["gold"]["required_fact_ids"]) <= {fact["fact_id"] for fact in case["gold"]["facts"]}


def test_authored_references_are_host_valid_without_being_model_quality_evidence(holdout: list[dict[str, Any]]) -> None:
    reports = [_evaluate(case, case["reference_proposals"]) for case in holdout]
    assert [report["outcome"] for report in reports] == ["valid_candidates"] * 3 + ["explicit_noop"] + ["valid_candidates"] * 3 + ["explicit_noop"]
    for case, report in zip(holdout, reports, strict=True):
        assert report["exact_fixture_output_match"] is True
        assert report["manual_semantic_review_required"] is True and report["publication_performed"] is False
        assert report["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
        assert report["after"]["source_bound_fact_count"] == len(case["gold"]["facts"])
        assert report["after"]["preserved_sibling_count"] == len(case["gold"]["siblings"])
        assert report["cost"]["provider_total_tokens"] is None and report["cost"]["token_price_estimate"] is None


@pytest.mark.parametrize("fixture_name", ["holdout", "unseen_final"])
async def test_provider_callback_receives_only_the_manifest_not_holdout_gold(request: pytest.FixtureRequest, fixture_name: str) -> None:
    cases = deepcopy(request.getfixturevalue(fixture_name))
    sentinel = "holdout-oracle-must-never-enter-the-model-payload"
    for case in cases:
        case["gold"]["private_oracle_marker"] = sentinel
        case["reference_proposals"][0]["reason"] = sentinel
    seen: list[dict[str, Any]] = []

    async def analyzer(manifest: dict[str, Any], policy: str, budget: int) -> EvaluationObservation:
        seen.append(manifest)
        assert set(manifest) == {"inputs", "entries", "estimated_input_tokens"}
        assert policy == "general" and budget == 2048
        assert sentinel not in str(manifest)
        assert all(item["snapshot"]["role_status"] == "role_unknown" for item in manifest["inputs"])
        return EvaluationObservation({"proposals": []})

    report = await run_baseline(cases, analyzer, count_tokens=len, token_counter="unit_codepoint_surrogate")
    assert len(seen) == len(cases) and report["outcome_counts"]["no_proposal"] == len(cases)
    assert report["answer_accuracy_measured"] is False and report["manual_semantic_review_required"] is True
    assert all(not result["mechanism_rejection_matched"] and result["exact_fixture_output_match"] is None for result in report["results"])


def test_empty_proposals_leave_required_preference_corrections_and_joint_decisions_missing(holdout: list[dict[str, Any]]) -> None:
    for case in holdout:
        if not case["gold"]["required_fact_ids"]:
            continue
        report = _evaluate(case, [])
        assert report["outcome"] == "no_proposal" and report["exact_fixture_output_match"] is None
        missing = {fact["fact_id"] for fact in report["after"]["facts"] if not fact["oracle_assertion_recognized"]}
        assert set(case["gold"]["required_fact_ids"]) <= missing


def test_a_joint_claim_with_one_source_gets_no_joint_provenance_credit(holdout: list[dict[str, Any]]) -> None:
    case = deepcopy(holdout[2])
    proposal = case["reference_proposals"][0]
    proposal["source_input_ids"] = ["source-631"]
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "valid_candidates"
    joint = next(fact for fact in report["after"]["facts"] if fact["fact_id"] == "joint-decision")
    assert joint["oracle_assertion_recognized"] is True and joint["necessary_sources_present"] is False


def test_correction_cannot_discard_the_existing_entry_source(holdout: list[dict[str, Any]]) -> None:
    case = deepcopy(holdout[1])
    proposal = case["reference_proposals"][0]
    proposal["source_input_ids"] = ["source-622"]
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "mechanism_rejected" and report["error_code"] == "maintenance_target_evidence_mismatch"
    assert report["after"] is None


def test_separate_subject_sources_cannot_be_merged_by_a_model(holdout: list[dict[str, Any]]) -> None:
    case = deepcopy(holdout[6])
    proposal = case["reference_proposals"][0]
    proposal["source_input_ids"] = ["source-671", "source-672"]
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "mechanism_rejected" and report["error_code"] == "maintenance_evidence_principal_mismatch"
    assert report["after"] is None


@pytest.mark.parametrize("index", [5, 7])
def test_quoted_instructions_and_self_reports_cannot_supply_verification_fields(holdout: list[dict[str, Any]], index: int) -> None:
    case = deepcopy(holdout[index])
    proposal = case["reference_proposals"][0]
    proposal["assessment"] = {"kind": "verified", "verification_status": "verified"}
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "mechanism_rejected" and report["error_code"] == "invalid_maintenance_action_fields"
    manifest = case_manifest(case, len)
    assert all(item["snapshot"]["origin"] == "legacy_combined" and item["snapshot"]["role_status"] == "role_unknown" for item in manifest["inputs"])


def test_unverified_metadata_does_not_prove_that_the_body_is_supported(holdout: list[dict[str, Any]]) -> None:
    case = holdout[7]
    unsupported = case["gold"]["known_incorrect_records"][0]
    proposal = {
        "action": "add",
        "content": unsupported["content"],
        "message_type": "semantic",
        "source_input_ids": ["source-681"],
        "reason": "Structurally valid but unsupported assertion for a scoring regression.",
    }
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "valid_candidates"
    assert report["assessment"]["verification_status"] == "unverified"
    assert report["after"]["known_error_statements"] == unsupported["errors"]
    assert report["manual_semantic_review_required"] is True


def test_legitimate_paraphrase_is_not_misreported_as_an_automatic_error(holdout: list[dict[str, Any]]) -> None:
    case = deepcopy(holdout[0])
    proposal = case["reference_proposals"][0]
    proposal["content"] = "架构讲解一般先写步骤；用户明确要求查看数据趋势时，再使用折线图。"
    report = _evaluate(case, [proposal])
    assert report["outcome"] == "valid_candidates" and report["exact_fixture_output_match"] is False
    assert report["after"]["recognized_fact_count"] == 0 and report["after"]["known_error_statements"] == []
    assert report["manual_semantic_review_required"] is True


@pytest.fixture
def unseen_final() -> list[dict[str, Any]]:
    return load_cases(Path(__file__).parents[1] / "fixtures/memory_maintenance_unseen.json")


def test_six_final_unseen_references_are_host_valid(unseen_final: list[dict[str, Any]]) -> None:
    assert len(unseen_final) == 6
    assert all(case["evaluation_split"] == "final_unseen" for case in unseen_final)
    reports = [_evaluate(case, case["reference_proposals"]) for case in unseen_final]
    assert [report["outcome"] for report in reports] == ["valid_candidates"] * 3 + ["explicit_noop"] + ["valid_candidates"] * 2
    assert all(report["exact_fixture_output_match"] is True and report["manual_semantic_review_required"] is True for report in reports)
    assert all(report["after"]["source_bound_fact_count"] == len(case["gold"]["facts"]) for case, report in zip(unseen_final, reports, strict=True))


def test_unseen_existing_siblings_are_credited_before_the_correction(unseen_final: list[dict[str, Any]]) -> None:
    case = unseen_final[1]
    report = _evaluate(case, [])
    assert report["before"]["recognized_fact_count"] == 2
    assert report["before"]["preserved_sibling_count"] == 2
    assert report["after"] == report["before"]
    assert report["outcome"] == "no_proposal" and report["exact_fixture_output_match"] is None
