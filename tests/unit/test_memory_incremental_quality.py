"""Authored incremental mechanism cases, not provider accuracy or SQL acceptance.

The fixture records semantic-review limits and fresh upstream source/test links.
Only production selection, host assembly and whole-input snapshot logic run here;
no model, external storage, approval, publication or database is exercised.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.evaluation import case_manifest, load_cases
from memory.maintenance.selection import CoverageState, effective_coverage, partition_sources, select_sources
from memory.maintenance.snapshots import MaintenanceSnapshots, analysis_payload
from memory.revisions.domain import RevisionError, RevisionValidationError, content_hash
from memory.revisions.storage import ManagedMessageStore


@pytest.fixture
def cases() -> dict[str, dict[str, Any]]:
    values = load_cases(Path(__file__).parents[1] / "fixtures/memory_incremental_quality.json")
    return {value["case_id"]: value for value in values}


def character_estimate(text: str) -> int:
    """Explicit unit surrogate, without tokenizer or provider hard-limit claims."""
    return len(text)


def expanded(case: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(case)
    for source in result["sources"]:
        source["content"] = source["content"] * source.pop("repeat", 1) + source.pop("suffix", "")
    return result


def manifest(case: dict[str, Any]) -> dict[str, Any]:
    return case_manifest(expanded(case), character_estimate)


def metadata(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [{key: deepcopy(item[key]) for key in ("source_id", "source_revision", "source_generation", "content_sha256", "scope", "valid_at")} for item in manifest(case)["inputs"]]


def selected(case: dict[str, Any]) -> dict[str, Any]:
    states = cast(dict[int, CoverageState], {int(key): value for key, value in case.get("states", {}).items()})
    return select_sources(metadata(case), states, max_sources=case.get("max_sources", 32))


def batches(case: dict[str, Any]) -> list[dict[str, Any]]:
    context = [item for item in metadata(case) if item["source_id"] in case.get("context_ids", [])]
    return partition_sources(selected(case)["sources"], context, manifest(case)["entries"], batch_size=case["batch_size"])


def confirmed_coverage(**changes: Any) -> bool:
    state = {
        "ledger_state": "analyzed",
        "job_state": "ready",
        "execution_state": "succeeded",
        "admission_exists": True,
        "actual_token_present": False,
        "source_matches": True,
        **changes,
    }
    return effective_coverage(**state)


def test_fixture_keeps_mechanism_results_separate_from_semantic_acceptance(cases: dict[str, dict[str, Any]]) -> None:
    assert set(cases) == {
        "old_event_new_source",
        "exact_principal_partition",
        "valid_empty_is_analyzed_not_published",
        "invalid_host_plan_has_no_coverage",
        "whole_source_deferred",
        "repeated_context_preserves_primary_and_siblings",
    }
    assert all(case["manual_semantic_review_required"] and case["semantic_review"] for case in cases.values())
    assert all(case["answer_accuracy_measured"] is False and case["publication_performed"] is False for case in cases.values())
    assert all(case["upstream_basis"] and all("/blob/" in link for link in case["upstream_basis"]) for case in cases.values())


def test_new_source_with_an_old_event_time_stays_eligible(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["old_event_new_source"]
    result = selected(case)
    assert [item["source_id"] for item in result["sources"]] == case["expected"]["selected_ids"]
    assert result["counts"] == case["expected"]["counts"]
    assert result["has_more"] is case["expected"]["has_more"]
    assert result["sources"][0]["valid_at"] < next(item["valid_at"] for item in metadata(case) if item["source_id"] == 9)


def test_partition_keeps_cross_session_context_inside_exact_user_and_agent(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["exact_principal_partition"]
    result = batches(case)
    assert [[item["source_id"] for item in row["primary_sources"]] for row in result] == case["expected"]["primary_batches"]
    assert [[item["source_id"] for item in row["context_sources"]] for row in result] == case["expected"]["context_batches"]
    for row in result:
        identities = {(item["scope"]["user_id"], item["scope"]["agent_id"]) for item in row["primary_sources"] + row["context_sources"]}
        assert len(identities) == 1
    assert {item["scope"]["session_id"] for item in result[0]["primary_sources"]} == {"s1", "s2"}
    assert result[1]["primary_sources"][0]["scope"]["user_id"] == ""
    assert result[1]["context_sources"] == []


def test_valid_empty_result_may_be_confirmed_without_a_candidate_or_publication(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["valid_empty_is_analyzed_not_published"]
    candidates = assemble_candidates(case["reference_proposals"], manifest(case), character_estimate, 4096)
    assert len(candidates) == case["expected"]["candidate_count"]
    assert confirmed_coverage()
    assert case["expected"]["publication_performed"] is False
    result = select_sources(metadata(case), dict.fromkeys(case["expected"]["covered_ids"], "covered"), max_sources=32)
    assert result["sources"] == [] and result["counts"]["covered"] == 1


@pytest.mark.parametrize(
    "state",
    [
        {"ledger_state": "reserved"},
        {"job_state": "failed"},
        {"execution_state": "running"},
        {"admission_exists": False},
        {"actual_token_present": True},
        {"source_matches": False},
    ],
)
def test_saved_or_empty_output_does_not_cover_unconfirmed_or_changed_inputs(state: dict[str, Any]) -> None:
    assert confirmed_coverage(**state) is False


def test_host_rejected_patch_and_cross_principal_plan_are_not_successful_analysis(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["invalid_host_plan_has_no_coverage"]
    for variant in case["variants"]:
        with pytest.raises(RevisionError) as refused:
            assemble_candidates(variant["proposals"], manifest(case), character_estimate, 4096)
        assert refused.value.code == variant["expected_error"]
    assert confirmed_coverage(ledger_state="reserved", job_state="failed", execution_state="failed") is False
    assert case["expected"]["covered_ids"] == [] and case["expected"]["publication_performed"] is False


async def test_production_snapshot_whole_defers_over_budget_source(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["whole_source_deferred"]
    full = manifest(case)
    rows = [
        SimpleNamespace(
            tenant_id="tenant",
            state="active",
            source_message_id=item["source_id"],
            source_revision=item["source_revision"],
            generation=item["source_generation"],
            content_sha256=item["content_sha256"],
            scope=deepcopy(item["scope"]),
        )
        for item in full["inputs"]
    ]
    physical = {
        item["source_id"]: {
            "memory_id": "bank",
            "message_id": item["source_id"],
            "message_type": "raw",
            "source_id": None,
            "content": item["snapshot"]["content"],
            "status": True,
            "forget_at": None,
            **item["scope"],
        }
        for item in full["inputs"]
    }
    # Only the SQL row values and authorized physical reads are test doubles.
    # Production MaintenanceSnapshots performs all packing and deferral.
    db = Mock(spec=AsyncSession)
    db.new, db.dirty, db.deleted = set(), set(), set()
    db.in_transaction.return_value = False
    db.scalar = AsyncMock(side_effect=["tenant", "active"])
    db.scalars = AsyncMock(return_value=rows)
    storage = Mock(spec=ManagedMessageStore)
    storage.read = AsyncMock(side_effect=lambda identity: deepcopy(physical[identity]))
    snapshots = MaintenanceSnapshots(cast(AsyncSession, db), tenant_id="tenant", storage=cast(ManagedMessageStore, storage), count_tokens=character_estimate)
    ids = [item["source_id"] for item in full["inputs"]]
    packed = await snapshots.snapshot("bank", ids, [], case["input_budget_tokens"])
    request = canonical_request(ids, [], "general", case["input_budget_tokens"], 4096)
    validate_manifest(packed, request, character_estimate)
    assert [item["source_id"] for item in packed["inputs"] if item["coverage"] == "included"] == case["expected"]["included_ids"]
    deferred = [item for item in packed["inputs"] if item["coverage"] == "deferred"]
    assert [item["source_id"] for item in deferred] == case["expected"]["deferred_ids"]
    assert all(item["snapshot"] == {} and item["defer_reason"] == case["expected"]["deferred_reason"] for item in deferred)
    long_source = physical[deferred[0]["source_id"]]["content"]
    assert deferred[0]["content_sha256"] == content_hash(long_source)
    assert long_source not in json.dumps(analysis_payload(packed, "general"), ensure_ascii=False)
    assert storage.read.await_count == 2
    assert confirmed_coverage(ledger_state="reserved", job_state="pending", execution_state="queued") is False

    leaked = deepcopy(packed)
    leaked["inputs"][1]["snapshot"] = {**full["inputs"][1]["snapshot"], "content": long_source[:32]}
    with pytest.raises(RevisionValidationError, match="invalid_maintenance_coverage"):
        validate_manifest(leaked, request, character_estimate)


def test_repeated_context_does_not_swallow_new_primary_or_sibling_facts(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["repeated_context_preserves_primary_and_siblings"]
    result = batches(case)
    primary = [[item["source_id"] for item in row["primary_sources"]] for row in result]
    context = [[item["source_id"] for item in row["context_sources"]] for row in result]
    assert primary == case["expected"]["primary_batches"] and context == case["expected"]["context_batches"]
    all_primary = [identity for group in primary for identity in group]
    assert all_primary == case["expected"]["new_covered_ids"] and len(set(all_primary)) == len(all_primary)
    assert set(all_primary).isdisjoint(case["context_ids"])
    built = []
    for index, row in enumerate(result):
        batch_case = deepcopy(case)
        visible = {item["source_id"] for item in row["primary_sources"] + row["context_sources"]}
        batch_case["sources"] = [source for source in batch_case["sources"] if source["source_id"] in visible]
        built.extend(assemble_candidates([case["reference_proposals"][index]], manifest(batch_case), character_estimate, 4096))
    assert [candidate["content"] for candidate in built] == case["expected"]["candidate_contents"]
    assert all(candidate["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"} for candidate in built)
    assert "备份每天02:00。" in built[0]["content"]
    assert case["expected"]["context_new_covered_ids"] == []

    missing_closure = deepcopy(case["reference_proposals"][0])
    missing_closure["source_input_ids"] = ["source-20"]
    with pytest.raises(RevisionValidationError, match="maintenance_target_evidence_mismatch"):
        assemble_candidates([missing_closure], manifest(case), character_estimate, 4096)
