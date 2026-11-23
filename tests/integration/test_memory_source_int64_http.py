"""Actual large-ID append outboxes can be withdrawn without touching a sibling."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.memory_revision_models import MemorySourceState
from memory.services.messages import MessageService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import api_request, manage_bank, result
from tests.support.memory_writes_http import writes_api as writes_api


def test_large_raw_source_withdrawal_handles_existing_append_outbox(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    manage_bank(env)
    source_ids = []
    for label in ("withdraw-large", "keep-large"):
        response = api_request(env, "POST", f"/memories/{mid}/messages", json={"operation_id": label, "user_input": label, "agent_response": "", "agent_id": "int64-source", "session_id": label})
        appended = result(response, status=response.status_code)
        assert appended["status"] == appended["index_sync_status"] == "ready"
        identity = appended["raw_source_id"]
        assert int(identity) > 2**53
        source_ids.append(identity)
    before = MessageService.get_by_message_id(mid, int(source_ids[1]), env["tenants"]["personal"])
    assert before is not None
    response = api_request(
        env, "POST", f"/memories/{mid}/sources/{source_ids[0]}/withdrawals", json={"operation_id": "withdraw-int64", "expected_source_revision": 1, "reason": "Owned int64 cleanup regression."}
    )
    operation = result(response, status=response.status_code)
    for _ in range(5):
        if operation["status"] == operation["index_sync_status"] == "ready":
            break
        response = api_request(env, "POST", f"/memories/{mid}/operations/withdraw-int64/reconciliations", json={})
        operation = result(response, status=response.status_code)
    assert operation["status"] == operation["index_sync_status"] == "ready", operation
    assert api_request(env, "GET", f"/memories/{mid}/sources/{source_ids[0]}").status_code == 404
    assert MessageService.get_by_message_id(mid, int(source_ids[0]), env["tenants"]["personal"]) is None
    after = MessageService.get_by_message_id(mid, int(source_ids[1]), env["tenants"]["personal"])
    assert after == before
    with Session(env["engine"]) as db:
        withdrawn = db.scalar(sa.select(MemorySourceState).where(MemorySourceState.memory_id == mid, MemorySourceState.source_message_id == int(source_ids[0])))
        retained = db.scalar(sa.select(MemorySourceState).where(MemorySourceState.memory_id == mid, MemorySourceState.source_message_id == int(source_ids[1])))
        assert withdrawn is not None and withdrawn.state == "forgotten"
        assert retained is not None and retained.state == "active"
    assert not env["chat_calls"]
