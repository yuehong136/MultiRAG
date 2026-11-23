"""Owned real-SQL target recall and explicit stale-confirmation rejection."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.memory_execution_models import MemoryMaintenanceExecution
from api.db.memory_incremental_models import MemoryMaintenanceCoverage, MemoryMaintenanceDiscovery
from api.db.memory_maintenance_models import MemoryMaintenanceCandidate, MemoryMaintenanceJob
from api.db.memory_revision_models import MemoryEntry, MemoryEntryRevision, MemoryRevisionSource, MemorySourceState
from memory.maintenance import profiles
from memory.maintenance.checkpoints import IncrementalCheckpoints
from memory.maintenance.execution import process_next
from memory.maintenance.incremental import DiscoveryService
from memory.maintenance.jobs import JobService
from memory.maintenance.snapshots import MaintenanceSnapshots
from memory.maintenance.target_conditions import TargetConditions
from memory.maintenance.targets import TargetRecommendations
from memory.revisions.domain import ModelBinding, RevisionConflict, RevisionNotFound, RevisionValidationError, content_hash
from memory.revisions.storage import ManagedMessageStore
from memory.writes.providers import MemoryWriteProviders
from tests.support.memory_maintenance_repository import environment as environment
from tests.support.memory_maintenance_repository import service as explicit_jobs

_ENTRY = "a" * 32
_BINDING = ModelBinding("00000000000000000000000000000001", "controlled-embedding", 2, "binding-hash")


async def seed(env: dict[str, Any]) -> None:
    env.update(messages={}, embeddings=0, recalls=0, binding=_BINDING, proposals=[])
    old = "常用地址为上海；归档保留14天。"
    new = "地址已改为北京，上海地址不再使用。"
    async with env["factory"]() as db, db.begin():
        for identity, content in ((10, old), (20, new)):
            row = await db.get(MemorySourceState, ("bank", identity))
            row.content_sha256 = content_hash(content)
            row.scope = deepcopy(env["scope"])
            env["messages"][identity] = {
                "id": f"bank_{identity}",
                "memory_id": "bank",
                "message_id": identity,
                "message_type": "raw",
                "source_id": 0,
                "content": content,
                "content_embed": [1.0, 0.0],
                "status": True,
                "valid_at": None,
                "invalid_at": None,
                "forget_at": None,
                **env["scope"],
            }
        await add_entry(db, env, _ENTRY, 101, old)
    env["semantic"] = [101]


async def add_entry(db: AsyncSession, env: dict[str, Any], identity: str, message_id: int, content: str, *, scope: dict[str, str] | None = None) -> None:
    actual_scope = scope or env["scope"]
    message = {
        "id": f"bank_{message_id}",
        "memory_id": "bank",
        "message_id": message_id,
        "message_type": "semantic",
        "source_id": 10,
        "content": content,
        "content_embed": [1.0, 0.0],
        "status": True,
        "valid_at": None,
        "invalid_at": None,
        "forget_at": None,
        **actual_scope,
    }
    env["messages"][message_id] = message
    db.add(MemoryEntry(entry_id=identity, memory_id="bank", tenant_id="tenant", legacy_message_id=message_id, current_revision=1, current_message_id=message_id, generation=1, state="active"))
    await db.flush()
    db.add(
        MemoryEntryRevision(
            entry_id=identity,
            revision=1,
            memory_id="bank",
            tenant_id="tenant",
            message_id=message_id,
            content=content,
            content_sha256=content_hash(content),
            parent_revision=None,
            origin="manual",
            assessment={"kind": "user_asserted", "verification_status": "unverified"},
            message_snapshot={**message, "model_binding": _BINDING.to_dict()},
        )
    )
    await db.flush()
    db.add(
        MemoryRevisionSource(
            entry_id=identity,
            revision=1,
            memory_id="bank",
            source_message_id=10,
            source_revision=1,
            source_generation=1,
            content_sha256=content_hash(env["messages"][10]["content"]),
            relation="context",
            locator={"scope": actual_scope},
        )
    )


def services(db: AsyncSession, env: dict[str, Any], *, denied: bool = False) -> tuple[TargetRecommendations, DiscoveryService]:
    jobs = explicit_jobs(db, env, actor_id="denied" if denied else "actor")

    class Storage:
        async def read(self, message_id: int) -> dict[str, Any] | None:
            assert not db.in_transaction(), "physical IO must not retain SQL"
            if callback := env.get("on_read"):
                await callback(message_id)
            return deepcopy(env["messages"].get(message_id))

    async def binding(session: AsyncSession, memory_id: str, dimension: int) -> ModelBinding:
        assert dimension == 2
        return env["binding"]

    async def embed(memory_id: str, messages: list[dict[str, Any]], expected: ModelBinding | None) -> Any:
        assert not db.in_transaction()
        env["embeddings"] += 1
        if callback := env.get("on_embed"):
            await callback()
        return {"vectors": [[1.0, 0.0]], "model_binding": env["binding"]}

    async def recall(memory_id: str, user_id: str, agent_id: str, vector: list[float]) -> list[dict[str, Any]]:
        assert not db.in_transaction() and (user_id, agent_id) == (env["scope"]["user_id"], env["scope"]["agent_id"])
        env["recalls"] += 1
        return [deepcopy(env["messages"][identity]) for identity in env["semantic"]]

    snapshots = MaintenanceSnapshots(db, tenant_id="tenant", storage=cast(ManagedMessageStore, Storage()), count_tokens=jobs.count_tokens)
    conditions = TargetConditions(db, tenant_id="tenant", authorize=jobs.authorize, storage=snapshots.storage, model_binding=binding, snapshots=snapshots)
    jobs.snapshotter, jobs.segment_snapshotter, jobs.validate_inputs, jobs.target_conditions = snapshots.snapshot, snapshots.segment_snapshot, snapshots.validate, conditions

    async def identity(memory_id: str) -> dict[str, str]:
        return {"strategy_sha256": "f" * 64, "model_reference": "controlled-chat", "config_reference": "config-hash"}

    checkpoints = IncrementalCheckpoints(db, tenant_id="tenant", identity=identity)
    jobs.validate_binding, jobs.checkpoint = checkpoints.validate, checkpoints.checkpoint
    recommendation = TargetRecommendations(db, conditions=conditions, embedder=embed, recall=recall, count_tokens=jobs.count_tokens)
    return recommendation, DiscoveryService(db, jobs=jobs, identity=identity, validate_entry=snapshots._ready_entry, segment_planner=snapshots.plan_segments)


def request(env: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {"source_ids": [20], "user_id": env["scope"]["user_id"], "agent_id": env["scope"]["agent_id"], "budget_tokens": 8192, **changes}


def selection(confirmation: dict[str, Any]) -> dict[str, Any]:
    return {"scope": {"user_id": "user", "agent_id": "agent", "session_id": None}, "entry_ids": [_ENTRY], "target_preconditions": confirmation}


async def test_chinese_correction_recommends_current_with_complete_body_and_closure_without_writes(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        target, _ = services(db, env)
        result = await target.recommend("bank", **request(env))
        assert result["targets"][0]["entry_id"] == _ENTRY
        assert result["targets"][0]["content"] == "常用地址为上海；归档保留14天。"
        assert result["targets"][0]["source_versions"][0]["source_id"] == "10"
        assert result["confirmation"]["inputs"][0]["source_id"] == "20"
        assert result["retrieval"]["inventory_complete"] and env["embeddings"] == env["recalls"] == 1
        assert result["budget"]["estimated_tokens"] <= 8192
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceDiscovery)) == 0


async def test_semantic_target_outside_lexical_window_is_not_lost(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db, db.begin():
        entry = await db.get(MemoryEntry, _ENTRY)
        entry.updated_at = entry.updated_at.replace(year=2000)
        for index in range(130):
            await add_entry(db, env, f"{index + 200:032x}", 1000 + index, "不同项目的历史摘要" + str(index))
    async with env["factory"]() as db:
        result = await services(db, env)[0].recommend("bank", **request(env))
        assert result["targets"][0]["entry_id"] == _ENTRY
        assert result["retrieval"]["lexical_inventory_truncated"] and not result["retrieval"]["inventory_complete"]


@pytest.mark.parametrize("change", ["target", "source", "input", "projection", "model"])
async def test_changed_confirmation_rejects_before_plan_creation(environment: dict[str, Any], change: str) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        confirmation = (await services(db, env)[0].recommend("bank", **request(env)))["confirmation"]
    async with env["factory"]() as db, db.begin():
        if change == "target":
            entry = await db.get(MemoryEntry, _ENTRY)
            entry.generation += 1
        elif change in {"source", "input"}:
            source = await db.get(MemorySourceState, ("bank", 10 if change == "source" else 20))
            source.generation += 1
    if change == "projection":
        env["messages"][101]["content_embed"] = [0.0, 1.0]
    if change == "model":
        env["binding"] = ModelBinding("00000000000000000000000000000002", "changed-embedding", 2, "changed-config")
    async with env["factory"]() as db:
        _, discovery = services(db, env)
        with pytest.raises(RevisionConflict):
            await discovery.preview("bank", **selection(confirmation))
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceDiscovery)) == 0


async def test_same_operation_replay_keeps_accepted_target_plan_after_later_change(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        recommender, discovery = services(db, env)
        confirmation = (await recommender.recommend("bank", **request(env)))["confirmation"]
        preview = await discovery.preview("bank", **selection(confirmation))
        first = await discovery.create("bank", operation_id="fixed-target-plan", expected_preview_sha256=preview["preview_sha256"], **selection(confirmation))
        manual = await discovery.jobs.create("bank", operation_id="fixed-target-job", source_ids=[10, 20], entry_ids=[_ENTRY], target_preconditions=confirmation)
    async with env["factory"]() as db, db.begin():
        entry = await db.get(MemoryEntry, _ENTRY)
        entry.generation += 1
    env["messages"][101]["content_embed"] = [0.0, 1.0]
    async with env["factory"]() as db:
        _, discovery = services(db, env)
        again = await discovery.create("bank", operation_id="fixed-target-plan", expected_preview_sha256=preview["preview_sha256"], **selection(confirmation))
        assert again["discovery_id"] == first["discovery_id"] and again["target_preconditions"] == confirmation
        replay = await discovery.jobs.create("bank", operation_id="fixed-target-job", source_ids=[10, 20], entry_ids=[_ENTRY], target_preconditions=confirmation)
        assert replay["job_id"] == manual["job_id"] and replay["target_preconditions"] == confirmation


async def test_model_change_during_embedding_fails_closed_and_foreign_scope_is_not_read(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)

    async def mutate() -> None:
        env["binding"] = ModelBinding("00000000000000000000000000000002", "changed", 2, "different")

    env["on_embed"] = mutate
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="target_model_changed"):
            await services(db, env)[0].recommend("bank", **request(env))
    env.pop("on_embed")
    async with env["factory"]() as db:
        with pytest.raises(RevisionConflict, match="target_scope_mismatch"):
            await services(db, env)[0].recommend("bank", **request(env, user_id="foreign"))
        with pytest.raises(RevisionNotFound):
            await services(db, env, denied=True)[0].recommend("bank", **request(env))


async def test_lexical_mode_never_embeds_and_long_query_or_target_is_not_silently_cut(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        result = await services(db, env)[0].recommend("bank", **request(env, retrieval_mode="lexical"))
        assert result["targets"][0]["entry_id"] == _ENTRY and env["embeddings"] == env["recalls"] == 0


async def test_fresh_recommendation_accepts_ready_current_after_source_aba_but_old_confirmation_does_not(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        old = (await services(db, env)[0].recommend("bank", **request(env)))["confirmation"]
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemorySourceState, ("bank", 10))
        source.generation += 2
    async with env["factory"]() as db:
        recommendations, discovery = services(db, env)
        with pytest.raises(RevisionConflict, match="target_preconditions_changed"):
            await discovery.preview("bank", **selection(old))
        refreshed = await recommendations.recommend("bank", **request(env))
        assert refreshed["targets"][0]["entry_id"] == _ENTRY
        assert refreshed["targets"][0]["source_versions"][0]["source_generation"] == "3"
        ref = await db.scalar(sa.select(MemoryRevisionSource).where(MemoryRevisionSource.entry_id == _ENTRY))
        assert ref.source_generation == 1


async def test_exact_empty_subject_is_preserved_and_foreign_equal_text_is_excluded(environment: dict[str, Any]) -> None:
    env = environment
    env["scope"] = {"user_id": "", "agent_id": " ", "session_id": "old"}
    await seed(env)
    async with env["factory"]() as db, db.begin():
        await add_entry(db, env, "b" * 32, 102, env["messages"][101]["content"], scope={"user_id": "other", "agent_id": " ", "session_id": "old"})
    async with env["factory"]() as db:
        result = await services(db, env)[0].recommend("bank", **request(env, retrieval_mode="lexical"))
        assert [item["entry_id"] for item in result["targets"]] == [_ENTRY]
        assert result["confirmation"]["inputs"][0]["scope"]["user_id"] == ""


async def test_whole_large_target_is_omitted_by_budget_and_source_query_is_never_prefix_truncated(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    body = "上海地址及其前置条件；" * 2000 + "不可丢弃末尾事实。"
    env["messages"][101]["content"] = body
    async with env["factory"]() as db, db.begin():
        revision = await db.get(MemoryEntryRevision, (_ENTRY, 1))
        revision.content, revision.content_sha256 = body, content_hash(body)
    async with env["factory"]() as db:
        result = await services(db, env)[0].recommend("bank", **request(env, retrieval_mode="lexical", budget_tokens=1024))
        assert result["targets"] == [] and result["omitted"]["budget"] == 1 and result["truncated"]
        assert result["budget"]["estimated_tokens"] <= 1024
    env["messages"][20]["content"] = "完整新来源前提；" * 2000
    async with env["factory"]() as db, db.begin():
        source = await db.get(MemorySourceState, ("bank", 20))
        source.content_sha256 = content_hash(env["messages"][20]["content"])
    async with env["factory"]() as db:
        recommender = services(db, env)[0]
        with pytest.raises(RevisionValidationError, match="target_query_budget_exceeded"):
            await recommender.recommend("bank", **request(env, query_budget_tokens=256))
        focused = await recommender.recommend("bank", **request(env, query="上海地址已改为北京", retrieval_mode="lexical", query_budget_tokens=256))
        assert focused["retrieval"]["query_origin"] == "explicit" and not focused["retrieval"]["query_truncated"]
        assert focused["confirmation"]["inputs"][0]["content_sha256"] == content_hash(env["messages"][20]["content"])
        assert env["embeddings"] == 0


async def test_single_target_closure_includes_input_capacity_and_storage_failure_never_becomes_empty_success(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db, db.begin():
        for source_id in range(30, 61):
            text = f"必要依据{source_id}"
            db.add(
                MemorySourceState(
                    memory_id="bank", tenant_id="tenant", source_message_id=source_id, source_revision=1, generation=1, content_sha256=content_hash(text), state="active", scope=env["scope"]
                )
            )
            db.add(
                MemoryRevisionSource(
                    entry_id=_ENTRY,
                    revision=1,
                    memory_id="bank",
                    source_message_id=source_id,
                    source_revision=1,
                    source_generation=1,
                    content_sha256=content_hash(text),
                    relation="context",
                    locator={"scope": env["scope"]},
                )
            )
    async with env["factory"]() as db:
        result = await services(db, env)[0].recommend("bank", **request(env, retrieval_mode="lexical"))
        assert not result["targets"] and result["omitted"]["closure_limit"] == 1

    async def fail(identity: int) -> None:
        raise RuntimeError("controlled storage failure")

    env["on_read"] = fail
    async with env["factory"]() as db:
        with pytest.raises(RuntimeError, match="controlled storage failure"):
            await services(db, env)[0].recommend("bank", **request(env))


async def test_confirmed_targets_cannot_broaden_scope_or_add_another_principal(environment: dict[str, Any]) -> None:
    env = environment
    await seed(env)
    async with env["factory"]() as db:
        confirmation = (await services(db, env)[0].recommend("bank", **request(env)))["confirmation"]
        _, discovery = services(db, env)
        invalid = selection(confirmation)
        invalid["scope"]["user_id"] = None
        with pytest.raises(RevisionValidationError, match="target_scope_mismatch"):
            await discovery.preview("bank", **invalid)
    foreign_scope = {**env["scope"], "user_id": "foreign"}
    content = "其他主体的原始记录。"
    env["messages"][30] = {**env["messages"][20], "id": "bank_30", "message_id": 30, "content": content, **foreign_scope}
    async with env["factory"]() as db, db.begin():
        db.add(
            MemorySourceState(memory_id="bank", tenant_id="tenant", source_message_id=30, source_revision=1, generation=1, content_sha256=content_hash(content), state="active", scope=foreign_scope)
        )
    async with env["factory"]() as db:
        _, discovery = services(db, env)
        with pytest.raises(RevisionConflict, match="target_scope_mismatch"):
            await discovery.jobs.create("bank", operation_id="foreign-extra", source_ids=[10, 20, 30], entry_ids=[_ENTRY], target_preconditions=confirmation)
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 0


@pytest.mark.parametrize("changed_binding", ["system", "input_contract"])
async def test_analysis_protocol_change_rediscoveries_reject_queued_child_without_losing_sealed_handles(environment: dict[str, Any], monkeypatch: pytest.MonkeyPatch, changed_binding: str) -> None:
    env = environment
    await seed(env)
    current_binding = profiles._SYSTEM if changed_binding == "system" else profiles.ANALYSIS_INPUT_CONTRACT
    previous_binding = current_binding + "\nPrevious analyst strategy." if changed_binding == "system" else "whole_source_json"
    binding_name = "_SYSTEM" if changed_binding == "system" else "ANALYSIS_INPUT_CONTRACT"
    assert previous_binding != current_binding

    class PreparedConfiguration:
        async def _prepared(self, memory_id: str, capability: str) -> tuple[Any, Any]:
            assert (memory_id, capability) == ("bank", "chat")
            return {}, SimpleNamespace(model_config={"llm_name": "controlled", "llm_factory": "chat"})

        @staticmethod
        def _chat_fingerprint(settings: Any, prepared: Any) -> str:
            return "config-hash"

    async def identity(memory_id: str) -> dict[str, str]:
        # Exercise the actual SYSTEM/input-contract identity, not a supplied hash.
        return await profiles.analysis_identity(cast(MemoryWriteProviders, PreparedConfiguration()), memory_id)

    def discoveries(db: AsyncSession) -> DiscoveryService:
        _, owner = services(db, env)
        checkpoints = IncrementalCheckpoints(db, tenant_id="tenant", identity=identity)
        owner.identity = identity
        owner.jobs.validate_binding, owner.jobs.checkpoint = checkpoints.validate, checkpoints.checkpoint
        return owner

    options = {"scope": {"user_id": "user", "agent_id": "agent", "session_id": None}, "entry_ids": [], "batch_size": 32, "max_sources": 2}
    monkeypatch.setattr(profiles, binding_name, previous_binding)
    async with env["factory"]() as db:
        owner = discoveries(db)
        old_preview = await owner.preview("bank", **options)
        sealed = await owner.create("bank", operation_id="sealed-old-strategy", expected_preview_sha256=old_preview["preview_sha256"], **options)
        submitted = await owner.run("bank", sealed["discovery_id"], operation_id="submit-old-strategy", limit=1)
        batch = submitted["batches"][0]
        queued = await owner.jobs.get("bank", batch["job_id"])
        assert queued["execution"]["state"] == "queued"
        assert (await owner.preview("bank", **options))["counts"]["inflight"] == 2

    monkeypatch.setattr(profiles, binding_name, current_binding)
    async with env["factory"]() as db:
        owner = discoveries(db)
        fresh = await owner.preview("bank", **options)
        assert fresh["profile"]["strategy_sha256"] != sealed["profile"]["strategy_sha256"]
        assert fresh["profile_sha256"] != sealed["profile_sha256"]
        assert fresh["counts"]["eligible"] == 2 and fresh["counts"]["covered"] == 0
        assert fresh["counts"]["inflight"] == 0
        with pytest.raises(RevisionConflict, match="incremental_analysis_profile_changed"):
            await owner.run("bank", sealed["discovery_id"], operation_id="new-command-old-profile", limit=1)

    async def jobs(db: AsyncSession, memory_id: str, *, actor_id: str) -> JobService:
        assert (memory_id, actor_id) == ("bank", "actor")
        return discoveries(db).jobs

    assert await process_next(env["factory"], jobs, worker_id="target-strategy-sql-test")
    assert env["analyses"] == 0
    async with env["factory"]() as db:
        execution = await db.get(MemoryMaintenanceExecution, batch["job_id"])
        assert execution.state == "failed" and execution.error_code == "incremental_analysis_profile_changed"
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCandidate)) == 0
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceCoverage).where(MemoryMaintenanceCoverage.state == "analyzed")) == 0
        await db.rollback()
        owner = discoveries(db)
        restored = await owner.get("bank", sealed["discovery_id"])
        replay = await owner.create("bank", operation_id="sealed-old-strategy", expected_preview_sha256=old_preview["preview_sha256"], **options)
        repeated = await owner.run("bank", sealed["discovery_id"], operation_id="submit-old-strategy", limit=1)
        for value in (restored, replay, repeated):
            assert value["discovery_id"] == sealed["discovery_id"]
            assert value["profile_sha256"] == sealed["profile_sha256"]
            assert value["batches"][0]["job_id"] == batch["job_id"]
            assert value["batches"][0]["analysis_operation_id"] == batch["analysis_operation_id"]
        operation = await owner.jobs.operation("bank", batch["analysis_operation_id"])
        assert operation["operation_id"] == batch["analysis_operation_id"]
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceJob)) == 1
        assert await db.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceExecution)) == 1
    assert env["analyses"] == 0
