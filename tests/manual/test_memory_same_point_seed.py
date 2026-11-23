"""Build and freeze two checkpoints through the real owned memory API."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from api.db import db_models
from api.db.db_models import Task
from common.config_utils import CONFIGS
from common.constants import SVR_CONSUMER_GROUP_NAME
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.identifiers import allocate_message_id
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.manual.memory_compaction_rollout import _owned_database
from tests.manual.memory_same_point_rollout import Capture, redis_scope
from tests.support.database import _alembic_config
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result


@pytest.fixture
def bootstrapped_engine(same_point_capture: Capture) -> Iterator[sa.Engine]:
    engine = sa.create_engine(same_point_capture.stores.postgres_url)
    try:
        assert engine.url == db_models.engine.url
        owned_config = same_point_capture.stores.overlay({})
        assert all(CONFIGS[store] == owned_config[store] for store in ("redis", "datav_redis", "milvus", "minio"))
        _owned_database(engine)
        with engine.begin() as conn:
            assert not sa.inspect(conn).has_schema("usr_ai")
            conn.execute(sa.text("CREATE SCHEMA usr_ai"))
            db_models.Base.metadata.create_all(conn)
            config = _alembic_config()
            config.attributes["connection"] = conn
            command.stamp(config, "head")
        yield engine
    finally:
        engine.dispose()


def _append_raw(env: dict[str, Any], operation: str, *, bank: str = "personal", actor: int = 1) -> int:
    payload = {"operation_id": operation, "user_input": "Synthetic recovery checkpoint input", "agent_response": "Owned fixture only", "agent_id": "rehearsal", "session_id": operation}
    response = result(api_request(env, "POST", f"/memories/{env['memories'][bank]}/messages", actor=actor, json=payload))
    assert response["queue_state"] == "not_required" and response["extraction_task_id"] is None
    return int(response["raw_source_id"])


def _queue_fixture(env: dict[str, Any]) -> dict[str, Any]:
    """Persist terminal and uncertain legacy outcomes without running a model."""
    redis = REDIS_CONN.REDIS
    queue, counter = env["queue"], env["counter"]
    assert redis.xlen(queue) == 0
    group = SVR_CONSUMER_GROUP_NAME
    consumer = "recovery-lost-ack-" + uuid4().hex
    # The team fixture remains in legacy mode; the managed personal bank owns
    # the compaction artifacts and rightly forbids standalone projection writes.
    tenant, mid = env["tenants"]["team"], env["memories"]["team"]
    store = ManagedMessageStore(tenant, mid)
    task_ids = []
    for name, progress in (("terminal", 1), ("unknown", 0)):
        identity = uuid4().hex
        raw_id = _append_raw(env, "recovery-raw-" + name, bank="team", actor=0)
        raw = asyncio.run(store.read(raw_id))
        assert raw is not None and raw["message_type"] == "raw"
        assigned = asyncio.run(allocate_message_id(minimum_message_id=raw_id, counter_key=counter))
        derived = {**raw, "id": f"{mid}_{assigned}", "message_id": assigned, "message_type": "semantic", "source_id": raw_id, "content": "Synthetic projection committed before acknowledgement"}
        env["journal"].event("reserve-same-point-task-outcome", task_id=identity, raw=raw_id, projection=assigned, progress=progress)
        assert not MessageService.insert_message([derived], tenant, mid)
        with Session(env["engine"]) as writer:
            writer.add(Task(id=identity, doc_id=mid, digest=str(raw_id), task_type="memory", progress=progress))
            writer.commit()
        payload = {
            "id": identity,
            "task_id": identity,
            "task_type": "memory",
            "memory_id": mid,
            "tenant_id": tenant,
            "source_id": raw_id,
            "message_dict": {"user_input": "Synthetic recovery checkpoint input", "agent_response": "Owned fixture only", **{key: raw[key] for key in ("agent_id", "session_id", "user_id")}},
        }
        assert REDIS_CONN.queue_product(queue, payload)
        task_ids.append(identity)
    env["journal"].event("reserve-same-point-stream-group", queue=queue, group=group, consumer=consumer)
    assert redis.xgroup_create(queue, group, id="0")
    checked = redis.xreadgroup(group, consumer, {queue: ">"}, count=2)
    assert len(checked) == 1 and len(checked[0][1]) == 2
    ids = [identity.decode() if isinstance(identity, bytes) else identity for identity, _ in checked[0][1]]
    return {"queue": queue, "counter": counter, "group": group, "consumer": consumer, "terminalTaskId": task_ids[0], "unknownTaskId": task_ids[1], "pendingIds": ids}


def test_seed_recovery_points(compaction_api: dict[str, Any], same_point_capture: Capture) -> None:
    env, capture = compaction_api, same_point_capture
    mid = env["memory_id"]
    base = f"/memories/{mid}"
    active_op, retired_op = "same-point-readable-compaction", "same-point-retired-compaction"
    active = result(api_request(env, "POST", base + "/context-compactions", json=request(env, active_op)))["artifact"]
    retired = result(api_request(env, "POST", base + "/context-compactions", json=request(env, retired_op)))["artifact"]
    assert active["package"] and retired["package"]
    path = base + "/context-compactions/" + retired["artifact_id"]
    preview = result(api_request(env, "GET", path + "/retention-preview"))
    retired = result(api_request(env, "POST", path + "/retirements", json={"expected_preview_sha256": preview["preview_sha256"]}))["artifact"]
    assert retired["error_code"] == "compaction_retired" and retired["package"] is None

    raw = str(_append_raw(env, "recovery-withdrawn-raw"))
    source = base + "/sources/" + raw
    state = result(api_request(env, "GET", source + "/state"))
    if state["source_revision"] is None:
        result(api_request(env, "POST", source + "/adoptions", json={"operation_id": "recovery-adopt-withdrawn", "expected_content_sha256": state["content_sha256"]}))
        state = result(api_request(env, "GET", source + "/state"))
    result(api_request(env, "POST", source + "/withdrawals", json={"operation_id": "recovery-withdrawal", "expected_source_revision": int(state["source_revision"]), "reason": "Synthetic only"}))
    assert result(api_request(env, "GET", source + "/state"))["state"] == "forgotten"
    assert api_request(env, "GET", source).status_code == 404
    banks = {kind: (env["tenants"][kind], identity) for kind, identity in env["memories"].items()}
    capture.bindings.update(
        _queue_fixture(env),
        banks=banks,
        collections=sorted(index_name(tenant) + "_" + identity for tenant, identity in banks.values()),
        token=env["jwts"][1],
        memoryId=mid,
        artifactId=active["artifact_id"],
        operationId=active_op,
        sourceIds=env["compaction_source_ids"],
        retiredArtifactId=retired["artifact_id"],
        retiredOperationId=retired_op,
        retiredSourceIds=env["compaction_source_ids"],
        withdrawnSourceId=raw,
    )
    queue = redis_scope(capture.bindings)
    assert [row["id"] for row in queue["pending"]] == capture.bindings["pendingIds"]
    capture.bindings["maxMessageId"] = queue["counter"]
    assert capture.bindings["maxMessageId"] > 2**53 and len(env["summary_calls"]) == 2
    capture.take("point-a", env["engine"])

    # Independent, later, valid checkpoint with changed SQL, physical RAW and
    # Redis high-water mark, taken through the same backup mechanism.
    later = _append_raw(env, "recovery-after-point-a")
    assert later > capture.bindings["maxMessageId"]
    assert REDIS_CONN.REDIS.xack(capture.bindings["queue"], capture.bindings["group"], capture.bindings["pendingIds"][0]) == 1
    capture.take("point-b", env["engine"])
