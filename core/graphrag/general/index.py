#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from copy import deepcopy
from hashlib import sha256
from typing import Any

import networkx as nx

from api.db.db_models import db_connection
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import has_canceled
from common.exceptions import TaskCanceledException
from common.graphrag_config import GraphRAGExecutionConfig
from core.graphrag.entity_resolution import EntityResolution
from core.graphrag.general.community_reports_extractor import CommunityReportsExtractor
from core.graphrag.general.extractor import Extractor
from core.graphrag.general.graph_extractor import GraphExtractor as GeneralKGExt
from core.graphrag.light.graph_extractor import GraphExtractor as LightKGExt
from core.graphrag.ner.graph_extractor import GraphExtractor as NerKGExt
from core.graphrag.persistence import GraphRAGLease, current_task, current_writer, delete_rows, graph_rows, settled_io
from core.graphrag.phase_markers import PHASE_COMMUNITY, PHASE_RESOLUTION, clear_phase_markers, has_phase_marker, set_phase_marker
from core.graphrag.utils import (
    GraphChange,
    chunk_id,
    does_graph_contains,
    get_graph,
    graph_merge,
    insert_chunks_bounded,
    load_graph_source_chunks,
    set_graph,
    tidy_graph,
)
from core.nlp import rag_tokenizer, search


def _has_cancel_and_exit(task_id: str, message: str, callback: Callable[..., Any] | None = None) -> None:
    if task_id and has_canceled(task_id):
        if callback:
            callback(msg=message)
        raise TaskCanceledException(f"Task {task_id} was cancelled")
    lease = current_writer.get()
    if lease is not None:
        lease.check()


