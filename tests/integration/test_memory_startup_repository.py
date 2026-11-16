"""Read-only fresh startup selection against real SQL, controlled physical IO."""

import json
from typing import Any

import pytest
import sqlalchemy as sa

from api.db.db_models import Base
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_verification_models import MemoryVerificationHead, MemoryVerificationRecord, MemoryVerificationSource
from api.db.memory_writer_models import MemoryWriterAdmission
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
        phase = owner._metadata_phase

        async def change_before_snapshot(memory_id: str, entry_ids: list[str], *, stage: Any) -> Any:
            if stage != "after_first":
                return await phase(memory_id, entry_ids, stage=stage)
            if result == "revoked":
                async with env["factory"]() as writer:
                    await verification(writer, env).revoke("bank", event["verification_id"], operation_id="changed-result", expected_head_version="1", reason="重新复核。")
            else:
                await check(env, result=result, operation_id="changed-result")
            return await phase(memory_id, entry_ids, stage=stage)

        monkeypatch.setattr(owner, "_metadata_phase", change_before_snapshot)
        packet = await owner.build("bank", **scope(env))
        expected = "verification_revoked" if result == "revoked" else result
        assert packet["items"] == [] and packet["omitted"]["counts"] == {expected: 1}


@pytest.mark.parametrize("admissions", [0, 1, 2])
@pytest.mark.parametrize("published", [False, True])
async def test_batch_consolidated_readiness_requires_one_admission_and_ready_projection(environment: dict[str, Any], admissions: int, published: bool) -> None:
    env = environment
    await seed(env)
    await check(env)
    async with env["factory"]() as db, db.begin():
        row = await db.get(MemoryEntryRevision, (ENTRY, 1))
        row.origin = "consolidated"
        db.add(
            MemoryPublicationOutbox(
                id="p" * 32,
                memory_id="bank",
                tenant_id="tenant",
                operation_id="projection",
                entry_id=ENTRY,
                revision=1,
                message_id=100,
                bank_generation=1,
                state="ready" if published else "staged",
                payload={},
            )
        )
        for index in range(admissions):
            db.add(
                MemoryWriterAdmission(
                    memory_id="bank",
                    operation_id=f"publication-{index}",
                    tenant_id="tenant",
                    actor_id="actor",
                    kind="dream_publish",
                    request_sha256="0" * 64,
                    writer_generation=1,
                    state="finished",
                    publication_state="ready",
                    message_ids=[100],
                )
            )
    async with env["factory"]() as db:
        owner = startup(db, env)
        if admissions == 1 and published:
            assert (await owner.build("bank", **scope(env)))["items"][0]["entry_id"] == ENTRY
            assert (await verification(db, env).snapshot("bank", ENTRY))["current_record"]["valid_for_current"]
        else:
            packet = await owner.build("bank", **scope(env))
            assert packet["items"] == [] and packet["omitted"]["counts"] == {"not_ready": 1}
            with pytest.raises(RevisionConflict, match="dreaming_target_not_ready"):
                await verification(db, env).snapshot("bank", ENTRY)


async def test_per_entry_source_sentinel_does_not_truncate_or_starve_other_closures(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    await check(env)
    invalid = "0" * 31 + "1"
    await seed(env, entry_id=invalid, message_id=101, content="不得作为已完整核验的条目返回。")
    async with env["factory"]() as db, db.begin():
        for identity in range(1000, 1032):
            db.add(
                MemoryRevisionSource(
                    entry_id=invalid,
                    revision=1,
                    memory_id="bank",
                    source_message_id=identity,
                    source_revision=1,
                    source_generation=1,
                    content_sha256=content_hash(str(identity)),
                    locator={"scope": env["scope"]},
                )
            )
    async with env["factory"]() as db:
        packet = await startup(db, env).build("bank", **scope(env))
    assert [item["entry_id"] for item in packet["items"]] == [ENTRY]
    assert packet["omitted"]["counts"] == {"source_closure_limit": 1} and packet["selection"]["physical_reads"] == 4
    assert "不得作为" not in json.dumps(packet, ensure_ascii=False)


async def test_stored_verification_source_binding_is_compared_not_only_record_hash(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    event = await check(env)
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemoryVerificationSource, (event["verification_id"], 10))
        source.projection_sha256 = "0" * 64
    async with env["factory"]() as db:
        packet = await startup(db, env).build("bank", **scope(env))
    assert packet["items"] == [] and packet["omitted"]["counts"] == {"verification_stale": 1}


@pytest.mark.parametrize(("changed_phase", "reads"), [("after_first", 2), ("after_second", 4)])
async def test_batch_fresh_phase_rejects_source_generation_change(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, changed_phase: str, reads: int) -> None:
    env = environment
    await seed(env)
    await check(env)
    env["reads"] = 0
    async with env["factory"]() as db:
        owner = startup(db, env)
        phase = owner._metadata_phase

        async def change(memory_id: str, entry_ids: list[str], *, stage: Any) -> Any:
            if stage == changed_phase:
                async with env["factory"]() as writer, writer.begin():
                    source = await writer.get(MemorySourceState, ("bank", 10))
                    source.generation += 2
            return await phase(memory_id, entry_ids, stage=stage)

        monkeypatch.setattr(owner, "_metadata_phase", change)
        with pytest.raises(RevisionConflict, match="verification_binding_changed"):
            await owner.build("bank", **scope(env))
    assert env["reads"] == reads


@pytest.mark.parametrize("state", [None, "active", "inactive"])
async def test_empty_inventory_preserves_unmanaged_and_inactive_bank_contract(environment: dict[str, Any], state: str | None) -> None:
    env = environment
    async with env["factory"]() as db, db.begin():
        bank = await db.get(ManagedMemoryState, "bank")
        if state is None:
            await db.delete(bank)
        else:
            bank.state = state
    async with env["factory"]() as db:
        if state == "inactive":
            with pytest.raises(RevisionConflict, match="startup_inventory_changed"):
                await startup(db, env).build("bank", **scope(env))
        else:
            packet = await startup(db, env).build("bank", **scope(env))
            assert packet["items"] == [] and packet["selection"]["physical_reads"] == 0


async def test_redaction_after_pre_reservation_does_not_rescan_late_candidates(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.support.memory_startup_scale import seed_inventory

    env = environment
    identities = await seed_inventory(env, size=65, confirmed=65)
    env["reads"] = 0
    async with env["factory"]() as db:
        owner = startup(db, env)
        phase = owner._metadata_phase

        async def redact(memory_id: str, entry_ids: list[str], *, stage: Any) -> Any:
            if stage == "after_first":
                async with env["factory"]() as writer, writer.begin():
                    head = await writer.get(MemoryVerificationHead, ("bank", identities[0]))
                    row = await writer.get(MemoryVerificationRecord, head.verification_id)
                    row.state, row.notes, row.applicability, row.evidence = "redacted", None, None, []
            return await phase(memory_id, entry_ids, stage=stage)

        monkeypatch.setattr(owner, "_metadata_phase", redact)
        packet = await owner.build("bank", **scope(env), inventory_limit=128, max_items=16, budget_tokens=32768)
    assert [item["entry_id"] for item in packet["items"]] == identities[1:17]
    assert packet["selection"]["physical_reads"] == env["reads"] == 254
    assert packet["selection"]["read_limited"] and not packet["selection"]["selection_complete"]
    assert packet["omitted"]["counts"] == {"verification_redacted": 1, "max_items": 47, "physical_read_limit": 1}
