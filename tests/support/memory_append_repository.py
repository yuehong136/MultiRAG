"""Shared owned memory append repository integration fixtures and helpers."""

from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState
from common.constants import MemoryType
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionStorageUnknown
from memory.revisions.sources import SourceService
from memory.revisions.storage import ManagedMessageStore
from memory.writes.retention import FifoRetentionService
from memory.writes.service import AppendService
from tests.support.database import scratch_database


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    with scratch_database(tmp_path / "append-repository") as engine:
        asynchronous = create_async_engine(engine.url)
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)
        try:
            async with factory() as db, db.begin():
                db.add(Memory(id="bank", tenant_id="tenant", name="owned append bank", embd_id="controlled", llm_id="controlled", memory_type=MemoryType.RAW.value | MemoryType.SEMANTIC.value))
                db.add(ManagedMemoryState(memory_id="bank", tenant_id="tenant", generation=1, capacity_generation=1, state="active", reserved_bytes=0))
            yield {
                "factory": factory,
                "engine": engine,
                "rows": {},
                "counter": 1,
                "embeddings": 0,
                "extractions": 0,
                "writes": 0,
                "queue": [],
                "dimension": None,
                "lose_write": False,
                "immediate_queue": False,
            }
        finally:
            await asynchronous.dispose()


class ControlledStore(ManagedMessageStore):
    def __init__(self, env: dict[str, Any], db: AsyncSession) -> None:
        super().__init__("tenant", "bank")
        self.env, self.db = env, db

    def _external(self) -> None:
        assert not self.db.in_transaction(), "Physical IO must not hold the caller SQL transaction"

    async def read(self, message_id: int) -> dict[str, Any] | None:
        self._external()
        return deepcopy(self.env["rows"].get(message_id))

    async def inventory(self) -> list[dict[str, Any]]:
        self._external()
        return deepcopy(list(self.env["rows"].values()))

    async def write(self, message: dict[str, Any]) -> None:
        self._external()
        self.env["writes"] += 1
        self.env["rows"][message["message_id"]] = deepcopy(message)
        if self.env["lose_write"]:
            self.env["lose_write"] = False
            raise RevisionStorageUnknown("lost write acknowledgement")

    async def set_status(self, message_id: int, status: bool) -> None:
        self._external()
        self.env["rows"][message_id]["status"] = status

    async def delete(self, message_ids: list[int]) -> None:
        self._external()
        for identity in message_ids:
            self.env["rows"].pop(identity, None)

    async def source_dependents(self, source_id: int) -> list[int]:
        self._external()
        return [identity for identity, message in self.env["rows"].items() if identity == source_id or message.get("source_id") == source_id]


def _service(db: AsyncSession, env: dict[str, Any]) -> AppendService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        assert await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "tenant")) == memory_id

    async def allocate(*, minimum_message_id: int) -> int:
        assert not db.in_transaction()
        env["counter"] = max(env["counter"], minimum_message_id) + 1
        return env["counter"]

    async def binding(session: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
        return ModelBinding(71, "controlled@Owned", dimension, "fixed-config")

    async def embed(memory_id: str, messages: list[dict[str, Any]], expected: ModelBinding | None) -> dict[str, Any]:
        assert not db.in_transaction()
        env["embeddings"] += 1
        return {"vectors": [[0.1, 0.2] for _ in messages], "model_binding": ModelBinding(71, "controlled@Owned", 2, "fixed-config")}

    async def extract(memory_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        assert not db.in_transaction() and raw["message_type"] == "raw"
        env["extractions"] += 1
        if "extract_started" in env:
            env["extract_started"].set()
            await env["extract_release"].wait()
        return {
            "items": [{"message_type": "semantic", "content": "derived fact", "valid_at": raw["valid_at"], "invalid_at": None}],
            "model_reference": "controlled-chat",
            "config_reference": "fixed-chat-config",
        }

    async def ensure(memory_id: str, dimension: int) -> None:
        assert not db.in_transaction()
        if env["dimension"] is not None and env["dimension"] != dimension:
            raise RevisionConflict("collection_dimension_changed")
        env["dimension"] = dimension

    async def evict(memory_id: str, source: dict[str, Any], operation_id: str) -> dict[str, Any]:
        assert not db.in_transaction()
        async with env["factory"]() as owner:
            managed = await owner.scalar(sa.select(ManagedMemoryState.memory_id).where(ManagedMemoryState.memory_id == memory_id)) is not None
            await owner.rollback()
            if managed:
                storage = ControlledStore(env, owner)

                async def read(mid: str, message_id: int) -> dict[str, Any] | None:
                    return await storage.read(message_id)

                sources = SourceService(owner, tenant_id="tenant", actor_id="actor", authorize=authorize, message_reader=read)
                return await sources.evict_source(memory_id, source, operation_id, storage=storage)
            return await FifoRetentionService(owner, tenant_id="tenant", actor_id="actor", authorize=authorize).evict_source(memory_id, source, operation_id, ControlledStore(env, owner))

    async def queue(payload: dict[str, Any]) -> bool:
        assert not db.in_transaction()
        env["queue"].append(deepcopy(payload))
        if env["immediate_queue"]:
            async with env["factory"]() as consumer:
                result = await _service(consumer, env).extract_source("bank", operation_id=payload["id"], task_id=payload["id"], source_id=payload["source_id"])
                assert result["status"] == "ready"
        return True

    return AppendService(
        db,
        tenant_id="tenant",
        actor_id="actor",
        authorize=authorize,
        allocate_message_id=allocate,
        count_tokens=len,
        model_binding=binding,
        storage=ControlledStore(env, db),
        embedder=embed,
        extractor=extract,
        fifo_evict=evict,
        ensure_index=ensure,
        queue_submit=queue,
    )
