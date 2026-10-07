"""Real storage/Redis GraphRAG lifecycle with controlled extraction/model responses."""

import asyncio
import json
import subprocess
from typing import Any
from uuid import uuid4

import pytest

from common import settings
from common.exceptions import TaskCanceledException
from core.graphrag import utils
from core.graphrag.general import index
from core.graphrag.phase_markers import PHASE_COMMUNITY, PHASE_RESOLUTION, clear_phase_markers, phase_key
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, add_source_document, api, native_rows, observe, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api


async def test_pause_resume_new_content_wipe_and_marker_readback(graphrag_api: dict[str, Any]) -> None:
    env = graphrag_api
    first = api(env, "POST")
    assert first["code"] == 0
    task_id = first["data"]["task_id"]
    model = Model()
    model.pause = True
    running = asyncio.create_task(worker(env, task_id, model))
    try:
        started = asyncio.create_task(model.started.wait())
        try:
            done, _ = await asyncio.wait([started, running], timeout=30, return_when=asyncio.FIRST_COMPLETED)
            if running in done:
                await running
            assert started in done, env["record"].get("messages")
        finally:
            started.cancel()
            await asyncio.gather(started, return_exceptions=True)
        before = observe(env, "resolution persisted before pause")
        assert before["markers"][PHASE_RESOLUTION] and not before["markers"][PHASE_COMMUNITY]
        graph_before = [row for row in before["rows"] if row.get("knowledge_graph_kwd") == "graph"]
        assert len(graph_before) == 1
        assert api(env, "DELETE", wipe=False) == {"code": 0, "data": {}}
        assert env["redis"].get(task_id + "-cancel")
        paused = observe(env, "pause requested while model active")
        assert paused["rows"] == before["rows"] and paused["markers"] == before["markers"]
        assert paused["dataset"]["graphrag_task_id"] == task_id
        assert api(env, "GET")["data"]["id"] == task_id
        assert api(env, "POST")["code"] != 0
        model.release.set()
        with pytest.raises(TaskCanceledException):
            await asyncio.wait_for(running, 30)
    finally:
        model.release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    settled = observe(env, "pause settled")
    assert not settled["writer_active"]
    assert settled["task_rows"][0]["progress"] == -1
    next_task = api(env, "POST")
    assert next_task["code"] == 0
    model.pause = False
    await worker(env, next_task["data"]["task_id"], model)
    resumed = observe(env, "resumed pending community only")
    assert all(resumed["markers"].values())
    assert model.resolution == 1 and model.community == 2
    assert DocumentExtractor.calls == [env["documents"][0]]
    reports = [row["pk"] for row in resumed["rows"] if row.get("knowledge_graph_kwd") == "community_report"]
    assert reports
    # Re-run skips both successfully persisted phases, with stable report IDs.
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    assert model.resolution == 1 and model.community == 2
    assert reports == [row["pk"] for row in native_rows(env) if row.get("knowledge_graph_kwd") == "community_report"]
    # A new content source in the same dataset invalidates both phase hints.
    new_doc = add_source_document(env)
    await worker(env, api(env, "POST")["data"]["task_id"], model, [env["documents"][0], new_doc])
    new = observe(env, "new content reran phases")
    assert model.resolution == 2 and model.community == 3
    assert DocumentExtractor.calls == [env["documents"][0], new_doc]
    checkpoint = json.loads(next(row["content_with_weight"] for row in new["rows"] if row.get("knowledge_graph_kwd") == "graph"))
    assert set(checkpoint["graph"]["source_id"]) == {env["documents"][0], new_doc}
    assert reports == [row["pk"] for row in new["rows"] if row.get("knowledge_graph_kwd") == "community_report"]
    detail = request_api(env, "GET", f"/datasets/{env['kb']}", credential=env["jwt"][0]).json()
    assert detail["code"] == 0 and detail["data"]["chunk_count"] == detail["data"]["document_count"] == 2
    assert api(env, "DELETE") == {"code": 0, "data": {}}
    wiped = observe(env, "wipe removed every graph kind and both markers")
    assert not any(row.get("knowledge_graph_kwd") in {"graph", "subgraph", "entity", "relation", "community_report"} for row in wiped["rows"])
    assert not any(wiped["markers"].values()) and wiped["dataset"]["graphrag_task_id"] == ""
    assert len(wiped["rows"]) == 2  # source chunks survive


