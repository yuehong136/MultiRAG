"""JSON source dependency cleanup preserves int64 IDs without int4 casts."""

from copy import deepcopy
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import Memory
from api.db.memory_revision_models import MemoryMutationOperation, MemoryPublicationOutbox, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.sources import SourceService
from tests.support.memory_append_repository import ControlledStore, _service
from tests.support.memory_append_repository import environment as environment

LARGE_ID = 9007199254741093


def sources(db: AsyncSession, env: dict[str, Any]) -> SourceService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "tenant")) == memory_id

    async def read(memory_id: str, message_id: int) -> dict[str, Any] | None:
        assert memory_id == "bank"
        return await ControlledStore(env, db).read(message_id)

    return SourceService(db, tenant_id="tenant", actor_id="actor", authorize=authorize, message_reader=read)


async def prepare(env: dict[str, Any]) -> tuple[int, dict[int, dict[str, Any]]]:
    async def append(operation_id: str) -> dict[str, Any]:
        async with env["factory"]() as db:
            return await _service(db, env).append_raw(
                "bank",
                operation_id=operation_id,
                content=f"来源{operation_id}；仅隔离使用。",
                scope={"user_id": "", "agent_id": "agent", "session_id": "session"},
                valid_at="2026-11-16 09:00:00",
                enqueue_extraction=False,
            )

    ordinary = await append("raw-ordinary")
    env["counter"] = LARGE_ID - 1
    large = await append("raw-large")
    assert ordinary["raw_source_id"] == 2 and large["raw_source_id"] == LARGE_ID
    # Real source-state operations carry a top-level JSON numeric source_id.
    # Both actual RAW append outboxes also remain present in this same bank.
    physical_before = deepcopy(env["rows"])
    try:
        async with env["factory"]() as db:
            service = sources(db, env)
            await service.change_physical_state("bank", LARGE_ID, operation_id="prior-large-state", status=True)
            prior = await service.reconcile_operation("bank", "prior-large-state", storage=ControlledStore(env, db))
            assert prior["status"] == "ready"
    except DBAPIError as error:
        async with env["factory"]() as reader:
            assert all(row.state == "active" and row.generation == 1 for row in await reader.scalars(sa.select(MemorySourceState)))
            assert await reader.get(MemoryMutationOperation, ("bank", "prior-large-state")) is None
            assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 2
            assert all(row.publication_state == "ready" and row.active_token is None for row in await reader.scalars(sa.select(MemoryWriterAdmission)))
        assert env["rows"] == physical_before and getattr(error.orig, "sqlstate", None) == "22003"
        raise
    async with env["factory"]() as reader:
        rows = list(await reader.scalars(sa.select(MemoryPublicationOutbox)))
        assert len(rows) == 3
        assert next(row for row in rows if row.operation_id == "prior-large-state").payload["source_id"] == LARGE_ID
    return ordinary["raw_source_id"], deepcopy(env["rows"])


@pytest.mark.parametrize("action", ["withdraw", "disable"])
@pytest.mark.parametrize("target", ["large", "ordinary"])
async def test_cleanup_dependencies_match_only_the_exact_int64_source(environment: dict[str, Any], action: str, target: str) -> None:
    env = environment
    ordinary, physical_before = await prepare(env)
    target_id, other_id = (LARGE_ID, ordinary) if target == "large" else (ordinary, LARGE_ID)
    main_operation = f"{action}-{target}"
    async with env["factory"]() as reader:
        before_states = {row.source_message_id: (row.state, row.generation, row.content_sha256) for row in await reader.scalars(sa.select(MemorySourceState))}
        other_outbox = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == ("raw-ordinary" if target == "large" else "raw-large")))
        assert other_outbox is not None
        other_snapshot = (other_outbox.state, deepcopy(other_outbox.payload), deepcopy(other_outbox.receipt))
        prior_id = await reader.scalar(sa.select(MemoryPublicationOutbox.id).where(MemoryPublicationOutbox.operation_id == "prior-large-state"))
    try:
        async with env["factory"]() as db:
            service = sources(db, env)
            if action == "withdraw":
                await service.withdraw_source("bank", target_id, operation_id=main_operation, expected_source_revision=1, reason="明确撤回当前来源。")
            else:
                await service.change_physical_state("bank", target_id, operation_id=main_operation, status=False)
            ready = await service.reconcile_operation("bank", main_operation, storage=ControlledStore(env, db))
            assert ready["status"] == "ready"
    except DBAPIError as error:
        # The first old-code run intentionally exposes PostgreSQL22003. Verify
        # independently that its interrupted SQL transaction changed no state.
        async with env["factory"]() as reader:
            assert {row.source_message_id: (row.state, row.generation, row.content_sha256) for row in await reader.scalars(sa.select(MemorySourceState))} == before_states
            assert await reader.get(MemoryMutationOperation, ("bank", main_operation)) is None
            assert await reader.scalar(sa.select(sa.func.count()).select_from(MemoryPublicationOutbox)) == 3
            assert all(row.publication_state == "ready" and row.active_token is None for row in await reader.scalars(sa.select(MemoryWriterAdmission)))
        assert env["rows"] == physical_before
        assert getattr(error.orig, "sqlstate", None) == "22003"
        raise
    async with env["factory"]() as reader:
        changed = await reader.get(MemorySourceState, ("bank", target_id))
        other = await reader.get(MemorySourceState, ("bank", other_id))
        assert changed is not None and other is not None
        assert changed.state == ("forgotten" if action == "withdraw" else "inactive")
        assert changed.generation == before_states[target_id][1] + 1
        assert (other.state, other.generation, other.content_sha256) == before_states[other_id]
        operation = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == main_operation))
        assert operation is not None
        assert (prior_id in operation.payload["dependent_outboxes"]) == (target == "large")
        untouched = await reader.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.id == other_outbox.id))
        assert untouched is not None and (untouched.state, untouched.payload, untouched.receipt) == other_snapshot
    assert env["rows"][other_id] == physical_before[other_id]
    if action == "withdraw":
        assert target_id not in env["rows"]
    else:
        assert env["rows"][target_id] == {**physical_before[target_id], "status": False}


async def test_canonical_string_json_source_id_has_the_same_precise_dependencies(environment: dict[str, Any]) -> None:
    env = environment
    ordinary, before = await prepare(env)
    async with env["factory"]() as db, db.begin():
        row = await db.scalar(sa.select(MemoryPublicationOutbox).where(MemoryPublicationOutbox.operation_id == "prior-large-state"))
        assert row is not None
        row.payload = {**row.payload, "source_id": str(LARGE_ID)}
        prior_id = row.id
    async with env["factory"]() as db:
        service = sources(db, env)
        assert prior_id in await service._state_dependencies("bank", source_id=LARGE_ID)
        assert prior_id not in await service._state_dependencies("bank", source_id=ordinary)
        await service.withdraw_source("bank", LARGE_ID, operation_id="string-id-withdraw", expected_source_revision=1, reason="撤回大ID文本来源。")
        assert (await service.reconcile_operation("bank", "string-id-withdraw", storage=ControlledStore(env, db)))["status"] == "ready"
    assert LARGE_ID not in env["rows"] and env["rows"][ordinary] == before[ordinary]
