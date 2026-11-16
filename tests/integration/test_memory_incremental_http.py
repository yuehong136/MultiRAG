"""Incremental discovery through real HTTP, SQL, storage and the owned worker."""

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntry
from tests.integration.test_memory_dreaming_http import wait_job
from tests.support.memory_incremental_http import LARGE_ID as _LARGE_ID
from tests.support.memory_incremental_http import SCOPE as _SCOPE
from tests.support.memory_incremental_http import create, endpoint, preview, run, seed_source, selection, wait_plan
from tests.support.memory_incremental_http import dreaming_api as dreaming_api
from tests.support.memory_incremental_http import incremental_api as incremental_api
from tests.support.memory_incremental_http import release_api as release_api
from tests.support.memory_incremental_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def test_preview_is_read_only_scoped_and_create_replays_original_plan(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    request = selection()
    observed = preview(env, request)
    assert observed["registered_active_only"] is True and observed["token_fit"] == "not_checked"
    assert observed["counts"]["selected_primary"] == 4 and observed["counts"]["covered"] == 0
    selected = [item["source_id"] for batch in observed["batches"] for item in batch["primary_sources"]]
    assert selected == env["incremental_ids"] and str(_LARGE_ID) in selected
    assert preview(env, selection(scope={**_SCOPE, "session_id": ""}))["counts"]["active"] == 0
    assert env["analysis_calls"] == []
    with Session(env["engine"]) as reader:
        for model in (MemoryMaintenanceDiscovery, MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceJob):
            assert reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"])) == 0
    assert api_request(env, "POST", endpoint(env, "/preview"), actor=2, json=request).status_code == 404
    assert api_request(env, "POST", endpoint(env, "/preview"), token="invalid", json=request).status_code == 401
    body = {**request, "operation_id": "stable-create", "expected_preview_sha256": observed["preview_sha256"]}
    created = api_request(env, "POST", endpoint(env), json=body)
    assert created.status_code in {200, 201, 202}
    plan = result(created, status=created.status_code)
    replay = api_request(env, "POST", endpoint(env), json=body)
    assert result(replay, status=replay.status_code)["discovery_id"] == plan["discovery_id"]
    assert api_request(env, "POST", endpoint(env), json={**body, "max_sources": 1}).status_code == 409
    recovered = result(api_request(env, "GET", endpoint(env), params={"operation_id": "stable-create"}))["discoveries"]
    assert [item["discovery_id"] for item in recovered] == [plan["discovery_id"]]
    seed_source(env, _LARGE_ID + 1, "An added source invalidates the old preview.")
    assert api_request(env, "POST", endpoint(env), json={**body, "operation_id": "changed-preview"}).status_code == 409
    repeated = api_request(env, "POST", endpoint(env), json=body)
    assert result(repeated, status=repeated.status_code)["discovery_id"] == plan["discovery_id"]
    assert env["analysis_calls"] == []


def test_batches_keep_complete_inputs_and_empty_proposals_cover_without_publication(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    env["empty_sources"] = {"901"}
    request = selection(batch_size=1)
    plan = create(env, request)
    submitted = run(env, plan, limit=1)
    assert submitted["discovery_id"] == plan["discovery_id"]
    wait_plan(env, plan["discovery_id"], lambda row: row["progress"]["analyzed"] == 1)
    run(env, plan, limit=1)
    assert len(env["analysis_calls"]) == 1
    assert api_request(env, "POST", endpoint(env, f"/{plan['discovery_id']}/runs"), json={"operation_id": "run-incremental", "limit": 2}).status_code == 409
    run(env, plan, operation="run-incremental-remaining")
    finished = wait_plan(env, plan["discovery_id"], lambda row: row["progress"]["analyzed"] == 4)
    assert len(env["analysis_calls"]) == 4
    run(env, plan, operation="run-incremental-remaining")
    assert len(env["analysis_calls"]) == 4
    assert preview(env, request)["counts"]["covered"] == 4
    actual = {str(item["source_id"]): item["snapshot"]["content"] for call in env["analysis_calls"] for item in call["inputs"]}
    assert actual == {identity: env["source_texts"][identity] for identity in env["incremental_ids"]}
    for batch in finished["batches"]:
        assert batch["execution"]["state"] == "succeeded"
        candidates = result(api_request(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{batch['job_id']}/candidates"))["candidates"]
        assert all(candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified" for candidate in candidates)
    with Session(env["engine"]) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.memory_id == env["memory_id"])) == 4
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry).where(MemoryEntry.memory_id == env["memory_id"])) == 1


@pytest.mark.parametrize("incremental_api", [{"oversize": 901}], indirect=True)
def test_deferred_source_stays_uncovered_without_starving_late_arrival(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    request = selection(input_budget_tokens=1024)
    plan = create(env, request)
    run(env, plan)
    wait_plan(env, plan["discovery_id"], lambda row: row["progress"]["pending"] == 0 and row["progress"]["running"] == 0)
    # Both actual executions must settle before effective coverage can be read.
    for batch in result(api_request(env, "GET", endpoint(env, "/" + plan["discovery_id"])))["batches"]:
        if batch["job_state"] is not None:
            wait_job(env, batch["job_id"])
    incomplete = result(api_request(env, "GET", endpoint(env, "/" + plan["discovery_id"])))
    assert incomplete["state"] == "partial" and incomplete["progress"]["blocked"] == 1
    seed_source(env, _LARGE_ID + 1, "A late source with an old event timestamp.")
    observed = preview(env, request)
    assert observed["counts"]["covered"] == 3 and observed["counts"]["blocked"] == 1
    selected = [item["source_id"] for batch in observed["batches"] for item in batch["primary_sources"]]
    assert selected == [str(_LARGE_ID + 1)]
    larger = preview(env, selection(input_budget_tokens=32768))
    assert larger["profile_sha256"] != observed["profile_sha256"]
    assert "901" in [item["source_id"] for batch in larger["batches"] for item in batch["primary_sources"]]


def test_invalid_proposal_is_not_cross_task_coverage(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    env["invalid_response"] = True
    request = selection(batch_size=4)
    plan = create(env, request)
    run(env, plan)
    ended = wait_plan(env, plan["discovery_id"], lambda row: row["progress"]["failed"] == 1)
    wait_job(env, ended["batches"][0]["job_id"])
    observed = preview(env, request)
    assert observed["counts"]["covered"] == 0 and observed["counts"]["eligible"] == 4
    with Session(env["engine"]) as reader:
        assert (
            reader.scalar(
                sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.memory_id == env["memory_id"], MemoryMaintenanceCoverage.state == "analyzed")
            )
            == 0
        )
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.memory_id == env["memory_id"])) == 0


