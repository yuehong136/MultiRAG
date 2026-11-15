"""Versioned memory HTTP contracts on scratch SQL and owned real message storage."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_revision_service
from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import RevisionStorageUnknown
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.integration.test_memory_authorization import insert_read_messages
from tests.integration.test_memory_authorization import memory_api as memory_api
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal

_TABLES = (MemoryPublicationOutbox, MemoryRevisionSource, MemoryEntryRevision, MemoryMutationOperation, MemoryEntry, MemorySourceState, ManagedMemoryState)


class RevisionEmbedding:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.extend(texts)
        return [[1.0, 0.0] for _ in texts], 0

    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0, 0.0], 0

    def close(self) -> None:
        pass


@pytest.fixture
def revision_api(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = memory_api
    calls: list[str] = []
    counter = env["queue"] + ":revision-ids"
    journal = ResourceJournal("memory-revision-http")
    journal.event("reserve-memory-revision-state", database=env["engine"].url.database, memory_ids=list(env["memories"].values()), counter=counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)
    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(lambda cls, *args, **kwargs: RevisionEmbedding(calls)))
    insert_read_messages(env, [{"message_id": 910, "content": "A project uses port 80. A separate fact remains."}])
    try:
        yield dict(env, embedding_calls=calls, counter=counter, memory_id=env["memories"]["personal"])
    finally:
        with Session(env["engine"]) as db:
            for model in _TABLES:
                db.execute(sa.delete(model).where(model.memory_id.in_(list(env["memories"].values()))))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id.in_(list(env["memories"].values())))) for model in _TABLES}
            assert not any(remaining.values()), remaining
        journal.event("memory-revision-sql-cleanup-readback", database=env["engine"].url.database, remaining=remaining)


def api_request(env: dict[str, Any], method: str, path: str, *, actor: int = 1, **kwargs: Any) -> Any:
    return env["client"].request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + env["jwts"][actor]}, timeout=60, **kwargs)


def result(response: Any, *, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["retcode"] == 0, body
    return body["data"]


def adopt(env: dict[str, Any]) -> tuple[str, str]:
    memory_id = env["memory_id"]
    data = result(api_request(env, "POST", f"/memories/{memory_id}/entries", json={"operation_id": "adopt-first", "legacy_message_id": "910"}))
    assert data["status"] == "ready"
    return memory_id, data["entry_id"]


def sql_snapshot(env: dict[str, Any]) -> dict[str, int]:
    with Session(env["engine"]) as db:
        return {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id == env["memory_id"])) for model in _TABLES}


def test_revision_adoption_preview_and_history_are_authorized_and_non_destructive(revision_api: dict[str, Any]) -> None:
    env, mid = revision_api, revision_api["memory_id"]
    denied = api_request(env, "POST", f"/memories/{mid}/entries", actor=2, json={"operation_id": "outsider", "legacy_message_id": 910})
    assert denied.status_code == 404 and not any(sql_snapshot(env).values())
    assert result(api_request(env, "GET", f"/memories/{mid}/messages/910/entry")) is None
    mid, entry = adopt(env)
    initial = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    assert initial["current_revision"] == 1 and initial["current_message_id"] == "910"
    before, counter = sql_snapshot(env), REDIS_CONN.REDIS.get(env["counter"])
    preview = result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{entry}/revisions",
            json={"operation_id": "preview", "expected_revision": 1, "patch": {"old_text": "port 80", "new_text": "port 8080"}, "reason": "explicit correction", "dry_run": True},
        )
    )
    assert preview["content"] == "A project uses port 8080. A separate fact remains."
    assert preview["dry_run"] is True and preview["source_checks"][0]["source_id"] == "900"
    assert sql_snapshot(env) == before and REDIS_CONN.REDIS.get(env["counter"]) == counter and env["embedding_calls"] == []
    assert result(api_request(env, "POST", f"/memories/{mid}/entries", json={"operation_id": "adopt-first", "legacy_message_id": 910}))["entry_id"] == entry
    assert result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}/revisions"))["revisions"][0]["revision"] == 1
    capabilities = result(api_request(env, "GET", f"/memories/{mid}/capabilities"))
    assert capabilities["revisions"]["managed"] and capabilities["recent_messages"]["pagination"] == "bounded_scan_cursor"
    with Session(env["engine"]) as db:
        db.add(
            MemoryMutationOperation(
                memory_id=mid,
                operation_id="old-source-state",
                tenant_id=env["tenants"]["personal"],
                actor_id=env["owners"][1],
                request_sha256="0" * 64,
                kind="change_state",
                status="stale",
                index_sync_status="unknown",
                result={"reconciliation_operation_id": "current-generation-state", "physical_cleanup_complete": False, "content": "not part of the operation contract"},
            )
        )
        db.commit()
    before = sql_snapshot(env)
    stale = result(api_request(env, "GET", f"/memories/{mid}/operations/old-source-state"))
    assert stale["reconciliation_operation_id"] == "current-generation-state" and stale["physical_cleanup_complete"] is False
    assert "content" not in stale and sql_snapshot(env) == before


def test_revision_publishes_one_current_projection_and_keeps_literal_history(revision_api: dict[str, Any]) -> None:
    env = revision_api
    mid, entry = adopt(env)
    command = {"operation_id": "correct-port", "expected_revision": 1, "patch": {"old_text": "port 80", "new_text": "port 8080"}, "reason": "explicit user correction"}
    published = result(api_request(env, "POST", f"/memories/{mid}/entries/{entry}/revisions", json=command))
    assert published["status"] == "ready" and published["new_revision"] == 2
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    assert current["current"]["content"] == "A project uses port 8080. A separate fact remains."
    assert current["current"]["assessment"]["kind"] == "user_asserted" and current["current"]["origin"] == "manual"
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"]) is None
    active = MessageService.get_by_message_id(mid, int(current["current_message_id"]), env["tenants"]["personal"])
    assert active and active["content"] == current["current"]["content"] and active["status"]
    history = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}/revisions", params={"limit": 1}))
    assert history["revisions"][0]["revision"] == 2 and history["next_cursor"]
    older = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}/revisions", params={"limit": 1, "cursor": history["next_cursor"]}))
    assert older["revisions"][0]["content"] == "A project uses port 80. A separate fact remains."
    assert result(api_request(env, "POST", f"/memories/{mid}/entries/{entry}/revisions", json=command))["new_revision"] == 2
    assert len(env["embedding_calls"]) == 1
    assert api_request(env, "POST", f"/memories/{mid}/entries/{entry}/revisions", json={**command, "content": "different", "patch": None}).status_code == 409
    assert api_request(env, "POST", f"/memories/{mid}/entries/{entry}/revisions", json={**command, "operation_id": "stale-correction"}).status_code == 409
    assert result(api_request(env, "GET", f"/memories/{mid}/messages/910/entry"))["current_revision"] == 2


def test_embedding_failure_keeps_old_supply_and_explicit_reconciliation_recovers(revision_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = revision_api
    mid, entry = adopt(env)
    real_embed = memory_revision_service._embed_revision

    async def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("controlled provider rejection")

    monkeypatch.setattr(memory_revision_service, "_embed_revision", fail)
    pending = result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{entry}/revisions",
            json={"operation_id": "recover-embedding", "expected_revision": 1, "content": "Corrected safely.", "reason": "explicit correction"},
        ),
        status=202,
    )
    assert pending["index_sync_status"] != "ready"
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    assert current["current_revision"] == 1 and MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])
    read = result(api_request(env, "GET", f"/memories/{mid}/operations/recover-embedding"))
    assert read["new_revision"] == pending["new_revision"] and read["outbox_id"] == pending["outbox_id"]
    monkeypatch.setattr(memory_revision_service, "_embed_revision", real_embed)
    ready = result(api_request(env, "POST", f"/memories/{mid}/operations/recover-embedding/reconciliations"))
    assert ready["status"] == "ready" and ready["new_revision"] == 2


def test_competing_expected_revisions_publish_at_most_one_current(revision_api: dict[str, Any]) -> None:
    env = revision_api
    mid, entry = adopt(env)

    def correct(index: int) -> Any:
        return api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{entry}/revisions",
            json={"operation_id": f"competing-{index}", "expected_revision": 1, "content": f"Correction {index}.", "reason": "concurrent correction"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(correct, range(2)))
    assert all(response.status_code in (200, 202, 409) for response in responses), [response.text for response in responses]
    for index, response in enumerate(responses):
        if response.status_code == 202:
            api_request(env, "POST", f"/memories/{mid}/operations/competing-{index}/reconciliations")
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    assert current["current_revision"] in (2, 3)
    with Session(env["engine"]) as db:
        ready = list(
            db.scalars(sa.select(MemoryMutationOperation).where(MemoryMutationOperation.memory_id == mid, MemoryMutationOperation.kind == "revise", MemoryMutationOperation.status == "ready"))
        )
        assert len(ready) == 1


def test_withdrawal_stops_supply_before_cleanup_and_blocks_late_legacy_rows(revision_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = revision_api
    mid, entry = adopt(env)
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{entry}/revisions",
            json={"operation_id": "correct-before-withdrawal", "expected_revision": 1, "content": "A corrected statement before withdrawal.", "reason": "explicit correction"},
        )
    )
    original_delete = ManagedMessageStore.delete

    async def unconfirmed_delete(*args: Any, **kwargs: Any) -> None:
        raise RevisionStorageUnknown("controlled delete response loss")

    monkeypatch.setattr(ManagedMessageStore, "delete", unconfirmed_delete)
    operation = result(
        api_request(env, "POST", f"/memories/{mid}/sources/900/withdrawals", json={"operation_id": "withdraw-raw", "expected_source_revision": 1, "reason": "original statement retracted"}), status=202
    )
    assert operation["index_sync_status"] != "ready"
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"]) is None
    assert api_request(env, "GET", f"/memories/{mid}/sources/900").status_code == 404
    redacted = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    assert redacted["current"]["content"] is None and redacted["current"]["redacted"]
    late = {
        "id": f"{mid}_999",
        "memory_id": mid,
        "message_id": 999,
        "message_type": "semantic",
        "source_id": 900,
        "user_id": "subject",
        "agent_id": "a",
        "session_id": "s",
        "valid_at": "2026-11-15 15:00:00",
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        "content": "Late legacy extraction.",
        "content_embed": [1.0, 0.0],
    }
    assert settings.msgStoreConn.insert([late], index_name(env["tenants"]["personal"]), mid) == []
    assert MessageService.get_by_message_id(mid, 999, env["tenants"]["personal"]) is None
    monkeypatch.setattr(ManagedMessageStore, "delete", original_delete)
    recovered = result(api_request(env, "POST", f"/memories/{mid}/operations/withdraw-raw/reconciliations"))
    assert recovered["status"] == "ready"
    recovered_read = result(api_request(env, "GET", f"/memories/{mid}/operations/withdraw-raw"))
    assert recovered_read["untraceable_copies"] is True and recovered_read["physical_cleanup_complete"] is False
    reenabling = api_request(env, "PUT", f"/messages/{mid}:900", json={"status": True}).json()
    assert reenabling["retcode"] != 0


def test_temporary_state_changes_keep_management_controls_and_bank_delete_retains_its_receipt(revision_api: dict[str, Any]) -> None:
    env = revision_api
    mid, entry = adopt(env)
    result(api_request(env, "PUT", f"/messages/{mid}:900", json={"status": False}))
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"]) is None
    assert result(api_request(env, "GET", f"/messages/{mid}:900/content"))["content"] == "personal"
    result(api_request(env, "PUT", f"/messages/{mid}:900", json={"status": True}))
    assert MessageService.get_by_message_id(mid, 910, env["tenants"]["personal"])
    result(
        api_request(
            env,
            "POST",
            f"/memories/{mid}/entries/{entry}/revisions",
            json={"operation_id": "after-source-reenabled", "expected_revision": 1, "content": "A current correction after reenable.", "reason": "explicit correction"},
        )
    )
    current = result(api_request(env, "GET", f"/memories/{mid}/entries/{entry}"))
    current_id = current["current_message_id"]
    result(api_request(env, "PUT", f"/messages/{mid}:{current_id}", json={"status": False}))
    assert MessageService.get_by_message_id(mid, int(current_id), env["tenants"]["personal"]) is None
    assert result(api_request(env, "GET", f"/messages/{mid}:{current_id}/content"))["content"] == current["current"]["content"]
    administrative = result(api_request(env, "GET", f"/memories/{mid}"))["messages"]["message_list"]
    visible = [item for raw in administrative for item in raw.get("extract", []) if str(item["message_id"]) == current_id]
    assert len(visible) == 1 and visible[0]["status"] is False and visible[0]["content"] == current["current"]["content"]
    management = result(api_request(env, "GET", f"/memories/{mid}/messages", params={"limit": 20}))
    assert all(str(message["message_id"]) != current_id for message in management["messages"])
    result(api_request(env, "PUT", f"/messages/{mid}:{current_id}", json={"status": True}))
    assert MessageService.get_by_message_id(mid, int(current_id), env["tenants"]["personal"])
    deleted = api_request(env, "DELETE", f"/memories/{mid}")
    assert deleted.status_code == 200 and deleted.json()["retcode"] == 0
    with Session(env["engine"]) as db:
        assert db.get(Memory, mid) is None
        operation_id = db.scalar(sa.select(MemoryMutationOperation.operation_id).where(MemoryMutationOperation.memory_id == mid, MemoryMutationOperation.kind == "delete_memory"))
        assert operation_id is not None
    receipt = result(api_request(env, "GET", f"/memories/{mid}/operations/{operation_id}"))
    assert receipt["lifecycle"] == "deleted" and receipt["tracked_cleanup_confirmed"] is True
    assert receipt["physical_cleanup_complete"] is False
    assert api_request(env, "GET", f"/memories/{mid}/entries/{entry}").status_code == 404


def test_unregistered_source_registration_is_explicit_and_hash_checked(revision_api: dict[str, Any]) -> None:
    env, mid = revision_api, revision_api["memory_id"]
    before = sql_snapshot(env)
    source = result(api_request(env, "GET", f"/memories/{mid}/sources/900/state"))
    assert source["managed"] is False and source["source_revision"] is None and sql_snapshot(env) == before
    wrong = api_request(env, "POST", f"/memories/{mid}/sources/900/adoptions", json={"operation_id": "register-wrong", "expected_content_sha256": "0" * 64})
    assert wrong.status_code == 409 and sql_snapshot(env) == before
    registered = result(api_request(env, "POST", f"/memories/{mid}/sources/900/adoptions", json={"operation_id": "register-source", "expected_content_sha256": source["content_sha256"]}))
    assert registered["source"]["source_revision"] == 1
