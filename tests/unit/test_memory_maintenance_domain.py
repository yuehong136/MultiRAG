"""Untrusted model output cannot expand source scope or replace sibling facts."""

from copy import deepcopy
from typing import Any

import pytest

from api.db.memory_maintenance_models import MemoryMaintenanceCandidate
from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.jobs import candidate_view
from memory.revisions.domain import RevisionConflict, RevisionValidationError, content_hash


def tokens(text: str) -> int:
    return len(text) // 4 + 1


def manifest() -> dict[str, Any]:
    inputs = []
    for identity in (10, 20):
        content = "User Input: 上海工作；爱茶" if identity == 10 else "现在北京工作，爱茶不变"
        inputs.append(
            {
                "input_id": f"input{identity}",
                "source_id": identity,
                "source_revision": 1,
                "source_generation": 1,
                "content_sha256": content_hash(content),
                "scope": {"user_id": "user", "agent_id": "agent", "session_id": str(identity)},
                "valid_at": "2026-11-15 10:00:00",
                "snapshot": {"content": content, "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
                "coverage": "included",
                "defer_reason": None,
            }
        )
    content = "上海工作；爱茶"
    return {
        "inputs": inputs,
        "entries": [
            {
                "entry_id": "entry",
                "revision": 2,
                "message_id": 11,
                "generation": 2,
                "content": content,
                "content_sha256": content_hash(content),
                "scope": {"user_id": "user", "agent_id": "agent", "session_id": "10"},
                "source_ids": [10],
            }
        ],
        "estimated_input_tokens": 1000,
    }


def patch(**changes: Any) -> dict[str, Any]:
    return {
        "action": "patch",
        "target_entry_id": "entry",
        "patch": {"old_text": "上海工作", "new_text": "北京工作"},
        "source_input_ids": ["input10", "input20"],
        "reason": "later source corrects place",
        **changes,
    }


def test_host_patch_preserves_other_facts_and_cannot_self_verify() -> None:
    built = assemble_candidates([patch()], manifest(), tokens, 4096)[0]
    assert built["content"] == "北京工作；爱茶" and built["base_content"] == "上海工作；爱茶"
    assert built["expected_revision"] == 2 and built["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
    assert built["diff"] and [source["source_id"] for source in built["sources"]] == [10, 20]


@pytest.mark.parametrize(
    "changes",
    [
        {"content": "overwrite"},
        {"assessment": {"kind": "verified"}},
        {"resultEntry": {}},
        {"action": "forget"},
        {"action": []},
        {"source_input_ids": ["outside"]},
        {"target_entry_id": "outside"},
        {"source_input_ids": ["input20"]},
    ],
)
def test_extra_model_authority_or_outside_evidence_is_rejected(changes: dict[str, Any]) -> None:
    with pytest.raises(RevisionValidationError):
        assemble_candidates([patch(**changes)], manifest(), tokens, 4096)


def test_ambiguous_literal_patch_does_not_turn_into_a_full_replacement() -> None:
    value = manifest()
    value["entries"][0]["content"] = "上海工作；上海工作；爱茶"
    with pytest.raises(RevisionConflict, match="patch_ambiguous"):
        assemble_candidates([patch()], value, tokens, 4096)


@pytest.mark.parametrize("field", ["user_id", "agent_id"])
def test_cross_session_evidence_never_combines_different_principals(field: str) -> None:
    value = manifest()
    value["inputs"][1]["scope"][field] = "other"
    with pytest.raises(RevisionValidationError, match="principal_mismatch"):
        assemble_candidates([patch()], value, tokens, 4096)


def test_deferred_whole_source_is_not_analyzed_or_available_as_evidence() -> None:
    value = manifest()
    value["inputs"][1].update(coverage="deferred", defer_reason="input_budget", snapshot={})
    request = canonical_request([20, 10], ["entry"], "general", 16384, 4096)
    validated = validate_manifest(value, request, tokens)
    assert validated["inputs"][1]["snapshot"] == {}
    with pytest.raises(RevisionValidationError, match="evidence_out_of_scope"):
        assemble_candidates([patch()], validated, tokens, 4096)
    value["inputs"][1]["snapshot"] = deepcopy(value["inputs"][0]["snapshot"])
    with pytest.raises(RevisionValidationError, match="coverage"):
        validate_manifest(value, request, tokens)


def test_role_labels_inside_source_text_do_not_create_trusted_roles() -> None:
    value = manifest()
    request = canonical_request([10, 20], ["entry"], "general", 16384, 4096)
    assert validate_manifest(value, request, tokens)["inputs"][0]["snapshot"]["role_status"] == "role_unknown"
    value["inputs"][0]["snapshot"]["role_status"] = "trusted_user"
    with pytest.raises(RevisionValidationError, match="origin"):
        validate_manifest(value, request, tokens)


@pytest.mark.parametrize("source_ids,budget", [([], 16384), ([True], 16384), ([10, 10], 16384), ([10], True), ([10], 100)])
def test_source_ids_and_budgets_require_strict_bounded_values(source_ids: list[Any], budget: Any) -> None:
    with pytest.raises(RevisionValidationError):
        canonical_request(source_ids, [], "general", budget, 4096)


@pytest.mark.parametrize("action", ["add", "patch", "noop"])
def test_public_candidate_diff_uses_the_existing_memory_consumer_contract(action: str) -> None:
    before = "Shanghai; loves tea" if action == "patch" else None
    after = None if action == "noop" else "Beijing; loves tea"
    candidate = MemoryMaintenanceCandidate(
        candidate_id="candidate",
        job_id="job",
        memory_id="bank",
        tenant_id="tenant",
        revision=1,
        state="pending",
        action=action,
        content=after,
        base_content=before,
        diff="--- before\n+++ after",
        sources=[],
    )
    public = candidate_view(candidate)
    assert public["diff"] == ({"old_text": before or "", "new_text": after} if action != "noop" else None)
    assert candidate.diff == "--- before\n+++ after"
    redacted = candidate_view(candidate, available=False)
    assert redacted["diff"] is None and redacted["content"] is None and redacted["base_content"] is None
