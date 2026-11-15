"""Candidate isolation and reviewed publication through real HTTP/SQL/vector storage."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_maintenance_service
from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import RevisionStorageUnknown, content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService
from tests.integration.test_memory_writes_http import WriteChat, api_request, manage_bank, result
from tests.integration.test_memory_writes_http import writes_api as writes_api
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_maintenance_worker import maintenance_worker

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


def create_job(env: dict[str, Any], *, with_target: bool = True, operation_id: str = "snapshot-dream") -> dict[str, Any]:
    return result(
        api_request(env, "POST", f"/memories/{env['memory_id']}/maintenance-jobs", json={"operation_id": operation_id, "source_ids": ["900"], "entry_ids": [env["entry_id"]] if with_target else []}),
        status=202,
    )


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


def test_candidates_are_isolated_until_explicit_reviewed_batch_publication(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    counter = REDIS_CONN.REDIS.get(env["counter"])
    job = create_job(env)
    assert job["coverage"]["analyzed"] == 0 and not env["analysis_calls"]
    assert result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}"))["state"] == "pending"
    candidates = analyze(env, job)
    assert {item["action"] for item in candidates} == {"add", "patch"}
    patch_candidate = next(item for item in candidates if item["action"] == "patch")
    assert patch_candidate["diff"] == {"old_text": _TARGET, "new_text": "The project uses port 8080. Keep the backup policy."}
    assert REDIS_CONN.REDIS.get(env["counter"]) == counter
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])["content"] == _TARGET
    assert len(asyncio.run(ManagedMessageStore(env["tenants"]["personal"], mid).inventory())) == 2
    unreviewed = api_request(
        env,
        "POST",
        f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications",
        json={"operation_id": "unreviewed", "candidates": [{"candidate_id": candidates[0]["candidate_id"], "expected_revision": 1}]},
    )
    assert unreviewed.status_code == 409
    selection = approve(env, job, candidates)
    payload = {"operation_id": "publish-reviewed", "candidates": selection}
    published = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json=payload))
    assert published["status"] == published["index_sync_status"] == "ready", published
    assert result(api_request(env, "GET", f"/memories/{mid}/operations/publish-reviewed"))["entries"] == published["entries"]
    assert result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json=payload))["entries"] == published["entries"]
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{env['entry_id']}"))
    assert current["current"]["content"] == "The project uses port 8080. Keep the backup policy."
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"]) is None
    assert len(env["analysis_calls"]) == 1
    with Session(env["engine"]) as reader:
        for entry in published["entries"]:
            assert isinstance(entry["message_id"], str) and int(entry["message_id"]) > 2**53
            row = reader.get(MemoryEntryRevision, (entry["entry_id"], entry["new_revision"]))
            assert row.origin == "consolidated" and row.assessment["verification_status"] == "unverified"
    still_visible = result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates"))["candidates"]
    assert all(not item["redacted"] for item in still_visible)


def test_source_withdrawal_clears_candidate_copies_and_fences_late_model_output(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    env["proposals"] = [env["proposals"][0]]
    job = create_job(env, with_target=False)
    env["analysis_started"], env["analysis_release"] = threading.Event(), threading.Event()
    submitted = api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": "late-model"})
    assert submitted.status_code == 202
    try:
        assert env["analysis_started"].wait(20)
        withdrawn = result(
            api_request(env, "POST", f"/memories/{mid}/sources/900/withdrawals", json={"operation_id": "withdraw-in-analysis", "expected_source_revision": 1, "reason": "source retracted"}),
            status=202,
        )
        assert withdrawn["index_sync_status"] != "ready"
        with Session(env["engine"]) as reader:
            assert reader.get(MemoryMaintenanceJob, job["job_id"]).state == "redacted"
            assert all(item.snapshot == {} for item in reader.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job["job_id"])))
    finally:
        env["analysis_release"].set()
    ended = wait_job(env, job["job_id"])
    assert ended["state"] == "redacted"
    assert result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates"))["candidates"] == []
    assert result(api_request(env, "POST", f"/memories/{mid}/operations/withdraw-in-analysis/reconciliations"))["index_sync_status"] == "ready"


def test_unknown_projection_write_recovers_fixed_reviewed_plan(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    job = create_job(env)
    selection = approve(env, job, analyze(env, job))
    real_write = ManagedMessageStore.write
    lost = False

    async def lost_ack(store: ManagedMessageStore, message: dict[str, Any]) -> None:
        nonlocal lost
        await real_write(store, message)
        if not lost:
            lost = True
            raise RevisionStorageUnknown("controlled committed physical write acknowledgement loss")

    monkeypatch.setattr(ManagedMessageStore, "write", lost_ack)
    pending = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json={"operation_id": "recover-plan", "candidates": selection}), status=202)
    assert pending["index_sync_status"] != "ready"
    ids = [entry["message_id"] for entry in pending["entries"]]
    count, counter = len(env["embedding_calls"]), REDIS_CONN.REDIS.get(env["counter"])
    monkeypatch.setattr(ManagedMessageStore, "write", real_write)
    ready = result(api_request(env, "POST", f"/memories/{mid}/operations/recover-plan/reconciliations"))
    assert ready["status"] == ready["index_sync_status"] == "ready"
    assert [entry["message_id"] for entry in ready["entries"]] == ids
    assert len(env["analysis_calls"]) == 1 and len(env["embedding_calls"]) == count and REDIS_CONN.REDIS.get(env["counter"]) == counter


def test_malformed_model_output_leaves_no_candidates_or_coverage_success(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    env["model_response"] = "invalid JSON from controlled model"
    job = create_job(env)
    submitted = api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": "bad-model"})
    assert submitted.status_code in {200, 202}
    failed = wait_job(env, job["job_id"])
    assert failed["state"] == "failed" and failed["coverage"]["analyzed"] == 0
    assert result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates"))["candidates"] == []
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])["content"] == _TARGET


def test_durable_submission_is_discovered_after_worker_restart_without_duplicate_analysis(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    env["worker_stop"].set()
    env["worker_thread"].join(timeout=10)
    assert not env["worker_thread"].is_alive()
    job = create_job(env)
    queued = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": "survive-restart"}), status=202)
    assert queued["execution"]["state"] == "queued"
    for _ in range(3):
        pending = result(api_request(env, "GET", f"/memories/{mid}/operations"))
        command = next(item for item in pending["operations"] if item["operation_id"] == "survive-restart")
        assert command["job_id"] == job["job_id"] and command["execution"]["state"] == "queued" and not command["can_reconcile"]
        detail = result(api_request(env, "GET", f"/memories/{mid}/operations/survive-restart"))
        assert detail["execution"]["state"] == "queued" and not env["analysis_calls"]
    with maintenance_worker(env):
        completed = wait_job(env, job["job_id"])
        assert completed["state"] == "ready" and completed["execution"]["state"] == "succeeded"
        replay = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": "survive-restart"}))
        assert replay["execution"]["state"] == "succeeded" and len(env["analysis_calls"]) == 1
        candidates = result(api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates"))["candidates"]
        assert candidates and all(item["state"] == "pending" and item["assessment"]["verification_status"] == "unverified" for item in candidates)
        assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])["content"] == _TARGET


def test_cancel_queued_execution_prevents_model_after_worker_returns(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    env["worker_stop"].set()
    env["worker_thread"].join(timeout=10)
    assert not env["worker_thread"].is_alive()
    job = create_job(env)
    result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/runs", json={"operation_id": "cancel-before-model"}), status=202)
    canceled = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/cancellations", json={"operation_id": "cancel-queued"}))
    assert canceled["state"] == canceled["execution"]["state"] == "canceled"
    with maintenance_worker(env):
        assert env["worker_polled"].wait(10)
        detail = result(api_request(env, "GET", f"/memories/{mid}/operations/cancel-before-model"))
        assert detail["execution"]["state"] == "canceled"
        assert not env["analysis_calls"]


def seed_second_source(env: dict[str, Any]) -> None:
    mid = env["memory_id"]
    env["journal"].event("reserve-dreaming-additional-source", memory_id=mid, message_id=901)
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    asyncio.run(
        store.write(
            {
                "id": mid + "_901",
                "memory_id": mid,
                "message_id": 901,
                "message_type": "raw",
                "source_id": 0,
                "user_id": "existing-subject",
                "agent_id": "existing-agent",
                "session_id": "second-session",
                "content": "A later cross-session project statement.",
                "content_embed": [1.0, 0.0],
                "valid_at": "2026-11-15 09:00:00",
                "invalid_at": None,
                "forget_at": None,
                "status": False,
            }
        )
    )
    asyncio.run(store.set_status(901, True))
    result(
        api_request(
            env, "POST", f"/memories/{mid}/sources/901/adoptions", json={"operation_id": "adopt-second-source", "expected_content_sha256": content_hash("A later cross-session project statement.")}
        )
    )


def create_two_source_job(env: dict[str, Any]) -> dict[str, Any]:
    return result(api_request(env, "POST", f"/memories/{env['memory_id']}/maintenance-jobs", json={"operation_id": "two-source-job", "source_ids": ["900", "901"], "entry_ids": []}), status=202)


def publish_selection(env: dict[str, Any], job: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates = analyze(env, job)
    selection = approve(env, job, candidates)
    published = result(api_request(env, "POST", f"/memories/{env['memory_id']}/maintenance-jobs/{job['job_id']}/publications", json={"operation_id": "publish-two-sources", "candidates": selection}))
    assert published["status"] == published["index_sync_status"] == "ready", published
    return published, candidates


def test_published_batch_anchor_withdrawal_preserves_an_independent_item(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    seed_second_source(env)
    env["proposals"] = [
        env["proposals"][0],
        {"action": "add", "content": "An independent fact from the second source.", "message_type": "semantic", "source_input_ids": ["source-901"], "reason": "independent evidence"},
    ]
    job = create_two_source_job(env)
    published, candidates = publish_selection(env, job)
    independent_id = next(item["candidate_id"] for item in candidates if item["sources"][0]["source_id"] == "901")
    independent = next(item for item in published["entries"] if item["candidate_id"] == independent_id)
    result(
        api_request(
            env, "POST", f"/memories/{mid}/sources/900/withdrawals", json={"operation_id": "withdraw-published-anchor", "expected_source_revision": 1, "reason": "only the first source is withdrawn"}
        )
    )
    still_served = MessageService.get_by_message_id(mid, int(independent["message_id"]), env["tenants"]["personal"])
    assert still_served and still_served["content"] == "An independent fact from the second source."
    with Session(env["engine"]) as reader:
        assert reader.get(MemoryEntry, independent["entry_id"]).state == "active"
        assert all(
            item.content is None and item.base_content is None and item.reason is None
            for item in reader.scalars(sa.select(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.job_id == job["job_id"]))
        )
    reconciled = result(api_request(env, "POST", f"/memories/{mid}/operations/publish-two-sources/reconciliations"))
    assert reconciled["status"] == "stale" and reconciled["index_sync_status"] != "ready"
    assert MessageService.get_by_message_id(mid, int(independent["message_id"]), env["tenants"]["personal"])


def test_cross_session_sources_survive_manual_revision_and_nonanchor_withdrawal(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    seed_second_source(env)
    env["proposals"] = [{**env["proposals"][0], "source_input_ids": ["source-900", "source-901"]}]
    job = create_two_source_job(env)
    published, _ = publish_selection(env, job)
    item = published["entries"][0]
    revision = result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{item['entry_id']}/revisions",
            json={"operation_id": "revise-cross-session", "expected_revision": 1, "content": "A manually refined cross-session preference.", "reason": "explicit correction"},
        )
    )
    assert revision["index_sync_status"] == "ready"
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{item['entry_id']}"))
    assert current["current"]["content"] == "A manually refined cross-session preference."
    result(
        api_request(env, "POST", f"/memories/{mid}/sources/901/withdrawals", json={"operation_id": "withdraw-nonanchor", "expected_source_revision": 1, "reason": "second necessary source withdrawn"})
    )
    assert MessageService.get_by_message_id(mid, int(current["current_message_id"]), env["tenants"]["personal"]) is None
    with Session(env["engine"]) as reader:
        assert all(row.content is None for row in reader.scalars(sa.select(MemoryEntryRevision).where(MemoryEntryRevision.entry_id == item["entry_id"])))


def test_bank_deletion_redacts_maintenance_snapshots_and_candidate_text(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    job = create_job(env)
    assert analyze(env, job)
    deleted = result(api_request(env, "POST", f"/memories/{mid}/deletions", json={"operation_id": "delete-with-candidates"}))
    assert deleted["lifecycle"] == "deleted" and deleted["tracked_cleanup_confirmed"] is True
    with Session(env["engine"]) as reader:
        assert reader.get(MemoryMaintenanceJob, job["job_id"]).entry_snapshots == []
        assert all(row.snapshot == {} for row in reader.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job["job_id"])))
        assert all(
            row.content is None and row.base_content is None and row.patch is None and row.diff is None
            for row in reader.scalars(sa.select(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.job_id == job["job_id"]))
        )
    assert api_request(env, "GET", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/candidates").status_code == 404


def test_cancel_unpublished_batch_cleans_fixed_staging_without_changing_current(dreaming_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    job = create_job(env)
    selection = approve(env, job, analyze(env, job))
    real_write = ManagedMessageStore.write

    async def lost_ack(store: ManagedMessageStore, message: dict[str, Any]) -> None:
        await real_write(store, message)
        raise RevisionStorageUnknown("controlled staging acknowledgement loss")

    monkeypatch.setattr(ManagedMessageStore, "write", lost_ack)
    pending = result(
        api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json={"operation_id": "cancel-staged-publication", "candidates": selection}), status=202
    )
    fixed_ids = [int(item["message_id"]) for item in pending["entries"]]
    assert pending["index_sync_status"] != "ready"
    monkeypatch.setattr(ManagedMessageStore, "write", real_write)
    canceled_job = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/cancellations", json={"operation_id": "cancel-staged-job"}))
    assert canceled_job["state"] == "canceled"
    canceled = result(api_request(env, "POST", f"/memories/{mid}/operations/cancel-staged-publication/reconciliations"))
    assert canceled["status"] == canceled["index_sync_status"] == "canceled"
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    assert all(asyncio.run(store.read(identity)) is None for identity in fixed_ids)
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])["content"] == _TARGET
    with Session(env["engine"]) as reader:
        assert reader.get(ManagedMemoryState, mid).reserved_bytes == 0
        assert reader.get(MemoryEntry, env["entry_id"]).current_revision == 1


def test_patch_preserves_existing_episodic_event_metadata(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    start, end = "2021-02-03 09:00:00", "2021-02-03 18:00:00"
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    env["journal"].event("reserve-dreaming-event-target", memory_id=mid, message_id=912)
    asyncio.run(
        store.write(
            {
                "id": mid + "_912",
                "memory_id": mid,
                "message_id": 912,
                "message_type": "episodic",
                "source_id": 900,
                "user_id": "existing-subject",
                "agent_id": "existing-agent",
                "session_id": "existing-session",
                "content": _TARGET,
                "content_embed": [1.0, 0.0],
                "valid_at": start,
                "invalid_at": end,
                "forget_at": None,
                "status": False,
            }
        )
    )
    asyncio.run(store.set_status(912, True))
    env["entry_id"] = result(api_request(env, "POST", f"/memories/{mid}/entries", json={"operation_id": "adopt-event-target", "legacy_message_id": "912"}))["entry_id"]
    env["proposals"] = [
        {
            "action": "patch",
            "target_entry_id": env["entry_id"],
            "patch": {"old_text": "port 80", "new_text": "port 8080"},
            "source_input_ids": ["source-900"],
            "reason": "correct one word, keep event metadata",
        }
    ]
    job = create_job(env)
    selection = approve(env, job, analyze(env, job))
    published = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json={"operation_id": "publish-event-patch", "candidates": selection}))
    assert published["status"] == published["index_sync_status"] == "ready"
    physical = asyncio.run(store.read(int(published["entries"][0]["message_id"])))
    assert physical and physical["valid_at"] == start and physical["invalid_at"] == end and physical["message_type"] == "episodic"
    assert physical["source_id"] == 900 and physical["session_id"] == "existing-session"