def test_source_withdrawal_fences_late_model_and_incremental_checkpoint(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    plan = create(env, selection(max_sources=1))
    env["analysis_started"], env["analysis_release"] = threading.Event(), threading.Event()
    try:
        submitted = run(env, plan, limit=1)
        assert env["analysis_started"].wait(20)
        response = api_request(
            env, "POST", f"/memories/{env['memory_id']}/sources/900/withdrawals", json={"operation_id": "withdraw-incremental", "expected_source_revision": 1, "reason": "source retracted"}
        )
        assert response.status_code == 202
    finally:
        env["analysis_release"].set()
    job = wait_job(env, submitted["batches"][0]["job_id"])
    assert job["state"] == "redacted"
    assert preview(env, selection())["counts"]["covered"] == 0
    with Session(env["engine"]) as reader:
        assert (
            reader.scalar(
                sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.memory_id == env["memory_id"], MemoryMaintenanceCoverage.state == "analyzed")
            )
            == 0
        )
    assert result(api_request(env, "POST", f"/memories/{env['memory_id']}/operations/withdraw-incremental/reconciliations"))["index_sync_status"] == "ready"


@pytest.mark.external_consumer
def test_pinned_web_incremental_selection_and_refresh_recovery(incremental_api: dict[str, Any], tmp_path: Path) -> None:
    env = incremental_api
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-incremental-web-consumer")
    configuration, output = tmp_path / "incremental-consumer.private.json", tmp_path / "incremental-consumer-result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "scope": _SCOPE,
                    "expectedSourceIds": env["incremental_ids"],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(output, "", journal, owned)
        journal.event("reserve-incremental-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-incremental.ts", output=str(output))
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-incremental.ts", str(configuration)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True
        journal.event("incremental-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("incremental-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
