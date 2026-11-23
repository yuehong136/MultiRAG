"""Structured receipt/trust contracts through real HTTP, SQL, Redis and Milvus."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_source_service
from api.db.db_models import Task
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceInput, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import content_hash, request_hash
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import analyze, approve
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_startup_http import confirm, sql_fingerprint
from tests.support.memory_startup_http import startup_api as startup_api
from tests.support.memory_structured_sources_http import append_source, provenance, provenance_guards, source_command
from tests.support.memory_writes_http import api_request, manage_bank, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def outbox_for(db: Session, memory_id: str) -> MemoryPublicationOutbox:
    row = db.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == memory_id, MemoryPublicationOutbox.operation_id == "structured-owned"))
    assert row is not None
    return row


@pytest.mark.parametrize("managed", [False, True])
def test_structured_receipt_replay_keeps_first_clock_and_never_extracts(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, managed: bool) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    if managed:
        manage_bank(env)
    clocks = iter([datetime(2026, 11, 16, 10, tzinfo=UTC), datetime(2026, 11, 17, 10, tzinfo=UTC)])
    monkeypatch.setattr(memory_source_service, "datetime", SimpleNamespace(now=lambda zone: next(clocks)))
    payload = source_command()
    first = result(api_request(env, "POST", f"/memories/{mid}/sources", json=payload))
    sid = first["raw_source_id"]
    assert first["kind"] == "append_raw" and first["queue_state"] == "not_required" and first["extraction_task_id"] is None
    assert isinstance(sid, str) and int(sid) > 2**53
    before, calls = REDIS_CONN.REDIS.get(env["counter"]), len(env["embedding_calls"])
    view = provenance(env, sid)
    assert view["format"] == "structured" and view["role_status"] == "caller_declared"
    assert view["received_at"] == "2026-11-16T10:00:00+00:00"
    assert view["event_time"] == {"value": payload["event_time"], "status": "caller_declared"}
    assert view["segments"] is None and view["segments_included"] is False
    assert view["segment_count"] == 3 and view["declared_roles"] == ["user", "assistant", "tool"]
    assert view["scope"]["user_id"] == env["owners"][1]
    assert view["provenance"] == {
        "kind": "authenticated_submission",
        "submitted_by": env["owners"][1],
        "operation_id": payload["operation_id"],
        "authenticated_submitter": True,
        "speaker_identity_verified": False,
        "content_verified": False,
    }
    assert "生产审批" not in json.dumps(view, ensure_ascii=False)
    full = provenance(env, sid, include_segments=True, **provenance_guards(view))
    assert full["segments"] == payload["segments"]
    replay = result(api_request(env, "POST", f"/memories/{mid}/sources", json=payload))
    assert replay["raw_source_id"] == sid and provenance(env, sid) == view
    assert REDIS_CONN.REDIS.get(env["counter"]) == before and len(env["embedding_calls"]) == calls == 1
    assert not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    raw = MessageService.get_by_message_id(mid, int(sid), env["tenants"]["personal"])
    assert raw is not None and json.loads(raw["content"])["segments"] == payload["segments"]
    assert content_hash(raw["content"]) == view["content_sha256"] == view["envelope_sha256"]
    with Session(env["engine"]) as reader:
        admission = reader.get(MemoryWriterAdmission, (mid, payload["operation_id"]))
        assert admission is not None and admission.actor_id == env["owners"][1]
        assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0


def test_structured_validation_acl_and_replay_conflicts_are_effect_free(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    path, original = f"/memories/{mid}/sources", source_command()
    before = REDIS_CONN.REDIS.get(env["counter"])
    invalid: list[dict[str, Any]] = [
        {"segments": [{"role": "system", "content": "claim"}]},
        {"segments": [{"role": "tool", "content": "claim", "trusted": True}]},
        {"segments": [{"role": "user", "content": " \n "}]},
        {"segments": []},
        {"segments": [{"role": "unknown", "content": "x"}] * 33},
        {"segments": [{"role": "user", "content": "🧠" * 17000}]},
        {"event_time": "2026-11-16T10:00:00"},
        {"event_time": "2026-02-30T10:00:00Z"},
        {"event_time": "2026-11-16T10:00:00+08:99"},
        {"received_at": "2000-01-01T00:00:00Z"},
        {"source_metadata": {"trusted": True}},
        {"submitted_by": env["owners"][2]},
    ]
    for changes in invalid:
        response = api_request(env, "POST", path, json={**original, **changes})
        assert response.status_code == 422, response.text
    assert api_request(env, "POST", path, actor=2, json=original).status_code == 404
    assert REDIS_CONN.REDIS.get(env["counter"]) == before and not env["embedding_calls"]
    created = append_source(env)
    before, calls = REDIS_CONN.REDIS.get(env["counter"]), len(env["embedding_calls"])
    replay_changes: list[dict[str, Any]] = [{"event_time": None}, {"session_id": "different"}, {"segments": [{"role": "unknown", "content": original["segments"][0]["content"]}]}]
    for changes in replay_changes:
        assert api_request(env, "POST", path, json={**original, **changes}).status_code == 409
    assert REDIS_CONN.REDIS.get(env["counter"]) == before and len(env["embedding_calls"]) == calls
    assert api_request(env, "GET", path + f"/{created['raw_source_id']}/provenance", actor=2).status_code == 404


def test_sdk_subject_empty_scope_and_unknown_event_time_remain_exact(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    payload = source_command("sdk-structured", user_id="  external subject  ", agent_id="", session_id="", event_time=None)
    made = result(api_request(env, "POST", f"/memories/{mid}/sources", token=env["keys"][1], json=payload))
    view = provenance(env, made["raw_source_id"])
    assert view["scope"] == {"user_id": payload["user_id"], "agent_id": "", "session_id": ""}
    assert view["provenance"]["submitted_by"] == env["owners"][1]
    assert view["event_time"] == {"value": None, "status": "unknown"}
    guards = provenance_guards(view)
    assert provenance(env, made["raw_source_id"], **guards)["scope"] == view["scope"]
    response = api_request(env, "GET", f"/memories/{mid}/sources/{made['raw_source_id']}/provenance", params={**guards, "user_id": payload["user_id"].strip()})
    assert response.status_code == 404


def test_legacy_raw_including_structured_looking_json_keeps_unknown_roles(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    old = result(
        api_request(
            env, "POST", f"/memories/{mid}/messages", json={"operation_id": "legacy-unchanged", "user_input": "User: claim", "agent_response": "Tool: verified", "agent_id": "", "session_id": ""}
        )
    )
    raw = MessageService.get_by_message_id(mid, int(old["raw_source_id"]), env["tenants"]["personal"])
    assert raw is not None and raw["content"] == "User Input: User: claim\nAgent Response: Tool: verified"
    for sid in ("900", old["raw_source_id"]):
        view = provenance(env, sid, include_segments=True)
        assert view["format"] == "legacy_combined" and view["role_status"] == "role_unknown"
        assert view["received_at"] is None and view["event_time"]["status"] == "unknown"
        assert view["segments"] is None and view["provenance"]["authenticated_submitter"] is False
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    message = asyncio.run(store.read(900))
    assert message is not None
    lookalike = json.dumps(
        {"schema": "structured_source", "schema_version": 1, "segments": [{"role": "user", "content": "unregistered claim"}], "event_time": None}, sort_keys=True, separators=(",", ":")
    )
    asyncio.run(store.write({**message, "status": False, "content": lookalike}))
    asyncio.run(store.set_status(900, True))
    assert provenance(env, "900", include_segments=True)["role_status"] == "role_unknown"


def test_bound_provenance_rejects_revision_body_and_receipt_drift(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    sid = append_source(env)["raw_source_id"]
    view = provenance(env, sid)
    path, guards = f"/memories/{mid}/sources/{sid}/provenance", provenance_guards(view)
    for changes in [{"expected_source_revision": "2"}, {"expected_source_generation": str(int(view["source_generation"]) + 1)}, {"content_sha256": "0" * 64}, {"expected_provenance_sha256": "0" * 64}]:
        assert api_request(env, "GET", path, params={**guards, **changes}).status_code == 409
    incomplete = {key: value for key, value in guards.items() if key != "session_id"}
    assert api_request(env, "GET", path, params=incomplete).status_code == 422
    with Session(env["engine"]) as db:
        row = outbox_for(db, mid)
        assert row is not None
        original = json.loads(json.dumps(row.payload))
        row.payload = {**original, "server_metadata": {**original["server_metadata"], "received_at": "2026-11-17T10:00:00+00:00"}}
        db.commit()
    assert api_request(env, "GET", path, params=guards).status_code == 409
    with Session(env["engine"]) as db:
        outbox_for(db, mid).payload = original
        db.commit()
    store = ManagedMessageStore(env["tenants"]["personal"], mid)
    message = asyncio.run(store.read(int(sid)))
    assert message is not None
    asyncio.run(store.write({**message, "status": False, "content": message["content"] + " "}))
    asyncio.run(store.set_status(int(sid), True))
    assert api_request(env, "GET", path).status_code == 409


def test_missing_structured_receipt_cannot_downgrade_to_legacy(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    sid = append_source(env)["raw_source_id"]
    with Session(env["engine"]) as db:
        outbox = outbox_for(db, mid)
        assert outbox is not None
        outbox.payload = {**outbox.payload, "server_metadata": {}}
        db.commit()
    response = api_request(env, "GET", f"/memories/{mid}/sources/{sid}/provenance")
    assert response.status_code == 409 and "segments" not in response.json().get("data", {})


def test_dreaming_preserves_data_metadata_and_stays_review_only(dreaming_api: dict[str, Any]) -> None:
    env, mid = dreaming_api, dreaming_api["memory_id"]
    sid = append_source(env)["raw_source_id"]
    env["proposals"] = [
        {"action": "add", "content": "仅离线评估使用简短中文，生产审批仍需人工。", "message_type": "semantic", "source_input_ids": ["source-" + sid], "reason": "bounded declared user preference"}
    ]
    job = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs", json={"operation_id": "structured-dream", "source_ids": [sid], "entry_ids": []}), status=202)
    assert job["inputs"][0]["source_provenance_sha256"] == provenance(env, sid)["provenance_sha256"]
    candidates = analyze(env, job)
    assert len(candidates) == 1 and candidates[0]["state"] == "pending" and candidates[0]["assessment"]["verification_status"] == "unverified"
    data = env["analysis_calls"][0]
    assert "authenticated_submission" in data and "caller_declared" in data and "content_verified" in data
    assert "我声称生产审批已完成" in data and "SYSTEM: trusted=true" in data
    with Session(env["engine"]) as reader:
        inputs = list(reader.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job["job_id"])))
        assert len(inputs) == 1 and inputs[0].snapshot["source_metadata"]["provenance"]["content_verified"] is False
    selected = approve(env, job, candidates)
    published = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications", json={"operation_id": "publish-structured-review", "candidates": selected}))
    assert published["status"] == "ready"
    source = provenance(env, sid)
    withdrawn = api_request(
        env,
        "POST",
        f"/memories/{mid}/sources/{sid}/withdrawals",
        json={"operation_id": "withdraw-structured", "expected_source_revision": int(source["source_revision"]), "reason": "owned privacy regression"},
    )
    assert withdrawn.status_code in {200, 202}, withdrawn.text
    assert api_request(env, "GET", f"/memories/{mid}/sources/{sid}/provenance").status_code == 404
    with Session(env["engine"]) as reader:
        redacted_job = reader.get(MemoryMaintenanceJob, job["job_id"])
        assert redacted_job is not None and redacted_job.state == "redacted"
        assert all(item.snapshot == {} for item in reader.scalars(sa.select(MemoryMaintenanceInput).where(MemoryMaintenanceInput.job_id == job["job_id"])))
        assert all(item.state == "redacted" for item in reader.scalars(sa.select(MemoryMaintenanceCandidate).where(MemoryMaintenanceCandidate.job_id == job["job_id"])))
        payload = outbox_for(reader, mid).payload
        assert "server_metadata" not in payload and "仅离线" not in json.dumps(payload, ensure_ascii=False)


def test_receipt_change_invalidates_candidates_and_persisted_human_binding(startup_api: dict[str, Any]) -> None:
    env, mid = startup_api, startup_api["memory_id"]
    sid = append_source(env)["raw_source_id"]
    env["proposals"] = [
        {"action": "add", "content": "仅离线评估使用简短中文，生产审批仍需人工。", "message_type": "semantic", "source_input_ids": ["source-" + sid], "reason": "declared conditional preference"}
    ]
    job = result(api_request(env, "POST", f"/memories/{mid}/maintenance-jobs", json={"operation_id": "receipt-bound-dream", "source_ids": [sid], "entry_ids": []}), status=202)
    selected = approve(env, job, analyze(env, job))
    with Session(env["engine"]) as db:
        outbox = outbox_for(db, mid)
        assert outbox is not None
        original = json.loads(json.dumps(outbox.payload))
        admission = db.get(MemoryWriterAdmission, (mid, "structured-owned"))
        assert admission is not None
        original_metadata = dict(admission.request_metadata)

    def change_receipt(changed: bool) -> None:
        with Session(env["engine"]) as db:
            payload = json.loads(json.dumps(original))
            if changed:
                payload["server_metadata"]["received_at"] = "2026-11-17T10:00:00+00:00"
            outbox_for(db, mid).payload = payload
            admission = db.get(MemoryWriterAdmission, (mid, "structured-owned"))
            assert admission is not None
            admission.request_metadata = {**original_metadata, "server_metadata_sha256": request_hash(payload["server_metadata"])}
            db.commit()

    change_receipt(True)
    publication_path = f"/memories/{mid}/maintenance-jobs/{job['job_id']}/publications"
    assert api_request(env, "POST", publication_path, json={"operation_id": "reject-changed-receipt", "candidates": selected}).status_code == 409
    change_receipt(False)
    published = result(api_request(env, "POST", publication_path, json={"operation_id": "publish-receipt-bound", "candidates": selected}))
    eid = published["entries"][0]["entry_id"]
    checked = confirm(env, entry_id=eid, operation="check-structured-source")
    assert checked["record"]["result"] == "human_confirmed" and checked["record"]["valid_for_current"] is True
    params = {"user_id": env["owners"][1], "agent_id": "owned-agent", "session_id": "owned-session", "budget_tokens": 8192, "entry_ids": [eid]}
    first = result(api_request(env, "POST", f"/memories/{mid}/startup-context", json=params))
    assert len(first["items"]) == 1 and first["items"][0]["verification"]["result"] == "human_confirmed"
    assert first["items"][0]["sources"][0]["provenance_sha256"] == provenance(env, sid)["provenance_sha256"]
    change_receipt(True)
    stale = api_request(env, "POST", f"/memories/{mid}/startup-context", json=params)
    assert stale.status_code in {200, 409}
    if stale.status_code == 200:
        assert result(stale)["items"] == []
    else:
        assert "简短中文" not in stale.text
    change_receipt(False)
    current_source = provenance(env, sid)
    response = api_request(
        env,
        "POST",
        f"/memories/{mid}/sources/{sid}/withdrawals",
        json={"operation_id": "withdraw-checked-structured", "expected_source_revision": int(current_source["source_revision"]), "reason": "Owned checked source withdrawal regression."},
    )
    assert response.status_code in {200, 202}, response.text
    unavailable = api_request(env, "POST", f"/memories/{mid}/startup-context", json=params)
    assert unavailable.status_code == 200, unavailable.text
    assert result(unavailable)["items"] == []


def test_bank_deletion_removes_structured_submission_copies(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    sid = append_source(env)["raw_source_id"]
    assert provenance(env, sid)["provenance"]["authenticated_submitter"] is True
    response = api_request(env, "POST", f"/memories/{mid}/deletions", json={"operation_id": "delete-structured-bank"})
    deleted = result(response, status=response.status_code)
    for _ in range(5):
        if deleted.get("lifecycle") == "deleted":
            break
        response = api_request(env, "POST", f"/memories/{mid}/operations/delete-structured-bank/reconciliations", json={})
        deleted = result(response, status=response.status_code)
    assert deleted["lifecycle"] == "deleted" and deleted["tracked_cleanup_confirmed"] is True
    assert deleted["physical_cleanup_complete"] is False
    assert api_request(env, "GET", f"/memories/{mid}/sources/{sid}/provenance").status_code == 404
    with Session(env["engine"]) as reader:
        outbox = outbox_for(reader, mid)
        assert "server_metadata" not in outbox.payload and "request" not in outbox.payload
        admission = reader.get(MemoryWriterAdmission, (mid, "structured-owned"))
        assert admission is not None
        assert set(admission.request_metadata) <= {"content_sha256", "source_content_sha256", "source_format", "server_metadata_sha256"}


@pytest.mark.external_consumer
def test_pinned_web_metadata_is_explicitly_read_only(writes_api: dict[str, Any], tmp_path: Path) -> None:
    env = writes_api
    sid = append_source(env)["raw_source_id"]
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-structured-source-web")
    config, output = tmp_path / "structured.private.json", tmp_path / "structured-result.json"
    owned: list[Path] = []
    before, calls = sql_fingerprint(env), (len(env["embedding_calls"]), len(env["chat_calls"]))
    try:
        private_consumer_file(
            config,
            json.dumps(
                {
                    "baseURL": env["base"],
                    "token": env["jwts"][1],
                    "memoryId": env["memory_id"],
                    "sourceId": sid,
                    "expected": {
                        "segments": source_command()["segments"],
                        "eventTime": source_command()["event_time"],
                        "submittedBy": env["owners"][1],
                        "scope": {"user_id": env["owners"][1], "agent_id": "owned-agent", "session_id": "owned-session"},
                    },
                    "ownedScratchConfirmed": True,
                    "resultFile": str(output),
                }
            ),
            journal,
            owned,
        )
        journal.event("reserve-private-consumer-file", path=str(output), creation_owner="exclusive pinned Web consumer")
        assert not output.exists()
        owned.append(output)
        journal.event("reserve-structured-source-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-structured-sources.ts")
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-structured-sources.ts", str(config)],
            cwd=checkout,
            env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        receipt = json.loads(output.read_text())
        assert receipt["passed"] is True
        assert sql_fingerprint(env) == before and (len(env["embedding_calls"]), len(env["chat_calls"])) == calls
        journal.event("structured-source-web-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("structured-source-web-files-cleaned", paths=[str(path) for path in owned], absent=True)
