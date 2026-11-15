"""Complete source partitions, exact subject boundaries and effective coverage."""

from typing import Any

import pytest

from memory.maintenance.selection import canonical_scope, effective_coverage, partition_sources, select_sources, source_key
from memory.revisions.domain import RevisionValidationError


def source(identity: int, *, user: str = "user", agent: str = "agent", session: str = "session") -> dict[str, Any]:
    return {"source_id": identity, "source_revision": 1, "source_generation": 1, "content_sha256": "a" * 64, "scope": {"user_id": user, "agent_id": agent, "session_id": session}}


def test_partition_never_drops_a_primary_or_mixes_subjects() -> None:
    rows = [source(index, user="a" if index % 2 else "b", session=str(index)) for index in range(1, 71)]
    batches = partition_sources(rows, [], [], batch_size=16)
    flat = [item["source_id"] for batch in batches for item in batch["primary_sources"]]
    assert sorted(flat) == list(range(1, 71)) and len(flat) == len(set(flat))
    assert all(len(batch["primary_sources"]) <= 16 for batch in batches)
    assert all(len({(item["scope"]["user_id"], item["scope"]["agent_id"]) for item in batch["primary_sources"]}) == 1 for batch in batches)


def test_explicit_context_is_repeated_within_count_budget_and_matching_subject() -> None:
    rows = [source(index) for index in range(1, 7)] + [source(20, user="other")]
    context = [source(1), source(100)]
    entries = [{"entry_id": "entry", "scope": source(100)["scope"]}]
    batches = partition_sources(rows, context, entries, batch_size=4)
    for batch in batches:
        ids = {item["source_id"] for item in [*batch["primary_sources"], *batch["context_sources"]]}
        assert len(ids) <= 4
        if batch["primary_sources"][0]["scope"]["user_id"] == "other":
            assert batch["context_sources"] == batch["entry_ids"] == []
        else:
            assert {1, 100} <= ids and batch["entry_ids"] == ["entry"]
    assert sum(len(batch["primary_sources"]) for batch in batches) == 7
    with pytest.raises(RevisionValidationError, match="incremental_context_exhausts_batch"):
        partition_sources([source(3)], context, entries, batch_size=2)


def test_known_blocked_and_covered_low_ids_do_not_starve_late_arrivals() -> None:
    rows = [source(index) for index in range(1, 301)]
    selected = select_sources(rows, {index: "covered" if index % 2 else "blocked" for index in range(1, 299)}, max_sources=1)
    assert [item["source_id"] for item in selected["sources"]] == [299]
    assert selected["counts"]["blocked"] == selected["counts"]["covered"] == 149 and selected["has_more"]
    original = source_key("bank", "profile", source(1))
    assert source_key("bank", "profile", {**source(1), "source_generation": 2}) != original
    assert source_key("bank", "new-model-profile", source(1)) != original


def test_scope_null_empty_and_whitespace_are_distinct() -> None:
    assert canonical_scope({"user_id": "", "agent_id": " ", "session_id": None}) == {"user_id": "", "agent_id": " ", "session_id": None}
    with pytest.raises(RevisionValidationError):
        canonical_scope({"user_id": None})


@pytest.mark.parametrize(
    "field,value", [("ledger_state", "reserved"), ("job_state", "failed"), ("execution_state", "running"), ("admission_exists", False), ("actual_token_present", True), ("source_matches", False)]
)
def test_saved_result_never_hides_unsettled_or_changed_work(field: str, value: Any) -> None:
    data: dict[str, Any] = {"ledger_state": "analyzed", "job_state": "ready", "execution_state": "succeeded", "admission_exists": True, "actual_token_present": False, "source_matches": True}
    assert effective_coverage(**data)
    data[field] = value
    assert not effective_coverage(**data)
