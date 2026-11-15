"""External projection failures never bypass the SQL source/current publication fence."""

import asyncio
import copy
from typing import Any

import pytest

from memory.revisions.domain import ModelBinding, RevisionConflict, validate_projection
from memory.revisions.projection import reconcile_operation


class Decisions:
    def __init__(self) -> None:
        self.state, self.status, self.current = "pending", "accepted", 1
        self.binding = ModelBinding(None, "embedding", 2, "config")
        self.message = {
            "message_id": 2,
            "memory_id": "bank",
            "message_type": "semantic",
            "source_id": 10,
            "user_id": "subject",
            "agent_id": "agent",
            "session_id": "session",
            "content": "corrected",
            "status": False,
            "forget_at": None,
        }
        self.withdrawn = False
        self.published = False
        self.finished: list[str] = []
        self.stages = self.publishes = 0

    async def get_operation(self, *args: Any) -> dict[str, Any]:
        return {"status": self.status, "outbox_id": "outbox"}

    async def claim_publication(self, *args: Any) -> dict[str, Any]:
        return await self.publication_snapshot("outbox")

    async def publication_snapshot(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.withdrawn:
            raise RevisionConflict("source_withdrawn")
        return {
            "outbox_id": "outbox",
            "lease_token": "token",
            "message_id": 2,
            "state": self.state,
            "payload": {"message": copy.deepcopy(self.message), "model_binding": self.binding.to_dict(), "old_message_id": 1, "published": self.published},
        }

    async def record_stage(self, outbox_id: str, message: dict[str, Any], **kwargs: Any) -> None:
        await self.publication_snapshot(outbox_id)
        validate_projection(message, self.message, self.binding, active=False)
        self.stages += 1
        self.state = "staged"

    async def publish_revision(self, *args: Any, **kwargs: Any) -> None:
        assert self.state == "staged"
        await self.publication_snapshot("outbox")
        self.current, self.state, self.status, self.published = 2, "sync_pending", "published", True
        self.publishes += 1

    async def mark_ready(self, outbox_id: str, message: dict[str, Any], *, old_message_inactive: bool, **kwargs: Any) -> dict[str, Any]:
        await self.publication_snapshot(outbox_id)
        validate_projection(message, self.message, self.binding, active=True)
        assert old_message_inactive is True
        self.state = self.status = "ready"
        return await self.get_operation()

    async def mark_unknown(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.state = "sync_pending" if self.published else "unknown"
        self.status = "persistence_unknown"
        return await self.get_operation()

    async def fail_operation(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.state = self.status = "stale"
        return await self.get_operation()

    async def finish_publication_attempt(self, outbox_id: str, *, lease_token: str) -> None:
        self.finished.append(lease_token)


class Physical:
    def __init__(self, decision: Decisions) -> None:
        self.decision = decision
        self.rows = {1: {**decision.message, "message_id": 1, "content": "old", "status": True, "content_embed": [1.0, 0.0]}}
        self.writes = self.embeddings = 0
        self.lose_reply = False
        self.withdraw_after_write = False

    async def read(self, identity: int) -> dict[str, Any] | None:
        return copy.deepcopy(self.rows.get(identity))

    async def embed(self, content: str, binding: dict[str, Any]) -> dict[str, Any]:
        self.embeddings += 1
        return {"vector": [0.0, 1.0], "model_binding": binding}

    async def write(self, value: dict[str, Any]) -> None:
        self.writes += 1
        self.rows[value["message_id"]] = copy.deepcopy(value)
        self.decision.withdrawn = self.withdraw_after_write
        if self.lose_reply:
            raise TimeoutError("unknown commit")

    async def status(self, identity: int, value: bool) -> None:
        self.rows[identity]["status"] = value

    async def reconcile(self) -> dict[str, Any]:
        return await reconcile_operation(self.decision, "bank", "op", embedder=self.embed, physical_reader=self.read, physical_writer=self.write, status_writer=self.status)  # type: ignore[arg-type]


async def test_projection_readback_then_sql_current_then_status_sync() -> None:
    decisions = Decisions()
    store = Physical(decisions)
    assert (await store.reconcile())["status"] == "ready"
    assert decisions.current == 2 and decisions.publishes == 1 and decisions.finished == ["token"]
    assert store.rows[1]["status"] is False and store.rows[2]["status"] is True


async def test_unknown_write_is_reconciled_by_stable_id_without_reembedding_or_rewrite() -> None:
    decisions = Decisions()
    store = Physical(decisions)
    store.lose_reply = True
    assert (await store.reconcile())["status"] == "persistence_unknown"
    assert decisions.current == 1 and store.writes == store.embeddings == 1
    store.lose_reply = False
    assert (await store.reconcile())["status"] == "ready"
    assert store.writes == store.embeddings == decisions.publishes == 1
    assert decisions.finished == ["token", "token"]


async def test_source_withdrawal_during_projection_does_not_publish_or_activate() -> None:
    decisions = Decisions()
    store = Physical(decisions)
    store.withdraw_after_write = True
    assert (await store.reconcile())["status"] == "stale"
    assert decisions.current == 1 and decisions.publishes == 0 and decisions.finished == ["token"]
    assert store.rows[1]["status"] is True and store.rows[2]["status"] is False


async def test_cancel_drains_callback_before_finishing_attempt() -> None:
    decisions = Decisions()
    store = Physical(decisions)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending_write(message: dict[str, Any]) -> None:
        entered.set()
        await release.wait()
        await store.write(message)

    task = asyncio.create_task(reconcile_operation(decisions, "bank", "op", embedder=store.embed, physical_reader=store.read, physical_writer=pending_write, status_writer=store.status))  # type: ignore[arg-type]
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert decisions.finished == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.writes == 1 and decisions.finished == ["token"] and decisions.current == 1
