"""Read-only fresh startup selection against real SQL, controlled physical IO."""

import json
from typing import Any

import pytest
import sqlalchemy as sa

from api.db.db_models import Base
from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemorySourceState
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_startup_repository import ENTRY, check, seed, startup, verification


def scope(env: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {"user_id": env["scope"]["user_id"], "agent_id": env["scope"]["agent_id"], "session_id": None, **changes}


async def counts(env: dict[str, Any]) -> dict[str, int]:
    async with env["factory"]() as db:
        return {table.name: int(await db.scalar(sa.select(sa.func.count()).select_from(table)) or 0) for table in Base.metadata.sorted_tables if table.schema == "usr_ai"}


async def test_human_confirmed_hypothesis_is_complete_deterministic_and_has_zero_sql_writes(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, invalid_at="2026-10-01 10:30:00")
    await check(env)
    before = await counts(env)
    async with env["factory"]() as db:
        owner = startup(db, env)
        first = await owner.build("bank", **scope(env))
        second = await owner.build("bank", **scope(env))
    assert first == second and len(first["items"]) == 1
    item = first["items"][0]
    assert item["assessment"] == {"kind": "hypothesis", "verification_status": "unverified"}
    assert item["invalid_at"] == "2026-10-01 10:30:00"
    assert not item["verification"]["checked_at_is_user_reported"]
    assert item["content"] in first["startup_text"] and item["verification"]["applicability"] in first["startup_text"]
    assert first["usage"]["estimated_tokens"] == len(json.dumps(first, ensure_ascii=False, separators=(",", ":"))) // 4 + 1
    assert await counts(env) == before and env["analyses"] == 0


@pytest.mark.parametrize("result", ["disputed", "needs_review", "revoked", "unverified", "stale"])
async def test_only_current_valid_human_confirmation_qualifies(environment: dict[str, Any], result: str) -> None:
    env = environment
    await seed(env)
    if result != "unverified":
        event = await check(env, result=result if result in {"disputed", "needs_review"} else "human_confirmed")
        if result == "revoked":
            async with env["factory"]() as db:
                await verification(db, env).revoke("bank", event["verification_id"], operation_id="revoke", expected_head_version="1", reason="需复核。")
        elif result == "stale":
            async with env["factory"]() as db, db.begin():
                row = await db.get(MemorySourceState, ("bank", 10))
                row.generation += 2
    async with env["factory"]() as db:
        packet = await startup(db, env).build("bank", **scope(env))
        assert packet["items"] == [] and packet["startup_text"] == ""
        assert sum(packet["omitted"]["counts"].values()) == 1


async def test_inventory_scope_filters_before_limit_and_explicit_foreign_id_has_no_body(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    await check(env)
    await seed(env, entry_id="0" * 31 + "1", message_id=101, content="其他主体私密正文。", scope={**env["scope"], "user_id": "other"})
    async with env["factory"]() as db:
        owner = startup(db, env)
        packet = await owner.build("bank", **scope(env), inventory_limit=1)
        assert [item["entry_id"] for item in packet["items"]] == [ENTRY]
        assert packet["selection"]["scanned_count"] == 1 and not packet["selection"]["inventory_truncated"]
        assert "其他主体" not in json.dumps(packet, ensure_ascii=False)
        explicit = await owner.build("bank", **scope(env), entry_ids=["0" * 31 + "1"])
        assert explicit["items"] == [] and explicit["omitted"]["counts"] == {"scope_mismatch": 1}


async def test_session_constrains_every_required_source_and_null_allows_cross_session(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    await check(env)
    async with env["factory"]() as db, db.begin():
        row = await db.get(MemoryEntryRevision, (ENTRY, 1))
        row.message_snapshot = {**row.message_snapshot, "session_id": "new-session"}
    env["physical"][100]["session_id"] = "new-session"
    await check(env, operation_id="fresh")
    async with env["factory"]() as db:
        constrained = await startup(db, env).build("bank", **scope(env, session_id="new-session"))
        assert constrained["items"] == [] and constrained["omitted"]["counts"] == {"source_scope_mismatch": 1}
        assert len((await startup(db, env).build("bank", **scope(env)))["items"]) == 1


async def test_large_early_entry_cannot_starve_later_budget_fitting_entry(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env, entry_id="0" * 31 + "1", message_id=101, content="长但完整的条目；" * 1000 + "禁止生产。")
    await check(env, entry_id="0" * 31 + "1", operation_id="large")
    await seed(env)
    await check(env)
    async with env["factory"]() as db:
        packet = await startup(db, env).build("bank", **scope(env), budget_tokens=2048, max_items=1)
        assert [item["entry_id"] for item in packet["items"]] == [ENTRY]
        assert packet["omitted"]["counts"] == {"budget": 1}
        assert packet["usage"]["estimated_tokens"] <= 2048
        assert not packet["selection"]["selection_complete"]


@pytest.mark.parametrize("change", ["source", "head", "entry"])
async def test_closing_rejects_changed_binding_before_any_body_is_returned(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    env = environment
    await seed(env)
    event = await check(env)
    async with env["factory"]() as db:
        owner = startup(db, env)
        closing = owner._closing

        async def race(memory_id: str, snapshots: list[dict[str, Any]]) -> None:
            if change == "head":
                async with env["factory"]() as writer:
                    await verification(writer, env).revoke("bank", event["verification_id"], operation_id="mid-read-revoke", expected_head_version="1", reason="新证据。")
            else:
                async with env["factory"]() as writer, writer.begin():
                    if change == "source":
                        source = await writer.get(MemorySourceState, ("bank", 10))
                        source.generation += 1
                    else:
                        entry = await writer.get(MemoryEntry, ENTRY)
                        entry.generation += 2
            await closing(memory_id, snapshots)

        monkeypatch.setattr(owner, "_closing", race)
        with pytest.raises(RevisionConflict):
            await owner.build("bank", **scope(env))


async def test_strict_storage_failure_never_looks_like_empty_startup(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    await check(env)

    async def fail(identity: int) -> None:
        raise RuntimeError("strict read failure")

    env["on_read"] = fail
    async with env["factory"]() as db:
        with pytest.raises(RuntimeError, match="strict read failure"):
            await startup(db, env).build("bank", **scope(env))
        with pytest.raises(RevisionNotFound):
            await startup(db, env, actor_id="denied").build("bank", **scope(env))


async def test_version_bound_unicode_excerpt_rejects_same_hash_source_aba(environment: dict[str, Any]) -> None:
    env = environment
    env["scope"] = {"user_id": "", "agent_id": "", "session_id": ""}
    await seed(env)
    text = "首行🙂条件\n尾部禁止生产。"
    env["physical"][10]["content"] = text
    async with env["factory"]() as db, db.begin():
        row = await db.get(MemorySourceState, ("bank", 10))
        row.content_sha256 = content_hash(text)
    args = {"expected_source_revision": 1, "expected_source_generation": 1, "content_sha256": content_hash(text), **env["scope"], "limit": 4}
    pieces, offset = [], 0
    async with env["factory"]() as db:
        owner = startup(db, env)
        while True:
            part = await owner.source_excerpt("bank", 10, offset=offset, **args)
            pieces.append(part["excerpt"])
            assert part["offset_unit"] == "unicode_codepoint" and part["scope"] == env["scope"]
            if part["next_offset"] is None:
                break
            offset = part["next_offset"]
    assert "".join(pieces) == text
    async with env["factory"]() as db:
        with pytest.raises(RevisionValidationError, match="invalid_startup_source_range"):
            await startup(db, env).source_excerpt("bank", 10, offset=len(text) + 1, **args)
    async with env["factory"]() as db, db.begin():
        row = await db.get(MemorySourceState, ("bank", 10))
        row.generation += 2
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="startup_source_changed"):
            await startup(db, env).source_excerpt("bank", 10, **args)


async def test_bound_source_change_during_physical_read_is_rejected(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)

    async def mutate(identity: int) -> None:
        env.pop("on_read")
        async with env["factory"]() as writer, writer.begin():
            row = await writer.get(MemorySourceState, ("bank", 10))
            row.state, row.generation = "forgotten", row.generation + 1

    env["on_read"] = mutate
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="startup_source_changed"):
            await startup(db, env).source_excerpt("bank", 10, expected_source_revision=1, expected_source_generation=1, content_sha256=content_hash("source 10"), **env["scope"])


@pytest.mark.parametrize("result", ["disputed", "needs_review", "revoked"])
async def test_latest_manual_result_change_before_snapshot_has_accurate_omission(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, result: str) -> None:
    env = environment
    await seed(env)
    event = await check(env)
    async with env["factory"]() as db:
        owner = startup(db, env)
        snapshot = owner.verification.snapshot

        async def change_before_snapshot(memory_id: str, entry_id: str, **kwargs: Any) -> dict[str, Any]:
            if result == "revoked":
                async with env["factory"]() as writer:
                    await verification(writer, env).revoke("bank", event["verification_id"], operation_id="changed-result", expected_head_version="1", reason="重新复核。")
            else:
                await check(env, result=result, operation_id="changed-result")
            return await snapshot(memory_id, entry_id, **kwargs)

        monkeypatch.setattr(owner.verification, "snapshot", change_before_snapshot)
        packet = await owner.build("bank", **scope(env))
        expected = "verification_revoked" if result == "revoked" else result
        assert packet["items"] == [] and packet["omitted"]["counts"] == {expected: 1}
