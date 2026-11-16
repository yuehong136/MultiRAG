"""Manual version-bound checks on the real HTTP, SQL and message-store path."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Base
from api.db.memory_revision_models import MemoryEntryRevision
from api.db.memory_verification_models import MemoryVerificationOperation
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_dreaming_http import dreaming_api as dreaming_api
from tests.support.memory_writes_http import api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file


def endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return f"/memories/{env['memory_id']}" + suffix


def entry_endpoint(env: dict[str, Any], suffix: str = "") -> str:
    return endpoint(env, f"/entries/{env['entry_id']}" + suffix)


@pytest.fixture
def verification_api(dreaming_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    env = dreaming_api
    tables = [table for table in reversed(Base.metadata.sorted_tables) if table.schema == "usr_ai" and "memory_id" in table.c and "verification" in table.name]
    assert tables, "verification metadata must be registered before scratch bootstrap"
    env["journal"].event("reserve-verification-http-scope", database=env["engine"].url.database, memory_id=env["memory_id"], tables=[table.fullname for table in tables])
    try:
        yield env
    finally:
        with Session(env["engine"]) as db:
            for table in tables:
                db.execute(sa.delete(table).where(table.c.memory_id == env["memory_id"]))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {table.fullname: reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.memory_id == env["memory_id"])) for table in tables}
        assert not any(remaining.values()), remaining
        env["journal"].event("verification-http-cleanup-readback", remaining=remaining, database=env["engine"].url.database)


def snapshot(env: dict[str, Any]) -> dict[str, Any]:
    return result(api_request(env, "GET", entry_endpoint(env, "/verification-snapshot")))


def command(observed: dict[str, Any], *, operation: str, decision: str = "human_confirmed") -> dict[str, Any]:
    return {
        "operation_id": operation,
        "expected_head_version": observed["head_version"],
        "expected_binding_sha256": observed["binding_sha256"],
        "result": decision,
        "method": "source_comparison",
        "applicability": "Only the project port; the backup policy was not checked.",
        "notes": "Synthetic human checked this exact revision against source 900.",
        "evidence": [{"description": "Source 900 is the comparison context, not proof of all external facts.", "reference": "source:900"}],
    }


def submit(env: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    response = api_request(env, "POST", entry_endpoint(env, "/verifications"), json=body)
    return result(response, status=response.status_code)


def revision_count(env: dict[str, Any]) -> int:
    with Session(env["engine"]) as reader:
        return int(reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntryRevision).where(MemoryEntryRevision.memory_id == env["memory_id"])) or 0)


def revise(env: dict[str, Any], replacement: str, *, operation: str) -> None:
    current = result(api_request(env, "GET", entry_endpoint(env)))
    response = api_request(
        env,
        "POST",
        entry_endpoint(env, "/revisions"),
        json={"operation_id": operation, "expected_revision": current["current_revision"], "content": replacement, "reason": "Synthetic explicit editor; not a verification operation."},
    )
    operation_result = result(response, status=response.status_code)
    assert operation_result["status"] == "ready" and operation_result["index_sync_status"] == "ready"


def test_manual_check_does_not_publish_or_call_models_and_requires_memory_access(verification_api: dict[str, Any]) -> None:
    env = verification_api
    before = result(api_request(env, "GET", entry_endpoint(env)))
    counts = (revision_count(env), len(env["embedding_calls"]), len(env["analysis_calls"]))
    capabilities = result(api_request(env, "GET", endpoint(env, "/capabilities")))["verification"]
    assert capabilities["supported"] is True and capabilities["automatic_verification"] is False and capabilities["model_calls"] is False
    assert capabilities["mutates_assessment"] is False
    observed = snapshot(env)
    assert observed["binding_sha256"] and observed["head_version"] == "0"
    body = command(observed, operation="human-check-port")
    for actor in (0, 2):
        assert api_request(env, "GET", entry_endpoint(env, "/verification-snapshot"), actor=actor).status_code == 404
        assert api_request(env, "POST", entry_endpoint(env, "/verifications"), actor=actor, json=body).status_code == 404
        assert api_request(env, "GET", endpoint(env, "/verification-todos"), actor=actor).status_code == 404
    for extra in ({"actor_id": env["owners"][0]}, {"verification_status": "verified"}, {"tenant_id": env["owners"][0]}):
        assert api_request(env, "POST", entry_endpoint(env, "/verifications"), json={**body, **extra}).status_code == 422
    created = submit(env, body)
    assert created["operation_id"] == body["operation_id"]
    assert created["record"]["effective_status"] == "human_confirmed" and created["record"]["valid_for_current"] is True
    assert submit(env, body) == created
    assert api_request(env, "POST", entry_endpoint(env, "/verifications"), json={**body, "result": "disputed"}).status_code == 409
    assert result(api_request(env, "GET", entry_endpoint(env))) == before
    assert (revision_count(env), len(env["embedding_calls"]), len(env["analysis_calls"])) == counts
    assert result(api_request(env, "GET", endpoint(env, "/verification-operations/" + body["operation_id"]))) == created


def test_record_cannot_confirm_changed_revision_even_if_words_return_to_original(verification_api: dict[str, Any]) -> None:
    env = verification_api
    old = snapshot(env)
    frozen = command(old, operation="check-before-edit")
    accepted = submit(env, frozen)
    original = "The project uses port 80. Keep the backup policy."
    revise(env, "The project uses port 8080. Keep the backup policy.", operation="change-port-after-check")
    revise(env, original, operation="restore-words-after-check")
    current = snapshot(env)
    assert current["binding_sha256"] != old["binding_sha256"]
    assert api_request(env, "POST", entry_endpoint(env, "/verifications"), json={**frozen, "operation_id": "stale-check-new-operation"}).status_code == 409
    # An accepted old operation remains recoverable; replay must not rebind it.
    recovered = submit(env, frozen)
    assert recovered["verification_id"] == accepted["verification_id"] and recovered["head_version"] == accepted["head_version"]
    assert recovered["record"]["binding_sha256"] == old["binding_sha256"]
    assert recovered["record"]["effective_status"] == "stale" and recovered["record"]["valid_for_current"] is False
    pending = result(api_request(env, "GET", endpoint(env, "/verification-todos")))
    assert any(item["entry_id"] == env["entry_id"] and item["effective_status"] == "stale" for item in pending["items"])
    assert len(env["analysis_calls"]) == 0


def test_current_dispute_is_replaced_only_by_explicit_new_human_check(verification_api: dict[str, Any]) -> None:
    env = verification_api
    first = submit(env, command(snapshot(env), operation="record-dispute", decision="disputed"))
    pending = result(api_request(env, "GET", endpoint(env, "/verification-todos")))
    assert env["entry_id"] in json.dumps(pending)
    next_snapshot = snapshot(env)
    assert int(next_snapshot["head_version"]) > 0
    submit(env, command(next_snapshot, operation="human-recheck-after-dispute"))
    after = result(api_request(env, "GET", endpoint(env, "/verification-todos")))
    assert env["entry_id"] not in json.dumps(after)
    history = result(api_request(env, "GET", entry_endpoint(env, "/verifications")))
    assert "disputed" in json.dumps(history) and "human_confirmed" in json.dumps(history)
    assert first["operation_id"] == "record-dispute" and not env["analysis_calls"]


def test_source_withdrawal_does_not_leak_verification_notes_in_history_or_operation(verification_api: dict[str, Any]) -> None:
    env = verification_api
    sentinel = "private-verification-note-should-be-redacted"
    body = command(snapshot(env), operation="check-before-withdrawal")
    body.update(notes=sentinel, applicability=sentinel, evidence=[{"description": sentinel, "reference": sentinel}])
    submit(env, body)
    source = result(api_request(env, "GET", endpoint(env, "/sources/900/state")))
    withdrawal = api_request(
        env,
        "POST",
        endpoint(env, "/sources/900/withdrawals"),
        json={"operation_id": "withdraw-checked-source", "expected_source_revision": source["source_revision"], "reason": "Synthetic source withdrawal."},
    )
    assert withdrawal.status_code in {200, 202}, withdrawal.text
    for suffix in (f"/entries/{env['entry_id']}/verifications", "/verification-operations/check-before-withdrawal", "/verification-todos"):
        response = api_request(env, "GET", endpoint(env, suffix))
        assert response.status_code == 200, response.text
        assert sentinel not in response.text
    with Session(env["engine"]) as reader:
        for table in Base.metadata.sorted_tables:
            if "verification" in table.name and "memory_id" in table.c:
                rows = reader.execute(sa.select(table).where(table.c.memory_id == env["memory_id"])).mappings().all()
                assert sentinel not in repr(rows), table.fullname
    assert not env["analysis_calls"]


def test_actual_projection_drift_is_detected_without_a_sql_revision_change(verification_api: dict[str, Any]) -> None:
    env = verification_api
    observed = snapshot(env)
    accepted = submit(env, command(observed, operation="check-before-physical-drift"))
    store = ManagedMessageStore(env["tenants"]["personal"], env["memory_id"])
    identity = int(observed["binding"]["entry"]["message_id"])
    message = asyncio.run(store.read(identity))
    assert message is not None
    env["journal"].event("reserve-verification-projection-drift", memory_id=env["memory_id"], message_id=identity)
    asyncio.run(store.write({**message, "status": False, "content": message["content"] + " Unexpected external rewrite."}))
    asyncio.run(store.set_status(identity, True))
    rewritten = asyncio.run(store.read(identity))
    assert rewritten is not None and rewritten["content"] != message["content"]
    operation = result(api_request(env, "GET", endpoint(env, "/verification-operations/" + accepted["operation_id"])))
    assert operation["record"]["valid_for_current"] is False
    assert operation["record"]["effective_status"] == "stale"
    assert operation["record"]["stale_reasons"]
    assert api_request(env, "GET", entry_endpoint(env, "/verification-snapshot")).status_code == 409
    assert not env["analysis_calls"]


def test_source_disable_and_reenable_requires_a_new_human_check(verification_api: dict[str, Any]) -> None:
    env = verification_api
    observed = snapshot(env)
    accepted = submit(env, command(observed, operation="check-before-source-generation-change"))
    for status in (False, True):
        result(api_request(env, "PUT", f"/messages/{env['memory_id']}:900", json={"status": status}))
    operation = result(api_request(env, "GET", endpoint(env, "/verification-operations/" + accepted["operation_id"])))
    assert operation["record"]["effective_status"] == "stale" and operation["record"]["valid_for_current"] is False
    fresh = snapshot(env)
    assert fresh["binding_sha256"] != observed["binding_sha256"]
    renewed = submit(env, command(fresh, operation="explicit-new-check-after-generation-change"))
    assert renewed["record"]["effective_status"] == "human_confirmed" and renewed["record"]["valid_for_current"] is True


def test_revocation_cannot_overwrite_a_newer_human_conclusion(verification_api: dict[str, Any]) -> None:
    env = verification_api
    first = submit(env, command(snapshot(env), operation="first-human-check"))
    second = submit(env, command(snapshot(env), operation="second-human-check", decision="disputed"))
    old_path = endpoint(env, "/verifications/" + first["verification_id"] + "/revocations")
    assert (
        api_request(env, "POST", old_path, json={"operation_id": "revoke-replaced-record", "expected_head_version": first["head_version"], "reason": "Must not clear a newer dispute."}).status_code
        == 409
    )
    path = endpoint(env, "/verifications/" + second["verification_id"] + "/revocations")
    body = {"operation_id": "explicit-revocation", "expected_head_version": second["head_version"], "reason": "The checker withdraws this manual conclusion."}
    assert api_request(env, "POST", path, actor=2, json=body).status_code == 404
    revoked = result(api_request(env, "POST", path, json=body))
    assert revoked["record"]["effective_status"] == "revoked" and revoked["record"]["valid_for_current"] is False
    assert result(api_request(env, "POST", path, json=body)) == revoked
    pending = result(api_request(env, "GET", endpoint(env, "/verification-todos")))
    assert len(pending["items"]) == 1
    assert pending["items"][0]["verification_id"] == second["verification_id"] and pending["items"][0]["effective_status"] == "revoked"
    assert not env["analysis_calls"]


def test_lost_commit_acknowledgement_keeps_the_same_operation_recoverable(verification_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = verification_api
    body = command(snapshot(env), operation="human-check-lost-acknowledgement")
    original_commit = AsyncSession.commit
    injected: list[bool] = []

    def mark_verification(session: Session, context: Any, instances: Any) -> None:
        if any(isinstance(item, MemoryVerificationOperation) and item.memory_id == env["memory_id"] and item.operation_id == body["operation_id"] for item in session.new):
            session.info["verification_acknowledgement_fault"] = True

    async def commit_with_lost_acknowledgement(db: AsyncSession) -> None:
        await original_commit(db)
        if db.sync_session.info.pop("verification_acknowledgement_fault", False) and not injected:
            injected.append(True)
            raise sa.exc.OperationalError("synthetic lost acknowledgement after real commit", {}, RuntimeError("controlled transport failure"))

    sa.event.listen(Session, "before_flush", mark_verification)
    monkeypatch.setattr(AsyncSession, "commit", commit_with_lost_acknowledgement)
    try:
        response = api_request(env, "POST", entry_endpoint(env, "/verifications"), json=body)
        assert injected == [True]
        assert response.status_code == 503, response.text
        assert response.json()["data"]["operation_id"] == body["operation_id"]
        observed = result(api_request(env, "GET", endpoint(env, "/verification-operations/" + body["operation_id"])))
        assert observed["status"] == "ready" and observed["record"]["effective_status"] == "human_confirmed"
        replay = submit(env, body)
        assert replay["verification_id"] == observed["verification_id"]
        history = result(api_request(env, "GET", entry_endpoint(env, "/verifications")))
        assert len(history["records"]) == 1
    finally:
        sa.event.remove(Session, "before_flush", mark_verification)
    assert not env["analysis_calls"]


@pytest.mark.external_consumer
def test_pinned_web_manual_verification_client(verification_api: dict[str, Any], tmp_path: Path) -> None:
    env = verification_api
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("memory-verification-web-consumer")
    configuration, output = tmp_path / "verification-consumer.private.json", tmp_path / "verification-consumer-result.json"
    owned: list[Path] = []
    before = result(api_request(env, "GET", entry_endpoint(env)))
    counts = (revision_count(env), len(env["embedding_calls"]), len(env["analysis_calls"]))
    try:
        private_consumer_file(
            configuration,
            json.dumps({"baseURL": env["base"], "token": env["jwts"][1], "memoryId": env["memory_id"], "entryId": env["entry_id"], "ownedScratchConfirmed": True, "resultFile": str(output)}),
            journal,
            owned,
        )
        private_consumer_file(output, "", journal, owned)
        journal.event("reserve-verification-web-consumer", web_sha=web_sha, memory_id=env["memory_id"], script="scripts/consumers/memory-verification.ts")
        completed = subprocess.run(
            ["node", "--import", "tsx", "scripts/consumers/memory-verification.ts", str(configuration)],
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
        assert result(api_request(env, "GET", entry_endpoint(env))) == before
        assert (revision_count(env), len(env["embedding_calls"]), len(env["analysis_calls"])) == counts
        journal.event("verification-web-consumer-readback", web_sha=web_sha, receipt=receipt)
    finally:
        for path in owned:
            path.unlink(missing_ok=True)
            assert not path.exists()
        journal.event("verification-web-private-files-cleaned", paths=[str(path) for path in owned], absent=True)