async def _await_attempt[T](operation: Awaitable[T], task_id: str, label: str, callback: Callable[..., Any] | None) -> T:
    """Own and drain the attempt, including cancellation during executor IO."""
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            _has_cancel_and_exit(task_id, f"Task {task_id} cancelled during {label}.", callback)
            await asyncio.wait({task}, timeout=0.25)
        result = task.result()
        _has_cancel_and_exit(task_id, f"Task {task_id} cancelled after {label}.", callback)
        return result
    finally:
        if not task.done():
            task.cancel()
        drain = asyncio.gather(task, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                continue
        drain.result()


async def _run_with_retry[T](
    label: str,
    coro_factory: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    timeout_seconds: int | float,
    backoff_seconds: int | float,
    backoff_max_seconds: int | float,
    callback: Callable[..., Any] | None = None,
    task_id: str = "",
) -> T:
    for attempt in range(1, attempts + 1):
        _has_cancel_and_exit(task_id, f"Task {task_id} cancelled before {label}.", callback)
        try:
            operation = _await_attempt(coro_factory(), task_id, label, callback)
            if timeout_seconds > 0:
                return await asyncio.wait_for(operation, timeout=timeout_seconds)
            return await operation
        except (TaskCanceledException, asyncio.CancelledError):
            raise
        except Exception as error:
            _has_cancel_and_exit(task_id, f"Task {task_id} cancelled after {label} failed.", callback)
            if callback:
                callback(msg=f"[GraphRAG] {label} failed attempt {attempt}/{attempts}: {error!r}")
            if attempt == attempts:
                raise
            delay = min(backoff_max_seconds, backoff_seconds * 2 ** (attempt - 1))
            deadline = asyncio.get_running_loop().time() + delay
            while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                _has_cancel_and_exit(task_id, f"Task {task_id} cancelled before retrying {label}.", callback)
                await asyncio.sleep(min(0.25, remaining))
    raise ValueError("GraphRAG retry attempts must be positive")


async def load_subgraph_from_store(tenant_id: str, kb_id: str, doc_id: str) -> nx.Graph | None:
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        index_name = search.index_name_one(tenant_id, kb.name)
    rows = await settled_io(graph_rows, index_name, kb_id, ["subgraph"], doc_id)
    selected = []
    for row in rows:
        sources = row.get("source_id", [])
        if isinstance(sources, str):
            sources = sources.split("\n")
        if sources == [doc_id] and row.get("removed_kwd", "N") == "N":
            selected.append(row)
    if not selected:
        return None
    stable_id = chunk_id({"content_with_weight": f"subgraph::{doc_id}", "kb_id": kb_id})
    selected = [row for row in selected if row["pk"] == stable_id] or selected
    if len(selected) != 1:
        raise RuntimeError("Ambiguous persisted subgraph checkpoints")
    graph = nx.node_link_graph(json.loads(selected[0]["content_with_weight"]), edges="edges")
    graph.graph["source_id"] = [doc_id]
    return graph


def phase_revision(graph: nx.Graph, config: dict[str, Any]) -> str:
    material = {"graph": nx.node_link_data(graph, edges="edges"), "config": config.get("graphrag", {})}
    return sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _select_extractor(config: dict[str, Any]) -> type[Extractor]:
    if config.get("method") == "general":
        return GeneralKGExt
    if config.get("method") == "ner":
        return NerKGExt
    return LightKGExt


async def run_graphrag_for_kb(
    row: dict[str, Any],
    doc_ids: list[str],
    language: str,
    kb_parser_config: dict[str, Any],
    chat_model: Any,
    embedding_model: Any,
    callback: Callable[..., Any],
    *,
    with_resolution: bool = True,
    with_community: bool = True,
    max_parallel_docs: int = 4,
) -> dict[str, Any]:
    config = GraphRAGExecutionConfig.model_validate(kb_parser_config.get("graphrag", {}))
    task_token = current_task.set(row["id"])
    try:
        _has_cancel_and_exit(row["id"], f"Task {row['id']} cancelled before acquiring writer lease.", callback)
        async with GraphRAGLease(row["tenant_id"], str(row["kb_id"]), wait=config.lock_wait_seconds) as lease:
            token = current_writer.set(lease)
            try:
                _has_cancel_and_exit(row["id"], f"Task {row['id']} cancelled after acquiring writer lease.", callback)
                return await _run_graphrag_for_kb(
                    row, doc_ids, language, kb_parser_config, chat_model, embedding_model, callback, with_resolution=with_resolution, with_community=with_community, max_parallel_docs=max_parallel_docs
                )
            finally:
                current_writer.reset(token)
    except TaskCanceledException:
        callback(prog=-1, msg=f"Task {row['id']} cancelled after GraphRAG writes settled.")
        raise
    finally:
        current_task.reset(task_token)


async def _run_graphrag_for_kb(
    row: dict[str, Any],
    doc_ids: list[str],
    language: str,
    kb_parser_config: dict[str, Any],
    chat_model: Any,
    embedding_model: Any,
    callback: Callable[..., Any],
    *,
    with_resolution: bool = True,
    with_community: bool = True,
    max_parallel_docs: int = 4,
) -> dict[str, Any]:
    tenant_id, kb_id = row["tenant_id"], row["kb_id"]
    graphrag_config = kb_parser_config.get("graphrag", {})
    config = GraphRAGExecutionConfig.model_validate(graphrag_config)
    batch_chunk_token_size = config.batch_chunk_token_size
    if type(max_parallel_docs) is not int or max_parallel_docs < 1:
        raise ValueError("GraphRAG max_parallel_docs must be positive")

    async def retry[T](label: str, operation: Callable[[], Awaitable[T]], attempts: int | None, timeout_seconds: int) -> T:
        return await _run_with_retry(
            label,
            operation,
            attempts=attempts or config.retry_attempts,
            timeout_seconds=timeout_seconds,
            backoff_seconds=config.retry_backoff_seconds,
            backoff_max_seconds=config.retry_backoff_max_seconds,
            callback=callback,
            task_id=row["id"],
        )

    start = asyncio.get_running_loop().time()

    if not doc_ids:
        logging.info(f"Fetching all docs for {kb_id}")
        with db_connection() as db:
            docs, _ = DocumentService.get_by_kb_id(
                db,
                kb_id=kb_id,
                page_number=0,
                items_per_page=0,
                orderby="create_time",
                desc=False,
                keywords="",
                run_status=[],
                types=[],
                suffix=[],
            )
        doc_ids = [doc["id"] for doc in docs]

    doc_ids = list(dict.fromkeys(doc_ids))
    if not doc_ids:
        callback(msg=f"[GraphRAG] kb:{kb_id} has no processable doc_id.")
        return {"ok_docs": [], "failed_docs": [], "total_docs": 0, "total_chunks": 0, "seconds": 0.0}

    callback(msg=f"[GraphRAG] dataset:{kb_id} has {len(doc_ids)} documents to process.")
    persisted_graph = await get_graph(tenant_id, kb_id)
    merged_doc_ids = set(persisted_graph.graph.get("source_id", [])) if persisted_graph is not None else set()

    async def load_doc_chunks(doc_id: str) -> list[str]:
        from common.token_utils import num_tokens_from_string

        chunks = []
        current_chunk = ""

        raw_chunks = await settled_io(load_graph_source_chunks, tenant_id, kb_id, doc_id)
        callback(msg=f"[GraphRAG] loaded {len(raw_chunks)} source chunks for doc:{doc_id}")
        if graphrag_config.get("method") == "ner":
            return raw_chunks
        for content in raw_chunks:
            if num_tokens_from_string(current_chunk + content) < batch_chunk_token_size:
                current_chunk += content
            else:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = content

        if current_chunk:
            chunks.append(current_chunk)

        callback(msg=f"[GraphRAG] combined {len(raw_chunks)} source chunks to {len(chunks)} chunks for LLM extraction for doc:{doc_id}")
        return chunks

    total_chunks = 0
    empty_docs: list[str] = []

    semaphore = asyncio.Semaphore(max_parallel_docs)

    subgraphs: dict[str, nx.Graph] = {}
    failed_docs: list[tuple[str, str]] = []  # (doc_id, error)

    async def build_one(doc_id: str) -> None:
        nonlocal total_chunks
        if has_canceled(row["id"]):
            callback(msg=f"Task {row['id']} cancelled, stopping execution.")
            raise TaskCanceledException(f"Task {row['id']} was cancelled")

        kg_extractor = _select_extractor(graphrag_config)

        async with semaphore:
            if has_canceled(row["id"]):
                raise TaskCanceledException(f"Task {row['id']} was cancelled")
            if doc_id in merged_doc_ids:
                callback(msg=f"[GraphRAG] doc:{doc_id} already merged, skipping extraction.")
                return
            # CHECKPOINT: bounded by semaphore so doc-store lookups respect max_parallel_docs
            existing_sg = await load_subgraph_from_store(tenant_id, kb_id, doc_id)
            if existing_sg is not None:
                subgraphs[doc_id] = existing_sg
                callback(msg=f"[GraphRAG] doc:{doc_id} subgraph found in store, skipping LLM extraction.")
                return
            try:
                chunks = await load_doc_chunks(doc_id)
                if has_canceled(row["id"]):
                    raise TaskCanceledException(f"Task {row['id']} was cancelled")
                total_chunks += len(chunks)
                if not chunks:
                    empty_docs.append(doc_id)
                    callback(msg=f"[GraphRAG] doc:{doc_id} has no available chunks, skip generation.")
                    return
                deadline = max(config.build_subgraph_min_timeout_seconds, len(chunks) * config.build_subgraph_timeout_per_chunk_seconds)
                msg = f"[GraphRAG] build_subgraph doc:{doc_id}"
                callback(msg=f"{msg} start (chunks={len(chunks)}, timeout={deadline}s)")

                async def build_attempt() -> nx.Graph | None:
                    checkpoint = await load_subgraph_from_store(tenant_id, kb_id, doc_id)
                    if checkpoint is not None:
                        return checkpoint
                    return await generate_subgraph(
                        kg_extractor, tenant_id, kb_id, doc_id, chunks, language, graphrag_config.get("entity_types", []), chat_model, embedding_model, callback, task_id=row["id"]
                    )

                sg = await retry(f"build_subgraph doc:{doc_id}", build_attempt, config.build_subgraph_retry_attempts, deadline)
                if sg is not None:
                    subgraphs[doc_id] = sg
                    callback(msg=f"{msg} done")
                else:
                    failed_docs.append((doc_id, "subgraph is empty"))
                    callback(msg=f"{msg} empty")
            except TaskCanceledException:
                raise
            except Exception as e:
                failed_docs.append((doc_id, repr(e)))
                callback(msg=f"[GraphRAG] build_subgraph doc:{doc_id} FAILED: {e!r}")

    if has_canceled(row["id"]):
        callback(msg=f"Task {row['id']} cancelled before processing documents.")
        raise TaskCanceledException(f"Task {row['id']} was cancelled")

    tasks = [asyncio.create_task(build_one(doc_id)) for doc_id in doc_ids]
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except BaseException as e:
        logging.error(f"Error in asyncio.gather: {e}")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    if has_canceled(row["id"]):
        callback(msg=f"Task {row['id']} cancelled after document processing.")
        raise TaskCanceledException(f"Task {row['id']} was cancelled")

    if len(empty_docs) == len(doc_ids) and persisted_graph is None and not subgraphs:
        callback(msg=f"[GraphRAG] kb:{kb_id} has no available chunks in all documents, skip.")
        return {"ok_docs": [], "failed_docs": doc_ids, "total_docs": len(doc_ids), "total_chunks": 0, "seconds": 0.0}

    ok_docs = [d for d in doc_ids if d in subgraphs]
    final_graph = persisted_graph
    for doc_id in ok_docs:
        if has_canceled(row["id"]):
            raise TaskCanceledException(f"Task {row['id']} was cancelled")
        # Invalidate before mutation, including a failed/partial new merge.
        clear_phase_markers(kb_id)

        async def merge_attempt() -> nx.Graph:
            current_graph = await get_graph(tenant_id, kb_id)
            if current_graph is not None and doc_id in current_graph.graph.get("source_id", []):
                # A prior attempt may have committed the authoritative checkpoint
                # before pruning failed. Settle cleanup without merging twice.
                change = GraphChange(added_updated_nodes=set(current_graph.nodes()), added_updated_edges=set(current_graph.edges()))
                await set_graph(tenant_id, kb_id, embedding_model, current_graph, change, callback)
                return current_graph
            return await merge_subgraph(tenant_id, kb_id, doc_id, deepcopy(subgraphs[doc_id]), embedding_model, callback)

        final_graph = await retry(f"merge_subgraph doc:{doc_id}", merge_attempt, config.merge_retry_attempts, config.merge_timeout_seconds)
    if failed_docs:
        raise RuntimeError(f"GraphRAG failed to build {len(failed_docs)} document subgraphs")
    if final_graph is None:
        if with_resolution or with_community:
            raise RuntimeError("No persisted graph found for resolution/community resume")
        return {"ok_docs": ok_docs, "failed_docs": failed_docs, "total_docs": len(doc_ids), "total_chunks": total_chunks, "seconds": asyncio.get_running_loop().time() - start}

    subgraph_nodes = {node for graph in subgraphs.values() for node in graph.nodes()} or set(final_graph.nodes())
    revision = phase_revision(final_graph, kb_parser_config)
    if with_resolution and not has_phase_marker(kb_id, PHASE_RESOLUTION, revision):

        async def resolution_attempt() -> nx.Graph:
            candidate = deepcopy(final_graph)
            await resolve_entities(candidate, subgraph_nodes, tenant_id, kb_id, None, chat_model, embedding_model, callback, task_id=row["id"])
            return candidate

        final_graph = await retry("entity resolution", resolution_attempt, config.resolution_retry_attempts, config.resolution_timeout_seconds)
        revision = phase_revision(final_graph, kb_parser_config)
        if has_canceled(row["id"]):
            raise TaskCanceledException(f"Task {row['id']} was cancelled")
        lease = current_writer.get()
        if lease is None:
            raise RuntimeError("GraphRAG phase completion requires a writer lease")
        lease.check()
        set_phase_marker(kb_id, PHASE_RESOLUTION, revision)
    if with_community and not has_phase_marker(kb_id, PHASE_COMMUNITY, revision):

        async def community_attempt() -> None:
            await extract_community(deepcopy(final_graph), tenant_id, kb_id, None, chat_model, embedding_model, callback, task_id=row["id"])

        await retry("community extraction", community_attempt, config.community_retry_attempts, config.community_timeout_seconds)
        if has_canceled(row["id"]):
            raise TaskCanceledException(f"Task {row['id']} was cancelled")
        lease = current_writer.get()
        if lease is None:
            raise RuntimeError("GraphRAG phase completion requires a writer lease")
        lease.check()
        set_phase_marker(kb_id, PHASE_COMMUNITY, revision)

    now = asyncio.get_running_loop().time()
    callback(msg=f"[GraphRAG] GraphRAG for KB {kb_id} done in {now - start:.2f} seconds. ok={len(ok_docs)} failed={len(failed_docs)} total_docs={len(doc_ids)} total_chunks={total_chunks}")
    return {
        "ok_docs": ok_docs,
        "failed_docs": failed_docs,  # [(doc_id, error), ...]
        "total_docs": len(doc_ids),
        "total_chunks": total_chunks,
        "seconds": now - start,
    }


async def generate_subgraph(
    extractor: type[Extractor],
    tenant_id: str,
    kb_id: str,
    doc_id: str,
    chunks: list[str],
    language: str,
    entity_types: list[str],
    llm_bdl: Any,
    embed_bdl: Any,
    callback: Callable[..., Any],
    task_id: str = "",
) -> nx.Graph | None:
    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled during subgraph generation for doc {doc_id}.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    contains = await does_graph_contains(tenant_id, kb_id, doc_id)
    if contains:
        callback(msg=f"Graph already contains {doc_id}")
        return None
    _has_cancel_and_exit(task_id, f"Task {task_id} cancelled before entity extraction.", callback)
    start = asyncio.get_running_loop().time()
    ext = extractor(
        llm_bdl,
        language=language,
        entity_types=entity_types,
    )
    ents, rels = await ext(doc_id, chunks, callback, task_id=task_id)
    subgraph = nx.Graph()

    for ent in ents:
        if task_id and has_canceled(task_id):
            callback(msg=f"Task {task_id} cancelled during entity processing for doc {doc_id}.")
            raise TaskCanceledException(f"Task {task_id} was cancelled")

        assert "description" in ent, f"entity {ent} does not have description"
        ent["source_id"] = [doc_id]
        subgraph.add_node(ent["entity_name"], **ent)

    ignored_rels = 0
    for rel in rels:
        if task_id and has_canceled(task_id):
            callback(msg=f"Task {task_id} cancelled during relationship processing for doc {doc_id}.")
            raise TaskCanceledException(f"Task {task_id} was cancelled")

        assert "description" in rel, f"relation {rel} does not have description"
        if not subgraph.has_node(rel["src_id"]) or not subgraph.has_node(rel["tgt_id"]):
            ignored_rels += 1
            continue
        rel["source_id"] = [doc_id]
        subgraph.add_edge(
            rel["src_id"],
            rel["tgt_id"],
            **rel,
        )
    if ignored_rels:
        callback(msg=f"ignored {ignored_rels} relations due to missing entities.")
    _has_cancel_and_exit(task_id, f"Task {task_id} cancelled before tidying subgraph.", callback)
    tidy_graph(subgraph, callback, check_attribute=False)

    subgraph.graph["source_id"] = [doc_id]
    chunk = {
        "content_with_weight": json.dumps(nx.node_link_data(subgraph, edges="edges"), ensure_ascii=False),
        "knowledge_graph_kwd": "subgraph",
        "kb_id": kb_id,
        "source_id": [doc_id],
        "available_int": 0,
        "removed_kwd": "N",
    }
    cid = chunk_id({"content_with_weight": f"subgraph::{doc_id}", "kb_id": kb_id})
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        kb_name = kb.name

    index_name = search.index_name_one(tenant_id, kb_name)
    _has_cancel_and_exit(task_id, f"Task {task_id} cancelled before saving subgraph.", callback)
    previous = await settled_io(graph_rows, index_name, kb_id, ["subgraph"], doc_id)
    await insert_chunks_bounded([{"id": cid, **chunk}], tenant_id, kb_id, index_name=index_name, callback=callback)
    stale = [r["pk"] for r in previous if r["pk"] != cid and r.get("source_id") in ([doc_id], doc_id)]
    await settled_io(delete_rows, index_name, kb_id, stale)
    now = asyncio.get_running_loop().time()
    callback(msg=f"generated subgraph for doc {doc_id} in {now - start:.2f} seconds.")
    return subgraph


async def merge_subgraph(
    tenant_id: str,
    kb_id: str,
    doc_id: str,
    subgraph: nx.Graph,
    embedding_model: Any,
    callback: Callable[..., Any],
) -> nx.Graph:
    start = asyncio.get_running_loop().time()
    change = GraphChange()
    old_graph = await get_graph(tenant_id, kb_id, subgraph.graph["source_id"])
    if old_graph is not None:
        logging.info("Merge with an exiting graph...................")
        tidy_graph(old_graph, callback)
        new_graph = graph_merge(old_graph, subgraph, change)
    else:
        new_graph = subgraph
        change.added_updated_nodes = set(new_graph.nodes())
        change.added_updated_edges = set(new_graph.edges())
    pr = nx.pagerank(new_graph)
    for node_name, pagerank in pr.items():
        new_graph.nodes[node_name]["pagerank"] = pagerank

    await set_graph(tenant_id, kb_id, embedding_model, new_graph, change, callback)
    now = asyncio.get_running_loop().time()
    callback(msg=f"merging subgraph for doc {doc_id} into the global graph done in {now - start:.2f} seconds.")
    return new_graph


async def resolve_entities(
    graph: nx.Graph,
    subgraph_nodes: set[str],
    tenant_id: str,
    kb_id: str,
    doc_id: str | None,
    llm_bdl: Any,
    embed_bdl: Any,
    callback: Callable[..., Any],
    task_id: str = "",
) -> None:
    # Check if task has been canceled before resolution
    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled during entity resolution.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    start = asyncio.get_running_loop().time()
    er = EntityResolution(
        llm_bdl,
    )
    reso = await er(graph, subgraph_nodes, callback=callback, task_id=task_id)
    graph = reso.graph
    change = reso.change
    callback(msg=f"Graph resolution removed {len(change.removed_nodes)} nodes and {len(change.removed_edges)} edges.")
    callback(msg="Graph resolution updated pagerank.")

    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled after entity resolution.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    await set_graph(tenant_id, kb_id, embed_bdl, graph, change, callback)
    now = asyncio.get_running_loop().time()
    callback(msg=f"Graph resolution done in {now - start:.2f}s.")


async def extract_community(
    graph: nx.Graph,
    tenant_id: str,
    kb_id: str,
    doc_id: str | None,
    llm_bdl: Any,
    embed_bdl: Any,
    callback: Callable[..., Any],
    task_id: str = "",
) -> tuple[list[dict[str, Any]], list[str]]:
    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled before community extraction.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    start = asyncio.get_running_loop().time()
    ext = CommunityReportsExtractor(
        llm_bdl,
    )
    cr = await ext(graph, callback=callback, task_id=task_id)

    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled during community extraction.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    community_structure = cr.structured_output
    community_reports = cr.output
    doc_ids = graph.graph["source_id"]

    now = asyncio.get_running_loop().time()
    callback(msg=f"Graph extracted {len(cr.structured_output)} communities in {now - start:.2f}s.")
    start = now
    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled during community indexing.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    chunks = []
    for stru, rep in zip(community_structure, community_reports):
        obj = {
            "report": rep,
            "evidences": "\n".join([f.get("explanation", "") for f in stru["findings"]]),
        }
        chunk = {
            "id": chunk_id({"content_with_weight": f"community_report::{stru['title']}", "kb_id": kb_id}),
            "docnm_kwd": stru["title"],
            "title_tks": rag_tokenizer.tokenize(stru["title"]),
            "content_with_weight": json.dumps(obj, ensure_ascii=False),
            "content_ltks": rag_tokenizer.tokenize(obj["report"] + " " + obj["evidences"]),
            "knowledge_graph_kwd": "community_report",
            "weight_flt": stru["weight"],
            "entities_kwd": stru["entities"],
            "important_kwd": stru["entities"],
            "kb_id": kb_id,
            "source_id": list(doc_ids),
            "available_int": 0,
        }
        chunk["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(chunk["content_ltks"])
        chunks.append(chunk)

    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        kb_name = kb.name

    index_name = search.index_name_one(tenant_id, kb_name)
    new_ids = {chunk["id"] for chunk in chunks}
    if len(new_ids) != len(chunks):
        raise ValueError("Duplicate community titles cannot identify stable reports")
    previous = await settled_io(graph_rows, index_name, kb_id, ["community_report"])
    await insert_chunks_bounded(chunks, tenant_id, kb_id, index_name=index_name, callback=callback, label="Insert community reports")
    stale = [row["pk"] for row in previous if row["pk"] not in new_ids]
    await settled_io(delete_rows, index_name, kb_id, stale)

    if task_id and has_canceled(task_id):
        callback(msg=f"Task {task_id} cancelled after community indexing.")
        raise TaskCanceledException(f"Task {task_id} was cancelled")

    now = asyncio.get_running_loop().time()
    callback(msg=f"Graph indexed {len(cr.structured_output)} communities in {now - start:.2f}s.")
    return community_structure, community_reports
