"""Retirement reclaims a slot only after a proven drain and preserves the original identity."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_compaction_models import MemoryCompactionArtifact, MemoryCompactionRoot
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.compaction.domain import MAX_ARTIFACTS
from memory.writes.admission import MAX_WRITER_INVENTORY
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_message_recovery_web import memory_effects, no_sql_mutations
from tests.support.memory_writes_http import api_request, result


def _paths(env: dict[str, Any], operation_id: str, artifact_id: str) -> tuple[str, str, str]:
    base = f"/memories/{env['memory_id']}"
    return f"{base}/context-compactions/inventory", f"{base}/context-compactions/{artifact_id}", f"{base}/context-compaction-operations/{operation_id}"


def _preview(env: dict[str, Any], path: str, *, actor: int = 1) -> dict[str, Any]:
    response = api_request(env, "GET", path + "/retention-preview", actor=actor)
    assert "no-store" in response.headers["Cache-Control"]
    return result(response)


def _retire(env: dict[str, Any], path: str, preview: dict[str, Any], *, actor: int = 1) -> Any:
    return api_request(env, "POST", path + "/retirements", json={"expected_preview_sha256": preview["preview_sha256"]}, actor=actor)


@pytest.mark.parametrize("legacy_null", [False, True])
def test_preview_confirmation_and_replay_remove_only_compaction_copies(compaction_api: dict[str, Any], legacy_null: bool) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    operation_id = "owned-compaction-retirement"
    body, base = request(env, operation_id), f"/memories/{mid}/context-compactions"
    if legacy_null:
        env["summary_response"] = ""
    created = result(api_request(env, "POST", base, json=body))
    artifact_id = created["artifact"]["artifact_id"]
    if legacy_null:
        with Session(env["engine"]) as writer:
            writer.execute(sa.update(MemoryCompactionArtifact).where(MemoryCompactionArtifact.artifact_id == artifact_id).values(package=sa.JSON.NULL))
            writer.commit()
    inventory, path, operation_path = _paths(env, operation_id, artifact_id)
    source_path = f"/memories/{mid}/sources/{env['compaction_source_ids'][1]}/state"
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        current = result(api_request(env, "GET", inventory))
        preview = _preview(env, path)
        raw = result(api_request(env, "GET", source_path))
        assert api_request(env, "GET", path + "/retention-preview", actor=2).status_code == 404
        assert api_request(env, "GET", inventory, actor=2).status_code == 404
        assert _retire(env, path, preview, actor=2).status_code == 404
        assert _retire(env, path, {"preview_sha256": "0" * 64}).status_code == 409
        assert api_request(env, "POST", path + "/retirements", json={"expected_preview_sha256": preview["preview_sha256"], "confirm": True}).status_code == 422
    assert writes == [] and memory_effects(env) == before
    assert current["retained_count"] == 1 and current["retired_count"] == current["writer_busy_count"] == current["unsettled_count"] == 0
    assert preview["eligible"] and preview["blockers"] == [] and not preview["writer_busy"]
    assert preview["affected"]["source_ids"] == env["compaction_source_ids"] and preview["affected"]["releases_slots"] == 1
    assert preview["affected"]["clears_package"] is not legacy_null

    retired = result(_retire(env, path, preview))
    artifact = retired["artifact"]
    assert retired["operation"]["status"] == "stale" and retired["operation"]["operation_id"] == operation_id
    assert artifact["error_code"] == "compaction_retired" and artifact["package"] is None and artifact["sources"] == [] and not artifact["valid_for_current"]
    assert artifact["artifact_id"] == artifact_id and artifact["retired_at"] == retired["operation"]["retired_at"]
    assert artifact["retirement"]["prior_state"] == ("failed" if legacy_null else "generated")
    assert artifact["retirement"]["source_count"] == 2 and artifact["usage"] == created["artifact"]["usage"]
    changed = memory_effects(env)
    assert {key: value for key, value in changed.items() if key != "sql_sha256"} == {key: value for key, value in before.items() if key != "sql_sha256"}
    with no_sql_mutations() as writes:
        assert result(api_request(env, "GET", source_path)) == raw
        assert result(api_request(env, "GET", operation_path)) == result(api_request(env, "GET", path)) == retired
        assert result(api_request(env, "POST", base, json=body)) == retired
        assert result(api_request(env, "POST", operation_path + "/cancellations", json={})) == retired
        assert result(_retire(env, path, preview)) == retired
        assert _retire(env, path, {"preview_sha256": "0" * 64}).status_code == 409
        after = result(api_request(env, "GET", inventory))
        history = _preview(env, path)
        listing = result(api_request(env, "GET", base))["items"]
    assert writes == [] and memory_effects(env) == changed and len(env["summary_calls"]) == 1
    assert after["retained_count"] == 0 and after["retired_count"] == 1 and after["available_slots"] == MAX_ARTIFACTS
    assert history["blockers"] == ["already_retired"] and not history["eligible"] and history["affected"]["source_ids"] == []
    assert listing[0]["retirement"] == artifact["retirement"] and listing[0]["package"] is None
    with Session(env["engine"]) as reader:
        stored = reader.get(MemoryCompactionArtifact, artifact_id)
        admission = reader.get(MemoryWriterAdmission, (mid, operation_id))
        publication = reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == mid, MemoryPublicationOutbox.operation_id == operation_id))
        operation = reader.get(MemoryMutationOperation, (mid, operation_id))
        assert stored is not None and stored.request == {} and stored.retirement == artifact["retirement"] and stored.request_sha256
        assert reader.scalar(sa.select(MemoryCompactionArtifact.package.is_(None)).where(MemoryCompactionArtifact.artifact_id == artifact_id))
        assert not reader.scalar(sa.select(sa.exists().where(MemoryCompactionRoot.artifact_id == artifact_id)))
        assert admission is not None and admission.request_metadata == {"artifact_id": artifact_id, "retired": True} and admission.active_token is None
        assert publication is not None and publication.payload == {"action": "context_compact", "reserved_bytes": 0}
        assert operation is not None and operation.result == {"artifact_id": artifact_id, "generated_only": True, "retired_at": artifact["retired_at"]}


def test_canceled_attempt_requires_actual_drain_and_fresh_preview(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    operation_id = "owned-compaction-retirement-race"
    base = f"/memories/{mid}/context-compactions"
    env["summary_started"], env["summary_release"] = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(api_request, env, "POST", base, json=request(env, operation_id))
        try:
            assert env["summary_started"].wait(20)
            running = result(api_request(env, "GET", f"/memories/{mid}/context-compaction-operations/{operation_id}"))
            inventory, path, operation_path = _paths(env, operation_id, running["artifact"]["artifact_id"])
            canceled = result(api_request(env, "POST", operation_path + "/cancellations", json={}))
            assert canceled["operation"]["status"] == "canceled" and canceled["operation"]["writer_busy"]
            snapshot = memory_effects(env)
            with no_sql_mutations() as writes:
                blocked = _preview(env, path)
                counts = result(api_request(env, "GET", inventory))
                denied = _retire(env, path, blocked)
            assert writes == [] and memory_effects(env) == snapshot
            assert blocked["blockers"] == ["writer_not_drained"] and not blocked["eligible"] and blocked["writer_busy"]
            assert counts["writer_busy_count"] == counts["unsettled_count"] == 1
            assert denied.status_code == 409 and denied.json()["data"]["error_code"] == "compaction_retention_not_eligible"
        finally:
            env["summary_release"].set()
        assert result(future.result(timeout=30))["artifact"]["package"] is None

    fresh = _preview(env, path)
    assert fresh["eligible"] and fresh["preview_sha256"] != blocked["preview_sha256"]
    denial = _retire(env, path, blocked)
    assert denial.status_code == 409 and denial.json()["data"]["error_code"] == "compaction_retention_changed"
    retired = result(_retire(env, path, fresh))
    assert retired["artifact"]["retirement"]["prior_state"] == "canceled" and retired["artifact"]["package"] is None
    assert len(env["summary_calls"]) == 1


def _fill_history(env: dict[str, Any], count: int) -> None:
    mid, tenant, creator = env["memory_id"], env["tenants"]["personal"], env["owners"][1]
    now = datetime.now(UTC)
    shared = {"memory_id": mid, "tenant_id": tenant}
    writer_receipt = {"outcome": "drained", "finished_tokens": ["token"]}
    outbox_receipt = {"active_attempts": [], "finished_attempts": ["token"]}
    outbox_payload = {"action": "context_compact", "reserved_bytes": 0}
    ids = [f"{offset:032x}" for offset in range(count)]
    env["journal"].event("reserve-retirement-history-fixtures", database=env["engine"].url.database, memory_id=mid, ids=ids)
    with Session(env["engine"]) as writer:
        writer.execute(
            sa.insert(MemoryCompactionArtifact),
            [
                {
                    **shared,
                    "artifact_id": identity,
                    "operation_id": f"history-{identity}",
                    "actor_id": creator,
                    "request_sha256": "0" * 64,
                    "request": {},
                    "state": "stale",
                    "input_manifest_sha256": "0" * 64,
                    "model_binding": {},
                    "error_code": "compaction_retired",
                    "cancellation_requested": True,
                    "redacted": True,
                    "retired_at": now,
                    "retirement": {"preview_sha256": "0" * 64, "retired_at": now.isoformat()},
                }
                for identity in ids
            ],
        )
        writer.execute(
            sa.insert(MemoryWriterAdmission),
            [
                {
                    **shared,
                    "operation_id": f"history-{identity}",
                    "actor_id": creator,
                    "kind": "context_compact",
                    "request_sha256": "0" * 64,
                    "writer_generation": 1,
                    "state": "drained",
                    "publication_state": "canceled",
                    "attempt": 1,
                    "finished_at": now,
                    "request_metadata": {"artifact_id": identity, "retired": True},
                    "receipt": writer_receipt,
                }
                for identity in ids
            ],
        )
        writer.execute(
            sa.insert(MemoryPublicationOutbox),
            [
                {
                    **shared,
                    "id": identity,
                    "operation_id": f"history-{identity}",
                    "bank_generation": 1,
                    "state": "redacted",
                    "error_code": "compaction_retired",
                    "payload": outbox_payload,
                    "receipt": outbox_receipt,
                }
                for identity in ids
            ],
        )
        writer.commit()


def test_long_retired_history_neither_consumes_slots_nor_masks_active_cleanup(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    count, operation_id = MAX_WRITER_INVENTORY + 1, "owned-compaction-before-delete"
    _fill_history(env, count)
    base = f"/memories/{mid}"
    appended = result(
        api_request(
            env,
            "POST",
            f"{base}/messages",
            json={"operation_id": "owned-raw-after-retirement", "user_input": "Owned RAW input", "agent_response": "Owned RAW response", "agent_id": "test", "session_id": "test"},
        )
    )
    source_id = appended["raw_source_id"]
    assert int(source_id) > max(map(int, env["compaction_source_ids"])) and appended["queue_state"] == "not_required"
    assert result(api_request(env, "GET", f"{base}/sources/{source_id}/state"))["state"] == "active"
    created = result(api_request(env, "POST", f"{base}/context-compactions", json=request(env, operation_id)))
    inventory, path, _ = _paths(env, operation_id, created["artifact"]["artifact_id"])
    assert result(api_request(env, "GET", inventory))["retired_count"] == count
    assert result(api_request(env, "GET", inventory))["available_slots"] == MAX_ARTIFACTS - 1
    retired = result(_retire(env, path, _preview(env, path)))
    active_operation_id = "history-" + "0" * 32
    with Session(env["engine"]) as writer:
        row = writer.get(MemoryWriterAdmission, (mid, active_operation_id))
        assert row is not None
        row.active_token, row.state = "2" * 32, "active"
        writer.commit()

    delete_id = "owned-delete-after-retirement"
    stopped = result(api_request(env, "POST", f"{base}/deletions", json={"operation_id": delete_id}), status=202)
    assert stopped["lifecycle"] != "deleted" and stopped["writer_drain"]["active_attempts"] == 1
    with Session(env["engine"]) as writer:
        row = writer.get(MemoryWriterAdmission, (mid, active_operation_id))
        assert row is not None
        row.active_token, row.state = None, "drained"
        writer.commit()

    completed = result(api_request(env, "POST", f"{base}/operations/{delete_id}/reconciliations", json={}))
    assert completed["lifecycle"] == "deleted" and completed["tracked_cleanup_confirmed"]
    with Session(env["engine"]) as reader:
        operation = reader.get(MemoryMutationOperation, (mid, operation_id))
        stored = reader.get(MemoryCompactionArtifact, retired["artifact"]["artifact_id"])
        publication = reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == mid, MemoryPublicationOutbox.operation_id == operation_id))
        assert operation is not None and operation.error_code == "compaction_retired" and operation.result["retired_at"] == retired["artifact"]["retired_at"]
        assert stored is not None and stored.retirement == retired["artifact"]["retirement"]
        assert publication is not None and publication.error_code == "compaction_retired"
    assert len(env["summary_calls"]) == 1
