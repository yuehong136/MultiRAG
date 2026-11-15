"""Real PostgreSQL decisions; external message/model boundaries are controlled."""

import asyncio
import importlib.util
import threading
from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory, TenantLLM
from api.db.memory_revision_models import (
    MEMORY_REVISION_MODELS,
    ManagedMemoryState,
    MemoryEntry,
    MemoryEntryRevision,
    MemoryMutationOperation,
    MemoryPublicationOutbox,
    MemoryRevisionSource,
    MemorySourceState,
)
from memory.revisions.domain import ModelBinding, RevisionBusy, RevisionCapacityError, RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, request_hash
from memory.revisions.service import RevisionService
from memory.revisions.sources import SourceService
from memory.revisions.storage import ManagedMessageStore
from tests.support.database import scratch_database


@pytest.fixture
def revision_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    """Each case owns its database; never clear the session-wide scratch fixture."""
    with scratch_database(tmp_path / "revision-repository") as engine:
        yield engine


@pytest.fixture
def absent_revision_tables(revision_engine: sa.Engine) -> sa.Engine:
    """Model a legacy install before this migration in this case's owned database."""
    with revision_engine.begin() as connection:
        for model in reversed(MEMORY_REVISION_MODELS):
            model.__table__.drop(connection)
    return revision_engine


def _migration() -> Any:
    path = Path(__file__).parents[2] / "configs/alembic/versions/f0a2c4e6b8d0_add_memory_revision_state.py"
    spec = importlib.util.spec_from_file_location("tested_memory_revision_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_revision_migration_creates_empty_side_tables_and_revalidates_existing(absent_revision_tables: sa.Engine) -> None:
    module = _migration()
    with absent_revision_tables.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            connection.execute(ManagedMemoryState.__table__.insert().values(memory_id="bank", tenant_id="tenant", generation=7, state="active", reserved_bytes=12))
            module.upgrade()
    with absent_revision_tables.connect() as reader:
        assert reader.scalar(sa.select(ManagedMemoryState.generation)) == 7
        assert {model.__tablename__ for model in MEMORY_REVISION_MODELS} <= set(sa.inspect(reader).get_table_names(schema="usr_ai"))
    with absent_revision_tables.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="Cannot discard"):
            module.downgrade()


@pytest.mark.parametrize("drift", ["columns", "check"])
def test_revision_migration_rejects_incompatible_existing_table(absent_revision_tables: sa.Engine, drift: str) -> None:
    module = _migration()
    with absent_revision_tables.begin() as connection:
        if drift == "columns":
            connection.execute(sa.text("CREATE TABLE usr_ai.t_ai_memory_managed_states (memory_id varchar(32) PRIMARY KEY)"))
        else:
            table = ManagedMemoryState.__table__.to_metadata(sa.MetaData())
            for constraint in list(table.constraints):
                if isinstance(constraint, sa.CheckConstraint):
                    table.constraints.remove(constraint)
            table.create(connection)
    with absent_revision_tables.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError, match="incompatible"):
            module.upgrade()


@pytest.fixture
async def revision_async_engine(revision_engine: sa.Engine) -> AsyncIterator[AsyncEngine]:
    async_engine = create_async_engine(revision_engine.url)
    try:
        yield async_engine
    finally:
        await async_engine.dispose()


