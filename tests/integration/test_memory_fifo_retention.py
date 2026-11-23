"""Strict unmanaged retention keeps its own durable source/operation contract."""

from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemoryMutationOperation, MemoryPublicationOutbox, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionPersistenceUnknown, content_hash
from memory.writes.retention import FifoRetentionService
from tests.support.database import scratch_database


def raw(identity: int = 1) -> dict[str, Any]:
    return {
        "message_id": identity,
        "memory_id": "bank",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "subject",
        "agent_id": "agent",
        "session_id": "session",
        "content": "raw source " + str(identity),
        "content_embed": [1.0, 0.0],
        "status": True,
    }


@pytest.fixture
def retention_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    with scratch_database(tmp_path / "memory-fifo-retention") as engine:
        yield engine


@pytest.fixture
async def bank(retention_engine: sa.Engine) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(retention_engine.url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(Memory(id="bank", tenant_id="owner", name="retention bank", embd_id="embedding", llm_id="chat"))
        db.add(MemoryWriterFence(memory_id="bank", tenant_id="owner", state="open", generation=1, capacity_generation=1, reserved_bytes=50, receipt={}))
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="extract",
                tenant_id="owner",
                actor_id="owner",
                kind="extract",
                request_sha256="a" * 64,
                writer_generation=1,
                source_message_id=1,
                state="queued",
                publication_state="pending",
                message_ids=[4],
                request_metadata={},
                receipt={},
            )
        )
        # A parent's active attempt for a DIFFERENT source is expected while
        # that append awaits its FIFO callback. It must stay admitted/reserved.
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="parent",
                tenant_id="owner",
                actor_id="owner",
                kind="append",
                request_sha256="b" * 64,
                writer_generation=1,
                source_message_id=5,
                state="active",
                publication_state="pending",
                message_ids=[5],
                active_token="parent-token",
                request_metadata={},
                receipt={},
            )
        )
        for operation_id, reserved, active in [("extract", 40, []), ("parent", 10, ["parent-token"])]:
            db.add(
                MemoryMutationOperation(
                    memory_id="bank",
                    operation_id=operation_id,
                    tenant_id="owner",
                    actor_id="owner",
                    kind=operation_id,
                    request_sha256="c" * 64,
                    status="accepted",
                    index_sync_status="pending",
                    result={},
                )
            )
            db.add(
                MemoryPublicationOutbox(
                    id=operation_id,
                    memory_id="bank",
                    operation_id=operation_id,
                    tenant_id="owner",
                    bank_generation=1,
                    state="pending",
                    attempt=0,
                    payload={"action": operation_id, "reserved_bytes": reserved, "reservation_owner": "writer_fence", "approved": {"content": "private draft", "content_embed": [1.0, 0.0]}},
                    receipt={"active_attempts": active},
                )
            )
    try:
        yield {"factory": factory}
    finally:
        await engine.dispose()


class Physical:
    tenant_id = "owner"
    memory_id = "bank"

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.rows = {1: raw(), 2: {**raw(2), "message_type": "semantic", "source_id": 1, "content": "derived"}, 3: raw(3)}
        self.deleted: list[list[int]] = []
        self.lose_reply = False

    async def inventory(self) -> list[dict[str, Any]]:
        assert not self.db.in_transaction()
        return deepcopy(list(self.rows.values()))

    async def delete(self, identities: list[int]) -> None:
        assert not self.db.in_transaction()
        self.deleted.append(identities.copy())
        for identity in identities:
            self.rows.pop(identity, None)
        if self.lose_reply:
            self.lose_reply = False
            raise TimeoutError("delete committed but its reply was lost")
        assert not any(identity in self.rows for identity in identities)


def service(db: AsyncSession) -> FifoRetentionService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "owner")) == memory_id

    return FifoRetentionService(db, tenant_id="owner", actor_id="owner", authorize=authorize)


