"""Fixed target-selection mechanisms and semantic negative controls.

The supplied semantic ranks and proposals are authored fixture inputs. These
tests exercise production pure helpers, not real embedding recall, SQL race
rejection, budget packing, model accuracy, approval or publication.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.domain import assemble_candidates
from memory.maintenance.evaluation import case_manifest, load_cases
from memory.maintenance.target_conditions import canonical_target_preconditions, exact_scope, projection_digest, public_conditions
from memory.maintenance.targets import interleave_targets, lexical_score, lexical_terms, recommendation_request
from memory.revisions.domain import ModelBinding, RevisionValidationError, content_hash, request_hash

_MEMORY_ID = "quality-bank"
_FIRST_MESSAGE_ID = 9007199254740993


@pytest.fixture
def cases() -> dict[str, dict[str, Any]]:
    return {case["case_id"]: case for case in load_cases(Path(__file__).parents[1] / "fixtures/memory_target_quality.json")}


def character_estimate(text: str) -> int:
    """Declared unit surrogate, not a target-model tokenizer guarantee."""
    return len(text)


def expanded(case: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(case)
    for entry in result["entries"]:
        entry["content"] = entry["content"] * entry.pop("repeat", 1) + entry.pop("suffix", "")
    return result


def manifest(case: dict[str, Any]) -> dict[str, Any]:
    return case_manifest(expanded(case), character_estimate)


def physical(source: dict[str, Any], *, message_id: int | None = None, content: str | None = None, source_id: int | None = None) -> dict[str, Any]:
    return {
        "memory_id": _MEMORY_ID,
        "message_id": source["source_id"] if message_id is None else message_id,
        "message_type": "raw" if message_id is None else "semantic",
        "source_id": source_id,
        **{key: source[key] for key in ("user_id", "agent_id", "session_id")},
        "content": source["content"] if content is None else content,
        "content_embed": [0.1, 0.2],
        "status": True,
        "forget_at": None,
        "valid_at": "2026-11-16 00:00:00",
        "invalid_at": "",
    }


def conditions(case: dict[str, Any], *, input_ids: list[int] | None = None, entry_ids: list[str] | None = None) -> dict[str, Any]:
    case = expanded(case)
    by_id = {source["source_id"]: source for source in case["sources"]}

    def reference(identity: int) -> dict[str, Any]:
        source = by_id[identity]
        return {
            "source_id": identity,
            "source_revision": 1,
            "source_generation": 1,
            "content_sha256": content_hash(source["content"]),
            "scope": {key: source[key] for key in ("user_id", "agent_id", "session_id")},
            "projection_sha256": projection_digest(physical(source)),
        }

    selected = set(entry_ids) if entry_ids is not None else {entry["entry_id"] for entry in case["entries"]}
    targets = []
    for index, entry in enumerate(case["entries"]):
        if entry["entry_id"] not in selected:
            continue
        anchor = by_id[entry["source_ids"][0]]
        projected = physical(anchor, message_id=_FIRST_MESSAGE_ID + index, content=entry["content"], source_id=anchor["source_id"])
        targets.append(
            {
                "entry_id": entry["entry_id"],
                "revision": 1,
                "generation": 1,
                "message_id": projected["message_id"],
                "content_sha256": content_hash(entry["content"]),
                "scope": {key: anchor[key] for key in ("user_id", "agent_id", "session_id")},
                "projection_sha256": projection_digest(projected),
                "source_versions": [reference(identity) for identity in entry["source_ids"]],
            }
        )
    return {
        "inputs": [reference(identity) for identity in (input_ids if input_ids is not None else [case["sources"][-1]["source_id"]])],
        "targets": targets,
        "model_binding": ModelBinding(56, "fixture-embedding@fixture", 2, "f" * 64).to_dict(),
    }


def test_fixture_declares_review_and_no_automatic_semantic_success(cases: dict[str, dict[str, Any]]) -> None:
    assert len(cases) == 8
    for case in cases.values():
        assert case["semantic_review"] and case["manual_semantic_review_required"] is True
        assert case["publication_performed"] is False and case["answer_accuracy_measured"] is False
        assert all("/blob/" in link for link in case["upstream_basis"])
        known = {source["source_id"] for source in case["sources"]}
        assert all(set(entry["source_ids"]) <= known for entry in case["entries"])


def test_chinese_correction_recalls_prior_entry_and_patch_preserves_siblings(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["chinese_correction_finds_prior_target"]
    ranked = sorted(case["entries"], key=lambda entry: -lexical_score(case["query"], entry["content"]))
    assert "纠正" in lexical_terms(case["query"])
    assert ranked[0]["entry_id"] == case["expected"]["target_entry_id"]
    assert lexical_score(case["query"], ranked[0]["content"]) > lexical_score(case["query"], ranked[1]["content"])
    candidate = assemble_candidates(case["reference_proposals"], manifest(case), character_estimate, 8192)[0]
    assert candidate["content"] == case["expected"]["content"]
    assert candidate["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
    canonical = canonical_target_preconditions(conditions(case, entry_ids=[ranked[0]["entry_id"]]))
    assert canonical is not None
    assert [source["source_id"] for source in canonical["targets"][0]["source_versions"]] == case["expected"]["closure_source_ids"]


def test_semantic_first_seat_survives_lexical_arm_without_claiming_real_recall(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["semantic_top_survives_lexical_prefix"]
    result = interleave_targets(case["semantic_order"], case["lexical_order"])
    assert result[0] == case["expected"]["first_entry_id"]
    assert result[:2] == case["expected"]["first_two"]
    assert set(result) == set(case["semantic_order"]) | set(case["lexical_order"])


def test_fusion_deduplicates_ids_without_collapsing_equal_text_lineage(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["deduplicate_ids_not_equal_text"]
    assert case["entries"][0]["content"] == case["entries"][1]["content"]
    assert interleave_targets(case["semantic_order"], case["lexical_order"]) == case["expected"]["entry_ids"]
    canonical = canonical_target_preconditions(conditions(case))
    assert canonical is not None and len(canonical["targets"]) == 2
    assert canonical["targets"][0]["source_versions"] != canonical["targets"][1]["source_versions"]


def test_empty_subject_remains_exact_and_foreign_closure_is_rejected(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["empty_subject_is_exact"]
    identity = case["entries"][0]["entry_id"]
    canonical = canonical_target_preconditions(conditions(case, input_ids=[402], entry_ids=[identity]))
    assert canonical is not None and canonical["inputs"][0]["scope"]["user_id"] == case["expected"]["exact_user_id"]
    request = recommendation_request(source_ids=[402], user_id="", agent_id="assistant")
    assert request["user_id"] == ""
    assert exact_scope({"user_id": " ", "agent_id": "assistant", "session_id": ""})["user_id"] == " "
    with pytest.raises(RevisionValidationError, match=case["expected"]["foreign_source_error"]):
        canonical_target_preconditions(conditions(case, input_ids=[402, 403], entry_ids=[identity]))
    proposal = {
        "action": "patch",
        "target_entry_id": identity,
        "patch": {"old_text": "Python", "new_text": "R"},
        "source_input_ids": ["source-401", "source-402", "source-403"],
        "reason": "Deliberate cross-subject negative control.",
    }
    with pytest.raises(RevisionValidationError, match=case["expected"]["cross_principal_patch_error"]):
        assemble_candidates([proposal], manifest(case), character_estimate, 8192)


def test_structural_validity_does_not_approve_overgeneralized_negative(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["similarity_is_not_semantic_approval"]
    correct = assemble_candidates(case["reference_proposals"], manifest(case), character_estimate, 8192)[0]
    wrong = assemble_candidates(case["known_wrong_host_valid_proposals"], manifest(case), character_estimate, 8192)[0]
    assert correct["content"] == case["expected"]["content"]
    assert wrong["content"] == case["expected"]["wrong_content"]
    assert correct["content"] != wrong["content"]
    for candidate in (correct, wrong):
        assert candidate["assessment"]["verification_status"] == case["expected"]["verification_status"]
    assert "只有批量离线任务" in case["sources"][1]["content"]
    assert "只有批量离线任务" not in wrong["content"]


def test_shown_patch_requires_every_target_source_and_rejects_hidden_target(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["shown_target_requires_full_closure"]
    candidate = assemble_candidates(case["reference_proposals"], manifest(case), character_estimate, 8192)[0]
    assert candidate["content"] == case["expected"]["content"]
    missing = deepcopy(case["reference_proposals"])
    missing[0]["source_input_ids"].remove("source-602")
    with pytest.raises(RevisionValidationError, match=case["expected"]["missing_closure_error"]):
        assemble_candidates(missing, manifest(case), character_estimate, 8192)
    hidden = deepcopy(case["reference_proposals"])
    hidden[0]["target_entry_id"] = case["expected"]["hidden_entry_id"]
    with pytest.raises(RevisionValidationError, match=case["expected"]["hidden_target_error"]):
        assemble_candidates(hidden, manifest(case), character_estimate, 8192)


def test_old_sources_in_a_separate_noop_do_not_complete_patch_evidence(cases: dict[str, dict[str, Any]]) -> None:
    """Reproduce the captured reply shape without relaxing the host contract."""
    case = cases["shown_target_requires_full_closure"]
    packed = manifest(case)
    patch = deepcopy(case["reference_proposals"][0])
    patch["source_input_ids"] = ["source-603"]
    noop = {"action": "noop", "source_input_ids": ["source-601", "source-602"], "reason": "Old evidence is cited here, outside the patch that depends on it."}
    proposals = [patch, noop]
    assert {identity for proposal in proposals for identity in proposal["source_input_ids"]} == {item["input_id"] for item in packed["inputs"]}
    assert set(patch["source_input_ids"]).isdisjoint(noop["source_input_ids"])
    for ordered in (proposals, list(reversed(proposals))):
        with pytest.raises(RevisionValidationError, match=case["expected"]["missing_closure_error"]):
            assemble_candidates(ordered, packed, character_estimate, 8192)


def test_public_confirmation_round_trips_int64_without_float_precision_loss(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["confirmation_changes_are_observable"]
    canonical = canonical_target_preconditions(conditions(case))
    assert canonical is not None
    public = public_conditions(canonical)
    assert public["targets"][0]["message_id"] == case["expected"]["decimal_message_id"]
    assert isinstance(canonical["targets"][0]["message_id"], int)
    assert canonical_target_preconditions(public) == canonical
    assert canonical["targets"][0]["message_id"] == _FIRST_MESSAGE_ID


@pytest.mark.parametrize("change", ["target_revision", "source_generation", "target_projection"])
def test_confirmation_fingerprint_binds_versions_and_projection(cases: dict[str, dict[str, Any]], change: str) -> None:
    case = cases["confirmation_changes_are_observable"]
    original = canonical_target_preconditions(conditions(case))
    assert original is not None and change in case["expected"]["fingerprint_changes"]
    changed = deepcopy(original)
    if change == "target_revision":
        changed["targets"][0]["revision"] += 1
    elif change == "source_generation":
        changed["inputs"][0]["source_generation"] += 2
    else:
        changed["targets"][0]["projection_sha256"] = "0" * 64
    recanonical = canonical_target_preconditions(changed)
    assert recanonical is not None
    assert request_hash(recanonical) != request_hash(original)


@pytest.mark.parametrize("field,value", [("status", False), ("valid_at", "2026-11-17 00:00:00"), ("invalid_at", "2026-11-18 00:00:00"), ("content_embed", [0.1, 0.3])])
def test_projection_fingerprint_binds_state_time_and_vector(cases: dict[str, dict[str, Any]], field: str, value: Any) -> None:
    message = physical(cases["confirmation_changes_are_observable"]["sources"][0])
    assert projection_digest({**message, field: value}) != projection_digest(message)


@pytest.mark.parametrize("identity", ["01", "9007199254740993.0", True, 0, 2**63])
def test_confirmation_rejects_ambiguous_or_out_of_range_identity(cases: dict[str, dict[str, Any]], identity: Any) -> None:
    value = conditions(cases["confirmation_changes_are_observable"])
    value["targets"][0]["message_id"] = identity
    with pytest.raises(RevisionValidationError):
        canonical_target_preconditions(value)


def test_union_closure_is_rejected_instead_of_silently_clipped(cases: dict[str, dict[str, Any]]) -> None:
    value = conditions(cases["shown_target_requires_full_closure"])
    template = value["targets"][0]["source_versions"][0]
    value["targets"][0]["source_versions"] = [{**deepcopy(template), "source_id": 1000 + index} for index in range(32)]
    with pytest.raises(RevisionValidationError, match="target_closure_limit"):
        canonical_target_preconditions(value)


def test_individually_selectable_alternatives_may_exceed_combined_closure(cases: dict[str, dict[str, Any]]) -> None:
    """Recommendations are alternatives; submit only the user's chosen subset."""
    value = conditions(cases["shown_target_requires_full_closure"])
    template = value["targets"][0]
    source = template["source_versions"][0]
    first, second = deepcopy(template), deepcopy(template)
    second["entry_id"] = "2" * 32
    second["message_id"] += 1
    first["source_versions"] = [{**deepcopy(source), "source_id": 1000 + index} for index in range(16)]
    second["source_versions"] = [{**deepcopy(source), "source_id": 1016 + index} for index in range(16)]
    for selected in (first, second):
        assert canonical_target_preconditions({**value, "targets": [selected]}) is not None
    with pytest.raises(RevisionValidationError, match="target_closure_limit"):
        canonical_target_preconditions({**value, "targets": [first, second]})


