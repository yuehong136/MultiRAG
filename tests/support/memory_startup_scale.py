"""Bulk SQL inventory for scale observations; physical IO stays controlled."""

from copy import deepcopy
from typing import Any

from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from memory.revisions.domain import content_hash
from tests.support.memory_startup_repository import check


async def seed_inventory(env: dict[str, Any], *, size: int, confirmed: int, source_count: int = 1, foreign: int = 0) -> list[str]:
    """Seed bounded rows, then use the real verification service for confirmations."""
    if not 0 <= confirmed <= size <= 160 or not 1 <= source_count <= 32 or not 0 <= foreign <= 160:
        raise ValueError("invalid_scale_fixture")
    env["physical"], env["reads"] = {}, 0
    scope = dict(env["scope"])
    source_ids = [10 * (index + 1) for index in range(source_count)]
    raw_rows: dict[int, dict[str, Any]] = {}
    for identity in source_ids:
        raw_rows[identity] = {
            "id": f"bank_{identity}",
            "memory_id": "bank",
            "message_id": identity,
            "source_id": 0,
            "message_type": "raw",
            "content": f"source {identity}",
            "content_embed": [1.0, 0.0],
            "status": True,
            "valid_at": None,
            "invalid_at": None,
            "forget_at": None,
            **scope,
        }
        env["physical"][identity] = deepcopy(raw_rows[identity])
    identities: list[str] = []
    revisions: list[MemoryEntryRevision] = []
    references: list[MemoryRevisionSource] = []
    async with env["factory"]() as db, db.begin():
        for identity in source_ids:
            existing = await db.get(MemorySourceState, ("bank", identity))
            if existing is None:
                db.add(
                    MemorySourceState(
                        memory_id="bank",
                        tenant_id="tenant",
                        source_message_id=identity,
                        source_revision=1,
                        generation=1,
                        state="active",
                        scope=scope,
                        content_sha256=content_hash(f"source {identity}"),
                    )
                )
            else:
                existing.scope = scope
        for index in range(size + foreign):
            is_foreign = index < foreign
            entry_identity = f"{index + 1 if is_foreign else 1000 + index:032x}"
            message_id = 100000 + index
            actual_scope = {**scope, "user_id": "foreign"} if is_foreign else scope
            content = f"完整事实{index}：仅隔离环境；不授权生产。"
            row = {**raw_rows[source_ids[0]], "id": f"bank_{message_id}", "message_id": message_id, "source_id": source_ids[0], "message_type": "semantic", "content": content, **actual_scope}
            env["physical"][message_id] = row
            db.add(
                MemoryEntry(
                    entry_id=entry_identity,
                    memory_id="bank",
                    tenant_id="tenant",
                    legacy_message_id=message_id,
                    current_revision=1,
                    next_revision=2,
                    current_message_id=message_id,
                    generation=1,
                    state="active",
                )
            )
            revisions.append(
                MemoryEntryRevision(
                    entry_id=entry_identity,
                    revision=1,
                    memory_id="bank",
                    tenant_id="tenant",
                    message_id=message_id,
                    parent_revision=None,
                    origin="legacy_adoption",
                    content=content,
                    content_sha256=content_hash(content),
                    assessment={"kind": "hypothesis", "verification_status": "unverified"},
                    message_snapshot=deepcopy(row),
                )
            )
            references.extend(
                MemoryRevisionSource(
                    entry_id=entry_identity,
                    revision=1,
                    memory_id="bank",
                    source_message_id=source_id,
                    source_revision=1,
                    source_generation=1,
                    content_sha256=content_hash(f"source {source_id}"),
                    relation="context",
                    locator={"scope": scope},
                )
                for source_id in source_ids
            )
            if not is_foreign:
                identities.append(entry_identity)
        await db.flush()
        db.add_all(revisions)
        await db.flush()
        db.add_all(references)
    for index, entry_identity in enumerate(identities[:confirmed]):
        await check(env, entry_id=entry_identity, operation_id=f"scale-check-{index}")
    env["reads"] = 0
    return identities
