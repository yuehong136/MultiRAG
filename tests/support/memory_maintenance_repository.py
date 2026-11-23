"""Shared owned memory maintenance repository integration fixtures and helpers."""

from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.db_models import Memory
from api.db.memory_revision_models import ManagedMemoryState, MemorySourceState
from memory.maintenance.jobs import JobService
from memory.revisions.domain import RevisionConflict, RevisionNotFound, content_hash
from tests.support.database import scratch_database


@pytest.fixture
async def environment(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    with scratch_database(tmp_path / "maintenance-repository") as engine:
        asynchronous = create_async_engine(engine.url)
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)
        scope = {"user_id": "user", "agent_id": "agent", "session_id": "session"}
        try:
            async with factory() as db, db.begin():
                db.add(Memory(id="bank", tenant_id="tenant", name="maintenance test", embd_id="controlled", llm_id="controlled"))
                db.add(ManagedMemoryState(memory_id="bank", tenant_id="tenant", generation=1, capacity_generation=1, reserved_bytes=0, state="active"))
                db.add_all(
                    [
                        MemorySourceState(
                            memory_id="bank",
                            tenant_id="tenant",
                            source_message_id=identity,
                            source_revision=1,
                            generation=1,
                            content_sha256=content_hash("source " + str(identity)),
                            state="active",
                            scope=scope,
                        )
                        for identity in (10, 20)
                    ]
                )
            yield {"factory": factory, "engine": engine, "scope": scope, "snapshots": 0, "analyses": 0, "defer_second": False, "proposals": None}
        finally:
            await asynchronous.dispose()


def service(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> JobService:
    async def authorize(session: AsyncSession, memory_id: str) -> None:
        if actor_id == "denied" or await session.scalar(sa.select(Memory.id).where(Memory.id == memory_id, Memory.tenant_id == "tenant")) is None:
            raise RevisionNotFound("memory_not_found")

    async def snapshotter(memory_id: str, source_ids: list[int], entry_ids: list[str], budget: int) -> dict[str, Any]:
        assert not db.in_transaction()
        env["snapshots"] += 1
        inputs = []
        for identity in source_ids:
            deferred = env["defer_second"] and identity == 20
            inputs.append(
                {
                    "input_id": f"input{identity}",
                    "source_id": identity,
                    "source_revision": 1,
                    "source_generation": 1,
                    "content_sha256": content_hash("source " + str(identity)),
                    "scope": dict(env["scope"]),
                    "valid_at": None,
                    "snapshot": {} if deferred else {"content": "source " + str(identity), "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"},
                    "coverage": "deferred" if deferred else "included",
                    "defer_reason": "input_budget" if deferred else None,
                }
            )
        return {"inputs": inputs, "entries": deepcopy(env.get("entries", [])), "estimated_input_tokens": 1024}

    async def validate_inputs(session: AsyncSession, memory_id: str, manifest: dict[str, Any]) -> None:
        for item in sorted(manifest["inputs"], key=lambda source: source["source_id"]):
            row = await session.scalar(
                sa.select(MemorySourceState)
                .where(MemorySourceState.memory_id == memory_id, MemorySourceState.source_message_id == item["source_id"])
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if (
                row is None
                or row.state != "active"
                or (row.source_revision, row.generation, row.content_sha256, row.scope) != (item["source_revision"], item["source_generation"], item["content_sha256"], item["scope"])
            ):
                raise RevisionConflict("maintenance_source_changed")
        for entry in manifest["entries"]:
            if env.get("current_revisions", {}).get(entry["entry_id"], entry["revision"]) != entry["revision"]:
                raise RevisionConflict("maintenance_target_changed")

    async def analyzer(memory_id: str, manifest: dict[str, Any], policy: str, budget: int) -> dict[str, Any]:
        assert not db.in_transaction()
        env["analyses"] += 1
        if "started" in env:
            env["started"].set()
            await env["release"].wait()
        proposals = (
            env["proposals"]
            if env["proposals"] is not None
            else [{"action": "add", "content": "candidate fact", "message_type": "semantic", "source_input_ids": ["input10"], "reason": "two sessions considered"}]
        )
        return {"proposals": deepcopy(proposals), "model_reference": "controlled-chat", "config_reference": "config-hash", "usage": {"input_tokens": 400, "output_tokens": 50}}

    return JobService(
        db, tenant_id="tenant", actor_id=actor_id, authorize=authorize, snapshotter=snapshotter, validate_inputs=validate_inputs, analyzer=analyzer, count_tokens=lambda text: len(text) // 4 + 1
    )


async def create(env: dict[str, Any]) -> dict[str, Any]:
    async with env["factory"]() as db:
        return await service(db, env).create("bank", operation_id="create", source_ids=[10, 20], entry_ids=[])