def test_repeated_source_with_different_generation_cannot_hide_in_closure(cases: dict[str, dict[str, Any]]) -> None:
    value = conditions(cases["shown_target_requires_full_closure"], input_ids=[601, 603])
    value["targets"][0]["source_versions"][0]["source_generation"] += 2
    with pytest.raises(RevisionValidationError, match="target_scope_mismatch"):
        canonical_target_preconditions(value)


def test_full_target_fingerprint_binds_tail_and_never_equates_it_with_prefix(cases: dict[str, dict[str, Any]]) -> None:
    case = expanded(cases["bounded_target_body_not_a_prefix"])
    full = case["entries"][0]["content"]
    assert full.endswith(case["expected"]["tail_fact"])
    assert len(full) > 1024 and case["expected"]["tail_fact"] not in full[:1024]
    value = canonical_target_preconditions(conditions(case))
    assert value is not None and value["targets"][0]["content_sha256"] == content_hash(full)
    assert value["targets"][0]["content_sha256"] != content_hash(full[:1024])
    message = physical(case["sources"][0], message_id=_FIRST_MESSAGE_ID, content=full, source_id=801)
    assert projection_digest(message) != projection_digest({**message, "content": full[:1024]})
    assert case["expected"]["prefix_is_not_complete_target"] is True