@pytest.mark.parametrize("failure", ["query", "insert", "prune"])
async def test_failed_phase_does_not_delete_old_reports_or_set_marker(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = graphrag_api
    model = Model()
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    before = observe(env, "successful baseline before " + failure)
    clear_phase_markers(env["kb"])
    store = settings.docStoreConn
    old_rows = native_rows(env)
    old_reports = [row for row in old_rows if row.get("knowledge_graph_kwd") == "community_report"]
    assert old_reports
    # Force a pending community phase, while retaining resolution success.
    persisted = await utils.get_graph(env["tenant"], env["kb"])
    from core.graphrag.phase_markers import set_phase_marker

    set_phase_marker(env["kb"], PHASE_RESOLUTION, index.phase_revision(persisted, env["parser_config"]))
    task = api(env, "POST")["data"]["task_id"]
    if failure == "query":
        original = store.graph_rows

        def fail_query(name: str, kb: str, kinds: list[str]) -> list[dict[str, Any]]:
            if kinds == ["community_report"]:
                raise RuntimeError("injected community query failure")
            return original(name, kb, kinds)

        monkeypatch.setattr(store, "graph_rows", fail_query)
    elif failure == "insert":
        original_insert = store.insert

        def fail_insert(chunks: list[dict[str, Any]], *args: Any) -> list[str]:
            if chunks[0].get("knowledge_graph_kwd") == "community_report":
                return ["injected insert failure"]
            return original_insert(chunks, *args)

        monkeypatch.setattr(store, "insert", fail_insert)
    else:
        stale = {**old_reports[0], "pk": uuid4().hex, "id": uuid4().hex}
        stale["id"] = stale["pk"]
        env["reader"].upsert(env["collection"], [stale])
        monkeypatch.setattr(store, "graph_delete_rows", lambda *args: (_ for _ in ()).throw(RuntimeError("injected prune failure")))
    with pytest.raises(RuntimeError, match=r"injected|insert failed"):
        await worker(env, task, model)
    after = observe(env, "failed " + failure)
    assert after["markers"][PHASE_RESOLUTION] and not after["markers"][PHASE_COMMUNITY]
    assert not after["writer_active"]
    assert {row["pk"] for row in after["rows"]} >= {row["pk"] for row in old_rows}
    if failure in {"query", "insert"}:
        assert after["rows"] == before["rows"]
    assert env["redis"].get(phase_key(env["kb"], PHASE_COMMUNITY)) is None


def test_owned_listener_smoke(graphrag_api: dict[str, Any]) -> None:
    result = subprocess.run(["make", "smoke"], env={**__import__("os").environ, "SMOKE_BASE_URL": graphrag_api["base"]}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("failure", ["query", "insert"])
async def test_failed_graph_publication_preserves_prior_checkpoint(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env, model = graphrag_api, Model()
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    old = next(row for row in native_rows(env) if row.get("knowledge_graph_kwd") == "graph")
    new_doc = add_source_document(env)
    task = api(env, "POST")["data"]["task_id"]
    store = settings.docStoreConn
    if failure == "query":
        original = store.graph_rows

        def fail_query(name: str, kb: str, kinds: list[str]) -> list[dict[str, Any]]:
            if kinds == ["graph", "subgraph", "entity", "relation"]:
                raise RuntimeError("injected graph snapshot failure")
            return original(name, kb, kinds)

        monkeypatch.setattr(store, "graph_rows", fail_query)
    else:
        insert = store.insert

        def fail_insert(chunks: list[dict[str, Any]], *args: Any) -> list[str]:
            if chunks[0].get("knowledge_graph_kwd") == "graph":
                return ["injected checkpoint write failure"]
            return insert(chunks, *args)

        monkeypatch.setattr(store, "insert", fail_insert)
    with pytest.raises(RuntimeError, match=r"injected|insert failed"):
        await worker(env, task, model, [env["documents"][0], new_doc])
    after = observe(env, "failed graph publication " + failure)
    assert next(row for row in after["rows"] if row.get("knowledge_graph_kwd") == "graph") == old
    assert not any(after["markers"].values()) and not after["writer_active"]


async def test_pause_drains_real_inflight_store_write_before_releasing_writer(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    env, model = graphrag_api, Model()
    await worker(env, api(env, "POST")["data"]["task_id"], model)
    old = next(row for row in native_rows(env) if row.get("knowledge_graph_kwd") == "graph")
    new_doc = add_source_document(env)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    insert = settings.docStoreConn.insert

    def slow_insert(chunks: list[dict[str, Any]], *args: Any) -> list[str]:
        if chunks[0].get("knowledge_graph_kwd") == "subgraph" and chunks[0].get("source_id") == [new_doc]:
            started.set()
            assert release.wait(30)
            result = insert(chunks, *args)
            finished.set()
            return result
        return insert(chunks, *args)

    monkeypatch.setattr(settings.docStoreConn, "insert", slow_insert)
    task = api(env, "POST")["data"]["task_id"]
    running = asyncio.create_task(worker(env, task, model, [env["documents"][0], new_doc]))
    try:
        assert await asyncio.to_thread(started.wait, 20)
        assert api(env, "DELETE", wipe=False)["code"] == 0
        pending = observe(env, "pause while native store write is pending")
        assert pending["writer_active"] and not running.done() and not finished.is_set()
        assert api(env, "POST")["code"] != 0
        release.set()
        with pytest.raises(TaskCanceledException):
            await asyncio.wait_for(running, 20)
        assert finished.is_set()
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    after = observe(env, "native write settled before cancellation returned")
    assert not after["writer_active"]
    assert next(row for row in after["rows"] if row.get("knowledge_graph_kwd") == "graph") == old
    assert any(row.get("knowledge_graph_kwd") == "subgraph" and row.get("source_id") == new_doc for row in after["rows"])
    monkeypatch.setattr(settings.docStoreConn, "insert", insert)
    await worker(env, api(env, "POST")["data"]["task_id"], model, [env["documents"][0], new_doc])
    assert DocumentExtractor.calls == [env["documents"][0], new_doc]
    assert model.resolution == 2 and model.community == 2
    observe(env, "resumed from cancelled write's persisted subgraph")


async def test_writer_lease_renews_and_rejects_competing_owner(graphrag_api: dict[str, Any]) -> None:
    from core.graphrag.persistence import GraphRAGLease, writer_key

    env = graphrag_api
    async with GraphRAGLease(env["tenant"], env["kb"], ttl=1) as lease:
        await asyncio.sleep(1.3)
        assert env["redis"].pttl(writer_key(env["tenant"], env["kb"])) > 0
        lease.check()
        with pytest.raises(RuntimeError, match="writer is still active"):
            with GraphRAGLease(env["tenant"], env["kb"], wait=0.0):
                pytest.fail("competing writer acquired the same lease")
        env["redis"].delete(writer_key(env["tenant"], env["kb"]))
        with pytest.raises(RuntimeError, match="lease was lost"):
            lease.check()
