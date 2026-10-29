"""Real storage/Redis GraphRAG lifecycle with controlled extraction/model responses."""

import asyncio
import json
import subprocess
from datetime import datetime
from itertools import pairwise
from typing import Any
from uuid import uuid4

import networkx as nx
import pytest
import sqlalchemy as sa

from api.apps.services import dataset_api_service
from api.db.db_models import Knowledgebase
from common import settings
from common.exceptions import TaskCanceledException
from core.graphrag import utils
from core.graphrag.general import index
from core.graphrag.phase_markers import PHASE_COMMUNITY, PHASE_RESOLUTION, clear_phase_markers, phase_key
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, add_source_document, api, native_rows, observe, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api
from tests.support.smoke import smoke_command


@pytest.mark.parametrize("endpoint", ["knowledge_graph", "graph", "index?type=graph"])
async def test_graph_delete_clears_task_finish_and_ui_readback(graphrag_api: dict[str, Any], endpoint: str) -> None:
    env = graphrag_api
    await worker(env, api(env, "POST")["data"]["task_id"], Model())
    # The controlled GraphRAG helper runs the graph entry point, not the outer
    # executor that normally records the KB completion timestamp.
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(graphrag_task_finish_at=datetime(2026, 1, 1)))
    before = observe(env, "completed before " + endpoint)
    assert before["dataset"]["graphrag_task_id"] and before["dataset"]["graphrag_task_finish_at"]
    assert any(before["markers"].values())
    path = f"/datasets/{env['kb']}/{endpoint}"
    denied = request_api(env, "DELETE", path, credential=env["jwt"][1]).json()
    assert denied["code"] != 0
    unchanged = observe(env, "denied " + endpoint)
    assert unchanged["rows"] == before["rows"] and unchanged["dataset"] == before["dataset"] and unchanged["task_rows"] == before["task_rows"]
    reply = request_api(env, "DELETE", path, credential=env["jwt"][0]).json()
    assert reply == {"code": 0, "data": True if endpoint == "knowledge_graph" else {}}
    after = observe(env, "cleared " + endpoint)
    assert after["dataset"]["graphrag_task_id"] == "" and after["dataset"]["graphrag_task_finish_at"] is None
    assert after["task_rows"] == [] and not any(after["markers"].values())
    assert not any(row.get("knowledge_graph_kwd") in {"graph", "subgraph", "entity", "relation", "community_report"} for row in after["rows"])
    assert api(env, "GET") == {"code": 0, "data": {}}
    graph = request_api(env, "GET", f"/datasets/{env['kb']}/graph", credential=env["jwt"][0]).json()
    assert graph == {"code": 0, "data": {"graph": {}, "mind_map": {}}}
    detail = request_api(env, "GET", f"/datasets/{env['kb']}", credential=env["jwt"][0]).json()["data"]
    assert detail["graphrag_task_id"] == "" and detail["graphrag_task_finish_at"] is None
    assert detail["chunk_count"] == detail["document_count"] == 1


