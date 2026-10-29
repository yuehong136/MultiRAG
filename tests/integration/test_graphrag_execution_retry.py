"""Configured retry through real API/SQL/Redis/Milvus and worker entry point."""

from copy import deepcopy
from typing import Any

import pytest
import sqlalchemy as sa

from api.db.db_models import Knowledgebase
from common import settings
from core.graphrag.general import index
from core.graphrag.phase_markers import PHASE_COMMUNITY, PHASE_RESOLUTION
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, api, observe, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api


def save_budgets(env: dict[str, Any]) -> dict[str, Any]:
    config = deepcopy(env["parser_config"])
    config["graphrag"].update(
        retry_attempts=2, retry_backoff_seconds=0, retry_backoff_max_seconds=0, merge_timeout_seconds=0, resolution_timeout_seconds=0, community_timeout_seconds=0, lock_acquire_timeout_seconds=0
    )
    reply = request_api(env, "PUT", f"/datasets/{env['kb']}", credential=env["jwt"][0], payload={"parser_config": config})
    assert reply.status_code == 200 and reply.json()["code"] == 0, reply.text
    with env["engine"].connect() as db:
        stored = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == env["kb"]))
    assert all(stored["graphrag"][key] == value for key, value in config["graphrag"].items())
    env["parser_config"] = stored
    return stored


