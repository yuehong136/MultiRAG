"""Opt-in real-provider target selection and correction over known synthetic data."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from memory.maintenance.evaluation import usage_cost
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_target_longitudinal import append_source, candidates_for, seed_published_entry, wait_job
from tests.support.memory_workload_cost import STAGES, workload_cost
from tests.support.memory_workload_review import answer_from_context, call, publish_reviewed, reviewed_candidates, write_private
from tests.support.memory_workload_runtime import memory_live_api as memory_live_api

CASES = json.loads((Path(__file__).parents[1] / "fixtures/memory_target_longitudinal.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_real_target_longitudinal(memory_live_api: dict[str, Any], request: pytest.FixtureRequest, case: dict[str, Any]) -> None:
    assert os.environ.get("MULTIRAG_MEMORY_LIVE") == "1", "explicit_live_memory_evaluation_required"
    env, mid = memory_live_api, memory_live_api["memory_id"]
    observer, journal = env["observer"], env["journal"]
    directory = Path(request.config.getoption("--test-report-dir"))
    label, subject = case["id"], "target-subject-alpha"
    report: dict[str, Any] = {"case_id": label, "synthetic_only": True, "known_case": True, "stages": dict.fromkeys(STAGES, "not_run"), "manual_semantic_review_required": True}
    started = time.monotonic()
    try:
        seeded = seed_published_entry(env, case, directory, label="seed-" + label, subject=subject, source=case["initial_source"], content=case["initial_memory"])
        report["seed"] = seeded
        old = seeded["entry"]
        report["stages"].update(source_embedding="completed", analysis="completed", publication_embedding="completed")
        other = None
        if "distractor_source" in case:
            other = seed_published_entry(
                env,
                case,
                directory,
                label="other-" + label,
                subject="target-subject-beta",
                source=case["distractor_source"],
                content=case["distractor_memory"],
            )
            report["distractor"] = other
        observer.set_stage("source_embedding", phase="new-source")
        new_source = append_source(env, case["new_source"], label="new-" + label, subject=subject)
        report["new_source_id"] = new_source
        report["actual_new_raw"] = env["source_evidence"][new_source]
        observer.set_stage("retrieval_embedding", phase="target-recommendation")
        report["stages"]["retrieval_embedding"] = "unknown"
        recommendation = call(
            env,
            "POST",
            f"/memories/{mid}/maintenance-targets/recommendations",
            json={"source_ids": [new_source], "user_id": subject, "agent_id": "target-longitudinal", "retrieval_mode": "hybrid", "limit": 8, "budget_tokens": 8192, "query_budget_tokens": 2048},
        )
        report["recommendation"] = recommendation
        target_ids = [target["entry_id"] for target in recommendation["targets"]]
        report["expected_target_rank"] = target_ids.index(old["entry_id"]) + 1 if old["entry_id"] in target_ids else None
        assert old["entry_id"] in target_ids, "real_retrieval_missed_known_current_target"
        assert all(target["scope"]["user_id"] == subject and target["scope"]["agent_id"] == "target-longitudinal" for target in recommendation["targets"])
        if other is not None:
            assert other["entry"]["entry_id"] not in target_ids
        assert call(env, "GET", f"/memories/{mid}/entries/{old['entry_id']}") == old, "recommendation_must_not_change_current_entry"
        confirmation = deepcopy(recommendation["confirmation"])
        confirmation["targets"] = [target for target in confirmation["targets"] if target["entry_id"] == old["entry_id"]]
        selection = {
            "scope": {"user_id": subject, "agent_id": "target-longitudinal", "session_id": None},
            "policy": case["policy"],
            "entry_ids": [old["entry_id"]],
            "target_preconditions": confirmation,
            "batch_size": 16,
            "max_sources": 8,
            "input_budget_tokens": 8192,
            "output_budget_tokens": 1024,
        }
        prefix = f"/memories/{mid}/maintenance-discoveries"
        preview = call(env, "POST", prefix + "/preview", json=selection)
        plan = call(env, "POST", prefix, json={**selection, "operation_id": "selected-plan-" + label, "expected_preview_sha256": preview["preview_sha256"]})
        report["preview"], report["plan"] = preview, plan
        assert len(plan["batches"]) == 1
        observer.set_stage("analysis", phase="new-source-with-selected-target")
        call(env, "POST", prefix + f"/{plan['discovery_id']}/runs", json={"operation_id": "selected-run-" + label, "limit": 1})
        refreshed = call(env, "GET", prefix + "/" + plan["discovery_id"])
        batch = refreshed["batches"][0]
        assert batch["job_id"]
        report["settled_job"] = wait_job(env, batch["job_id"])
        candidates = candidates_for(env, batch["job_id"])
        report["new_candidates"] = candidates
        assert call(env, "GET", f"/memories/{mid}/entries/{old['entry_id']}") == old, "analysis_cannot_publish_or_overwrite"
        approved = reviewed_candidates(
            directory,
            "update-" + label,
            candidates,
            journal,
            allow_empty=not case["requires_update"],
            evidence={
                "case": case,
                "actual_new_raw": env["source_evidence"][new_source],
                "actual_old_raw": seeded["actual_raw"],
                "old_entry": old,
                "subject": subject,
                "phase": "review_new_proposals_before_explicit_publication",
            },
        )
        if case["requires_update"]:
            assert any(candidate["action"] == "patch" and candidate["target_entry_id"] == old["entry_id"] for candidate in approved), "no_independently_accepted_correction_to_selected_old_entry"
        selected_ids = {candidate["candidate_id"] for candidate in approved}
        for candidate in candidates:
            if candidate["candidate_id"] not in selected_ids:
                call(
                    env,
                    "POST",
                    f"/memories/{mid}/maintenance-jobs/{batch['job_id']}/candidates/{candidate['candidate_id']}/reviews",
                    json={"operation_id": "reject-" + candidate["candidate_id"], "expected_revision": candidate["revision"], "decision": "reject"},
                )
        observer.set_stage("publication_embedding", phase="accepted-new-correction")
        report["new_publications"] = publish_reviewed(env, mid, approved) if approved else []
        current = call(env, "GET", f"/memories/{mid}/entries/{old['entry_id']}")
        report["current_after_review"] = current
        if not approved:
            assert current == old
        assert current["current"]["assessment"]["verification_status"] == "unverified"
        observer.set_stage("retrieval_embedding", phase="answer-context")
        context = call(
            env,
            "POST",
            f"/memories/{mid}/context",
            json={
                "mode": "query",
                "query": case["question"],
                "budget_tokens": 2048,
                "max_items": 5,
                "candidate_limit": 20,
                "source_excerpt_chars": 0,
                "user_id": subject,
                "agent_id": "target-longitudinal",
                "similarity_threshold": 0,
            },
        )
        report["context"] = context
        report["stages"]["retrieval_embedding"] = "completed"
        observer.set_stage("answer", phase="answer-after-explicit-review")
        report["stages"]["answer"] = "unknown"
        report["answer"] = asyncio.run(answer_from_context(env, context, case["question"]))
        report["stages"]["answer"] = "completed"
    except BaseException as error:
        report["error_code"] = type(error).__name__
        report["failed_phase"] = observer.phase
        report["stages"][observer.stage] = "failed"
        raise
    finally:
        report["elapsed_seconds_including_review_wait"] = time.monotonic() - started
        quote_path = os.environ.get("MULTIRAG_MEMORY_PRICE_QUOTES")
        prices = json.loads(Path(quote_path).read_text()) if quote_path else None
        phase_attempts: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for attempt in observer.attempts:
            phase_attempts[attempt["workload_phase"]].append(attempt)
            if report["stages"][attempt["stage"]] == "not_run":
                report["stages"][attempt["stage"]] = "unknown"
        report["phase_costs"] = {phase: usage_cost(rows, prices) for phase, rows in phase_attempts.items()}
        report["cost"] = workload_cost(observer.attempts, report["stages"], prices)
        write_private(directory / ("target-longitudinal-" + label + ".report.json"), report, journal)
