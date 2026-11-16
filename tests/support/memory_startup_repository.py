"""Owned real-SQL startup fixtures with controlled strict physical reads only."""

from copy import deepcopy
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.revisions.domain import content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.services.startup import StartupService
from memory.verification.service import VerificationService
from tests.support.memory_maintenance_repository import service as existing_jobs

ENTRY = "a" * 32


async def seed(
    env: dict[str, Any], *, entry_id: str = ENTRY, message_id: int = 100, content: str = "仅允许隔离内网；不授权生产访问。", scope: dict[str, str] | None = None, invalid_at: str | None = None
) -> None:
    actual = deepcopy(scope if scope is not None else env["scope"])
    env.setdefault("physical", {})
    env.setdefault("reads", 0)
    raw = {
        "id": "bank_10",
        "memory_id": "bank",
        "message_id": 10,
        "source_id": 0,
        "message_type": "raw",
        "content": "source 10",
        "content_embed": [1.0, 0.0],
        "status": True,
        "valid_at": None,
        "invalid_at": None,
        "forget_at": None,
        **actual,
    }
    message = {
        **raw,
        "id": f"bank_{message_id}",
        "message_id": message_id,
        "source_id": 10,
        "message_type": "episodic" if invalid_at is not None else "semantic",
        "content": content,
        "invalid_at": invalid_at,
    }
    env["physical"][message_id] = message
    if scope is None:
        env["physical"][10] = raw
    async with env["factory"]() as db, db.begin():
        if scope is None:
            source = await db.get(MemorySourceState, ("bank", 10))
            source.scope = actual
        db.add(
            MemoryEntry(
                entry_id=entry_id, memory_id="bank", tenant_id="tenant", legacy_message_id=message_id, current_revision=1, next_revision=2, current_message_id=message_id, generation=1, state="active"
            )
        )
        await db.flush()
        db.add(
            MemoryEntryRevision(
                entry_id=entry_id,
                revision=1,
                memory_id="bank",
                tenant_id="tenant",
                message_id=message_id,
                parent_revision=None,
                origin="legacy_adoption",
                content=content,
                content_sha256=content_hash(content),
                assessment={"kind": "hypothesis", "verification_status": "unverified"},
                message_snapshot=deepcopy(message),
            )
        )
        await db.flush()
        db.add(
            MemoryRevisionSource(
                entry_id=entry_id,
                revision=1,
                memory_id="bank",
                source_message_id=10,
                source_revision=1,
                source_generation=1,
                content_sha256=content_hash("source 10"),
                relation="context",
                locator={"scope": actual},
            )
        )


def verification(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> VerificationService:
    jobs = existing_jobs(db, env, actor_id=actor_id)

    class Storage:
        async def read(self, message_id: int) -> dict[str, Any] | None:
            assert not db.in_transaction(), "No SQL transaction across strict physical reads"
            env["reads"] += 1
            if hook := env.get("on_read"):
                await hook(message_id)
            return deepcopy(env["physical"].get(message_id))

    store = cast(ManagedMessageStore, Storage())
    snapshots = MaintenanceSnapshots(db, tenant_id="tenant", storage=store, count_tokens=len)
    return VerificationService(db, tenant_id="tenant", actor_id=actor_id, authorize=jobs.authorize, storage=store, ready_entry=snapshots._ready_entry, ready_entries=snapshots._ready_entries)


async def check(env: dict[str, Any], *, entry_id: str = ENTRY, result: str = "human_confirmed", operation_id: str = "human-check") -> dict[str, Any]:
    async with env["factory"]() as db:
        service = verification(db, env)
        snapshot = await service.snapshot("bank", entry_id)
        return await service.record(
            "bank",
            entry_id,
            operation_id=operation_id,
            expected_head_version=snapshot["head_version"],
            expected_binding_sha256=snapshot["binding_sha256"],
            result=result,
            method="source_comparison",
            applicability="只适用于隔离演练，不能用于生产。",
            notes="人工声明不代表客观真理。",
            evidence=[],
            checked_at=None,
        )


def startup(db: AsyncSession, env: dict[str, Any], *, actor_id: str = "actor") -> StartupService:
    return StartupService(verification(db, env, actor_id=actor_id), count_tokens=lambda value: len(value) // 4 + 1)