@pytest.mark.parametrize("endpoint", ["knowledge_graph", "graph", "index?type=graph"])
@pytest.mark.parametrize("failure", ["cancel", "snapshot", "delete"])
async def test_failed_graph_clear_never_unbinds_completed_task(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, endpoint: str, failure: str) -> None:
    env = graphrag_api
    await worker(env, api(env, "POST")["data"]["task_id"], Model())
    before = observe(env, "before failed " + endpoint + " " + failure)
    if failure == "cancel":
        original = dataset_api_service.REDIS_CONN.set

        def cancel(key: str, *args: Any, **kwargs: Any) -> Any:
            return False if key.endswith("-cancel") else original(key, *args, **kwargs)

        monkeypatch.setattr(dataset_api_service.REDIS_CONN, "set", cancel)
    else:

        def fail(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("injected graph clear " + failure + " failure")

        monkeypatch.setattr(dataset_api_service, "graph_rows" if failure == "snapshot" else "delete_rows", fail)
    result = request_api(env, "DELETE", f"/datasets/{env['kb']}/{endpoint}", credential=env["jwt"][0]).json()
    assert result["code"] != 0
    after = observe(env, "failed " + endpoint + " " + failure)
    assert after["dataset"] == before["dataset"] and after["task_rows"] == before["task_rows"]
    assert after["rows"] == before["rows"] and after["markers"] == before["markers"]


@pytest.mark.parametrize("endpoint", ["graph", "index?type=graph"])
async def test_failed_graph_binding_update_preserves_sql_task_mapping(graphrag_api: dict[str, Any], endpoint: str) -> None:
    env = graphrag_api
    await worker(env, api(env, "POST")["data"]["task_id"], Model())
    before = observe(env, "before SQL clear failure")

    def fail_binding_update(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().upper().startswith("UPDATE") and "graphrag_task_id" in statement:
            raise RuntimeError("injected graph task binding SQL failure")

    sa.event.listen(env["engine"], "before_cursor_execute", fail_binding_update)
    try:
        result = request_api(env, "DELETE", f"/datasets/{env['kb']}/{endpoint}", credential=env["jwt"][0]).json()
        assert result["code"] != 0
    finally:
        sa.event.remove(env["engine"], "before_cursor_execute", fail_binding_update)
    after = observe(env, "SQL failure retained retryable mapping")
    assert after["dataset"] == before["dataset"] and after["task_rows"] == before["task_rows"]
    # Native wiping precedes SQL commit; a failed SQL transaction cannot undo
    # that acknowledged external operation. The complete SQL mapping is kept.
    assert not any(row.get("knowledge_graph_kwd") in dataset_api_service.GRAPH_KINDS for row in after["rows"])


async def test_nullable_native_graph_rows_resume_and_prune_by_kind(graphrag_api: dict[str, Any]) -> None:
    env, store = graphrag_api, settings.docStoreConn
    kb, doc = env["kb"], env["documents"][0]
    names = ["ALICE CHEN", "AURORA LABS", "HARBOR CITY"]
    graph = nx.Graph()
    graph.graph["source_id"] = [doc]
    for name in names:
        graph.add_node(name, entity_type="person", description=name, source_id=[doc])
    for source, target in pairwise(names):
        graph.add_edge(source, target, description="related", weight=1, keywords=["related"], source_id=[doc])
    null_fields = {"entity_kwd": None, "name_kwd": None, "from_entity_kwd": None, "to_entity_kwd": None}
    previous = [
        {**null_fields, "id": "old-graph", "knowledge_graph_kwd": "graph", "content_with_weight": json.dumps(nx.node_link_data(graph, edges="edges"))},
        {**null_fields, "id": "old-subgraph", "knowledge_graph_kwd": "subgraph", "content_with_weight": json.dumps(nx.node_link_data(graph, edges="edges"))},
        *[{**null_fields, "id": utils.entity_chunk_id(kb, name), "knowledge_graph_kwd": "entity", "entity_kwd": name, "content_with_weight": name} for name in names],
        *[
            {**null_fields, "id": utils.relation_chunk_id(kb, source, target), "knowledge_graph_kwd": "relation", "from_entity_kwd": source, "to_entity_kwd": target, "content_with_weight": "related"}
            for source, target in graph.edges()
        ],
    ]
    for row in previous:
        row.update(kb_id=kb, source_id=[doc], available_int=0, q_2_vec=[0.25, 0.75])
    assert store.insert(previous, env["collection"], kb) == []

    def complete_rows() -> list[dict[str, Any]]:
        return env["reader"].query(env["collection"], filter=f"kb_id == {json.dumps(kb)}", output_fields=["*", "vector", "q_2_vec"], consistency_level="Strong", limit=100)

    before = complete_rows()
    assert len(before) == 8  # original source + graph/subgraph + 3 entities + 2 relations
    assert all(row["name_kwd"] is None for row in before)
    assert all(row["entity_kwd"] is None for row in before if row["knowledge_graph_kwd"] != "entity")
    expected = {row["pk"]: row for row in before if row["knowledge_graph_kwd"] in {None, "entity", "relation"}}
    await utils.set_graph(env["tenant"], kb, None, graph, utils.GraphChange(), None)
    after = complete_rows()
    assert len(after) == 8
    assert all(next(row for row in after if row["pk"] == pk) == old for pk, old in expected.items())
    assert not {"old-graph", "old-subgraph"} & {row["pk"] for row in after}
    # A later resolution removes one entity and its edge without interpreting
    # nullable fields from the other row kinds as entity/relation identities.
    graph.remove_node(names[0])
    change = utils.GraphChange(removed_nodes={names[0]}, removed_edges={utils.get_from_to(names[0], names[1])})
    await utils.set_graph(env["tenant"], kb, None, graph, change, None)
    final = complete_rows()
    assert len(final) == 6
    assert {row["entity_kwd"] for row in final if row["knowledge_graph_kwd"] == "entity"} == set(names[1:])
    assert sum(row["knowledge_graph_kwd"] == "relation" for row in final) == 1
    retained_ids = {utils.entity_chunk_id(kb, name) for name in names[1:]} | {utils.relation_chunk_id(kb, names[1], names[2])}
    retained_ids.update(row["pk"] for row in before if row["knowledge_graph_kwd"] is None)
    # Empty-change resume preserved every row above. A node deletion refreshes
    # all surviving entities' indexed metadata/paths (85688b42), while reusing
    # their exact vectors and provenance. Source and relation rows stay intact.
    unchanged_ids = {pk for pk in retained_ids if expected[pk]["knowledge_graph_kwd"] != "entity"}
    assert all(next(row for row in final if row["pk"] == pk) == expected[pk] for pk in unchanged_ids)
    for name in names[1:]:
        pk = utils.entity_chunk_id(kb, name)
        actual = next(row for row in final if row["pk"] == pk)
        original = expected[pk]
        for field in ["pk", "kb_id", "source_id", "vector", "q_2_vec"]:
            assert actual[field] == original[field], field
        assert actual["entity_kwd"] == name and actual["entity_type_kwd"] == "person"
        assert json.loads(actual["content_with_weight"]) == graph.nodes[name]
        assert actual["content_with_weight"] != original["content_with_weight"]
        assert actual["rank_flt"] == 0.0
        neighbor = names[2] if name == names[1] else names[1]
        assert json.loads(actual["n_hop_with_weight"]) == [{"path": [name, neighbor], "weights": [1.0]}]
        assert actual["name_kwd"] is None and actual["from_entity_kwd"] is None and actual["to_entity_kwd"] is None
    persisted = await utils.get_graph(env["tenant"], kb)
    assert persisted is not None and set(persisted.nodes) == set(names[1:]) and persisted.number_of_edges() == 1
    env["record"]["nullable_kind_dispatch"] = {
        "before_count": 8,
        "after_resume_count": 8,
        "after_resolution_count": 6,
        "source_and_relation_rows_unchanged": True,
        "survivor_vectors_and_provenance_preserved": True,
        "survivor_metadata_and_paths_refreshed": True,
    }
    env["record"]["nullable_native_snapshots"] = {"before": before, "after_resume": after, "after_resolution": final}


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

    set_phase_marker(env["kb"], PHASE_RESOLUTION, index.phase_revision(persisted, env["parser_config"], model, nodes=set(persisted.nodes())))
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
    result = subprocess.run(smoke_command(), env={**__import__("os").environ, "SMOKE_BASE_URL": graphrag_api["base"]}, capture_output=True, text=True, timeout=60)
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


@pytest.mark.parametrize("change", ["none", "graph", "config", "model"])
@pytest.mark.parametrize("stage", ["resolution", "community"])
async def test_durable_substage_resume_counts_real_model_calls_and_cleans_after_publication(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stage: str, change: str) -> None:
    from core.graphrag.checkpoints import StageCheckpoints
    from core.graphrag.general import community_reports_extractor
    from tests.support.graphrag_acceptance import Embedding

    env = graphrag_api
    doc = env["documents"][0]
    graph = nx.Graph()
    graph.graph["source_id"] = [doc]
    for name, entity_type in [("ALPHA", "person"), ("ALPHAS", "person"), ("BETA", "organization"), ("BETAS", "organization")]:
        graph.add_node(name, entity_type=entity_type, description=name, source_id=[doc])
    graph.add_edge("ALPHA", "ALPHAS", description="first relation", weight=1, keywords=["related"], source_id=[doc])
    graph.add_edge("BETA", "BETAS", description="second relation", weight=1, keywords=["related"], source_id=[doc])
    await utils.set_graph(env["tenant"], env["kb"], Embedding(), graph, utils.GraphChange(), None)
    if stage == "community":
        monkeypatch.setattr(community_reports_extractor.leiden, "run", lambda *args: {"0": {"a": {"nodes": ["ALPHA", "ALPHAS"], "weight": 1}, "b": {"nodes": ["BETA", "BETAS"], "weight": 1}}})
    saved = asyncio.Event()
    loop = asyncio.get_running_loop()
    original_save = StageCheckpoints._save
    caches: set[StageCheckpoints] = set()

    def save(cache: StageCheckpoints, key: str, payload: Any, lease: Any = None) -> None:
        original_save(cache, key, payload, lease)
        caches.add(cache)
        loop.call_soon_threadsafe(saved.set)

    monkeypatch.setattr(StageCheckpoints, "_save", save)

    class PartialModel:
        llm_name = "durable-substage-controlled-model"
        max_length = 8192
        model_config = {"id": 1, "api_base": "http://controlled.invalid", "max_tokens": 8192}
        failed = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> str:
            self.calls.append(system)
            if "BETA" in system:
                await asyncio.wait_for(saved.wait(), timeout=10)
                if self.failed:
                    raise TimeoutError("injected after first durable LLM result")
            if stage == "resolution":
                return "<|>1<|>&&No&&"
            title = "BETA report" if "BETA" in system else "ALPHA report"
            return json.dumps({"title": title, "summary": "summary", "findings": [{"summary": "finding", "explanation": "evidence"}], "rating": 1.0, "rating_explanation": "reason"})

    model = PartialModel()
    config = {"graphrag": {"retry_attempts": 1, "retry_backoff_seconds": 0}}
    row = {"id": "", "tenant_id": env["tenant"], "kb_id": env["kb"]}

    async def run() -> None:
        await index.run_graphrag_for_kb(row, [doc], "English", config, model, Embedding(), lambda **kwargs: None, with_resolution=stage == "resolution", with_community=stage == "community")

    before = native_rows(env)
    with pytest.raises(TimeoutError, match="injected after first durable"):
        await run()
    assert len(model.calls) == 2 and len(caches) == 1
    cache = next(iter(caches))
    keys = env["redis"].smembers(cache.index_key)
    assert len(keys) == 1
    data_key = cache.data_key(next(iter(keys)).decode())
    payload = json.loads(env["redis"].get(data_key))["payload"]
    assert payload == [] if stage == "resolution" else payload["title"] == "ALPHA report"
    assert 0 < env["redis"].ttl(data_key) <= 604800 and 0 < env["redis"].ttl(cache.index_key) <= 604800
    assert native_rows(env) == before
    phase = PHASE_RESOLUTION if stage == "resolution" else PHASE_COMMUNITY
    assert env["redis"].get(phase_key(env["kb"], phase)) is None
    model.failed = False
    if change == "graph":
        changed_graph = await utils.get_graph(env["tenant"], env["kb"])
        changed_graph.nodes["ALPHA"]["description"] = "changed description"
        await utils.set_graph(env["tenant"], env["kb"], Embedding(), changed_graph, utils.GraphChange(), None)
    elif change == "config":
        config["graphrag"]["batch_chunk_token_size"] = 1000
    elif change == "model":
        model.model_config = {**model.model_config, "id": 2}
    await run()
    assert len(model.calls) == (3 if change == "none" else 4)
    if change == "none":
        assert not env["redis"].exists(cache.index_key) and not env["redis"].exists(data_key)
    else:
        # The old input's successful work is never replayed into new inputs;
        # superseded scopes retain their bounded TTL for exact-input resumes.
        assert env["redis"].exists(cache.index_key) and env["redis"].exists(data_key)
        assert len(caches) == 2
        new_cache = next(value for value in caches if value != cache)
        assert not env["redis"].exists(new_cache.index_key)
    assert env["redis"].get(phase_key(env["kb"], phase)) is not None
    rows = native_rows(env)
    if stage == "community":
        reports = [row for row in rows if row.get("knowledge_graph_kwd") == "community_report"]
        assert {row["docnm_kwd"] for row in reports} == {"ALPHA report", "BETA report"}
    else:
        persisted = [row for row in rows if row.get("knowledge_graph_kwd") == "graph"]
        assert len(persisted) == 1 and len(json.loads(persisted[0]["content_with_weight"])["nodes"]) == 4
    env["journal"].event("substage-resume-readback", stage=stage, change=change, real_model_calls=len(model.calls), cached_substeps=1, completed_scope_absent=True, graph_rows=len(rows))


@pytest.mark.parametrize("operation", ["save", "cleanup", "marker"])
async def test_lost_writer_is_fenced_at_redis_execution(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    from core.graphrag.checkpoints import StageCheckpoints, fingerprint
    from core.graphrag.persistence import GraphRAGLease, current_writer
    from core.graphrag.phase_markers import set_phase_marker
    from core.utils.redis_conn import REDIS_CONN

    env = graphrag_api
    cache = StageCheckpoints(env["tenant"], env["kb"], "resolution", fingerprint("fenced-test"))
    key, new_key = fingerprint("batch"), fingerprint("new-batch")
    async with GraphRAGLease(env["tenant"], env["kb"]) as lease:
        token = current_writer.set(lease)
        try:
            await cache.save(key, [])
            original_eval = REDIS_CONN.REDIS.eval
            replaced = False

            def replace_owner(script: str, numkeys: int, *args: Any) -> Any:
                nonlocal replaced
                if not replaced:
                    replaced = True
                    env["redis"].set(lease.key, "replacement-owner", ex=60)
                    # A replacement writer can publish before the stalled old
                    # writer's command reaches Redis. Its cache must survive.
                    cache._save(new_key, [["new", "result"]])
                return original_eval(script, numkeys, *args)

            monkeypatch.setattr(REDIS_CONN.REDIS, "eval", replace_owner)
            if operation == "marker":
                assert not set_phase_marker(env["kb"], PHASE_RESOLUTION, "done", lease=lease)
            else:
                with pytest.raises(RuntimeError, match="lease was lost"):
                    if operation == "save":
                        await cache.save(key, [["old", "overwrite"]])
                    else:
                        await cache.cleanup(page_size=1)
            assert json.loads(env["redis"].get(cache.data_key(key)))["payload"] == []
            assert env["redis"].get(cache.data_key(new_key)) is not None
            assert env["redis"].exists(cache.index_key)
            assert env["redis"].get(phase_key(env["kb"], PHASE_RESOLUTION)) is None
            env["journal"].event("fence-readback", operation=operation, replacement_cache_preserved=True, marker_absent=True)
        finally:
            current_writer.reset(token)


async def test_redis_checkpoint_pages_expiry_corruption_and_index_failure(graphrag_api: dict[str, Any]) -> None:
    from redis.exceptions import ResponseError

    from core.graphrag.checkpoints import StageCheckpoints, fingerprint
    from core.graphrag.persistence import GraphRAGLease, current_writer

    env = graphrag_api
    cache = StageCheckpoints(env["tenant"], env["kb"], "community", fingerprint("pages"))
    keys = [fingerprint(i) for i in range(6)]
    async with GraphRAGLease(env["tenant"], env["kb"]) as lease:
        token = current_writer.set(lease)
        try:
            for key in keys:
                await cache.save(key, {"title": key})
            assert len(await cache.load(page_size=1)) == 6
            env["redis"].set(cache.data_key(keys[0]), "invalid-json", ex=60)
            env["redis"].pexpire(cache.data_key(keys[1]), 1)
            swapped = json.loads(env["redis"].get(cache.data_key(keys[2])))
            env["redis"].set(cache.data_key(keys[3]), json.dumps(swapped), ex=60)
            await asyncio.sleep(0.02)
            assert set(await cache.load(page_size=1)) == {keys[2], keys[4], keys[5]}
            assert await StageCheckpoints("foreign", env["kb"], "community", cache.revision).load() == {}
            assert await StageCheckpoints(env["tenant"], "foreign", "community", cache.revision).load() == {}
            await cache.cleanup(page_size=1)
            assert not env["redis"].exists(cache.index_key)
            assert not any(env["redis"].exists(cache.data_key(key)) for key in keys)
            # An invalid index cannot partially publish a new data record.
            env["redis"].set(cache.index_key, "wrong-type", ex=60)
            with pytest.raises(ResponseError, match="Invalid GraphRAG checkpoint index"):
                await cache.save(keys[0], {})
            assert env["redis"].get(cache.data_key(keys[0])) is None
            env["journal"].event("checkpoint-pages-readback", valid=3, expired=1, corrupt=1, swapped=1, failed_publication_no_data=True)
        finally:
            current_writer.reset(token)


async def test_cancellation_is_fenced_at_completion_marker_execution(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from core.graphrag.persistence import GraphRAGLease
    from core.graphrag.phase_markers import set_phase_marker
    from core.utils.redis_conn import REDIS_CONN

    env = graphrag_api
    task = "marker-cancel-" + uuid4().hex
    key = task + "-cancel"
    env["journal"].event("reserve-marker-cancellation", key=key)
    env["checkpoint_keys"].add(key)
    async with GraphRAGLease(env["tenant"], env["kb"]) as lease:
        original_eval = REDIS_CONN.REDIS.eval

        def cancel_before_exec(script: str, numkeys: int, *args: Any) -> Any:
            env["redis"].set(key, "1", ex=60)
            return original_eval(script, numkeys, *args)

        monkeypatch.setattr(REDIS_CONN.REDIS, "eval", cancel_before_exec)
        assert not set_phase_marker(env["kb"], PHASE_RESOLUTION, "done", lease=lease, task_id=task)
        assert env["redis"].get(phase_key(env["kb"], PHASE_RESOLUTION)) is None
        env["journal"].event("cancel-marker-readback", marker_absent=True)
