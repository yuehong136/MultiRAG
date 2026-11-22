"""Shared owned memory dreaming http integration fixtures and helpers."""

import asyncio
import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_maintenance_service
from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from memory.services.messages import MessageService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_maintenance_worker import maintenance_worker
from tests.support.memory_writes_http import WriteChat, api_request, manage_bank, result
from tests.support.memory_writes_http import writes_api as writes_api

_TARGET = "The project uses port 80. Keep the backup policy."


@pytest.fixture
def dreaming_api(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env, mid = writes_api, writes_api["memory_id"]
    monkeypatch.setattr(memory_maintenance_service, "_COUNTER_KEY", env["counter"])
    env["journal"].event(
        "reserve-dreaming-scope",
        memory_id=mid,
        sources=[900],
        seeded_message_ids=[910],
        tables=[model.__tablename__ for model in (MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob)],
    )
    assert (
        MessageService.insert_message(
            [
                {
                    "id": mid + "_910",
                    "memory_id": mid,
                    "message_id": 910,
                    "message_type": "semantic",
                    "source_id": 900,
                    "user_id": "existing-subject",
                    "agent_id": "existing-agent",
                    "session_id": "existing-session",
                    "content": _TARGET,
                    "content_embed": [1.0, 0.0],
                    "valid_at": "2026-11-15 08:00:00",
                    "invalid_at": None,
                    "forget_at": None,
                    "status": True,
                }
            ],
            env["tenants"]["personal"],
            mid,
        )
        == []
    )
    manage_bank(env)
    adopted = result(api_request(env, "POST", f"/memories/{mid}/entries", json={"operation_id": "adopt-target", "legacy_message_id": "910"}))
    env["entry_id"] = adopted["entry_id"]
    env["analysis_calls"] = []
    env["proposals"] = [
        {"action": "add", "content": "A review-only project preference.", "message_type": "semantic", "source_input_ids": ["source-900"], "reason": "reusable preference"},
        {"action": "patch", "target_entry_id": env["entry_id"], "patch": {"old_text": "port 80", "new_text": "port 8080"}, "source_input_ids": ["source-900"], "reason": "a reviewable correction"},
    ]

    async def chat(self: WriteChat, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        assert "never publish" in system and gen_conf["max_tokens"] <= 8192
        env["analysis_calls"].append(history[-1]["content"])
        if "analysis_started" in env:
            env["analysis_started"].set()
            await asyncio.to_thread(env["analysis_release"].wait, 30)
        return env.get("model_response", json.dumps({"proposals": env["proposals"]})), 0

    monkeypatch.setattr(WriteChat, "async_chat", chat)
    try:
        with maintenance_worker(env):
            yield env
    finally:
        if "analysis_release" in env:
            env["analysis_release"].set()
        with Session(env["engine"]) as db:
            for model in (MemoryMaintenanceExecution, MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob):
                db.execute(sa.delete(model).where(model.memory_id == mid))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == mid))
                for model in (MemoryMaintenanceExecution, MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob)
            }
            assert not any(remaining.values()), remaining
        env["journal"].event("dreaming-sql-cleanup-readback", memory_id=mid, remaining=remaining)


def analyze(env: dict[str, Any], job: dict[str, Any]) -> list[dict[str, Any]]:
    mid, jid = env["memory_id"], job["job_id"]
    submitted = api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{jid}/runs", json={"operation_id": "run-dream"})
    accepted = result(submitted, status=submitted.status_code)
    assert submitted.status_code in {200, 202} and accepted["analysis_operation_id"] == "run-dream"
    completed = wait_job(env, jid)
    assert completed["state"] == "ready", completed
    return result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{jid}/candidates"))["candidates"]


def wait_job(env: dict[str, Any], job_id: str, *, execution_terminal: bool = True) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        assert not env.get("worker_errors"), env.get("worker_errors")
        view = result(api_request(env, "GET", f"/memories/{env['memory_id']}/maintenance-jobs/{job_id}"))
        execution = view.get("execution") or {}
        if view["state"] not in {"pending", "running", "cancel_requested"} and (not execution_terminal or execution.get("state") not in {"queued", "claimed", "running"}):
            return view
        time.sleep(0.05)
    raise AssertionError("maintenance execution did not settle")


def approve(env: dict[str, Any], job: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reviewed = []
    for candidate in candidates:
        value = result(
            api_request(
                env,
                "POST",
                f"/memories/{env['memory_id']}/maintenance-jobs/{job['job_id']}/candidates/{candidate['candidate_id']}/reviews",
                json={"operation_id": "approve-" + candidate["candidate_id"], "expected_revision": candidate["revision"], "decision": "approve"},
            )
        )
        assert value["state"] == "approved" and value["revision"] > candidate["revision"]
        assert value["assessment"]["verification_status"] == "unverified"
        reviewed.append({"candidate_id": value["candidate_id"], "expected_revision": value["revision"]})
    return reviewed
