"""Incremental discovery through real HTTP, SQL, storage and the owned worker."""

import asyncio
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_incremental_models import MemoryMaintenanceBatch, MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntry
from memory.revisions.domain import content_hash
from memory.revisions.storage import ManagedMessageStore
from tests.integration.test_memory_dreaming_http import wait_job
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_writes_http import WriteChat, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

_LARGE_ID = 9007199254740993
_SCOPE = {"user_id": "existing-subject", "agent_id": "existing-agent", "session_id": None}


def seed_source(env: dict[str, Any], identity: int, text: str, *, subject: str = "existing-subject", session: str = "later-session") -> None:
    mid = env["memory_id"]
    env["journal"].event("reserve-incremental-raw-source", memory_id=mid, message_id=identity)
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    asyncio.run(
        store.write(
            {
                "id": f"{mid}_{identity}",
                "memory_id": mid,
                "message_id": identity,
                "message_type": "raw",
                "source_id": 0,
                "user_id": subject,
                "agent_id": "existing-agent",
                "session_id": session,
                "content": text,
                "content_embed": [1.0, 0.0],
                "valid_at": "2020-01-01 00:00:00",
                "invalid_at": None,
                "forget_at": None,
                "status": False,
            }
        )
    )
    asyncio.run(store.set_status(identity, True))
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{identity}/adoptions",
            json={"operation_id": f"register-incremental-{identity}", "expected_content_sha256": content_hash(text)},
        )
    )
    env["source_texts"][str(identity)] = text


@pytest.fixture
def incremental_api(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    env = dreaming_api
    options = getattr(request, "param", {})
    env["source_texts"] = {"900": "Owned raw personal"}
    env["incremental_ids"] = ["900", "901", "902", str(_LARGE_ID)]
    for identity in (901, 902, _LARGE_ID):
        content = "An enduring source-specific constraint " + str(identity) + "."
        if identity == options.get("oversize"):
            content = "A complete oversized source: " + "measurement premise; " * 1800
        seed_source(env, identity, content)
    seed_source(env, 904, "A different subject's constraint.", subject="other-subject")

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        assert "never publish" in system and gen_conf["max_tokens"] <= 8192
        payload = json.loads(history[-1]["content"])
        env["analysis_calls"].append(payload)
        if "analysis_started" in env:
            env["analysis_started"].set()
            await asyncio.to_thread(env["analysis_release"].wait, 30)
        if env.get("invalid_response"):
            return json.dumps(
                {
                    "proposals": [
                        {
                            "action": "patch",
                            "target_entry_id": "f" * 32,
                            "patch": {"old_text": "missing", "new_text": "invented"},
                            "source_input_ids": [payload["inputs"][0]["input_id"]],
                            "reason": "invalid target",
                        }
                    ]
                }
            ), 0
        proposals = [
            {
                "action": "add",
                "message_type": "semantic",
                "content": "Source reports: " + item["snapshot"]["content"],
                "source_input_ids": [item["input_id"]],
                "reason": "Unverified controlled proposal for explicit human review.",
            }
            for item in payload["inputs"]
            if str(item["source_id"]) not in env.get("empty_sources", set())
        ]
        return json.dumps({"proposals": proposals}), 0

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    try:
        yield env
    finally:
        if "analysis_release" in env:
            env["analysis_release"].set()
        env["worker_stop"].set()
        env["worker_thread"].join(timeout=40)
        assert not env["worker_thread"].is_alive(), "owned incremental worker did not drain"
        # Remove only this fixture's metadata before the parent fixture removes
        # its child jobs. The scratch database is independently dropped later.
        with Session(env["engine"]) as db:
            for model in (MemoryMaintenanceCoverage, MemoryMaintenanceBatch, MemoryMaintenanceDiscovery):
                db.execute(sa.delete(model).where(model.memory_id == env["memory_id"]))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"]))
                for model in (MemoryMaintenanceCoverage, MemoryMaintenanceBatch, MemoryMaintenanceDiscovery)
            }
            assert not any(remaining.values()), remaining
        env["journal"].event("incremental-metadata-cleanup-readback", memory_id=env["memory_id"], remaining=remaining)


def selection(**overrides: Any) -> dict[str, Any]:
    return {"scope": dict(_SCOPE), "policy": "general", "entry_ids": [], "batch_size": 2, "max_sources": 128, "input_budget_tokens": 16384, "output_budget_tokens": 4096, **overrides}


def endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return f"/memories/{env['memory_id']}/maintenance-discoveries" + suffix


def preview(env: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    return result(api_request(env, "POST", endpoint(env, "/preview"), json=request))


def create(env: dict[str, Any], request: dict[str, Any], *, operation: str = "create-incremental") -> dict[str, Any]:
    observed = preview(env, request)
    response = api_request(env, "POST", endpoint(env), json={**request, "operation_id": operation, "expected_preview_sha256": observed["preview_sha256"]})
    assert response.status_code in {200, 201, 202}, response.text
    return result(response, status=response.status_code)


def run(env: dict[str, Any], plan: dict[str, Any], *, operation: str = "run-incremental", limit: int = 8) -> dict[str, Any]:
    response = api_request(env, "POST", endpoint(env, f"/{plan['discovery_id']}/runs"), json={"operation_id": operation, "limit": limit})
    assert response.status_code in {200, 202}, response.text
    return result(response, status=response.status_code)


def wait_plan(env: dict[str, Any], identity: str, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert not env.get("worker_errors"), env.get("worker_errors")
        observed = result(api_request(env, "GET", endpoint(env, "/" + identity)))
        if predicate(observed):
            return observed
        time.sleep(0.05)
    raise AssertionError("incremental discovery did not reach its expected state")


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