@pytest.fixture
async def repository(revision_async_engine: AsyncEngine) -> AsyncIterator[dict[str, Any]]:
    factory = async_sessionmaker(revision_async_engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        db.add(Memory(id="bank", tenant_id="tenant", name="owned revision bank", embd_id="legacy-alias", tenant_embd_id=None, llm_id="unused"))
        db.add(
            TenantLLM(
                id=71,
                tenant_id="tenant",
                llm_name="legacy-alias",
                llm_factory="Controlled",
                mdl_type="embedding",
                api_key="unused-test-credential",
                api_base="http://controlled.invalid",
                max_tokens=8192,
                status="1",
            )
        )
    rows = {
        1: {
            "memory_id": "bank",
            "message_id": 1,
            "message_type": "raw",
            "source_id": 0,
            "user_id": "",
            "agent_id": "agent",
            "session_id": "session",
            "content": "source facts",
            "content_embed": [1.0, 0.0],
            "status": True,
            "forget_at": None,
        },
        2: {
            "memory_id": "bank",
            "message_id": 2,
            "message_type": "semantic",
            "source_id": 1,
            "user_id": "",
            "agent_id": "agent",
            "session_id": "session",
            "content": "fact one; fact two",
            "content_embed": [1.0, 0.0],
            "status": True,
            "forget_at": None,
        },
    }
    yield {"factory": factory, "sync_url": revision_async_engine.url, "rows": rows, "counter": 2, "allocation_count": 0, "allocation_lock": asyncio.Lock()}


def _service(db: AsyncSession, env: dict[str, Any]) -> RevisionService:
    async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
        assert not db.in_transaction(), "External reads must not hold SQL"
        assert memory_id == "bank"
        return deepcopy(env["rows"].get(message_id))

    async def allocate(*, minimum_message_id: int) -> int:
        assert not db.in_transaction(), "ID allocation must not hold SQL"
        async with env["allocation_lock"]:
            env["counter"] = max(env["counter"], minimum_message_id) + 1
            env["allocation_count"] += 1
            return env["counter"]

    async def authorize(session: AsyncSession, memory_id: str) -> None:
        assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "tenant")) == memory_id

    async def binding(session: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
        row = (await session.execute(sa.select(TenantLLM.id, TenantLLM.llm_name, TenantLLM.llm_factory, TenantLLM.api_base).where(TenantLLM.tenant_id == "tenant", TenantLLM.id == 71))).one()
        return ModelBinding(row.id, f"{row.llm_name}@{row.llm_factory}", dimension, request_hash({"model": row.llm_name, "provider": row.llm_factory, "url": row.api_base}))

    async def measure(memory_id: str) -> int:
        assert not db.in_transaction(), "Storage measurement must not hold SQL"
        return 1024

    return RevisionService(
        db, tenant_id="tenant", actor_id="actor", message_reader=read, allocate_message_id=allocate, count_tokens=len, authorize=authorize, model_binding=binding, measure_memory_bytes=measure
    )


async def _adopt(env: dict[str, Any]) -> str:
    async with env["factory"]() as db:
        result = await _service(db, env).adopt_entry("bank", operation_id="adopt", legacy_message_id=2)
        return result["entry_id"]


async def _publish(env: dict[str, Any], operation_id: str) -> dict[str, Any]:
    async with env["factory"]() as db:
        service = _service(db, env)
        snapshot = await service.claim_publication("bank", operation_id)
        token, outbox_id = snapshot["lease_token"], snapshot["outbox_id"]
        staged = {**snapshot["payload"]["message"], "content_embed": [1.0, 0.0]}
        try:
            await service.record_stage(outbox_id, staged, lease_token=token)
            await service.publish_revision(outbox_id, lease_token=token)
            return await service.mark_ready(outbox_id, {**staged, "status": True}, old_message_inactive=True, lease_token=token)
        except RevisionConflict:
            await service.fail_operation(outbox_id, "publication_conflict", stale=True, lease_token=token)
            raise
        finally:
            await service.finish_publication_attempt(outbox_id, lease_token=token)


async def test_revision_acceptance_preserves_current_and_idempotent_request(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        service = _service(db, env)
        first = await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="new fact; fact two", reason="explicit correction")
        current = await service.get_entry("bank", entry_id)
        assert current["current_revision"] == 1 and current["current"]["content"] == "fact one; fact two"
        assert await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="new fact; fact two", reason="explicit correction") == first
        assert env["allocation_count"] == 1
        with pytest.raises(RevisionConflict, match="operation_request_mismatch"):
            await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="different", reason="explicit correction")
        assert env["allocation_count"] == 1
    result = await _publish(env, "revise")
    assert result["status"] == "ready"
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryEntry.current_revision).where(MemoryEntry.entry_id == entry_id)) == 2
        assert await reader.scalar(sa.select(MemoryEntryRevision.content).where(MemoryEntryRevision.entry_id == entry_id, MemoryEntryRevision.revision == 1)) == "fact one; fact two"
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0


