"""Target recommendation, explicit confirmation and existing publication APIs."""

from __future__ import annotations

import json
import os
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_incremental_models import MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntryRevision
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_incremental_http import SCOPE, seed_source, wait_plan
from tests.support.memory_incremental_http import incremental_api as incremental_api
from tests.support.memory_writes_http import WriteChat, WriteEmbedding, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


@pytest.fixture(autouse=True)
def target_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    def encode_queries(self: WriteEmbedding, text: str) -> tuple[list[float], int]:
        vectors, tokens = self.encode([text])
        return vectors[0], tokens

    monkeypatch.setattr(WriteEmbedding, "encode_queries", encode_queries, raising=False)


def endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return f"/memories/{env['memory_id']}" + suffix


def counts(env: dict[str, Any]) -> dict[str, int]:
    with Session(env["engine"]) as reader:
        return {
            model.__tablename__: int(reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"])) or 0)
            for model in (MemoryMaintenanceDiscovery, MemoryMaintenanceJob, MemoryMaintenanceCandidate, MemoryEntryRevision)
        }


def recommendation(env: dict[str, Any], *, mode: str = "hybrid") -> dict[str, Any]:
    return result(
        api_request(
            env,
            "POST",
            endpoint(env, "/maintenance-targets/recommendations"),
            json={
                "source_ids": ["905"],
                "user_id": SCOPE["user_id"],
                "agent_id": SCOPE["agent_id"],
                "query": "project port 80 backup policy",
                "retrieval_mode": mode,
                "limit": 5,
                "budget_tokens": 8192,
                "query_budget_tokens": 2048,
            },
        )
    )


def selected(env: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
    confirmation = deepcopy(observed["confirmation"])
    confirmation["targets"] = [target for target in confirmation["targets"] if target["entry_id"] == env["entry_id"]]
    assert len(confirmation["targets"]) == 1
    return {
        "scope": dict(SCOPE),
        "policy": "decisions",
        "entry_ids": [env["entry_id"]],
        "target_preconditions": confirmation,
        "batch_size": 16,
        "max_sources": 8,
        "input_budget_tokens": 16384,
        "output_budget_tokens": 1024,
    }


def seed_correction(env: dict[str, Any]) -> None:
    seed_source(env, 905, "The project port changes from 80 to 8080. Keep the backup policy.", session="correction-session")


@pytest.mark.parametrize("mode", ["hybrid", "lexical"])
def test_recommendation_is_bounded_read_only_and_authorized(incremental_api: dict[str, Any], mode: str) -> None:
    env = incremental_api
    seed_correction(env)
    before = counts(env)
    embedded = len(env["embedding_calls"])
    observed = recommendation(env, mode=mode)
    assert counts(env) == before and env["analysis_calls"] == []
    assert len(env["embedding_calls"]) - embedded == (1 if mode == "hybrid" else 0)
    target = next(item for item in observed["targets"] if item["entry_id"] == env["entry_id"])
    assert target["content"] == "The project uses port 80. Keep the backup policy."
    assert target["scope"]["user_id"] == SCOPE["user_id"] and target["scope"]["agent_id"] == SCOPE["agent_id"]
    assert target["source_versions"] and target["projection_sha256"]
    assert observed["budget"]["estimated_tokens"] <= 8192
    assert observed["retrieval"]["embedding_calls"] == (1 if mode == "hybrid" else 0)
    denied = api_request(env, "POST", endpoint(env, "/maintenance-targets/recommendations"), actor=2, json={"source_ids": ["905"], "user_id": SCOPE["user_id"], "agent_id": SCOPE["agent_id"]})
    assert denied.status_code == 404
    mismatch = api_request(env, "POST", endpoint(env, "/maintenance-targets/recommendations"), json={"source_ids": ["904"], "user_id": SCOPE["user_id"], "agent_id": SCOPE["agent_id"]})
    assert mismatch.status_code in {409, 422}
    assert counts(env) == before
    assert len(env["embedding_calls"]) - embedded == (1 if mode == "hybrid" else 0)


def test_changed_target_rejected_but_original_plan_replays(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    seed_correction(env)
    selection = selected(env, recommendation(env))
    prefix = endpoint(env, "/maintenance-discoveries")
    preview = result(api_request(env, "POST", prefix + "/preview", json=selection))
    command = {**selection, "operation_id": "selected-plan", "expected_preview_sha256": preview["preview_sha256"]}
    response = api_request(env, "POST", prefix, json=command)
    plan = result(response, status=response.status_code)
    entry = result(api_request(env, "GET", endpoint(env, "/entries/" + env["entry_id"])))
    mutation = api_request(
        env,
        "POST",
        endpoint(env, "/entries/" + env["entry_id"] + "/revisions"),
        json={
            "operation_id": "change-after-recommend",
            "expected_revision": entry["current_revision"],
            "patch": {"old_text": "port 80", "new_text": "port 8081"},
            "reason": "Controlled concurrent editor.",
        },
    )
    assert result(mutation, status=mutation.status_code)["status"] == "ready"
    after = counts(env)
    stale = api_request(env, "POST", prefix + "/preview", json=selection)
    assert stale.status_code == 409
    replay = api_request(env, "POST", prefix, json=command)
    assert result(replay, status=replay.status_code)["discovery_id"] == plan["discovery_id"]
    assert counts(env) == after and env["analysis_calls"] == []


def test_withdrawn_input_cannot_confirm_recommendation(incremental_api: dict[str, Any]) -> None:
    env = incremental_api
    seed_correction(env)
    selection = selected(env, recommendation(env))
    source = result(api_request(env, "GET", endpoint(env, "/sources/905/state")))
    response = api_request(
        env,
        "POST",
        endpoint(env, "/sources/905/withdrawals"),
        json={"operation_id": "withdraw-recommendation-input", "expected_source_revision": source["source_revision"], "reason": "Controlled source withdrawal."},
    )
    assert response.status_code in {200, 202}, response.text
    before = counts(env)
    stale = api_request(env, "POST", endpoint(env, "/maintenance-discoveries/preview"), json=selection)
    assert stale.status_code == 409
    assert counts(env) == before and env["analysis_calls"] == []


def test_selected_target_uses_pending_review_and_existing_publication(incremental_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = incremental_api
    seed_correction(env)
    selection = selected(env, recommendation(env))
    old = result(api_request(env, "GET", endpoint(env, "/entries/" + env["entry_id"])))

    async def correction(self: WriteChat, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        payload = json.loads(history[-1]["content"])
        env["analysis_calls"].append(payload)
        assert [target["entry_id"] for target in payload["entries"]] == [env["entry_id"]]
        return json.dumps(
            {
                "proposals": [
                    {
                        "action": "patch",
                        "target_entry_id": env["entry_id"],
                        "patch": {"old_text": "port 80", "new_text": "port 8080"},
                        "source_input_ids": [source["input_id"] for source in payload["inputs"]],
                        "reason": "Controlled explicit correction preserving the backup sibling.",
                    }
                ]
            }
        ), 0

    monkeypatch.setattr(WriteChat, "async_chat", correction)
    prefix = endpoint(env, "/maintenance-discoveries")
    preview = result(api_request(env, "POST", prefix + "/preview", json=selection))
    response = api_request(env, "POST", prefix, json={**selection, "operation_id": "reviewable-plan", "expected_preview_sha256": preview["preview_sha256"]})
    plan = result(response, status=response.status_code)
    run = api_request(env, "POST", prefix + f"/{plan['discovery_id']}/runs", json={"operation_id": "reviewable-run", "limit": 1})
    assert run.status_code in {200, 202}, run.text
    current = wait_plan(env, plan["discovery_id"], lambda item: item["batches"][0]["state"] == "analyzed")
    job_id = current["batches"][0]["job_id"]
    candidate = result(api_request(env, "GET", endpoint(env, f"/maintenance-jobs/{job_id}/candidates")))["candidates"][0]
    assert candidate["state"] == "pending" and candidate["assessment"]["verification_status"] == "unverified"
    assert result(api_request(env, "GET", endpoint(env, "/entries/" + env["entry_id"]))) == old
    review = result(
        api_request(
            env,
            "POST",
            endpoint(env, f"/maintenance-jobs/{job_id}/candidates/{candidate['candidate_id']}/reviews"),
            json={"operation_id": "explicit-target-review", "expected_revision": candidate["revision"], "decision": "approve"},
        )
    )
    published = api_request(
        env,
        "POST",
        endpoint(env, f"/maintenance-jobs/{job_id}/publications"),
        json={"operation_id": "explicit-target-publication", "candidates": [{"candidate_id": candidate["candidate_id"], "expected_revision": review["revision"]}]},
    )
    operation = result(published, status=published.status_code)
    assert operation["status"] == "ready" and operation["index_sync_status"] == "ready"
    final = result(api_request(env, "GET", endpoint(env, "/entries/" + env["entry_id"])))
    assert final["current"]["content"] == "The project uses port 8080. Keep the backup policy."
    assert final["current"]["assessment"]["verification_status"] == "unverified"


@pytest.mark.external_consumer
def test_pinned_web_target_recommendation_and_explicit_selection(incremental_api: dict[str, Any], tmp_path: Path) -> None:
    env = incremental_api
    seed_correction(env)
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-targets-web-consumer")
    configuration, output = tmp_path / "targets-consumer.private.json", tmp_path / "targets-consumer-result.json"
    owned: list[Path] = []
    try:
        private_consumer_file(
            configuration,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "sourceIds": ["905"],
                    "userId": SCOPE["user_id"],
                    "agentId": SCOPE["agent_id"],
                    "query": "project port 80 backup policy",
                    "expectedTargetEntryId": env["entry_id"],
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        private_consumer_file(output, "", journal, owned)
        before = counts(env)
        journal.event("reserve-target-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-targets.ts")
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-targets.ts", str(configuration)],
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
        after = counts(env)
        assert after[MemoryEntryRevision.__tablename__] == before[MemoryEntryRevision.__tablename__]
        assert after[MemoryMaintenanceCandidate.__tablename__] == before[MemoryMaintenanceCandidate.__tablename__]
        assert not env["analysis_calls"]
        journal.event("targets-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("targets-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
