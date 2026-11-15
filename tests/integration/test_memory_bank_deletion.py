"""Real SQL deletion fences and recovery, with a controlled strict storage boundary."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory, Task, UserTenant
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from memory.revisions.deletion import MemoryDeletionService
from memory.revisions.domain import RevisionBusy, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, content_hash
from memory.revisions.sources import SourceService
from memory.writes.admission import WriterCoordinator, acknowledge_legacy_quiescence, writer_drain_snapshot
from tests.support.database import scratch_database


@pytest.fixture
def deletion_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    with scratch_database(tmp_path / "memory-bank-deletion") as engine:
        yield engine


@pytest.fixture
async def bank(deletion_engine: sa.Engine) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(deletion_engine.url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(
            Memory(
                id="bank",
                tenant_id="owner",
                name="private memory name",
                description="private description",
                system_prompt="private system prompt",
                user_prompt="private user prompt",
                permissions="team",
                embd_id="embedding",
                llm_id="chat",
            )
        )
        db.add(ManagedMemoryState(memory_id="bank", tenant_id="owner", state="active", generation=1, capacity_generation=1, reserved_bytes=100))
        db.add(
            MemorySourceState(
                memory_id="bank",
                tenant_id="owner",
                source_message_id=1,
                source_revision=1,
                generation=1,
                state="active",
                content_sha256=content_hash("private raw"),
                scope={"user_id": "subject", "agent_id": "agent", "session_id": "session"},
            )
        )
        db.add(UserTenant(id="member", tenant_id="owner", user_id="member", role="normal", invited_by="owner", status="1"))
        db.add(Task(id="old-completed-task", doc_id="bank", task_type="memory", progress=1.0, progress_msg="private previous output"))
        # A finished protocol admission is the evidence of quiescence. Task
        # progress alone deliberately cannot prove that an old IO has drained.
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="completed-extract",
                tenant_id="owner",
                actor_id="owner",
                kind="extract",
                request_sha256="b" * 64,
                writer_generation=1,
                task_id="old-completed-task",
                source_message_id=1,
                state="finished",
                publication_state="ready",
                message_ids=[2],
                request_metadata={},
                receipt={"finished_tokens": ["previous-token"]},
            )
        )
        db.add(MemoryEntry(entry_id="entry", memory_id="bank", tenant_id="owner", legacy_message_id=2, current_message_id=2, current_revision=1, next_revision=2, generation=1, state="active"))
        await db.flush()
        db.add(
            MemoryEntryRevision(
                entry_id="entry",
                revision=1,
                memory_id="bank",
                tenant_id="owner",
                message_id=2,
                content="private revision",
                content_sha256=content_hash("private revision"),
                origin="manual",
                assessment={"kind": "user_asserted", "reason": "private reason"},
                message_snapshot={"content": "private stale snapshot", "content_embed": [1.0, 0.0]},
            )
        )
        await db.flush()
        db.add(
            MemoryRevisionSource(
                entry_id="entry",
                revision=1,
                memory_id="bank",
                source_message_id=1,
                source_revision=1,
                source_generation=1,
                content_sha256=content_hash("private raw"),
                relation="context",
                locator={"excerpt": "private excerpt"},
            )
        )
        db.add(
            MemoryMutationOperation(
                memory_id="bank",
                operation_id="old",
                tenant_id="owner",
                actor_id="owner",
                request_sha256="a" * 64,
                kind="revise",
                status="accepted",
                index_sync_status="pending",
                result={"content": "private result"},
            )
        )
        db.add(
            MemoryPublicationOutbox(
                id="old",
                memory_id="bank",
                operation_id="old",
                tenant_id="owner",
                entry_id="entry",
                revision=1,
                message_id=2,
                expected_revision=1,
                entry_generation=1,
                bank_generation=1,
                state="pending",
                attempt=0,
                payload={"action": "replace", "message": {"content": "private pending", "content_embed": [1.0, 0.0]}, "reserved_bytes": 100},
                receipt={},
            )
        )
    try:
        yield {"factory": factory}
    finally:
        await engine.dispose()


class StrictStorage:
    tenant_id = "owner"
    memory_id = "bank"

    def __init__(self, factory: async_sessionmaker[AsyncSession], service_db: AsyncSession, *, lose_reply: bool = False) -> None:
        self.factory, self.service_db = factory, service_db
        self.present = True
        self.lose_reply = lose_reply
        self.drop_rpcs = 0

    async def drop(self) -> None:
        assert not self.service_db.in_transaction(), "Storage IO cannot hold the SQL transaction"
        async with self.factory() as reader:
            assert await reader.scalar(sa.select(Memory.id)) == "bank"
            assert await reader.scalar(sa.select(ManagedMemoryState.state)) == "deleting"
            assert await reader.scalar(sa.select(MemoryWriterFence.state)) == "closing"
        if self.present:
            self.drop_rpcs += 1
            self.present = False
            if self.lose_reply:
                self.lose_reply = False
                raise TimeoutError("drop committed but its reply was lost")
        assert self.present is False


async def test_delete_stops_before_drop_and_removes_live_resource_after_confirmation(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        pending = await service.request_delete("bank", operation_id="delete")
        assert pending["lifecycle"] == "deleting" and pending["tracked_cleanup_confirmed"] is False
        async with bank["factory"]() as reader:
            assert await reader.scalar(sa.select(Memory.id)) == "bank"
            assert await reader.scalar(sa.select(MemorySourceState.state)) == "deleted"
        storage = StrictStorage(bank["factory"], db)
        result = await service.reconcile_operation("bank", "delete", storage=storage)
        assert result["status"] == "ready" and result["tracked_cleanup_confirmed"] is True
        assert result["physical_cleanup_complete"] is False and result["untraceable_copies"] is True
        assert (await service.get_operation("bank", "delete"))["lifecycle"] == "deleted"
        assert storage.drop_rpcs == 1
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(Memory.id)) is None
        assert await reader.scalar(sa.select(Task.id)) is None
        assert await reader.scalar(sa.select(ManagedMemoryState.state)) == "deleted"
        assert await reader.scalar(sa.select(ManagedMemoryState.delete_permissions)) == "team"
        assert await reader.scalar(sa.select(MemoryWriterFence.state)) == "deleted"
        version = await reader.scalar(sa.select(MemoryEntryRevision))
        assert version.content is None and version.message_snapshot == {} and "reason" not in version.assessment
        assert await reader.scalar(sa.select(MemorySourceState.scope)) == {}
        assert await reader.scalar(sa.select(MemoryRevisionSource.locator)) == {}
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        assert await reader.scalar(sa.select(MemoryMutationOperation.result).where(MemoryMutationOperation.operation_id == "old")) == {}


async def test_drop_unknown_preserves_sql_and_reconciles_the_same_operation(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        await service.request_delete("bank", operation_id="delete")
        storage = StrictStorage(bank["factory"], db, lose_reply=True)
        unknown = await service.reconcile_operation("bank", "delete", storage=storage)
        assert unknown["status"] == "persistence_unknown" and unknown["operation_id"] == "delete"
        async with bank["factory"]() as reader:
            assert await reader.scalar(sa.select(Memory.id)) == "bank"
        ready = await service.reconcile_operation("bank", "delete", storage=storage)
        assert ready["status"] == "ready" and storage.drop_rpcs == 1
        assert await service.reconcile_operation("bank", "delete", storage=storage) == await service.get_operation("bank", "delete")


async def test_unfinished_source_attempt_blocks_until_it_really_finishes(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as writer, writer.begin():
        await writer.execute(sa.update(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == "old").values(state="ready", receipt={"active_attempts": ["old-token"]}))
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        await service.request_delete("bank", operation_id="delete")
        storage = StrictStorage(bank["factory"], db)
        waiting = await service.reconcile_operation("bank", "delete", storage=storage)
        assert waiting["blocked_by"] == ["active_attempts"] and storage.drop_rpcs == 0
        async with bank["factory"]() as writer, writer.begin():
            await writer.execute(sa.update(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == "old").values(receipt={"active_attempts": [], "finished_attempts": ["old-token"]}))
        assert (await service.reconcile_operation("bank", "delete", storage=storage))["status"] == "ready"


async def test_deleted_operation_uses_tombstone_and_current_team_membership(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db:
        member = MemoryDeletionService(db, actor_id="member")
        await member.request_delete("bank", operation_id="delete")
        await member.reconcile_operation("bank", "delete", storage=StrictStorage(bank["factory"], db))
        assert (await member.get_operation("bank", "delete"))["status"] == "ready"
        async with bank["factory"]() as writer, writer.begin():
            await writer.execute(sa.update(UserTenant).where(UserTenant.id == "member").values(status="0"))
        with pytest.raises(RevisionNotFound):
            await member.get_operation("bank", "delete")
        assert (await MemoryDeletionService(db, actor_id="owner").get_operation("bank", "delete"))["status"] == "ready"
        with pytest.raises(RevisionNotFound):
            await MemoryDeletionService(db, actor_id="outsider").get_operation("bank", "delete")


async def test_stop_commit_lost_reply_keeps_the_public_operation(bank: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        real_commit = db.commit

        async def lost_commit() -> None:
            await real_commit()
            raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lost_commit)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await service.request_delete("bank", operation_id="public-delete")
        assert error.value.operation_id == "public-delete"
        monkeypatch.setattr(db, "commit", real_commit)
        operation = await service.get_operation("bank", "public-delete")
        assert operation["lifecycle"] == "deleting"
        assert await service.request_delete("bank", operation_id="public-delete") == operation
        storage = StrictStorage(bank["factory"], db)
        assert (await service.reconcile_operation("bank", "public-delete", storage=storage))["status"] == "ready"
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMutationOperation).where(MemoryMutationOperation.kind == "delete_memory")) == 1


async def test_untracked_task_requires_explicit_operator_attestation(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as writer, writer.begin():
        writer.add(Task(id="untracked-old-task", doc_id="bank", task_type="memory", digest="9", progress=1.0))
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        pending = await service.request_delete("bank", operation_id="delete")
        assert pending["blocked_by"] == ["untracked_tasks"] and pending["needs_operator_reconciliation"] is True
        storage = StrictStorage(bank["factory"], db)
        assert (await service.reconcile_operation("bank", "delete", storage=storage))["lifecycle"] == "deleting"
        assert storage.drop_rpcs == 0
        snapshot = await writer_drain_snapshot(db, memory_id="bank", tenant_id="owner")
        await db.rollback()
        await acknowledge_legacy_quiescence(
            db,
            memory_id="bank",
            tenant_id="owner",
            expected_generation=snapshot["generation"],
            expected_untracked_task_ids_sha256=snapshot["untracked_task_ids_sha256"],
            evidence_reference="controlled-operator-stopped-owned-worker",
        )
        result = await service.reconcile_operation("bank", "delete", storage=storage)
        assert result["lifecycle"] == "deleted" and result["legacy_quiescence_attested"] is True
        assert result["drain_verification"] == "operator_attestation_and_tracked_attempts"
        assert result["legacy_quiescence_attestation"]["verification"] == "operator_attestation"


async def test_final_commit_unknown_preserves_authorizable_tombstone_operation(bank: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="owner")
        await service.request_delete("bank", operation_id="delete")
        real_commit, commits = db.commit, 0

        async def lost_final_commit() -> None:
            nonlocal commits
            commits += 1
            await real_commit()
            if commits == 2:
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost final acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lost_final_commit)
        storage = StrictStorage(bank["factory"], db)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await service.reconcile_operation("bank", "delete", storage=storage)
        assert error.value.operation_id == "delete"
        monkeypatch.setattr(db, "commit", real_commit)
        assert (await service.get_operation("bank", "delete"))["status"] == "ready"
        assert (await service.reconcile_operation("bank", "delete", storage=storage))["status"] == "ready"
        assert storage.drop_rpcs == 1
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(Memory.id)) is None
        receipt = await reader.scalar(sa.select(MemoryPublicationOutbox.receipt).where(MemoryPublicationOutbox.operation_id == "delete"))
        assert receipt["active_attempts"] == [] and len(receipt["finished_attempts"]) == 1


class SourcePhysical:
    tenant_id = "owner"
    memory_id = "bank"

    def __init__(self) -> None:
        scope = {"memory_id": "bank", "user_id": "subject", "agent_id": "agent", "session_id": "session", "status": True}
        self.rows = {
            1: {**scope, "message_id": 1, "message_type": "raw", "source_id": 0, "content": "private raw"},
            2: {**scope, "message_id": 2, "message_type": "semantic", "source_id": 1, "content": "private revision"},
            4: {**scope, "message_id": 4, "message_type": "semantic", "source_id": 1, "content": "private pending", "status": False},
        }
        self.deleted: list[int] = []

    async def read(self, memory_id: str, identity: int) -> dict[str, Any] | None:
        assert memory_id == self.memory_id
        return dict(self.rows[identity]) if identity in self.rows else None

    async def set_status(self, identity: int, status: bool) -> None:
        self.rows[identity]["status"] = status

    async def delete(self, identities: list[int]) -> None:
        self.deleted.extend(identities)
        for identity in identities:
            self.rows.pop(identity, None)

    async def source_dependents(self, source_id: int) -> list[int]:
        return [identity for identity, message in self.rows.items() if identity == source_id or message.get("source_id") == source_id]


async def _active_source_writer(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as db, db.begin():
        db.add(MemoryWriterFence(memory_id="bank", tenant_id="owner", state="open", generation=1, capacity_generation=1, reserved_bytes=0, receipt={}))
        await db.execute(sa.update(ManagedMemoryState).values(reserved_bytes=40))
        await db.execute(sa.update(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == "old").values(state="ready", receipt={"reservation_released": True}))
        db.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="new-extract",
                tenant_id="owner",
                actor_id="owner",
                kind="extract",
                request_sha256="d" * 64,
                writer_generation=1,
                source_message_id=1,
                state="active",
                publication_state="pending",
                active_token="active-token",
                message_ids=[4],
                request_metadata={},
                receipt={},
            )
        )
        db.add(
            MemoryMutationOperation(
                memory_id="bank", operation_id="new-extract", tenant_id="owner", actor_id="owner", request_sha256="e" * 64, kind="extract", status="accepted", index_sync_status="pending", result={}
            )
        )
        db.add(
            MemoryPublicationOutbox(
                id="new-extract",
                memory_id="bank",
                tenant_id="owner",
                operation_id="new-extract",
                bank_generation=1,
                state="pending",
                attempt=1,
                payload={"action": "extract", "reserved_bytes": 40, "reservation_owner": "managed_bank", "approved": {"content": "private pending"}},
                receipt={},
            )
        )


async def test_source_disable_cancels_pending_writer_and_preserves_published_reenable(bank: dict[str, Any]) -> None:
    await _active_source_writer(bank)
    physical = SourcePhysical()
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        await source.change_physical_state("bank", 1, operation_id="disable", status=False)
        waiting = await source.reconcile_operation("bank", "disable", storage=physical)  # type: ignore[arg-type]
        assert waiting["index_sync_status"] == "unknown" and physical.deleted == []
        await WriterCoordinator(db, tenant_id="owner", actor_id="owner", authorize=authorize).finish("bank", "new-extract", token="active-token")
        assert (await source.reconcile_operation("bank", "disable", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.deleted == [4] and physical.rows[1]["status"] is False and 2 in physical.rows
        await source.change_physical_state("bank", 1, operation_id="enable", status=True)
        assert (await source.reconcile_operation("bank", "enable", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.rows[1]["status"] is True and 2 in physical.rows and 4 not in physical.rows
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        assert await reader.scalar(sa.select(MemoryWriterAdmission.publication_state).where(MemoryWriterAdmission.operation_id == "new-extract")) == "canceled"


async def test_managed_fifo_refuses_canonical_active_writer_before_source_stop(bank: dict[str, Any]) -> None:
    await _active_source_writer(bank)
    physical = SourcePhysical()
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        with pytest.raises(RevisionBusy, match="active_writer"):
            await source.evict_source("bank", physical.rows[1], "fifo-child", storage=physical)  # type: ignore[arg-type]
        assert physical.deleted == []
    async with bank["factory"]() as reader:
        state = await reader.scalar(sa.select(MemorySourceState))
        assert state.state == "active" and state.generation == 1
        assert await reader.scalar(sa.select(MemoryMutationOperation.operation_id).where(MemoryMutationOperation.operation_id == "fifo-child")) is None


async def test_managed_fifo_replays_fixed_metadata_after_ready_commit_ack_loss(bank: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    physical = SourcePhysical()
    raw = dict(physical.rows[1])
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        real_commit, commits = db.commit, 0

        async def lose_ready_commit() -> None:
            nonlocal commits
            commits += 1
            await real_commit()
            if commits == 3:
                raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost child ready acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lose_ready_commit)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await source.evict_source("bank", raw, "fifo-child", storage=physical)  # type: ignore[arg-type]
        assert error.value.operation_id == "fifo-child" and physical.rows == {}
        deleted = physical.deleted.copy()
        monkeypatch.setattr(db, "commit", real_commit)
        stub = {key: value for key, value in raw.items() if key != "content"}
        stub["content_sha256"] = content_hash(raw["content"])
        assert (await source.evict_source("bank", stub, "fifo-child", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.deleted == deleted
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await source.evict_source("bank", {**stub, "content_sha256": "0" * 64}, "fifo-child", storage=physical)  # type: ignore[arg-type]


async def test_disabling_pending_raw_confirms_absence_without_recreating_it(bank: dict[str, Any]) -> None:
    await _active_source_writer(bank)
    async with bank["factory"]() as db, db.begin():
        await db.execute(sa.delete(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == "old"))
        await db.execute(sa.delete(MemoryRevisionSource))
        await db.execute(sa.delete(MemoryEntryRevision))
        await db.execute(sa.delete(MemoryEntry))
        await db.execute(sa.update(MemorySourceState).values(state="pending"))
        await db.execute(sa.update(MemoryWriterAdmission).where(MemoryWriterAdmission.operation_id == "new-extract").values(kind="append", state="drained", active_token=None, message_ids=[1]))
    physical = SourcePhysical()
    physical.rows = {1: {**physical.rows[1], "status": False}}
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        await source.change_physical_state("bank", 1, operation_id="disable-pending", status=False)
        result = await source.reconcile_operation("bank", "disable-pending", storage=physical)  # type: ignore[arg-type]
        assert result["status"] == "ready" and physical.rows == {} and physical.deleted == [1]
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(MemorySourceState.state)) == "inactive"
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0


async def _ready_generated_batch(bank: dict[str, Any]) -> SourcePhysical:
    physical = SourcePhysical()
    physical.rows.pop(4)
    physical.rows[2].update(content_embed=[1.0, 0.0], valid_at=None, invalid_at=None, forget_at=None)
    physical.rows[3] = {**physical.rows[2], "message_id": 3, "content": "independent sibling fact"}
    async with bank["factory"]() as db, db.begin():
        await db.execute(sa.delete(MemoryPublicationOutbox))
        await db.execute(sa.delete(MemoryRevisionSource))
        await db.execute(sa.delete(MemoryEntryRevision))
        await db.execute(sa.delete(MemoryEntry))
        await db.execute(sa.update(ManagedMemoryState).values(reserved_bytes=0))
        await db.execute(sa.update(MemoryWriterAdmission).values(message_ids=[2, 3]))
        db.add(
            MemoryPublicationOutbox(
                id="generated-batch",
                memory_id="bank",
                tenant_id="owner",
                operation_id="completed-extract",
                bank_generation=1,
                state="ready",
                attempt=1,
                payload={
                    "action": "extract",
                    "approved": True,
                    "messages": [{**physical.rows[identity], "status": False} for identity in (2, 3)],
                    "model_binding": {"tenant_model_id": None, "model_reference": "approved-embedding", "dimension": 2, "config_reference": "approved-config"},
                    "source_binding": {
                        "source_id": 1,
                        "revision": 1,
                        "generation": 1,
                        "content_sha256": content_hash("private raw"),
                        "scope": {"user_id": "subject", "agent_id": "agent", "session_id": "session"},
                        "managed": True,
                    },
                },
                receipt={"reservation_released": True, "active_attempts": []},
            )
        )
    return physical


async def test_ready_generated_fact_state_control_is_atomic_and_keeps_its_sibling(bank: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    physical = await _ready_generated_batch(bank)
    # A published fact remains valid after temporary source ABA. Its immutable
    # producer plan keeps generation 1; the new baseline captures current 3.
    async with bank["factory"]() as writer, writer.begin():
        await writer.execute(sa.update(MemorySourceState).values(generation=3))
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        real_commit = db.commit

        async def lost_commit() -> None:
            await real_commit()
            raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lost_commit)
        with pytest.raises(RevisionPersistenceUnknown) as error:
            await source.change_physical_state("bank", 2, operation_id="fact-disable", status=False)
        assert error.value.operation_id == "fact-disable"
        monkeypatch.setattr(db, "commit", real_commit)
        await source.change_physical_state("bank", 2, operation_id="fact-disable", status=False)
        assert (await source.reconcile_operation("bank", "fact-disable", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.rows[2]["status"] is False and physical.rows[3]["status"] is True
        await source.change_physical_state("bank", 2, operation_id="fact-enable", status=True)
        assert (await source.reconcile_operation("bank", "fact-enable", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.rows[2]["status"] is True
        await source.change_physical_state("bank", 2, operation_id="fact-forget", forget=True)
        assert (await source.reconcile_operation("bank", "fact-forget", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert 2 not in physical.rows and physical.rows[3]["status"] is True
    async with bank["factory"]() as reader:
        entries = list(await reader.scalars(sa.select(MemoryEntry)))
        assert len(entries) == 1 and entries[0].legacy_message_id == 2 and entries[0].state == "withdrawn"
        revision = await reader.scalar(sa.select(MemoryEntryRevision))
        assert revision.revision == 1 and revision.parent_revision is None and revision.content is None
        reference = await reader.scalar(sa.select(MemoryRevisionSource))
        assert reference.source_message_id == 1 and reference.source_generation == 3
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryMutationOperation).where(MemoryMutationOperation.kind == "adopt")) == 0
        plan = await reader.scalar(sa.select(MemoryPublicationOutbox.payload).where(MemoryPublicationOutbox.id == "generated-batch"))
        assert "content" not in plan["messages"][0] and "content_embed" not in plan["messages"][0]
        assert plan["messages"][1]["content"] == "independent sibling fact"
        assert await reader.scalar(sa.select(MemoryWriterAdmission.publication_state)) == "ready"


@pytest.mark.parametrize("corruption", ["vector", "source"])
async def test_generated_fact_adoption_rejects_changed_physical_evidence(bank: dict[str, Any], corruption: str) -> None:
    physical = await _ready_generated_batch(bank)
    if corruption == "vector":
        physical.rows[2]["content_embed"] = [0.0, 1.0]
    else:
        physical.rows[1]["content"] = "changed raw evidence"
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        with pytest.raises(RevisionConflict):
            await source.change_physical_state("bank", 2, operation_id="fact-disable", status=False)
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryEntry.entry_id)) is None
        assert await reader.scalar(sa.select(MemoryMutationOperation.operation_id).where(MemoryMutationOperation.operation_id == "fact-disable")) is None


async def test_ready_generated_fact_disabled_before_bank_activation_can_reenable(bank: dict[str, Any]) -> None:
    physical = await _ready_generated_batch(bank)
    physical.rows[2]["status"] = False
    async with bank["factory"]() as db:

        async def authorize(session: AsyncSession, memory_id: str) -> None:
            assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id)) == memory_id

        source = SourceService(db, tenant_id="owner", actor_id="owner", authorize=authorize, message_reader=physical.read)
        await source.change_physical_state("bank", 2, operation_id="enable-previously-disabled", status=True)
        assert (await source.reconcile_operation("bank", "enable-previously-disabled", storage=physical))["status"] == "ready"  # type: ignore[arg-type]
        assert physical.rows[2]["status"] is True and physical.rows[3]["status"] is True
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryEntry.state)) == "active"
        assert await reader.scalar(sa.select(MemoryEntryRevision.message_id)) == 2


async def test_unmanaged_bank_delete_cancels_queued_writer_and_retains_acl_operation(bank: dict[str, Any]) -> None:
    async with bank["factory"]() as writer, writer.begin():
        await writer.execute(sa.delete(MemoryPublicationOutbox))
        await writer.execute(sa.delete(MemoryRevisionSource))
        await writer.execute(sa.delete(MemoryEntryRevision))
        await writer.execute(sa.delete(MemoryEntry))
        await writer.execute(sa.delete(ManagedMemoryState))
        writer.add(
            MemoryWriterAdmission(
                memory_id="bank",
                operation_id="queued",
                tenant_id="owner",
                actor_id="owner",
                kind="append_raw",
                request_sha256="f" * 64,
                writer_generation=1,
                state="queued",
                publication_state="pending",
                message_ids=[],
                request_metadata={},
                receipt={},
            )
        )
    async with bank["factory"]() as db:
        service = MemoryDeletionService(db, actor_id="member")
        with pytest.raises(RevisionNotFound):
            await service.get_operation("bank", "unmanaged-delete")
        async with bank["factory"]() as reader:
            assert await reader.scalar(sa.select(ManagedMemoryState.memory_id)) is None
        pending = await service.request_delete("bank", operation_id="unmanaged-delete")
        assert pending["lifecycle"] == "deleting" and pending["blocked_by"] == []
        result = await service.reconcile_operation("bank", "unmanaged-delete", storage=StrictStorage(bank["factory"], db))
        assert result["status"] == "ready" and result["tracked_cleanup_confirmed"] is True
        assert (await service.get_operation("bank", "unmanaged-delete"))["lifecycle"] == "deleted"
    async with bank["factory"]() as reader:
        assert await reader.scalar(sa.select(Memory.id)) is None
        assert await reader.scalar(sa.select(MemoryWriterAdmission.state).where(MemoryWriterAdmission.operation_id == "queued")) == "canceled"
        assert await reader.scalar(sa.select(ManagedMemoryState.delete_permissions)) == "team"