async def test_two_same_expected_revision_publications_have_one_winner(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)

    async def revise(operation: str) -> dict[str, Any]:
        async with env["factory"]() as db:
            return await _service(db, env).revise_entry("bank", entry_id, operation_id=operation, expected_revision=1, content=f"new {operation}", reason="correction")

    await asyncio.gather(revise("one"), revise("two"))
    results = await asyncio.gather(_publish(env, "one"), _publish(env, "two"), return_exceptions=True)
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert sum(isinstance(result, dict) and result["status"] == "ready" for result in results) == 1
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntryRevision)) == 3
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox).where(MemoryPublicationOutbox.state == "ready")) == 1


async def test_concurrent_same_operation_creates_one_revision_and_stable_projection(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)

    async def revise() -> dict[str, Any]:
        async with env["factory"]() as db:
            return await _service(db, env).revise_entry("bank", entry_id, operation_id="one-operation", expected_revision=1, content="new same fact", reason="correction")

    left, right = await asyncio.gather(revise(), revise())
    assert left == right
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntryRevision)) == 2
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 1


async def test_source_generation_aba_blocks_old_pending_but_allows_fresh_revision(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        await _service(db, env).revise_entry("bank", entry_id, operation_id="old", expected_revision=1, content="old pending", reason="correction")
    async with env["factory"]() as writer, writer.begin():
        await writer.execute(sa.update(MemorySourceState).values(generation=3, state="active"))
    async with env["factory"]() as db:
        service = _service(db, env)
        with pytest.raises(RevisionConflict, match="source_state_changed"):
            await service.claim_publication("bank", "old")
        await service.revise_entry("bank", entry_id, operation_id="new", expected_revision=1, content="fresh pending", reason="correction")
    result = await _publish(env, "new")
    async with env["factory"]() as reader:
        refs = (await reader.execute(sa.select(MemoryRevisionSource.revision, MemoryRevisionSource.source_generation).order_by(MemoryRevisionSource.revision))).all()
        assert refs == [(1, 1), (2, 1), (3, 3)]
        assert result["status"] == "ready"


async def test_fresh_resolved_binding_change_rejects_pending_publication(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        service = _service(db, env)
        await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="new body", reason="correction")
        async with env["factory"]() as writer, writer.begin():
            await writer.execute(sa.update(TenantLLM).where(TenantLLM.id == 71).values(api_base="http://changed.invalid"))
        with pytest.raises(RevisionConflict, match="model_binding_changed"):
            await service.claim_publication("bank", "revise")
        assert (await service.get_entry("bank", entry_id))["current_revision"] == 1


async def test_lost_sql_commit_ack_replays_same_operation_without_new_id(repository: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        service = _service(db, env)
        commit = db.commit

        async def unknown_commit() -> None:
            await commit()
            raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", unknown_commit)
        with pytest.raises(RevisionPersistenceUnknown):
            await service.revise_entry("bank", entry_id, operation_id="same-operation", expected_revision=1, content="new body", reason="correction")
        monkeypatch.setattr(db, "commit", commit)
        accepted = await service.get_operation("bank", "same-operation")
        replay = await service.revise_entry("bank", entry_id, operation_id="same-operation", expected_revision=1, content="new body", reason="correction")
        assert accepted == replay and accepted["outbox_id"] and env["allocation_count"] == 1


async def test_post_publish_unknown_recovers_sync_without_repeating_cas(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        service = _service(db, env)
        await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="new body", reason="correction")
        snapshot = await service.claim_publication("bank", "revise")
        outbox_id, token = snapshot["outbox_id"], snapshot["lease_token"]
        staged = {**snapshot["payload"]["message"], "content_embed": [1.0, 0.0]}
        await service.record_stage(outbox_id, staged, lease_token=token)
        await service.publish_revision(outbox_id, lease_token=token)
        await service.mark_unknown(outbox_id, "activation_ack_unknown", lease_token=token)
        await service.finish_publication_attempt(outbox_id, lease_token=token)
        async with env["factory"]() as writer, writer.begin():
            await writer.execute(sa.update(MemorySourceState).values(generation=3, state="active"))
            await writer.execute(sa.update(MemoryEntry).values(generation=4, state="active"))
        recovered = await service.claim_publication("bank", "revise")
        assert recovered["state"] == "sync_pending"
        token = recovered["lease_token"]
        result = await service.mark_ready(outbox_id, {**staged, "status": True}, old_message_inactive=True, lease_token=token)
        await service.finish_publication_attempt(outbox_id, lease_token=token)
        assert result["status"] == "ready"
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryEntry.generation)) == 4
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryEntryRevision)) == 2
        assert await reader.scalar(sa.select(MemoryMutationOperation.status).where(MemoryMutationOperation.operation_id == "revise")) == "ready"


