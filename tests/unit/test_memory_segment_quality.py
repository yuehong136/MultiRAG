"""Fixed fragment and explicit-context cases, not model accuracy or SQL acceptance.

Production span planning, manifest validation and candidate assembly run here.
The character counter is a declared unit surrogate; no provider, publication,
database, durable coverage settlement or automatic cross-batch merge runs.
"""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.evaluation import case_manifest, load_cases
from memory.maintenance.segments import CONTEXT_CODEPOINTS, segmentation_request, split_source, validate_fragment
from memory.maintenance.snapshots import analysis_payload
from memory.revisions.domain import RevisionValidationError, content_hash


@pytest.fixture
def cases() -> dict[str, dict[str, Any]]:
    values = load_cases(Path(__file__).parents[1] / "fixtures/memory_segment_quality.json")
    return {case["case_id"]: case for case in values}


def character_estimate(text: str) -> int:
    """Explicit character surrogate, not a provider-tokenizer guarantee."""
    return len(text)


def expanded(case: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(case)
    for source in result["sources"]:
        source["content"] = source.pop("prefix", "") * source.pop("prefix_repeat", 1) + source["content"] * source.pop("repeat", 1) + source.pop("suffix", "")
    return result


def manifest(case: dict[str, Any]) -> dict[str, Any]:
    return case_manifest(expanded(case), character_estimate)


def source_input(case: dict[str, Any], source_id: int | None = None) -> dict[str, Any]:
    inputs = manifest(case)["inputs"]
    return inputs[0] if source_id is None else next(item for item in inputs if item["source_id"] == source_id)


def planned_parts(case: dict[str, Any], source_id: int | None = None, *, max_parts: int = 32) -> list[dict[str, Any]]:
    source = source_input(case, source_id)
    configuration = segmentation_request({"mode": "segmented", "fragment_token_limit": 256, "max_parts_per_source": max_parts})
    assert configuration is not None
    return split_source("quality-bank", source, source["snapshot"]["content"], configuration, count_tokens=character_estimate, fits=lambda _fragment: True)


def fragment_manifest(case: dict[str, Any], fragments: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Keep explicitly selected whole context and replace only planned read spans."""
    result = manifest(case)
    for source in result["inputs"]:
        if source["source_id"] in fragments:
            fragment = fragments[source["source_id"]]
            full = source["snapshot"]["content"]
            source["input_id"] = "part-" + fragment["fragment_id"][:24]
            source["fragment"] = deepcopy(fragment)
            source["snapshot"]["content"] = full[fragment["read_start"] : fragment["read_end"]]
    result["estimated_input_tokens"] = character_estimate(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    request = canonical_request([source["source_id"] for source in result["inputs"]], [entry["entry_id"] for entry in result["entries"]], "general", 32768, 8192)
    return validate_manifest(result, request, character_estimate)


def bound_proposals(case: dict[str, Any], packed: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve authored source labels to the host's selected fragment input IDs."""
    names = {f"source-{source['source_id']}": source["input_id"] for source in packed["inputs"]}
    result = deepcopy(case["reference_proposals"])
    for proposal in result:
        proposal["source_input_ids"] = [names[identity] for identity in proposal["source_input_ids"]]
    return result


def part_containing(case: dict[str, Any], source_id: int, statement: str) -> dict[str, Any]:
    content = source_input(case, source_id)["snapshot"]["content"]
    return next(part for part in planned_parts(case, source_id) if statement in content[part["read_start"] : part["read_end"]])


def assert_exact_source_spans(content: str, parts: list[dict[str, Any]]) -> None:
    assert parts and parts[0]["core_start"] == 0 and parts[-1]["core_end"] == len(content)
    assert [part["part_index"] for part in parts] == list(range(len(parts)))
    assert all(part["part_count"] == len(parts) for part in parts)
    assert len({part["fragment_id"] for part in parts}) == len(parts)
    assert all(left["core_end"] == right["core_start"] for left, right in pairwise(parts))
    assert "".join(content[part["core_start"] : part["core_end"]] for part in parts) == content
    for part in parts:
        read = content[part["read_start"] : part["read_end"]]
        assert part["offset_unit"] == "unicode_codepoint"
        assert part["core_start"] - part["read_start"] <= CONTEXT_CODEPOINTS
        assert part["read_end"] - part["core_end"] <= CONTEXT_CODEPOINTS
        assert len(read) <= 256
        assert part["core_sha256"] == content_hash(content[part["core_start"] : part["core_end"]])
        assert validate_fragment(part, content=read) == part


def test_fixture_exposes_gold_and_does_not_claim_semantic_automation(cases: dict[str, dict[str, Any]]) -> None:
    assert len(cases) == 8
    for case in cases.values():
        assert case["gold_facts"] and case["semantic_review"] and case["manual_semantic_review_required"]
        assert case["publication_performed"] is False and case["answer_accuracy_measured"] is False
        assert all("/blob/" in link for link in case["upstream_basis"])
        known = {source["source_id"] for source in case["sources"]}
        for fact in case["gold_facts"]:
            assert set(fact.get("source_ids", [fact.get("source_id")])) <= known


@pytest.mark.parametrize(
    "case_id",
    ["unicode_original_spans", "condition_negation_overlap", "many_facts_late_fragment", "identical_text_distinct_locations", "source_version_and_part_limit", "fragment_payload_provenance"],
)
def test_core_union_exactly_reconstructs_source_without_crediting_overlap(cases: dict[str, dict[str, Any]], case_id: str) -> None:
    case = cases[case_id]
    content = source_input(case)["snapshot"]["content"]
    parts = planned_parts(case)
    assert len(parts) >= case["expected"]["minimum_parts"]
    assert_exact_source_spans(content, parts)
    assert sum(part["core_end"] - part["core_start"] for part in parts) == len(content)
    assert sum(part["read_end"] - part["read_start"] for part in parts) > len(content)
    for fact in case["gold_facts"]:
        assert fact["statement"] in content
        assert any(fact["statement"] in content[part["read_start"] : part["read_end"]] for part in parts)


def test_emoji_combining_marks_and_whitespace_are_not_normalized(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["unicode_original_spans"]
    content = source_input(case)["snapshot"]["content"]
    assert content.startswith("🙂e\u0301中\n\n")
    assert len("🙂e\u0301") == 3 and len("🙂e\u0301".encode()) == 7
    assert "\r\n" in content and "  " in content
    parts = planned_parts(case)
    assert_exact_source_spans(content, parts)
    assert case["expected"]["normalization_performed"] is False


def test_overlap_retains_condition_negation_and_independent_sibling(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["condition_negation_overlap"]
    content = source_input(case)["snapshot"]["content"]
    parts = planned_parts(case)
    assert parts[0]["core_end"] == case["expected"]["first_core_end"]
    condition = case["gold_facts"][0]["statement"]
    start = content.index(condition)
    assert start < parts[0]["core_end"] < start + len(condition)
    selected = part_containing(case, 37, condition)
    packed = fragment_manifest(case, {37: selected})
    assert condition in analysis_payload(packed, "procedures")["inputs"][0]["snapshot"]["content"]
    candidate = assemble_candidates(bound_proposals(case, packed), packed, character_estimate, 8192)[0]
    assert "未经批准不得重启" in candidate["content"] and "只读健康检查始终可用" in candidate["content"]
    assert candidate["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}


def test_early_consumption_does_not_shrink_the_planned_late_fragment_inventory(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["many_facts_late_fragment"]
    content = source_input(case)["snapshot"]["content"]
    parts = planned_parts(case)
    consumed = {parts[index]["fragment_id"] for index in case["expected"]["consumed_parts"]}
    replanned = planned_parts(case)
    assert replanned == parts
    remaining = [part for part in replanned if part["fragment_id"] not in consumed]
    needle = case["gold_facts"][-1]["statement"]
    assert needle not in content[parts[0]["read_start"] : parts[0]["read_end"]]
    assert any(needle in content[part["read_start"] : part["read_end"]] for part in remaining)
    assert len(consumed) < len(parts) and case["expected"]["whole_source_covered"] is False
    assert sum(fact["statement"] in content[: parts[0]["read_end"]] for fact in case["gold_facts"][:-1]) >= 2


def test_equal_text_at_distinct_positions_does_not_deduplicate_fragment_identity(cases: dict[str, dict[str, Any]]) -> None:
    parts = planned_parts(cases["identical_text_distinct_locations"])
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for part in parts:
        by_hash[part["core_sha256"]].append(part)
    repeated = next(group for group in by_hash.values() if len(group) > 1)
    assert len({part["core_start"] for part in repeated}) == len(repeated)
    assert len({part["fragment_id"] for part in repeated}) == len(repeated)


def test_explicit_prior_target_and_full_source_closure_preserve_sibling(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["explicit_prior_entry_correction"]
    correction = "更正：入口端口改为8081，备份时间不变。"
    fragment = part_containing(case, 2202, correction)
    packed = fragment_manifest(case, {2202: fragment})
    assert "fragment" not in packed["inputs"][0]
    proposals = bound_proposals(case, packed)
    candidate = assemble_candidates(proposals, packed, character_estimate, 8192)[0]
    assert candidate["content"] == case["expected"]["candidate_content"]
    assert candidate["base_content"] == case["entries"][0]["content"]
    assert candidate["expected_revision"] == 1
    assert {source["source_id"] for source in candidate["sources"]} == {2201, 2202}
    assert next(source for source in candidate["sources"] if source["source_id"] == 2202)["fragment"] == fragment

    no_target = deepcopy(packed)
    no_target["entries"] = []
    with pytest.raises(RevisionValidationError, match=case["expected"]["missing_target_error"]):
        assemble_candidates(proposals, no_target, character_estimate, 8192)
    no_closure = deepcopy(proposals)
    no_closure[0]["source_input_ids"] = [next(source["input_id"] for source in packed["inputs"] if source["source_id"] == 2202)]
    with pytest.raises(RevisionValidationError, match=case["expected"]["missing_closure_error"]):
        assemble_candidates(no_closure, packed, character_estimate, 8192)


def test_late_older_rule_negative_control_exposes_the_need_for_semantic_review(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["older_decision_imported_later"]
    packed = manifest(case)
    assert packed["inputs"][1]["source_id"] > packed["inputs"][0]["source_id"]
    assert packed["inputs"][1]["valid_at"] < packed["inputs"][0]["valid_at"]
    original = deepcopy(packed["entries"])
    reference = assemble_candidates(case["reference_proposals"], packed, character_estimate, 8192)
    assert reference[0]["action"] == case["expected"]["reference_action"]
    assert reference[0]["content"] is None and packed["entries"] == original

    wrong = assemble_candidates(case["known_wrong_host_valid_proposals"], packed, character_estimate, 8192)[0]
    assert wrong["content"] == case["expected"]["wrong_record"]
    assert wrong["assessment"]["verification_status"] == "unverified"
    assert case["expected"]["automatic_semantic_rejection"] is False
    assert case["gold_facts"][0]["statement"] not in wrong["content"]


def test_source_version_change_invalidates_identity_and_limits_never_return_a_prefix(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["source_version_and_part_limit"]
    source = source_input(case)
    content = source["snapshot"]["content"]
    configuration = segmentation_request({"mode": "segmented", "fragment_token_limit": 256})
    assert configuration is not None
    original = planned_parts(case)
    changed = {**source, "source_generation": source["source_generation"] + 2}
    after = split_source("quality-bank", changed, content, configuration, count_tokens=character_estimate, fits=lambda _part: True)
    assert {part["fragment_id"] for part in original}.isdisjoint(part["fragment_id"] for part in after)
    assert [(part["core_start"], part["core_end"]) for part in after] == [(part["core_start"], part["core_end"]) for part in original]
    with pytest.raises(RevisionValidationError, match=case["expected"]["max_one_part_error"]):
        planned_parts(case, max_parts=1)
    with pytest.raises(RevisionValidationError, match=case["expected"]["no_read_span_fits_error"]):
        split_source("quality-bank", source, content, configuration, count_tokens=character_estimate, fits=lambda _part: False)


def test_fragment_payload_keeps_whole_source_identity_and_rejects_changed_read_text(cases: dict[str, dict[str, Any]]) -> None:
    case = cases["fragment_payload_provenance"]
    source = source_input(case)
    fragment = part_containing(case, 46, case["gold_facts"][0]["statement"])
    packed = fragment_manifest(case, {46: fragment})
    read = packed["inputs"][0]["snapshot"]["content"]
    assert read == source["snapshot"]["content"][fragment["read_start"] : fragment["read_end"]]
    assert content_hash(read) == fragment["read_sha256"] != source["content_sha256"]
    assert packed["inputs"][0]["content_sha256"] == source["content_sha256"]
    assert packed["inputs"][0]["input_id"] == "part-" + fragment["fragment_id"][:24]
    candidate = assemble_candidates(bound_proposals(case, packed), packed, character_estimate, 8192)[0]
    assert candidate["sources"][0]["content_sha256"] == source["content_sha256"]
    assert candidate["sources"][0]["fragment"] == fragment
    assert packed["inputs"][0]["snapshot"]["role_status"] == "role_unknown"
    with pytest.raises(RevisionValidationError, match=case["expected"]["tamper_error"]):
        validate_fragment(fragment, content=read[:-1] + "X")
