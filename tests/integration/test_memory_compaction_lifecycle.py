"""Late model completion cannot revive canceled or withdrawn context data."""

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Memory
from api.db.memory_compaction_models import MemoryCompactionArtifact
from api.db.memory_revision_models import MemoryMutationOperation
from api.db.memory_writer_models import MemoryWriterAdmission
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result


def _stored(env: dict[str, Any], operation_id: str) -> dict[str, Any]:
    with Session(env["engine"]) as reader:
        artifact = reader.scalar(sa.select(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == env["memory_id"], MemoryCompactionArtifact.operation_id == operation_id))
        admission = reader.get(MemoryWriterAdmission, (env["memory_id"], operation_id))
        operation = reader.get(MemoryMutationOperation, (env["memory_id"], operation_id))
        assert artifact is not None and admission is not None and operation is not None
        return {
            "state": artifact.state,
            "package": artifact.package,
            "redacted": artifact.redacted,
            "updated_at": artifact.updated_at,
            "active_token": admission.active_token,
            "attempt": admission.attempt,
            "writer_state": admission.state,
            "operation_status": operation.status,
        }


@pytest.mark.parametrize("stop", ["cancel", "retained_withdrawal", "bank_deletion"])
def test_stop_during_model_io_retains_drain_proof_and_rejects_late_package(compaction_api: dict[str, Any], stop: str) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    body = request(env, "compact-paused")
    env["summary_started"], env["summary_release"] = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(api_request, env, "POST", f"/memories/{mid}/context-compactions", json=body)
        try:
            assert env["summary_started"].wait(20)
            initial = _stored(env, body["operation_id"])
            assert initial["active_token"] and initial["state"] == "running" and initial["attempt"] == 1
            operation_path = f"/memories/{mid}/context-compaction-operations/{body['operation_id']}"
            running = result(api_request(env, "GET", operation_path))
            assert running["operation"]["writer_busy"] and running["artifact"]["package"] is None
            assert _stored(env, body["operation_id"]) == initial
            assert api_request(env, "POST", operation_path + "/cancellations", actor=2, json={}).status_code == 404
            assert _stored(env, body["operation_id"]) == initial

            if stop == "cancel":
                stopped = result(api_request(env, "POST", operation_path + "/cancellations", json={}))
                assert stopped["operation"]["status"] == "canceled" and stopped["operation"]["writer_busy"]
            elif stop == "retained_withdrawal":
                source = running["artifact"]["sources"][1]
                withdrawn = result(
                    api_request(
                        env,
                        "POST",
                        f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
                        json={"operation_id": "withdraw-paused-retained", "expected_source_revision": int(source["source_revision"]), "reason": "Retained evidence retracted during model IO"},
                    ),
                    status=202,
                )
                assert withdrawn["index_sync_status"] != "ready"
            else:
                deleted = result(api_request(env, "POST", f"/memories/{mid}/deletions", json={"operation_id": "delete-paused-bank"}), status=202)
                assert deleted["lifecycle"] != "deleted" and deleted["tracked_cleanup_confirmed"] is False
            pending = _stored(env, body["operation_id"])
            assert pending["active_token"] == initial["active_token"] and pending["package"] is None
            assert pending["state"] == ("canceled" if stop == "cancel" else "stale")
            assert len(env["summary_calls"]) == 1
        finally:
            env["summary_release"].set()
        completed = future.result(timeout=30)

    final = _stored(env, body["operation_id"])
    assert final["active_token"] is None and final["writer_state"] == "canceled" and final["attempt"] == 1
    assert final["package"] is None and final["state"] == ("canceled" if stop == "cancel" else "stale")
    assert len(env["summary_calls"]) == 1
    if stop != "bank_deletion":
        assert completed.status_code == 200, completed.text
        before_reads = _stored(env, body["operation_id"])
        replay = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
        assert replay["artifact"]["package"] is None and replay["operation"]["can_retry"] is False
        result(api_request(env, "GET", operation_path))
        result(api_request(env, "GET", f"/memories/{mid}/context-compactions"))
        assert _stored(env, body["operation_id"]) == before_reads and len(env["summary_calls"]) == 1
        if stop == "retained_withdrawal":
            result(api_request(env, "POST", f"/memories/{mid}/operations/withdraw-paused-retained/reconciliations", json={}))
    else:
        assert completed.status_code in {404, 409, 503}, completed.text
        deleted = result(api_request(env, "POST", f"/memories/{mid}/operations/delete-paused-bank/reconciliations", json={}))
        assert deleted["lifecycle"] == "deleted" and deleted["tracked_cleanup_confirmed"] is True
        with Session(env["engine"]) as reader:
            assert reader.get(Memory, mid) is None
        assert api_request(env, "GET", operation_path).status_code == 404
        assert _stored(env, body["operation_id"])["package"] is None