async def test_fifo_retires_full_source_without_activating_managed_mode(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        retention, physical = service(db), Physical(db)
        result = await retention.evict_source("bank", raw(), "fifo-child", physical)
        assert result["status"] == "ready" and result["tracked_cleanup_confirmed"] is True
        assert result["physical_cleanup_complete"] is False and physical.deleted == [[1, 2]]
        assert set(physical.rows) == {3}
        assert await retention.evict_source("bank", raw(), "fifo-child", physical) == result
        assert physical.deleted == [[1, 2]]
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(ManagedMemoryState.memory_id)) is None
        assert await reader.scalar(sa.select(MemorySourceState.state)) == "forgotten"
        assert await reader.scalar(sa.select(MemoryWriterFence.state)) == "open"
        assert await reader.scalar(sa.select(MemoryWriterFence.reserved_bytes)) == 10
        assert await reader.scalar(sa.select(MemoryWriterAdmission.publication_state).where(MemoryWriterAdmission.operation_id == "extract")) == "canceled"
        assert await reader.scalar(sa.select(MemoryWriterAdmission.active_token).where(MemoryWriterAdmission.operation_id == "parent")) == "parent-token"
        outbox = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == "extract"))
        assert "approved" not in outbox.payload and outbox.receipt["reservation_released"] is True


async def test_fifo_uses_canonical_active_token_and_rolls_back_stop(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as writer, writer.begin():
        # Deliberately omit the mirror: canonical admission must still block.
        await writer.execute(sa.update(MemoryWriterAdmission).where(MemoryWriterAdmission.operation_id == "extract").values(state="active", active_token="live-token"))
    async with bank["factory"]() as db:
        physical = Physical(db)
        with pytest.raises(RevisionBusy, match="active_writer"):
            await service(db).evict_source("bank", raw(), "fifo-child", physical)
        assert physical.deleted == [] and set(physical.rows) == {1, 2, 3}
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(MemorySourceState.source_message_id)) is None
        assert await reader.scalar(sa.select(MemoryMutationOperation.operation_id).where(MemoryMutationOperation.operation_id == "fifo-child")) is None
        assert await reader.scalar(sa.select(MemoryWriterFence.reserved_bytes)) == 50
        assert await reader.scalar(sa.select(MemoryWriterAdmission.publication_state).where(MemoryWriterAdmission.operation_id == "extract")) == "pending"


async def test_fifo_rejects_a_changed_fixed_source_plan(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        physical = Physical(db)
        physical.rows[1]["content"] = "changed since selection"
        with pytest.raises(RevisionConflict, match="fifo_source_changed"):
            await service(db).evict_source("bank", raw(), "fifo-child", physical)
        assert physical.deleted == []


async def test_fifo_unknown_delete_reconciles_absence_without_reissuing_delete(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        retention, physical = service(db), Physical(db)
        physical.lose_reply = True
        result = await retention.evict_source("bank", raw(), "fifo-child", physical)
        assert result["status"] == "persistence_unknown" and physical.deleted == [[1, 2]]
        async with bank["factory"]() as reader:
            assert await reader.scalar(sa.select(MemorySourceState.state)) == "forgotten"
            assert await reader.scalar(sa.select(MemoryWriterFence.reserved_bytes)) == 50
        original = raw()
        stub = {key: value for key, value in original.items() if key != "content"}
        stub["content_sha256"] = content_hash(original["content"])
        result = await retention.evict_source("bank", stub, "fifo-child", physical)
        assert result["status"] == "ready" and physical.deleted == [[1, 2]]
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryWriterFence.reserved_bytes)) == 10


async def test_fifo_stop_commit_unknown_keeps_same_child_operation(bank: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async with bank["factory"]() as db:
        retention, physical = service(db), Physical(db)
        real_commit = db.commit

        async def lost_commit() -> None:
            await real_commit()
            raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lost_commit)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await retention.evict_source("bank", raw(), "fifo-child", physical)
        assert error.value.operation_id == "fifo-child" and physical.deleted == []
        monkeypatch.setattr(db, "commit", real_commit)
        assert (await retention.get_operation("bank", "fifo-child"))["stop_supply"] == "withdrawn"
        assert (await retention.evict_source("bank", raw(), "fifo-child", physical))["status"] == "ready"
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMutationOperation).where(MemoryMutationOperation.kind == "fifo_evict")) == 1
