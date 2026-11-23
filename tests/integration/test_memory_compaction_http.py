"""Explicit context artifacts preserve lifecycle, source gates and model idempotency."""

import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Task
from api.db.memory_compaction_models import MemoryCompactionArtifact
from api.db.memory_revision_models import MemoryEntry
from api.db.memory_writer_models import MemoryWriterAdmission
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result


def test_generated_package_replays_without_model_or_long_term_writes(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    body = request(env)
    created = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert created["operation"]["status"] == "generated" and created["operation"]["can_retry"] is False
    artifact = created["artifact"]
    assert artifact["valid_for_current"] and artifact["package"]["summary"] == env["summary_response"]
    assert [item["source_id"] for item in artifact["sources"]] == env["compaction_source_ids"]
    assert artifact["package"]["covered"][0]["source_id"] == env["compaction_source_ids"][0]
    retained = json.loads(artifact["package"]["retained"][0]["content"])
    assert "先核对来源" in retained["segments"][0]["content"]
    assert artifact["usage"]["estimated_after_tokens"] < artifact["usage"]["estimated_before_tokens"]
    assert artifact["usage"]["provider_tokens"] is None and len(env["summary_calls"]) == 1
    embeddings = list(env["embedding_calls"])
    replay = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert replay == created and len(env["summary_calls"]) == 1 and env["embedding_calls"] == embeddings
    by_id = result(api_request(env, "GET", f"/memories/{mid}/context-compactions/{artifact['artifact_id']}"))
    assert by_id == created
    listing = result(api_request(env, "GET", f"/memories/{mid}/context-compactions"))
    assert listing["items"][0]["package"] is None and listing["items"][0]["valid_for_current"] is None
    denied = api_request(env, "POST", f"/memories/{mid}/operations/{body['operation_id']}/reconciliations", json={})
    assert denied.status_code == 409 and len(env["summary_calls"]) == 1
    with Session(env["engine"]) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntry).where(MemoryEntry.memory_id == mid)) == 0
        assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0
        admission = reader.get(MemoryWriterAdmission, (mid, body["operation_id"]))
        assert admission is not None and admission.active_token is None and admission.attempt == 1 and admission.state == "drained"


def test_changed_order_conflicts_and_retained_source_withdrawal_redacts_every_copy(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    body = request(env)
    created = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    conflict = api_request(env, "POST", f"/memories/{mid}/context-compactions", json={**body, "source_ids": list(reversed(body["source_ids"]))})
    assert conflict.status_code == 409 and len(env["summary_calls"]) == 1
    source = created["artifact"]["sources"][1]
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/sources/{source['source_id']}/withdrawals",
            json={"operation_id": "withdraw-retained", "expected_source_revision": int(source["source_revision"]), "reason": "Remove retained evidence"},
        )
    )
    current = result(api_request(env, "GET", f"/memories/{mid}/context-compaction-operations/{body['operation_id']}"))
    assert current["operation"]["status"] == "stale" and current["artifact"]["package"] is None
    replay = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert replay["artifact"]["package"] is None and len(env["summary_calls"]) == 1
    with Session(env["engine"]) as reader:
        stored = reader.get(MemoryCompactionArtifact, created["artifact"]["artifact_id"])
        assert stored is not None and stored.redacted and stored.package is None


@pytest.mark.parametrize("response,error", [("", "empty_compaction_summary"), ("<think>only reasoning</think>", "empty_compaction_summary"), ("无进展。" * 2000, "compaction_no_effect")])
def test_invalid_or_nonreducing_output_never_becomes_generated(compaction_api: dict[str, Any], response: str, error: str) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    env["summary_response"] = response
    value = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env)))
    assert value["operation"]["status"] == "failed" and value["operation"]["error_code"] == error
    assert value["artifact"]["package"] is None and len(env["summary_calls"]) == 1