@pytest.mark.parametrize("stage", ["generate_subgraph", "merge_subgraph", "resolve_entities", "extract_community"])
async def test_postpublication_failure_retries_without_duplicate_merge(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    env = graphrag_api
    save_budgets(env)
    original = getattr(index, stage)
    calls = 0

    async def interrupted(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        result = await original(*args, **kwargs)
        if calls == 1:
            raise RuntimeError("controlled interruption after checked storage publication")
        return result

    monkeypatch.setattr(index, stage, interrupted)
    task = api(env, "POST")["data"]["task_id"]
    await worker(env, task, Model())
    completed = observe(env, "retried postpublication interruption " + stage)
    assert calls == (1 if stage in {"generate_subgraph", "merge_subgraph"} else 2)
    assert DocumentExtractor.calls == [env["documents"][0]]
    assert not completed["writer_active"] and all(completed["markers"].values())
    assert next(row for row in completed["task_rows"] if row["id"] == task)["progress"] == 1
    graph = await index.get_graph(env["tenant"], env["kb"])
    assert graph is not None and graph.graph["source_id"] == [env["documents"][0]]
    assert len(graph.nodes()) == 2 and sorted(edge["weight"] for _, _, edge in graph.edges(data=True)) == [2]
    # A new real API queue invocation sees committed phase markers and graph.
    monkeypatch.setattr(index, stage, original)
    await worker(env, api(env, "POST")["data"]["task_id"], Model())
    resumed = observe(env, "resumed after retry " + stage)
    assert resumed["rows"] == completed["rows"] and DocumentExtractor.calls == [env["documents"][0]]


async def test_exhausted_community_storage_failure_leaves_phase_retryable(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = graphrag_api
    save_budgets(env)
    original = settings.docStoreConn.insert

    def fail_report(chunks: list[dict[str, Any]], *args: Any) -> list[str]:
        if any(chunk.get("knowledge_graph_kwd") == "community_report" for chunk in chunks):
            return ["controlled native community write failure"]
        return original(chunks, *args)

    monkeypatch.setattr(settings.docStoreConn, "insert", fail_report)
    task = api(env, "POST")["data"]["task_id"]
    initial_model = Model()
    with pytest.raises(RuntimeError, match="controlled native"):
        await worker(env, task, initial_model)
    failed = observe(env, "community storage retry exhausted")
    assert failed["markers"][PHASE_RESOLUTION] and not failed["markers"][PHASE_COMMUNITY]
    assert not failed["writer_active"] and not any(row.get("knowledge_graph_kwd") == "community_report" for row in failed["rows"])
    assert next(row for row in failed["task_rows"] if row["id"] == task)["progress"] == -1
    cache_keys = [key for key in env["checkpoint_keys"] if key.startswith("graphrag:checkpoint:")]
    assert any(env["redis"].exists(key) for key in cache_keys)
    assert initial_model.community == 1
    monkeypatch.setattr(settings.docStoreConn, "insert", original)
    model = Model()
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    recovered = observe(env, "community storage recovered with checked graph resume")
    assert all(recovered["markers"].values()) and not recovered["writer_active"]
    # The model report succeeded before storage failed. A new queued task must
    # publish that durable result without a second paid generation.
    assert model.resolution == model.community == 0 and DocumentExtractor.calls == [env["documents"][0]]
    assert not any(env["redis"].exists(key) for key in cache_keys)
    env["journal"].event("storage-recovery-calls-readback", failed_run_community_calls=initial_model.community, recovered_run_community_calls=model.community, checkpoint_keys_absent=True)


async def test_phase_timeout_drains_real_write_then_reuses_checkpoint(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    env = graphrag_api
    config = save_budgets(env)
    config["graphrag"].update(build_subgraph_timeout_per_chunk_seconds=1, build_subgraph_min_timeout_seconds=1)
    reply = request_api(env, "PUT", f"/datasets/{env['kb']}", credential=env["jwt"][0], payload={"parser_config": config})
    assert reply.json()["code"] == 0
    started, release, finished = (threading.Event() for _ in range(3))
    original = settings.docStoreConn.insert
    writes = 0

    def slow_checkpoint(chunks: list[dict[str, Any]], *args: Any) -> list[str]:
        nonlocal writes
        if chunks[0].get("knowledge_graph_kwd") == "subgraph" and not started.is_set():
            writes += 1
            started.set()
            assert release.wait(20)
            result = original(chunks, *args)
            finished.set()
            return result
        return original(chunks, *args)

    monkeypatch.setattr(settings.docStoreConn, "insert", slow_checkpoint)
    task = api(env, "POST")["data"]["task_id"]
    running = asyncio.create_task(worker(env, task, Model()))
    try:
        assert await asyncio.to_thread(started.wait, 10)
        await asyncio.sleep(1.2)
        pending = observe(env, "timeout drains native checkpoint write before retry")
        assert pending["writer_active"] and not running.done() and not finished.is_set()
        assert writes == 1 and DocumentExtractor.calls == [env["documents"][0]]
        release.set()
        await asyncio.wait_for(running, 20)
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    completed = observe(env, "timeout recovery reused checked native checkpoint")
    assert finished.is_set() and not completed["writer_active"] and all(completed["markers"].values())
    assert DocumentExtractor.calls == [env["documents"][0]]
    assert any("failed attempt 1/2" in item.get("msg", "") and "TimeoutError" in item["msg"] for item in env["record"]["messages"])


async def test_real_writer_timeout_and_cancel_leave_owner_lease_untouched(graphrag_api: dict[str, Any]) -> None:
    from common.exceptions import TaskCanceledException
    from core.graphrag.persistence import GraphRAGLease, current_task

    env = graphrag_api
    task = api(env, "POST")["data"]["task_id"]
    before = observe(env, "before writer contention")
    async with GraphRAGLease(env["tenant"], env["kb"]) as owner:
        with pytest.raises(TimeoutError):
            async with GraphRAGLease(env["tenant"], env["kb"], wait=0.1):
                pytest.fail("contending writer acquired a held lease")
        owner.check()
        env["redis"].set(task + "-cancel", "x")
        token = current_task.set(task)
        try:
            with pytest.raises(TaskCanceledException):
                async with GraphRAGLease(env["tenant"], env["kb"], wait=60):
                    pytest.fail("cancelled waiter acquired a held lease")
        finally:
            current_task.reset(token)
        owner.check()
        assert observe(env, "owner survived timed-out and cancelled waiters")["writer_active"]
    after = observe(env, "owner released only its own lease")
    assert not after["writer_active"] and after["rows"] == before["rows"] and after["task_rows"] == before["task_rows"]
