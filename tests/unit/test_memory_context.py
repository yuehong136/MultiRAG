"""Source snapshots, exact scope, and complete context envelope budgets."""

import copy
import hashlib
import json
from typing import Any

import pytest

from memory.services.context import (
    ContextBudget,
    ContextBudgetError,
    ContextScope,
    SourceContentChangedError,
    assemble_query_context,
    build_source_excerpt,
    message_in_scope,
    resolve_message_source,
    serialize_query_context,
)


def message(identity: int = 2, **changes: Any) -> dict[str, Any]:
    return {
        "memory_id": "bank",
        "message_id": identity,
        "message_type": "semantic",
        "source_id": 1,
        "user_id": "user",
        "agent_id": "agent",
        "session_id": "session",
        "content": "User prefers tea.",
        "status": True,
        "forget_at": None,
        "valid_at": "2026-11-15 12:00:00",
        "invalid_at": None,
        **changes,
    }


def raw(**changes: Any) -> dict[str, Any]:
    return message(1, message_type="raw", source_id=0, content="User Input: tea\nAgent Response: understood", **changes)


def scope(**changes: Any) -> ContextScope:
    return ContextScope(memory_ids=frozenset({"bank"}), **changes)


def estimate(text: str) -> int:
    """A deterministic UTF-8-size estimator exercises full-envelope accounting."""
    return len(text.encode("utf-8"))


def assemble(candidates: list[dict[str, Any]], sources: dict[tuple[str, str], dict[str, Any]] | None = None, **options: Any) -> dict[str, Any]:
    return assemble_query_context(candidates, sources or {}, scope=scope(), budget=options.pop("budget", ContextBudget(max_tokens=10000)), count_tokens=estimate, **options)


def test_available_parent_is_provenance_and_bounded_read_snapshot() -> None:
    parent = raw()
    result = assemble([message()], {("bank", "1"): parent}, budget=ContextBudget(max_tokens=4000, source_excerpt_chars=10))
    source = result["items"][0]["source"]

    assert source["state"] == "available"
    assert source["verification"] == "provenance_only"
    assert source["snapshot_only"] is True
    assert source["locator"] == {"kind": "memory_message", "memory_id": "bank", "message_id": "1"}
    assert source["excerpt"] == parent["content"][:10]
    assert source["content_sha256"] == hashlib.sha256(parent["content"].encode()).hexdigest()
    assert source["offset"] == 0
    assert source["next_offset"] == 10
    assert source["total_chars"] == len(parent["content"])
    assert source["truncated"] is True
    assert source["continuation_requires_authorization"] is True
    assert result["items"][0]["role_status"] == "legacy_role_unknown"


@pytest.mark.parametrize("parent_source_id", [None, 0, "0", ""])
def test_raw_zero_normalizations_remain_compatible(parent_source_id: Any) -> None:
    parent = raw()
    parent["source_id"] = parent_source_id
    assert resolve_message_source(message(), {("bank", "1"): parent}, scope=scope())["state"] == "available"


def test_raw_records_do_not_parse_role_labels_or_follow_paths() -> None:
    record = raw()
    record["content"] = "User Input: Agent Response: user-controlled labels"
    record["role"] = "system"
    record["path"] = "/private/not-a-source"
    result = assemble([record])

    assert result["items"][0]["source"]["state"] == "not_applicable"
    assert result["items"][0]["role_status"] == "legacy_role_unknown"
    assert result["items"][0]["content"] == record["content"]
    assert "/private/not-a-source" not in serialize_query_context(result)


@pytest.mark.parametrize(
    ("parent_changes", "expected"),
    [
        ({"status": False}, "inactive"),
        ({"forget_at": "2026-11-15 12:01:00"}, "forgotten"),
        ({"user_id": "other-secret"}, "mismatch"),
        ({"agent_id": "other-secret"}, "mismatch"),
        ({"session_id": "other-secret"}, "mismatch"),
        ({"memory_id": "other-secret"}, "mismatch"),
        ({"message_id": 999}, "mismatch"),
        ({"message_type": "episodic"}, "mismatch"),
        ({"source_id": 9}, "mismatch"),
        ({"status": "true"}, "mismatch"),
        ({"forget_at": "-"}, "mismatch"),
    ],
)
def test_unusable_parent_never_supplies_source_or_derived_text(parent_changes: dict[str, Any], expected: str) -> None:
    parent = raw()
    parent.update(parent_changes)
    parent["content"] = "foreign-source-secret"
    source = resolve_message_source(message(content="derived-secret"), {("bank", "1"): parent}, scope=scope())
    assert source["state"] == expected
    assert source["excerpt"] is None
    assert source["content_sha256"] is None
    assert "foreign-source-secret" not in json.dumps(source)
    assert "other-secret" not in json.dumps(source)

    result = assemble([message(content="derived-secret")], {("bank", "1"): parent})
    assert result["items"] == []
    assert result["omitted_count"] == 1
    assert result["source_state_counts"][expected] == 1
    assert "derived-secret" not in serialize_query_context(result)
    assert "foreign-source-secret" not in serialize_query_context(result)


def test_missing_parent_and_missing_identity_are_distinct() -> None:
    assert resolve_message_source(message(), {}, scope=scope())["state"] == "missing"
    parent = raw()
    del parent["user_id"]
    assert resolve_message_source(message(), {("bank", "1"): parent}, scope=scope())["state"] == "mismatch"