async def test_ready_projection_withdrawal_does_not_release_capacity_twice(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        await _service(db, env).revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="corrected fact", reason="correction")
    await _publish(env, "revise")
    deleted: list[list[int]] = []

    class ControlledStore(ManagedMessageStore):
        async def source_dependents(self, source_id: int) -> list[int]:
            assert source_id == 1
            return [1, 2]

        async def delete(self, message_ids: list[int]) -> None:
            deleted.append(message_ids)

    async with env["factory"]() as db:
        revision = _service(db, env)
        sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
        await sources.withdraw_source("bank", 1, operation_id="withdraw", expected_source_revision=1, reason="source withdrawn")
        first = await sources.reconcile_operation("bank", "withdraw", storage=ControlledStore("tenant", "bank"))
        repeated = await sources.reconcile_operation("bank", "withdraw", storage=ControlledStore("tenant", "bank"))
        assert first == repeated and first["status"] == "ready"
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        outbox = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == "revise"))
        assert outbox is not None and outbox.receipt["reservation_released"] is True and not outbox.receipt["active_attempts"]
        assert await reader.scalar(sa.select(MemoryEntry.state)) == "withdrawn"
        assert list((await reader.scalars(sa.select(MemoryEntryRevision.content))).all()) == [None, None]
    assert deleted == [[1, 2, 3]]


async def test_capacity_reservation_counts_decoded_float_size(repository: dict[str, Any]) -> None:
    env = repository
    dimension = 1536
    env["rows"][2]["content_embed"] = [1.0] * dimension
    entry_id = await _adopt(env)
    # This fits the former wire-size estimate but not the existing physical
    # inventory metric (Python floats). Accepting it would overfill this bank.
    async with env["factory"]() as writer, writer.begin():
        await writer.execute(sa.update(Memory).where(Memory.id == "bank").values(memory_size=1024 + len("corrected fact") + dimension * 8 + 4096 + 1))
    async with env["factory"]() as db:
        with pytest.raises(RevisionCapacityError, match="insufficient_revision_capacity"):
            await _service(db, env).revise_entry("bank", entry_id, operation_id="too-large", expected_revision=1, content="corrected fact", reason="correction")
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(MemoryEntry.current_revision)) == 1
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 0


async def test_capacity_measurement_is_rejected_after_another_entry_becomes_ready(repository: dict[str, Any]) -> None:
    env = repository
    first_entry = await _adopt(env)
    env["rows"][4] = {**env["rows"][2], "message_id": 4, "content": "another entry"}
    env["counter"] = 4
    async with env["factory"]() as db:
        service = _service(db, env)
        second_entry = (await service.adopt_entry("bank", operation_id="adopt-other", legacy_message_id=4))["entry_id"]
        await service.revise_entry("bank", first_entry, operation_id="first", expected_revision=1, content="corrected first", reason="correction")
    measured, first_published = asyncio.Event(), asyncio.Event()

    async def delayed_measure(memory_id: str) -> int:
        assert memory_id == "bank"
        measured.set()
        await first_published.wait()
        return 1024

    async def second_revision() -> dict[str, Any]:
        async with env["factory"]() as db:
            service = _service(db, env)
            service.measure_memory_bytes = delayed_measure
            return await service.revise_entry("bank", second_entry, operation_id="second", expected_revision=1, content="corrected second", reason="correction")

    task = asyncio.create_task(second_revision())
    try:
        await asyncio.wait_for(measured.wait(), timeout=5)
        await _publish(env, "first")
        first_published.set()
        with pytest.raises(RevisionConflict, match="capacity_measurement_changed"):
            await task
    finally:
        first_published.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(ManagedMemoryState.capacity_generation)) == 2
        assert await reader.scalar(sa.select(ManagedMemoryState.reserved_bytes)) == 0
        assert await reader.scalar(sa.select(MemoryEntry.current_revision).where(MemoryEntry.entry_id == second_entry)) == 1
        assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 1


