"""Production input/closure guards and blinded-review protocol, not model scoring."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_source_service
from api.db.joint_services import memory_write_runtime
from memory.maintenance.domain import assemble_candidates, canonical_request, validate_manifest
from memory.maintenance.provider import MaintenanceAnalyst
from memory.maintenance.snapshots import analysis_payload
from memory.revisions.domain import RevisionValidationError, content_hash
from memory.structured_sources import evidence_snapshot, source_envelope, submission_metadata
from memory.writes.providers import MemoryWriteProviders
from tests.support import memory_source_comparison as comparison

FIXTURE = Path(__file__).parents[1] / "fixtures/memory_source_comparison_baseline.json"


def case() -> dict[str, Any]:
    return comparison.load_cases(FIXTURE, expected_kind="known_baseline")[0]


def manifest(branch: comparison.Branch, *, two_sources: bool = False) -> dict[str, Any]:
    source = case()["sources"][0]
    inputs = []
    for index in range(2 if two_sources else 1):
        content = comparison.expected_content(source, branch)
        metadata = None
        if branch == "structured":
            metadata = submission_metadata(
                actor_id="owner", operation_id=f"source-{index}", content_sha256=content_hash(content), received_at="2026-11-16T12:00:00+00:00", reference_time="2026-11-16 20:00:00"
            )
        inputs.append(
            {
                "input_id": f"source-{index + 1}",
                "source_id": index + 1,
                "source_revision": 1,
                "source_generation": 1,
                "content_sha256": content_hash(content),
                "scope": {"user_id": comparison.SUBJECT, "agent_id": comparison.AGENT, "session_id": source["session_id"]},
                "valid_at": "2026-11-16 20:00:00",
                "snapshot": evidence_snapshot(content, metadata),
                "coverage": "included",
                "defer_reason": None,
            }
        )
    return {"inputs": inputs, "entries": [], "estimated_input_tokens": 200}


def branch_view(name: str, *, empty: bool = False) -> dict[str, Any]:
    candidates = (
        []
        if empty
        else [
            {
                "candidate_id": "hidden-" + name,
                "action": "add",
                "content": "仅限离线测试；不授权生产。",
                "reason": "有范围的声明",
                "assessment": {"kind": "hypothesis", "verification_status": "unverified"},
                "sources": [{"source_id": 9007199254741093}],
            }
        ]
    )
    return {"branch": name, "state": "succeeded", "sources": [{"source_id": "9007199254741093", "provenance": {"format": name}}], "candidates": candidates}


def test_baseline_requests_preserve_both_fields_and_explicit_unknown_time() -> None:
    for item in comparison.load_cases(FIXTURE, expected_kind="known_baseline"):
        original = deepcopy(item)
        for source in item["sources"]:
            old = comparison.source_request(source, "legacy", "old")
            new = comparison.source_request(source, "structured", "new")
            assert [old["user_input"], old["agent_response"]] == [segment["content"] for segment in new["segments"]]
            assert new["event_time"] == source["event_time"]
            assert all(old[key] == new[key] for key in ("user_id", "agent_id", "session_id"))
            assert source_envelope(new["segments"], new["event_time"])["event_time"] is None or source["event_time"] in old["user_input"] + old["agent_response"]
        assert item == original


def test_one_sided_time_fact_and_duplicate_criteria_are_rejected() -> None:
    item = case()
    item["sources"][0]["event_time"] = "2026-11-16T12:00:00+00:00"
    with pytest.raises(ValueError, match="not_in_both_plaintexts"):
        comparison.validate_case(item)
    item = case()
    item["rubric"]["forbidden"][0]["id"] = item["rubric"]["required"][0]["id"]
    with pytest.raises(ValueError, match="duplicate"):
        comparison.validate_case(item)


@pytest.mark.parametrize(("identifier", "valid"), [("heldout_case", True), ("heldout-case", True), ("../escape", False), ("case/path", False), ("has space", False)])
def test_fixture_identifier_accepts_operation_safe_underscore_only(identifier: str, valid: bool) -> None:
    item = case()
    item["id"] = identifier
    if valid:
        assert comparison.validate_case(item)["id"] == identifier
    else:
        with pytest.raises(ValueError, match="identity"):
            comparison.validate_case(item)


def test_frozen_call_order_is_counterbalanced_and_bounded() -> None:
    orders = [comparison.branch_order(i) for i in range(6)]
    assert sum(order[0] == "legacy" for order in orders) == 3
    assert sum(order[0] == "structured" for order in orders) == 3
    assert orders == [comparison.branch_order(i) for i in range(6)]
    with pytest.raises(ValueError):
        comparison.branch_order(6)


def test_server_reference_clock_is_equal_and_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    original = memory_source_service.datetime
    with comparison.fixed_submission_clock(monkeypatch, 1):
        instant = memory_source_service.datetime.now(comparison.UTC)
        assert memory_write_runtime.current_timestamp() == int(instant.timestamp() * 1000)
    assert memory_source_service.datetime is original


@pytest.mark.parametrize("branch", comparison.BRANCHES)
def test_real_manifest_validation_keeps_complete_text_and_claim_status(branch: comparison.Branch) -> None:
    value = manifest(branch)
    request = canonical_request([1], [], "preferences", 8192, 1024)
    validated = validate_manifest(value, request, lambda text: len(text) // 4)
    snapshot = validated["inputs"][0]["snapshot"]
    assert snapshot["content"] == comparison.expected_content(case()["sources"][0], branch)
    assert snapshot["role_status"] == ("role_unknown" if branch == "legacy" else "caller_declared")
    if branch == "structured":
        proof = snapshot["source_metadata"]["provenance"]
        assert proof["content_verified"] is proof["speaker_identity_verified"] is False


def test_required_old_source_closure_still_uses_host_guard() -> None:
    value = manifest("structured", two_sources=True)
    value["entries"] = [
        {
            "entry_id": "entry",
            "revision": 1,
            "message_id": 3,
            "generation": 1,
            "content": "仅限离线。",
            "content_sha256": content_hash("仅限离线。"),
            "scope": value["inputs"][0]["scope"],
            "source_ids": [1, 2],
        }
    ]
    patch = {"action": "patch", "target_entry_id": "entry", "patch": {"old_text": "离线", "new_text": "内网"}, "source_input_ids": ["source-1"], "reason": "不裁剪依赖"}
    with pytest.raises(RevisionValidationError, match="evidence_mismatch"):
        assemble_candidates([patch], value, lambda text: len(text) // 4, 1024)
    patch["source_input_ids"] = ["source-1", "source-2"]
    assert len(assemble_candidates([patch], value, lambda text: len(text) // 4, 1024)[0]["sources"]) == 2


@pytest.mark.parametrize("branch", comparison.BRANCHES)
async def test_actual_analyst_receives_only_evidence_not_rubric(monkeypatch: pytest.MonkeyPatch, branch: comparison.Branch) -> None:
    from memory.maintenance import provider

    value = manifest(branch)
    original = deepcopy(value)
    calls: list[dict[str, Any]] = []

    class Providers:
        def __init__(self, db: AsyncSession) -> None:
            self.db = db

        async def _prepared(self, memory_id: str, model_type: str) -> tuple[dict[str, Any], SimpleNamespace]:
            return {}, SimpleNamespace(model_config={"llm_name": "controlled", "llm_factory": "unit"})

        def _chat_fingerprint(self, settings: Any, prepared: Any) -> str:
            return "fixed"

    class Bundle:
        async def async_chat(self, system: str, history: list[dict[str, Any]], generation: dict[str, Any], **options: Any) -> str:
            calls.append({"body": json.loads(history[0]["content"]), "generation": generation})
            return '{"proposals":[]}'

    monkeypatch.setattr(provider.LLMBundle, "from_prepared", staticmethod(lambda prepared: Bundle()))
    async with AsyncSession() as db:
        result = await MaintenanceAnalyst(cast(MemoryWriteProviders, Providers(db))).analyze("bank", value, "preferences", 1024)
    assert result["proposals"] == [] and len(calls) == 1
    assert calls[0]["body"] == analysis_payload(value, "preferences")
    assert set(calls[0]["body"]) == {"policy", "inputs", "entries"}
    assert not any(rule["description"] in json.dumps(calls) for kind in case()["rubric"].values() for rule in kind)
    assert value == original and calls[0]["generation"]["max_tokens"] == 1024


def test_blind_review_hides_branch_and_physical_identity_without_scoring_empty() -> None:
    report = {"branches": {"legacy": branch_view("legacy", empty=True), "structured": branch_view("structured")}, "actual": "raw report"}
    original = deepcopy(report)
    request, mapping = comparison.review_request(case(), report)
    text = comparison.canonical(request)
    assert all(word not in text for word in ("legacy", "structured", "provenance", "9007199254741093", "hidden-"))
    assert request["report_sha256"] == comparison.digest(report)
    assert set(mapping["variants"].values()) == {"legacy", "structured"}
    assert sorted(len(view["candidates"]) for view in request["variants"].values()) == [0, 1]
    assert report == original and "score" not in request
    shown = next(view for view in request["variants"].values() if view["candidates"])
    assert shown["candidates"][0]["source_indices"] == [0]


def test_review_requires_exact_hash_criteria_and_explicit_independent_judgment() -> None:
    request, _ = comparison.review_request(case(), {"branches": {name: branch_view(name, empty=True) for name in comparison.BRANCHES}})
    view = {
        "required": {rule["id"]: "unassessable" for rule in request["rubric"]["required"]},
        "forbidden": {rule["id"]: "unassessable" for rule in request["rubric"]["forbidden"]},
        "citation_closure": "unassessable",
        "uncertainty": "unassessable",
        "notes": "未把空输出冒充全部正确。",
    }
    decision = {"review_sha256": request["review_sha256"], "reviewed_by": "independent-reviewer", "synthetic_only": True, "variants": {alias: deepcopy(view) for alias in request["variants"]}}
    assert comparison.validate_review(request, decision) == decision
    decision["review_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="binding"):
        comparison.validate_review(request, decision)
    request["instruction"] = "Changed after the source review request was frozen."
    with pytest.raises(ValueError, match="request_changed"):
        comparison.validate_review(request, decision)


def test_source_unknown_ack_is_saved_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    response = SimpleNamespace(status_code=202, json=lambda: {"retcode": 0, "data": {"operation_id": "stable", "status": "persistence_unknown", "index_sync_status": "unknown"}})

    def append(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[1])
        return response

    monkeypatch.setattr(comparison, "api_request", append)
    env = {"memory_id": "bank", "keys": ["synthetic"], "observer": SimpleNamespace(set_stage=lambda *args, **kwargs: None), "comparison_branches": {}}
    outcome = comparison.run_branch(env, monkeypatch, case(), "legacy")
    assert outcome["state"] == "source_unsettled" and len(calls) == 1
    assert outcome["sources"][0]["operation"]["operation_id"] == "stable" and outcome["candidates"] == []
