"""Real API setup for explicitly requested, synthetic longitudinal evaluation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tests.support.memory_workload_review import call, publish_reviewed, reviewed_candidates
from tests.support.memory_writes_http import api_request, result


def append_source(env: dict[str, Any], text: str, *, label: str, subject: str) -> str:
    """The SDK identity owns the bank; user_id remains only a content selector."""
    mid = env["memory_id"]
    response = api_request(
        env,
        "POST",
        f"/memories/{mid}/messages",
        token=env["keys"][0],
        json={"operation_id": label, "user_input": text, "agent_response": "", "user_id": subject, "agent_id": "target-longitudinal", "session_id": label},
    )
    value = result(response, status=response.status_code)
    assert value["status"] == "ready" and value["index_sync_status"] == "ready"
    identity = str(value["raw_source_id"])
    source = call(env, "GET", f"/memories/{mid}/sources/{identity}/state")
    call(env, "POST", f"/memories/{mid}/sources/{identity}/adoptions", json={"operation_id": label + "-adopt", "expected_content_sha256": source["content_sha256"]})
    evidence = call(env, "GET", f"/memories/{mid}/sources/{identity}", params={"limit": 8192, "user_id": subject, "agent_id": "target-longitudinal"})
    assert evidence["state"] == "available" and evidence["truncated"] is False
    env.setdefault("source_evidence", {})[identity] = evidence
    return identity


def wait_job(env: dict[str, Any], job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        assert not env["worker_errors"], [type(error).__name__ for error in env["worker_errors"]]
        job = call(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{job_id}")
        execution = job.get("execution") or {}
        if execution.get("state") in {"succeeded", "failed", "canceled", "needs_reconciliation"}:
            assert execution["state"] == "succeeded" and job["state"] == "ready", job
            return job
        time.sleep(0.1)
    raise AssertionError("longitudinal_job_did_not_settle")


def candidates_for(env: dict[str, Any], job_id: str) -> list[dict[str, Any]]:
    items = call(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{job_id}/candidates")["candidates"]
    for candidate in items:
        assert candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified"
        candidate["job_id"] = job_id
    return items


def seed_published_entry(env: dict[str, Any], case: dict[str, Any], directory: Path, *, label: str, subject: str, source: str, content: str) -> dict[str, Any]:
    """Publish an independently reviewed add, then author a known old fixture revision.

    The manual old content is visible setup, not evidence of model extraction.
    New-correction expected answers never enter a model request.
    """
    observer, mid = env["observer"], env["memory_id"]
    observer.set_stage("source_embedding", phase=label + "-raw")
    source_id = append_source(env, source, label=label + "-source", subject=subject)
    job = call(
        env,
        "POST",
        f"/memories/{mid}/maintenance-jobs",
        json={"operation_id": label + "-job", "source_ids": [source_id], "entry_ids": [], "policy": case["policy"], "input_budget_tokens": 8192, "output_budget_tokens": 1024},
    )
    observer.set_stage("analysis", phase=label + "-analysis")
    call(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": label + "-run"})
    wait_job(env, job["job_id"])
    candidates = candidates_for(env, job["job_id"])
    selected = reviewed_candidates(
        directory, label, candidates, env["journal"], evidence={"phase": "seed_one_source_supported_add", "source": source, "actual_raw": env["source_evidence"][source_id], "subject": subject}
    )
    assert len(selected) == 1 and selected[0]["action"] == "add", "seed_requires_one_independently_reviewed_add"
    observer.set_stage("publication_embedding", phase=label + "-publication")
    operations = publish_reviewed(env, mid, selected)
    entry_id = operations[0]["entries"][0]["entry_id"]
    entry = call(env, "GET", f"/memories/{mid}/entries/{entry_id}")
    if entry["current"]["content"] == content:
        return {
            "source_id": source_id,
            "actual_raw": env["source_evidence"][source_id],
            "entry": entry,
            "seed_job": job,
            "seed_candidates": candidates,
            "seed_publications": operations,
            "manual_fixture_operation": {"not_required": True, "reason": "published_content_already_matches_old_fixture"},
        }
    observer.set_stage("publication_embedding", phase=label + "-manual-fixture")
    operation = call(
        env,
        "POST",
        f"/memories/{mid}/entries/{entry_id}/revisions",
        json={
            "operation_id": label + "-manual",
            "expected_revision": entry["current_revision"],
            "content": content,
            "reason": "Explicit synthetic old-memory fixture authoring; not a model quality result.",
        },
    )
    for _ in range(5):
        if operation["status"] == "ready" and operation["index_sync_status"] == "ready":
            break
        operation = call(env, "POST", f"/memories/{mid}/operations/{label}-manual/reconciliations", json={})
    assert operation["status"] == "ready" and operation["index_sync_status"] == "ready", operation
    current = call(env, "GET", f"/memories/{mid}/entries/{entry_id}")
    assert current["current"]["content"] == content and current["current"]["assessment"]["verification_status"] == "unverified"
    return {
        "source_id": source_id,
        "actual_raw": env["source_evidence"][source_id],
        "entry": current,
        "seed_job": job,
        "seed_candidates": candidates,
        "seed_publications": operations,
        "manual_fixture_operation": operation,
    }