async def test_delayed_entry_disable_cannot_overwrite_later_enable(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    statuses: list[bool] = []

    class ControlledStore(ManagedMessageStore):
        async def set_status(self, message_id: int, status: bool) -> None:
            assert message_id == 2
            statuses.append(status)
            env["rows"][message_id]["status"] = status

    async with env["factory"]() as db:
        revision = _service(db, env)
        sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
        await sources.change_physical_state("bank", 2, operation_id="disable", status=False)
        await sources.change_physical_state("bank", 2, operation_id="enable", status=True)
        enabled = await sources.reconcile_operation("bank", "enable", storage=ControlledStore("tenant", "bank"))
        delayed = await sources.reconcile_operation("bank", "disable", storage=ControlledStore("tenant", "bank"))
        assert enabled["status"] == "ready" and delayed["status"] == "stale"
        corrected = await sources.reconcile_operation("bank", delayed["reconciliation_operation_id"], storage=ControlledStore("tenant", "bank"))
        assert corrected["status"] == "ready" and statuses and all(statuses)
    async with env["factory"]() as reader:
        entry = await reader.get(MemoryEntry, entry_id)
        assert entry is not None and entry.state == "active" and entry.generation == 3


async def test_entry_forget_waits_for_older_enable_attempt_to_finish(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    started, release = asyncio.Event(), asyncio.Event()
    deleted: list[int] = []

    class ControlledStore(ManagedMessageStore):
        async def set_status(self, message_id: int, status: bool) -> None:
            assert message_id == 2 and status is True
            started.set()
            await release.wait()
            env["rows"][message_id]["status"] = status

        async def delete(self, message_ids: list[int]) -> None:
            for message_id in message_ids:
                deleted.append(message_id)
                env["rows"].pop(message_id, None)

    async def reconcile_enable() -> dict[str, Any]:
        async with env["factory"]() as db:
            revision = _service(db, env)
            sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
            return await sources.reconcile_operation("bank", "enable", storage=ControlledStore("tenant", "bank"))

    async with env["factory"]() as db:
        revision = _service(db, env)
        sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
        await sources.change_physical_state("bank", 2, operation_id="enable", status=True)
    task = asyncio.create_task(reconcile_enable())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with env["factory"]() as db:
            revision = _service(db, env)
            sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
            await sources.change_physical_state("bank", 2, operation_id="forget", forget=True)
            waiting = await sources.reconcile_operation("bank", "forget", storage=ControlledStore("tenant", "bank"))
            assert waiting["index_sync_status"] == "unknown" and not deleted
        release.set()
        older = await task
        assert older["index_sync_status"] != "ready"
        async with env["factory"]() as db:
            revision = _service(db, env)
            sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
            finished = await sources.reconcile_operation("bank", "forget", storage=ControlledStore("tenant", "bank"))
            assert finished["status"] == "ready" and 2 in deleted and 2 not in env["rows"]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with env["factory"]() as reader:
        entry = await reader.get(MemoryEntry, entry_id)
        assert entry is not None and entry.state == "withdrawn"
        outboxes = list((await reader.scalars(sa.select(MemoryPublicationOutbox))).all())
        assert all(not row.receipt.get("active_attempts") for row in outboxes)


async def test_expired_lease_cannot_overlap_an_undrained_projection_writer(repository: dict[str, Any]) -> None:
    env = repository
    entry_id = await _adopt(env)
    async with env["factory"]() as db:
        service = _service(db, env)
        await service.revise_entry("bank", entry_id, operation_id="revise", expected_revision=1, content="corrected fact", reason="correction")
        claimed = await service.claim_publication("bank", "revise")
    outbox_id, token = claimed["outbox_id"], claimed["lease_token"]
    started, release = asyncio.Event(), asyncio.Event()
    staged = {**claimed["payload"]["message"], "content_embed": [1.0, 0.0]}

    async def delayed_write() -> None:
        started.set()
        await release.wait()
        env["rows"][claimed["message_id"]] = deepcopy(staged)

    writer = asyncio.create_task(delayed_write())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        async with env["factory"]() as db, db.begin():
            await db.execute(sa.update(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == outbox_id).values(lease_until=datetime.now(UTC) - timedelta(seconds=1)))
        async with env["factory"]() as db:
            service = _service(db, env)
            with pytest.raises(RevisionBusy, match="publication_needs_operator_reconciliation"):
                await service.claim_publication("bank", "revise")
            assert (await service.get_entry("bank", entry_id))["current_revision"] == 1
        release.set()
        await writer
        async with env["factory"]() as db:
            service = _service(db, env)
            await service.finish_publication_attempt(outbox_id, lease_token=token)
            recovered = await service.claim_publication("bank", "revise")
            token = recovered["lease_token"]
            await service.record_stage(outbox_id, env["rows"][claimed["message_id"]], lease_token=token)
            await service.publish_revision(outbox_id, lease_token=token)
            ready = await service.mark_ready(outbox_id, {**staged, "status": True}, old_message_inactive=True, lease_token=token)
            await service.finish_publication_attempt(outbox_id, lease_token=token)
            assert ready["status"] == "ready"
    finally:
        release.set()
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def _wait_for_database_lock(env: dict[str, Any], blocked: asyncio.Task[Any]) -> None:
    """Observe the real owned database wait, not a timing assumption about workers."""
    async with env["factory"]() as reader:
        for _ in range(500):
            waiting = await reader.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND cardinality(pg_blocking_pids(pid)) > 0)"))
            await reader.rollback()
            if waiting:
                return
            assert not blocked.done(), "Expected the contender to wait for the owning Memory row lock"
            await asyncio.sleep(0.01)
    raise AssertionError("The owned Memory row lock was not observed")


def _delete_from_owned_database(env: dict[str, Any]) -> bool:
    from sqlalchemy.orm import Session

    from api.apps.services.memory_api_service import delete_memory

    engine = sa.create_engine(env["sync_url"])
    try:
        with Session(engine) as db:
            return delete_memory(db, "bank", user_id="actor")
    finally:
        engine.dispose()


async def test_unmanaged_delete_lock_wins_before_first_adoption(repository: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import Session

    from api.apps.services import memory_api_service

    env = repository
    deletion_holds_lock = asyncio.Event()
    allow_deletion = threading.Event()
    loop = asyncio.get_running_loop()
    checks = 0

    def authorize(db: Session, user_id: str, memory_id: str) -> Memory:
        nonlocal checks
        assert user_id == "actor" and memory_id == "bank"
        memory = db.scalar(sa.select(Memory).where(Memory.id == memory_id, Memory.tenant_id == "tenant"))
        assert memory is not None
        checks += 1
        if checks == 2:
            loop.call_soon_threadsafe(deletion_holds_lock.set)
            assert allow_deletion.wait(timeout=5)
        return memory

    monkeypatch.setattr(memory_api_service, "_require_memory_access", authorize)
    monkeypatch.setattr(memory_api_service.MessageService, "has_index", lambda tenant_id, memory_id: False)
    deletion = asyncio.create_task(asyncio.to_thread(_delete_from_owned_database, env))
    adoption: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(deletion_holds_lock.wait(), timeout=5)
        adoption = asyncio.create_task(_adopt(env))
        await _wait_for_database_lock(env, adoption)
        allow_deletion.set()
        assert await deletion is True
        with pytest.raises(RevisionNotFound, match="memory_not_found"):
            await adoption
    finally:
        allow_deletion.set()
        await asyncio.gather(*(task for task in (deletion, adoption) if task is not None), return_exceptions=True)
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(Memory)) == 0
        assert await reader.scalar(sa.select(sa.func.count()).select_from(ManagedMemoryState)) == 0


async def test_first_adoption_lock_makes_legacy_delete_recheck_managed_state(repository: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.orm import Session

    from api.apps.services import memory_api_service

    env = repository
    adoption_holds_lock, allow_adoption = asyncio.Event(), asyncio.Event()

    def authorize(db: Session, user_id: str, memory_id: str) -> Memory:
        assert user_id == "actor" and memory_id == "bank"
        memory = db.scalar(sa.select(Memory).where(Memory.id == memory_id, Memory.tenant_id == "tenant"))
        assert memory is not None
        return memory

    monkeypatch.setattr(memory_api_service, "_require_memory_access", authorize)
    monkeypatch.setattr(memory_api_service.MessageService, "has_index", lambda tenant_id, memory_id: False)

    async def adopt() -> dict[str, Any]:
        async with env["factory"]() as db:
            service = _service(db, env)
            original = service.model_binding

            async def binding(session: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
                value = await original(session, memory_id, dimension)
                adoption_holds_lock.set()
                await allow_adoption.wait()
                return value

            service.model_binding = binding
            return await service.adopt_entry("bank", operation_id="adopt", legacy_message_id=2)

    adoption = asyncio.create_task(adopt())
    deletion: asyncio.Task[bool] | None = None
    try:
        await asyncio.wait_for(adoption_holds_lock.wait(), timeout=5)
        deletion = asyncio.create_task(asyncio.to_thread(_delete_from_owned_database, env))
        await _wait_for_database_lock(env, deletion)
        allow_adoption.set()
        assert (await adoption)["status"] == "ready"
        with pytest.raises(RevisionConflict, match="managed_memory_delete_requires_writer_coordination"):
            await deletion
    finally:
        allow_adoption.set()
        await asyncio.gather(*(task for task in (deletion, adoption) if task is not None), return_exceptions=True)
    async with env["factory"]() as reader:
        assert await reader.scalar(sa.select(sa.func.count()).select_from(Memory)) == 1
        assert await reader.scalar(sa.select(sa.func.count()).select_from(ManagedMemoryState)) == 1


@pytest.mark.parametrize("mode,expected_state", [("forget", "forgotten"), ("disable", "inactive"), ("enable", "active")])
async def test_legacy_raw_baseline_and_main_operation_survive_lost_commit_ack(repository: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str, expected_state: str) -> None:
    env = repository
    await _adopt(env)
    env["rows"][9] = {**env["rows"][1], "message_id": 9, "content": "unregistered source", "status": mode != "enable"}
    changes: dict[str, Any] = {"forget": True} if mode == "forget" else {"status": mode == "enable"}
    async with env["factory"]() as db:
        revision = _service(db, env)
        sources = SourceService(db, tenant_id="tenant", actor_id="actor", authorize=revision.authorize, message_reader=revision.message_reader)
        commit = db.commit
        committed = 0

        async def lost_ack() -> None:
            nonlocal committed
            await commit()
            committed += 1
            raise sa.exc.DBAPIError("COMMIT", {}, ConnectionError("lost acknowledgement"), connection_invalidated=True)

        monkeypatch.setattr(db, "commit", lost_ack)
        with pytest.raises(RevisionPersistenceUnknown) as caught:
            await sources.change_physical_state("bank", 9, operation_id="legacy-main", **changes)
        assert caught.value.operation_id == "legacy-main" and committed == 1
        operation = await revision.get_operation("bank", "legacy-main")
        replay = await sources.change_physical_state("bank", 9, operation_id="legacy-main", **changes)
        assert replay["operation_id"] == operation["operation_id"] == "legacy-main"
        assert replay["outbox_id"] == operation["outbox_id"] and committed == 1
        monkeypatch.setattr(db, "commit", commit)
    async with env["factory"]() as reader:
        source = await reader.get(MemorySourceState, ("bank", 9))
        assert source is not None and source.state == expected_state and source.source_revision == 1 and source.generation == 2
        operations = list((await reader.scalars(sa.select(MemoryMutationOperation))).all())
        assert {row.operation_id for row in operations} == {"adopt", "legacy-main"}
        outboxes = list((await reader.scalars(sa.select(MemoryPublicationOutbox))).all())
        assert len(outboxes) == 1 and outboxes[0].operation_id == "legacy-main" and outboxes[0].attempt == 0
    assert env["rows"][9]["status"] is (mode != "enable")
