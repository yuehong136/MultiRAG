"""Capture a real withdrawal and retirement after a recoverable checkpoint."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from core.utils.redis_conn import REDIS_CONN
from memory.revisions.identifiers import allocate_message_id
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.manual.test_memory_same_point_seed import _append_raw, _queue_fixture
from tests.manual.test_memory_same_point_seed import bootstrapped_engine as bootstrapped_engine
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result

if TYPE_CHECKING:
    from tests.manual.memory_same_point_rollout import Capture


@pytest.fixture
def dependent_legacy_projection(writes_api: dict[str, Any]) -> tuple[str, int]:
    """Create an existing legacy projection before compaction makes the bank managed."""
    env, mid = writes_api, writes_api["memory_id"]
    tenant = env["tenants"]["personal"]
    raw = result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/messages",
            json={
                "operation_id": "tail-owned-source",
                "agent_id": "compaction-agent",
                "session_id": "tail-source",
                "user_input": "仅内部合成评审，先核对来源与条件，禁止生产用途。" * 120,
                "agent_response": "本条件仅适用于合成彩排。",
            },
        )
    )["raw_source_id"]
    row = asyncio.run(ManagedMessageStore(tenant, mid).read(int(raw)))
    assert row is not None and row["message_type"] == "raw"
    projection = asyncio.run(allocate_message_id(minimum_message_id=int(raw), counter_key=env["counter"]))
    env["journal"].event("reserve-post-checkpoint-legacy-projection", memory_id=mid, source_id=raw, projection=projection)
    derived = {**row, "message_id": projection, "source_id": int(raw), "message_type": "semantic", "content": "Synthetic source-dependent color rule."}
    assert not MessageService.insert_message([derived], tenant, mid)
    return str(raw), projection


def test_seed_post_checkpoint(dependent_legacy_projection: tuple[str, int], compaction_api: dict[str, Any], same_point_capture: Capture) -> None:
    env, capture = compaction_api, same_point_capture
    mid = env["memory_id"]
    base = f"/memories/{mid}"

    def post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return result(api_request(env, "POST", base + path, json=payload))

    def get(path: str) -> dict[str, Any]:
        return result(api_request(env, "GET", base + path))

    raw, projection = dependent_legacy_projection
    source = f"/sources/{raw}"
    post(source + "/adoptions", {"operation_id": "tail-adoption", "expected_content_sha256": get(source + "/state")["content_sha256"]})
    source_before = get(source + "/state")
    entry = post("/entries", {"operation_id": "tail-adopt-projection", "legacy_message_id": str(projection)})["entry_id"]
    assert get(f"/entries/{entry}")["current"]["content"] == "Synthetic source-dependent color rule."
    active_op, retired_op, dependent_op = "tail-active", "tail-retired", "tail-dependent"
    active = post("/context-compactions", request(env, active_op))["artifact"]
    retired = post("/context-compactions", request(env, retired_op))["artifact"]
    dependent = post(
        "/context-compactions",
        {**request(env, dependent_op), "source_ids": [raw, env["compaction_source_ids"][1]]},
    )["artifact"]
    assert all(artifact["package"] for artifact in (active, retired, dependent))
    retire = f"/context-compactions/{retired['artifact_id']}"
    preview = get(retire + "/retention-preview")
    banks = {name: (env["tenants"][name], identity) for name, identity in env["memories"].items()}
    capture.bindings.update(
        _queue_fixture(env),
        banks=banks,
        collections=sorted(index_name(tenant) + "_" + identity for tenant, identity in banks.values()),
        token=env["jwts"][1],
        memoryId=mid,
        actorId=env["owners"][1],
        artifactId=active["artifact_id"],
        operationId=active_op,
        sourceIds=env["compaction_source_ids"],
        retiredArtifactId=retired["artifact_id"],
        retiredOperationId=retired_op,
        dependentOperationId=dependent_op,
        dependentEntryId=entry,
        withdrawnSourceId=raw,
    )
    assert len(env["summary_calls"]) == 3
    capture.take("point-a", env["engine"])

    withdrawal = {"operation_id": "tail-source-withdrawal", "expected_source_revision": int(source_before["source_revision"]), "reason": "Synthetic recovery rehearsal"}
    retirement = post(retire + "/retirements", {"expected_preview_sha256": preview["preview_sha256"]})["artifact"]
    post(source + "/withdrawals", withdrawal)
    source_after = get(source + "/state")
    assert source_after["state"] == "forgotten" and api_request(env, "GET", base + source).status_code == 404
    assert retirement["error_code"] == "compaction_retired" and retirement["package"] is None
    assert get(f"/context-compaction-operations/{dependent_op}")["artifact"]["package"] is None
    assert get(f"/entries/{entry}")["current"]["redacted"] is True
    assert len(env["summary_calls"]) == 3

    # These unrelated later writes distinguish all three stores. They are not
    # included in the recovery tail or replayed at the earlier checkpoint.
    _append_raw(env, "tail-later-raw")
    assert REDIS_CONN.REDIS.xack(capture.bindings["queue"], capture.bindings["group"], capture.bindings["pendingIds"][0]) == 1
    capture.take("point-b", env["engine"])
    capture.bindings["tail"] = {
        "actor_id": env["owners"][1],
        "memory_id": mid,
        "state_a": capture.points["point-a"].state,
        "state_b": capture.points["point-b"].state,
        "high_water": 2,
        "operations": [
            {"sequence": 1, "artifact_id": retirement["artifact_id"], "operation_id": retired_op, "preview_sha256": preview["preview_sha256"]},
            {"sequence": 2, "source_id": raw, "source_before": source_before, "source_after": source_after, "request": withdrawal},
        ],
    }
