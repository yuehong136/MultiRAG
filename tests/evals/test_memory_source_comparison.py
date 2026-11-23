"""Opt-in real-provider A/B observations, never automatic quality or publication."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.provider import _SYSTEM
from memory.maintenance.snapshots import ANALYSIS_INPUT_CONTRACT
from memory.revisions.domain import content_hash
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_source_comparison import (
    AGENT,
    INPUT_BUDGET,
    MAX_CASES,
    OUTPUT_BUDGET,
    SUBJECT,
    branch_order,
    capture_analyst,
    digest,
    load_cases,
    run_branch,
    save_review,
    verify_source_pair,
)
from tests.support.memory_startup_longitudinal import costs
from tests.support.memory_structured_sources_live import require_direct_openai_models
from tests.support.memory_workload_review import write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api
from tests.support.memory_writes_http import WriteChat
from tests.support.memory_writes_http import writes_api as writes_api

BASELINE = Path(__file__).parents[1] / "fixtures/memory_source_comparison_baseline.json"
CASES = [("known_baseline", case) for case in load_cases(BASELINE, expected_kind="known_baseline")]
if os.environ.get("MULTIRAG_SOURCE_COMPARISON_HOLDOUT"):
    CASES += [("independent_holdout", case) for case in load_cases(Path(os.environ["MULTIRAG_SOURCE_COMPARISON_HOLDOUT"]), expected_kind="independent_holdout")]
assert len(CASES) <= MAX_CASES and len({case["id"] for _, case in CASES}) == len(CASES)


@pytest.mark.parametrize(("case_index", "kind", "case"), [(i, kind, case) for i, (kind, case) in enumerate(CASES)], ids=[case["id"] for _, case in CASES])
def test_real_source_representation_comparison(
    memory_live_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, case_index: int, kind: str, case: dict[str, Any]
) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER") == "OpenAI"
    env = memory_live_api
    require_direct_openai_models(env)
    env["observer"].max_attempts = 2 * len(case["sources"]) + 2
    env["comparison_branches"], env["comparison_requests"] = {}, []
    order = branch_order(case_index)
    report: dict[str, Any] = {
        "case_id": case["id"],
        "fixture_kind": kind,
        "fixture_sha256": digest(case),
        "synthetic_only": True,
        "memory_id": env["memory_id"],
        "subject": SUBJECT,
        "agent": AGENT,
        "branch_order": list(order),
        "input_budget_tokens": INPUT_BUDGET,
        "output_budget_tokens": OUTPUT_BUDGET,
        "system_sha256": content_hash(_SYSTEM),
        "input_contract": ANALYSIS_INPUT_CONTRACT,
        "branches": env["comparison_branches"],
        "analyst_requests": env["comparison_requests"],
        "evaluation_completed": False,
        "semantic_review": "pending_independent_blinded_review",
        "candidate_approval_or_publication": False,
        "scope": "Paired exact plaintext; representation, claimed role/event-time metadata and server provenance are treatment. No answer model, old entries or semantic auto-scoring.",
    }
    started = time.monotonic()
    try:
        with capture_analyst(env, monkeypatch):
            for branch in order:
                run_branch(env, monkeypatch, case, branch)
        if all(len(report["branches"][name]["sources"]) == len(case["sources"]) and all("content" in row for row in report["branches"][name]["sources"]) for name in order):
            for index, source in enumerate(case["sources"]):
                verify_source_pair(source, report["branches"]["legacy"]["sources"][index], report["branches"]["structured"]["sources"][index])
            report["source_equivalence"] = "actual_public_API_body_hash_scope_and_reference_time_checked"
        else:
            report["source_equivalence"] = "unavailable_after_source_failure"
        report["paired_model_source_coverage_comparable"] = all(report["branches"][branch].get("complete_model_source_coverage") is True for branch in order)
        report["evaluation_completed"] = True
    except BaseException as error:
        report["harness_error_code"] = type(error).__name__
        raise
    finally:
        for branch in ("legacy", "structured"):
            report["branches"].setdefault(branch, {"branch": branch, "state": "not_run", "sources": [], "candidates": [], "payload": None})
        report["elapsed_seconds"] = time.monotonic() - started
        report["provider_attempts"] = env["observer"].attempts
        report["cost"] = costs(env)
        save_review(Path(request.config.getoption("--test-report-dir")), case, report, env)


def test_controlled_paired_source_wire_and_model_capture(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Actual owned API/SQL/Milvus/worker; controlled models incur no provider call."""
    original = dreaming_api
    # The reusable controlled fixture owns personal Memory as actor1; the live
    # workload owns its Memory as actor0. Keep the harness actor0 convention by
    # selecting the controlled fixture's real owner tokens, never widening ACL.
    owner = original["owners"].index(original["tenants"]["personal"])
    env = dict(original, jwts=[original["jwts"][owner]], keys=[original["keys"][owner]])

    class Observer:
        phase = ""

        def set_stage(self, stage: str, *, phase: str = "") -> None:
            self.phase = phase

    async def empty(self: WriteChat, system: str, history: list[dict[str, Any]], generation: dict[str, Any]) -> tuple[str, int]:
        return json.dumps({"proposals": []}), 0

    monkeypatch.setattr(WriteChat, "async_chat", empty)
    env.update(observer=Observer(), comparison_branches={}, comparison_requests=[])
    sample = CASES[0][1]
    try:
        with capture_analyst(env, monkeypatch):
            for branch in branch_order(0):
                outcome = run_branch(env, monkeypatch, sample, branch)
                assert outcome["state"] == "succeeded" and outcome["candidates"] == [], outcome
                assert outcome["payload"]["entries"] == []
        for index, source in enumerate(sample["sources"]):
            verify_source_pair(source, env["comparison_branches"]["legacy"]["sources"][index], env["comparison_branches"]["structured"]["sources"][index])
        assert len(env["comparison_requests"]) == 2
        assert all(row["body_estimated_tokens"] > 0 for row in env["comparison_requests"])
    finally:
        write_private(
            Path(request.config.getoption("--test-report-dir")) / "controlled-comparison-report.json",
            {"branches": env["comparison_branches"], "requests": env["comparison_requests"], "real_provider_calls": 0},
            env["journal"],
        )