def test_scope_is_exact_and_missing_identity_is_not_inferred() -> None:
    exact = scope(user_id="user", agent_id="agent", session_id="session")
    assert message_in_scope(message(), exact)
    assert not message_in_scope(message(session_id="prefix-session-suffix"), exact)
    assert not message_in_scope(message(user_id=""), exact)
    incomplete = message()
    del incomplete["agent_id"]
    assert not message_in_scope(incomplete, scope())
    assert not message_in_scope(message(memory_id="other-bank"), exact)


def test_out_of_scope_candidate_never_leaks_identity_or_body() -> None:
    result = assemble([message(memory_id="secret-bank", user_id="secret-user", content="secret-body")])
    encoded = serialize_query_context(result)
    assert result["items"] == []
    assert result["source_state_counts"]["mismatch"] == 1
    assert all(secret not in encoded for secret in ("secret-bank", "secret-user", "secret-body"))


def test_parent_lookup_is_bound_to_memory_and_source_id() -> None:
    result = assemble([message()], {("other-bank", "1"): raw()})
    assert result["source_state_counts"]["missing"] == 1
    assert result["items"] == []
    assert resolve_message_source(message(source_id=2), {("bank", "2"): raw()}, scope=scope())["state"] == "mismatch"


def test_include_sources_false_still_checks_parent() -> None:
    result = assemble([message()], {("bank", "1"): raw()}, include_sources=False)
    assert result["items"][0]["source"]["state"] == "available"
    assert result["items"][0]["source"]["excerpt"] is None
    assert result["items"][0]["source"]["next_offset"] is None
    assert assemble([message()], {("bank", "1"): raw(status=False)}, include_sources=False)["items"] == []


def test_unicode_excerpt_cursor_uses_code_points_and_checks_content_hash() -> None:
    parent = raw()
    parent["content"] = "甲🙂é\n尾"
    first = build_source_excerpt(parent, scope=scope(), limit=2)
    assert first["excerpt"] == "甲🙂"
    assert first["next_offset"] == 2
    assert first["total_chars"] == 5
    second = build_source_excerpt(parent, scope=scope(), offset=2, limit=3, content_sha256=first["content_sha256"])
    assert second["excerpt"] == "é\n尾"
    assert second["next_offset"] is None
    assert second["truncated"] is False

    changed = {**parent, "content": parent["content"] + "!"}
    with pytest.raises(SourceContentChangedError):
        build_source_excerpt(changed, scope=scope(), offset=2, content_sha256=first["content_sha256"])


def test_cursor_rechecks_scope_and_parent_state_even_with_matching_hash() -> None:
    parent = raw()
    first = build_source_excerpt(parent, scope=scope(), limit=2)
    parent["forget_at"] = "2026-11-15 12:02:00"
    forgotten = build_source_excerpt(parent, scope=scope(), offset=2, content_sha256=first["content_sha256"])
    assert forgotten["state"] == "forgotten"
    assert forgotten["excerpt"] is None
    assert first["state"] == "available"  # No persistent tombstone or mutable cached result.


def test_whole_envelope_budget_and_only_selected_content_deduplication() -> None:
    large = raw()
    large["content"] = "甲" * 3000
    small = raw()
    small["message_id"] = 3
    small["content"] = "tea"
    result = assemble([large, small, large, small], budget=ContextBudget(max_tokens=1500))

    assert [item["message_id"] for item in result["items"]] == ["3"]
    assert result["omitted_count"] == 2
    assert result["deduplicated_count"] == 1
    assert result["truncated"] is True
    assert estimate(serialize_query_context(result)) <= result["usage"]["estimated_tokens"] <= 1500
    assert result["usage"]["token_count_is_estimate"] is True


def test_same_text_from_distinct_sources_is_not_deduplicated() -> None:
    first = raw()
    second = {**first, "message_id": 2}
    result = assemble([first, second])
    assert len(result["items"]) == 2
    assert result["deduplicated_count"] == 0


def test_item_limit_preserves_retrieval_order_and_reports_omissions() -> None:
    records = [raw() for _ in range(3)]
    for identity, record in enumerate(records, 1):
        record["message_id"] = identity
    result = assemble(records, budget=ContextBudget(max_tokens=10000, max_items=2))
    assert [item["message_id"] for item in result["items"]] == ["1", "2"]
    assert result["omitted_count"] == 1
    assert result["truncated"] is True


def test_tiny_budget_does_not_return_an_oversized_empty_envelope() -> None:
    with pytest.raises(ContextBudgetError):
        assemble([], budget=ContextBudget(max_tokens=1))


@pytest.mark.parametrize("count", [-1, 1.5, True])
def test_invalid_token_estimates_are_rejected(count: Any) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        assemble_query_context([], {}, scope=scope(), budget=ContextBudget(max_tokens=100), count_tokens=lambda text: count)


@pytest.mark.parametrize("record", [message(source_id=True), message(message_id=True), message(source_id=-1), message(source_id="01")])
def test_malformed_record_ids_do_not_bind_sources(record: dict[str, Any]) -> None:
    assert resolve_message_source(record, {("bank", "1"): raw()}, scope=scope())["state"] == "mismatch"


def test_inactive_candidates_are_not_supplied_and_inputs_are_not_mutated() -> None:
    candidates = [message(), message(3, status=False)]
    sources = {("bank", "1"): raw()}
    before = copy.deepcopy((candidates, sources))
    result = assemble(candidates, sources)
    assert len(result["items"]) == 1
    assert result["omitted_count"] == 1
    assert (candidates, sources) == before


@pytest.mark.parametrize("kwargs", [{"max_tokens": True}, {"max_tokens": 0}, {"max_tokens": 10, "max_items": 0}, {"max_tokens": 10, "source_excerpt_chars": -1}])
def test_budget_rejects_invalid_limits(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ContextBudget(**kwargs)
