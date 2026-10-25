# Copyright (c) 2024 Microsoft Corporation.
# Licensed under the MIT License
"""
Reference:
 - [graphrag](https://github.com/microsoft/graphrag)
 - [LightRag](https://github.com/HKUDS/LightRAG)
"""

import asyncio
import dataclasses
import html
import json
import logging
import os
import re
import time
from collections import defaultdict
from collections.abc import Callable
from hashlib import md5
from typing import Any

import networkx as nx
import numpy as np
import xxhash
from networkx.readwrite import json_graph

from api.db.db_models import db_connection
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.asyncio_utils import LoopLocalSemaphore
from common.connection_utils import timeout
from common.misc_utils import get_uuid
from core.graphrag.persistence import checked_insert, delete_rows, graph_rows, settled_io, validate_chunks
from core.graphrag.phase_markers import clear_phase_markers
from core.nlp import rag_tokenizer, search
from core.utils.redis_conn import REDIS_CONN

GRAPH_FIELD_SEP = "<SEP>"

ErrorHandlerFn = Callable[[BaseException | None, str | None, dict | None], None]

chat_limiter = LoopLocalSemaphore(int(os.environ.get("MAX_CONCURRENT_CHATS", 10)))


_INSERT_BULK_SIZE = max(1, int(os.environ.get("GRAPHRAG_INSERT_BULK_SIZE", 64)))
_INSERT_CONCURRENCY = max(1, int(os.environ.get("GRAPHRAG_INSERT_CONCURRENCY", 4)))


async def insert_chunks_bounded(chunks: list[dict[str, Any]], tenant_id: str, kb_id: str, *, index_name: str, callback: Callable[..., Any] | None = None, label: str = "Insert chunks") -> None:
    """Fixed worker count; stop dispatch on error and drain every executor write.

    Timeout never triggers an overlapping retry. settled_io waits for the
    underlying thread even on cancellation; gather below owns every worker.
    """
    if not chunks:
        return
    offsets = iter(range(0, len(chunks), _INSERT_BULK_SIZE))
    failed = asyncio.Event()
    done = 0

    async def worker() -> None:
        nonlocal done
        try:
            while not failed.is_set():
                offset = next(offsets, None)
                if offset is None:
                    return
                batch = chunks[offset : offset + _INSERT_BULK_SIZE]
                for attempt in range(3):
                    try:
                        deadline = 3 if os.environ.get("ENABLE_TIMEOUT_ASSERTION") else 30000000
                        await asyncio.wait_for(settled_io(checked_insert, batch, index_name, kb_id), deadline)
                        break
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        if attempt == 2 or failed.is_set():
                            raise
                        await asyncio.sleep(2**attempt)
                done += len(batch)
                if callback:
                    callback(msg=f"{label}: {done}/{len(chunks)}")
        except BaseException:
            failed.set()
            raise

    tasks = [asyncio.create_task(worker()) for _ in range(min(_INSERT_CONCURRENCY, (len(chunks) + _INSERT_BULK_SIZE - 1) // _INSERT_BULK_SIZE))]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        failed.set()
        for task in tasks:
            task.cancel()
        drain = asyncio.gather(*tasks, return_exceptions=True)
        while not drain.done():
            try:
                await asyncio.shield(drain)
            except asyncio.CancelledError:
                continue
        raise


@dataclasses.dataclass
class GraphChange:
    removed_nodes: set[str] = dataclasses.field(default_factory=set)
    added_updated_nodes: set[str] = dataclasses.field(default_factory=set)
    removed_edges: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    added_updated_edges: set[tuple[str, str]] = dataclasses.field(default_factory=set)


def perform_variable_replacements(input: str, history: list[dict] | None = None, variables: dict | None = None) -> str:
    """Perform variable replacements on the input string and in a chat log."""
    if history is None:
        history = []
    if variables is None:
        variables = {}
    result = input

    def replace_all(input: str) -> str:
        result = input
        for k, v in variables.items():
            result = result.replace(f"{{{k}}}", str(v))
        return result

    result = replace_all(result)
    for i, entry in enumerate(history):
        if entry.get("role") == "system":
            entry["content"] = replace_all(entry.get("content") or "")

    return result


def clean_str(input: Any) -> str:
    """Clean an input string by removing HTML escapes, control characters, and other unwanted characters."""
    # If we get non-string input, just give it back
    if not isinstance(input, str):
        return input

    result = html.unescape(input.strip())
    # https://stackoverflow.com/questions/4324790/removing-control-characters-from-a-string-in-python
    return re.sub(r"[\"\x00-\x1f\x7f-\x9f]", "", result)


def dict_has_keys_with_types(data: dict, expected_fields: list[tuple[str, type]]) -> bool:
    """Return True if the given dictionary has the given keys with the given types."""
    for field, field_type in expected_fields:
        if field not in data:
            return False

        value = data[field]
        if not isinstance(value, field_type):
            return False
    return True


def get_llm_cache(llmnm, txt, history, genconf):
    hasher = xxhash.xxh64()
    hasher.update((str(llmnm) + str(txt) + str(history) + str(genconf)).encode("utf-8"))

    k = hasher.hexdigest()
    bin = REDIS_CONN.get(k)
    if not bin:
        return None
    return bin


def set_llm_cache(llmnm, txt, v, history, genconf):
    hasher = xxhash.xxh64()
    hasher.update((str(llmnm) + str(txt) + str(history) + str(genconf)).encode("utf-8"))
    k = hasher.hexdigest()
    REDIS_CONN.set(k, v.encode("utf-8"), 24 * 3600)


def get_embed_cache(llmnm, txt):
    hasher = xxhash.xxh64()
    hasher.update(str(llmnm).encode("utf-8"))
    hasher.update(str(txt).encode("utf-8"))

    k = hasher.hexdigest()
    bin = REDIS_CONN.get(k)
    if not bin:
        return
    return np.array(json.loads(bin))


def set_embed_cache(llmnm, txt, arr):
    hasher = xxhash.xxh64()
    hasher.update(str(llmnm).encode("utf-8"))
    hasher.update(str(txt).encode("utf-8"))

    k = hasher.hexdigest()
    arr = json.dumps(arr.tolist() if isinstance(arr, np.ndarray) else arr)
    REDIS_CONN.set(k, arr.encode("utf-8"), 24 * 3600)


def get_tags_from_cache(kb_ids):
    hasher = xxhash.xxh64()
    hasher.update(str(kb_ids).encode("utf-8"))

    k = hasher.hexdigest()
    bin = REDIS_CONN.get(k)
    if not bin:
        return
    return bin


def set_tags_to_cache(kb_ids, tags):
    hasher = xxhash.xxh64()
    hasher.update(str(kb_ids).encode("utf-8"))

    k = hasher.hexdigest()
    REDIS_CONN.set(k, json.dumps(tags).encode("utf-8"), 600)


def tidy_graph(graph: nx.Graph, callback, check_attribute: bool = True):
    """
    Ensure all nodes and edges in the graph have some essential attribute.
    """

    def is_valid_item(node_attrs: dict) -> bool:
        valid_node = True
        for attr in ["description", "source_id"]:
            if attr not in node_attrs:
                valid_node = False
                break
        return valid_node

    if check_attribute:
        purged_nodes = []
        for node, node_attrs in graph.nodes(data=True):
            if not is_valid_item(node_attrs):
                purged_nodes.append(node)
        for node in purged_nodes:
            graph.remove_node(node)
        if purged_nodes and callback:
            callback(msg=f"Purged {len(purged_nodes)} nodes from graph due to missing essential attributes.")

    purged_edges = []
    for source, target, attr in graph.edges(data=True):
        if check_attribute:
            if not is_valid_item(attr):
                purged_edges.append((source, target))
        if "keywords" not in attr:
            attr["keywords"] = []
    for source, target in purged_edges:
        graph.remove_edge(source, target)
    if purged_edges and callback:
        callback(msg=f"Purged {len(purged_edges)} edges from graph due to missing essential attributes.")


def get_from_to(node1, node2):
    if node1 < node2:
        return (node1, node2)
    else:
        return (node2, node1)


def graph_merge(g1: nx.Graph, g2: nx.Graph, change: GraphChange):
    """Merge graph g2 into g1 in place."""
    for node_name, attr in g2.nodes(data=True):
        change.added_updated_nodes.add(node_name)
        if not g1.has_node(node_name):
            g1.add_node(node_name, **attr)
            continue
        node = g1.nodes[node_name]
        node["description"] += GRAPH_FIELD_SEP + attr["description"]
        # A node's source_id indicates which chunks it came from.
        node["source_id"] += attr["source_id"]

    for source, target, attr in g2.edges(data=True):
        change.added_updated_edges.add(get_from_to(source, target))
        edge = g1.get_edge_data(source, target)
        if edge is None:
            g1.add_edge(source, target, **attr)
            continue
        edge["weight"] += attr.get("weight", 0)
        edge["description"] += GRAPH_FIELD_SEP + attr["description"]
        edge["keywords"] += attr["keywords"]
        # A edge's source_id indicates which chunks it came from.
        edge["source_id"] += attr["source_id"]

    for node_degree in g1.degree:
        g1.nodes[str(node_degree[0])]["rank"] = int(node_degree[1])
    # A graph's source_id indicates which documents it came from.
    if "source_id" not in g1.graph:
        g1.graph["source_id"] = []
    g1.graph["source_id"] += g2.graph.get("source_id", [])
    return g1


def compute_args_hash(*args):
    return md5(str(args).encode()).hexdigest()


def handle_single_entity_extraction(
    record_attributes: list[str],
    chunk_key: str,
):
    if len(record_attributes) < 4 or record_attributes[0] != '"entity"':
        return None
    # add this record as a node in the G
    entity_name = clean_str(record_attributes[1].upper())
    if not entity_name.strip():
        return None
    entity_type = clean_str(record_attributes[2].upper())
    entity_description = clean_str(record_attributes[3])
    entity_source_id = chunk_key
    return {
        "entity_name": entity_name.upper(),
        "entity_type": entity_type.upper(),
        "description": entity_description,
        "source_id": entity_source_id,
    }


def handle_single_relationship_extraction(record_attributes: list[str], chunk_key: str):
    if len(record_attributes) < 5 or record_attributes[0] != '"relationship"':
        return None
    # add this record as edge
    source = clean_str(record_attributes[1].upper())
    target = clean_str(record_attributes[2].upper())
    edge_description = clean_str(record_attributes[3])

    edge_keywords = clean_str(record_attributes[4])
    edge_source_id = chunk_key
    weight = float(record_attributes[-1]) if is_float_regex(record_attributes[-1]) else 1.0
    pair = sorted([source.upper(), target.upper()])
    return {
        "src_id": pair[0],
        "tgt_id": pair[1],
        "weight": weight,
        "description": edge_description,
        "keywords": edge_keywords,
        "source_id": edge_source_id,
        "metadata": {"created_at": time.time()},
    }


def pack_user_ass_to_openai_messages(*args: str):
    roles = ["user", "assistant"]
    return [{"role": roles[i % 2], "content": content} for i, content in enumerate(args)]


def split_string_by_multi_markers(content: str, markers: list[str]) -> list[str]:
    """Split a string by multiple markers"""
    if not markers:
        return [content]
    results = re.split("|".join(re.escape(marker) for marker in markers), content)
    return [r.strip() for r in results if r.strip()]


def is_float_regex(value):
    return bool(re.match(r"^[-+]?[0-9]*\.?[0-9]+$", value))


def chunk_id(chunk):
    return xxhash.xxh64((chunk["content_with_weight"] + chunk["kb_id"]).encode("utf-8")).hexdigest()


def entity_chunk_id(kb_id: str, name: str) -> str:
    return chunk_id({"content_with_weight": json.dumps(["entity", name]), "kb_id": kb_id})


def relation_chunk_id(kb_id: str, source: str, target: str) -> str:
    return chunk_id({"content_with_weight": json.dumps(["relation", *get_from_to(source, target)]), "kb_id": kb_id})


async def graph_node_to_chunk(kb_id: str, embd_mdl: Any, ent_name: str, meta: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
    global chat_limiter
    enable_timeout_assertion = os.environ.get("ENABLE_TIMEOUT_ASSERTION")
    chunk = {
        "id": entity_chunk_id(kb_id, ent_name),
        "important_kwd": [ent_name],
        "title_tks": rag_tokenizer.tokenize(ent_name),
        "entity_kwd": ent_name,
        "knowledge_graph_kwd": "entity",
        "entity_type_kwd": meta["entity_type"],
        "content_with_weight": json.dumps(meta, ensure_ascii=False),
        "content_ltks": rag_tokenizer.tokenize(meta["description"]),
        "source_id": meta["source_id"],
        "kb_id": kb_id,
        "available_int": 0,
    }
    chunk["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(chunk["content_ltks"])
    ebd = get_embed_cache(embd_mdl.llm_name, ent_name)
    if ebd is None:
        async with chat_limiter:
            timeout = 3 if enable_timeout_assertion else 30000000
            ebd, _ = await asyncio.wait_for(settled_io(embd_mdl.encode, [ent_name]), timeout=timeout)
        ebd = ebd[0]
        set_embed_cache(embd_mdl.llm_name, ent_name, ebd)
    assert ebd is not None
    chunk["q_%d_vec" % len(ebd)] = ebd
    chunks.append(chunk)


@timeout(3, 3)
async def get_relation(tenant_id, kb_id, from_ent_name, to_ent_name, size=1):
    ents = from_ent_name
    if isinstance(ents, str):
        ents = [from_ent_name]
    if isinstance(to_ent_name, str):
        to_ent_name = [to_ent_name]
    ents.extend(to_ent_name)
    ents = list(set(ents))
    conds = {"fields": ["content_with_weight"], "size": size, "from_entity_kwd": ents, "to_entity_kwd": ents, "knowledge_graph_kwd": ["relation"]}
    res = []
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        kb_name = kb.name

    # 缓存 index_name，避免重复查询
    index_name = search.index_name(tenant_id, [kb_name])
    milvus_res = await settings.retriever.search(conds, index_name, [kb_id] if isinstance(kb_id, str) else kb_id)
    for id in milvus_res.ids:
        try:
            if size == 1:
                return json.loads(milvus_res.field[id]["content_with_weight"])
            res.append(json.loads(milvus_res.field[id]["content_with_weight"]))
        except Exception:
            continue
    return res


async def graph_edge_to_chunk(kb_id: str, embd_mdl: Any, from_ent_name: str, to_ent_name: str, meta: dict[str, Any], chunks: list[dict[str, Any]]) -> None:
    enable_timeout_assertion = os.environ.get("ENABLE_TIMEOUT_ASSERTION")
    chunk = {
        "id": relation_chunk_id(kb_id, from_ent_name, to_ent_name),
        "from_entity_kwd": from_ent_name,
        "to_entity_kwd": to_ent_name,
        "knowledge_graph_kwd": "relation",
        "content_with_weight": json.dumps(meta, ensure_ascii=False),
        "content_ltks": rag_tokenizer.tokenize(meta["description"]),
        "important_kwd": meta["keywords"],
        "source_id": meta["source_id"],
        "weight_int": int(meta["weight"]),
        "kb_id": kb_id,
        "available_int": 0,
    }
    chunk["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(chunk["content_ltks"])
    txt = f"{from_ent_name}->{to_ent_name}"
    ebd = get_embed_cache(embd_mdl.llm_name, txt)
    if ebd is None:
        async with chat_limiter:
            timeout = 3 if enable_timeout_assertion else 300000000
            ebd, _ = await asyncio.wait_for(settled_io(embd_mdl.encode, [txt + f": {meta['description']}"]), timeout=timeout)
        ebd = ebd[0]
        set_embed_cache(embd_mdl.llm_name, txt, ebd)
    assert ebd is not None
    chunk["q_%d_vec" % len(ebd)] = ebd
    chunks.append(chunk)


def load_graph_source_chunks(tenant_id: str, kb_id: str, doc_id: str) -> list[str]:
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        index_name = search.index_name_one(tenant_id, kb.name)
    store = settings.docStoreConn
    if store.db_type() != "milvus":
        raise RuntimeError("GraphRAG checkpoints require checked atomic upsert storage")
    rows = store.graph_source_rows(index_name, kb_id, doc_id)
    if any(row.get("kb_id") != kb_id or row.get("doc_id") != doc_id for row in rows):
        raise RuntimeError("Invalid GraphRAG source snapshot scope")
    return [row["content_with_weight"] for row in rows if row.get("content_with_weight")]


async def does_graph_contains(tenant_id: str, kb_id: str, doc_id: str) -> bool:
    graph = await get_graph(tenant_id, kb_id)
    return graph is not None and doc_id in graph.graph.get("source_id", [])


async def get_graph_doc_ids(tenant_id, kb_id) -> list[str]:
    conds = {"fields": ["source_id"], "removed_kwd": "N", "size": 1, "knowledge_graph_kwd": ["graph"]}
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        kb_name = kb.name

    index_name = search.index_name(tenant_id, [kb_name])
    res = await settings.retriever.search(conds, index_name, [kb_id])
    doc_ids = []
    if res.total == 0:
        return doc_ids
    for id in res.ids:
        doc_ids = res.field[id]["source_id"]
    return doc_ids


async def get_graph(tenant_id: str, kb_id: str, exclude_rebuild: str | list[str] | None = None) -> nx.Graph | None:
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        index_name = search.index_name_one(tenant_id, kb.name)
    rows = await settled_io(graph_rows, index_name, kb_id, ["graph"])
    if not rows:
        return None
    checkpoint_id = chunk_id({"content_with_weight": "graph_checkpoint", "kb_id": kb_id})
    selected = [row for row in rows if row["pk"] == checkpoint_id] or rows
    if len(selected) != 1:
        raise RuntimeError("Ambiguous persisted graph checkpoints")
    row = selected[0]
    if row.get("removed_kwd", "N") != "N":
        return await rebuild_graph(tenant_id, kb_id, exclude_rebuild)
    graph = json_graph.node_link_graph(json.loads(row["content_with_weight"]), edges="edges")
    if "source_id" not in graph.graph:
        sources = row.get("source_id", [])
        graph.graph["source_id"] = sources.split("\n") if isinstance(sources, str) else sources
    return graph


async def set_graph(tenant_id: str, kb_id: str, embd_mdl: Any, graph: nx.Graph, change: GraphChange, callback: Callable[..., Any] | None) -> None:
    global chat_limiter
    start = asyncio.get_running_loop().time()

    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        kb_name = kb.name

    index_name = search.index_name_one(tenant_id, kb_name)

    # Complete snapshot before any writes. Legacy APIs hide query failures.
    previous = await settled_io(graph_rows, index_name, kb_id, ["graph", "subgraph", "entity", "relation"])
    graph.graph["checkpoint_revision"] = get_uuid()
    clear_phase_markers(kb_id)

    chunks = [
        {
            "id": get_uuid(),
            "content_with_weight": json.dumps(nx.node_link_data(graph, edges="edges"), ensure_ascii=False),
            "knowledge_graph_kwd": "graph",
            "kb_id": kb_id,
            "source_id": graph.graph.get("source_id", []),
            "available_int": 0,
            "removed_kwd": "N",
        }
    ]

    # generate updated subgraphs
    for source in graph.graph["source_id"]:
        subgraph = graph.subgraph([n for n in graph.nodes if source in graph.nodes[n]["source_id"]]).copy()
        subgraph.graph["source_id"] = [source]
        for n in subgraph.nodes:
            subgraph.nodes[n]["source_id"] = [source]
        chunks.append(
            {
                "id": chunk_id({"content_with_weight": f"subgraph::{source}", "kb_id": kb_id}),
                "content_with_weight": json.dumps(nx.node_link_data(subgraph, edges="edges"), ensure_ascii=False),
                "knowledge_graph_kwd": "subgraph",
                "kb_id": kb_id,
                "source_id": [source],
                "available_int": 0,
                "removed_kwd": "N",
            }
        )

    tasks = []
    for ii, node in enumerate(change.added_updated_nodes):
        node_attrs = graph.nodes[node]
        tasks.append(asyncio.create_task(graph_node_to_chunk(kb_id, embd_mdl, node, node_attrs, chunks)))
        if ii % 100 == 9 and callback:
            callback(msg=f"Get embedding of nodes: {ii}/{len(change.added_updated_nodes)}")
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except BaseException as e:
        logging.error(f"Error in get_embedding_of_nodes: {e}")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    tasks = []
    for ii, (from_node, to_node) in enumerate(change.added_updated_edges):
        edge_attrs = graph.get_edge_data(from_node, to_node)
        if not edge_attrs:
            continue
        tasks.append(asyncio.create_task(graph_edge_to_chunk(kb_id, embd_mdl, from_node, to_node, edge_attrs, chunks)))
        if ii % 100 == 9 and callback:
            callback(msg=f"Get embedding of edges: {ii}/{len(change.added_updated_edges)}")
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except BaseException as e:
        logging.error(f"Error in get_embedding_of_edges: {e}")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    now = asyncio.get_running_loop().time()
    if callback:
        callback(msg=f"set_graph converted graph change to {len(chunks)} chunks in {now - start:.2f}s.")
    start = now

    # Auxiliary rows first, authoritative graph checkpoint last. Failed inserts
    # leave the prior graph readable, and pruning never precedes persistence.
    checkpoint = chunks[0]
    checkpoint["id"] = chunk_id({"content_with_weight": "graph_checkpoint", "kb_id": kb_id})
    await settled_io(validate_chunks, chunks, index_name)
    await insert_chunks_bounded(chunks[1:], tenant_id, kb_id, index_name=index_name, callback=callback)
    await insert_chunks_bounded([checkpoint], tenant_id, kb_id, index_name=index_name, callback=callback)
    new_ids = {chunk["id"] for chunk in chunks}
    all_ids = new_ids | {row["pk"] for row in previous}
    graph_edges = {get_from_to(source, target) for source, target in graph.edges()}
    updated_nodes = change.removed_nodes | change.added_updated_nodes
    updated_edges = change.removed_edges | change.added_updated_edges
    sources = set(graph.graph.get("source_id", []))
    stale = []
    for row in previous:
        kind = row["knowledge_graph_kwd"]
        if row["pk"] in new_ids:
            continue
        if kind == "graph":
            stale.append(row["pk"])
        elif kind == "subgraph":
            source_ids = row.get("source_id", [])
            if isinstance(source_ids, str):
                source_ids = source_ids.split("\n")
            if set(source_ids) <= sources:
                stale.append(row["pk"])
        elif kind == "entity":
            entity = row.get("entity_kwd")
            if not isinstance(entity, str) or not entity.strip():
                raise ValueError("Invalid persisted graph entity identity")
            canonical_id = entity_chunk_id(kb_id, entity)
            entity_replaced = row["pk"] != canonical_id and canonical_id in all_ids
            if entity not in graph or entity in updated_nodes or entity_replaced:
                stale.append(row["pk"])
        elif kind == "relation":
            source, target = row.get("from_entity_kwd"), row.get("to_entity_kwd")
            if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
                raise ValueError("Invalid persisted graph relation identity")
            pair = get_from_to(source, target)
            canonical_id = relation_chunk_id(kb_id, *pair)
            relation_replaced = row["pk"] != canonical_id and canonical_id in all_ids
            if pair not in graph_edges or pair in updated_edges or relation_replaced:
                stale.append(row["pk"])
    await settled_io(delete_rows, index_name, kb_id, stale)
    if callback:
        callback(msg=f"set_graph persisted {len(chunks)} chunks and pruned {len(stale)} stale rows.")


def is_continuous_subsequence(subseq, seq):
    def find_all_indexes(tup, value):
        indexes = []
        start = 0
        while True:
            try:
                index = tup.index(value, start)
                indexes.append(index)
                start = index + 1
            except ValueError:
                break
        return indexes

    index_list = find_all_indexes(seq, subseq[0])
    for idx in index_list:
        if idx != len(seq) - 1:
            if seq[idx + 1] == subseq[-1]:
                return True
    return False


def merge_tuples(list1, list2):
    result = []
    for tup in list1:
        last_element = tup[-1]
        if last_element in tup[:-1]:
            result.append(tup)
        else:
            matching_tuples = [t for t in list2 if t[0] == last_element]
            already_match_flag = 0
            for match in matching_tuples:
                matchh = (match[1], match[0])
                if is_continuous_subsequence(match, tup) or is_continuous_subsequence(matchh, tup):
                    continue
                already_match_flag = 1
                merged_tuple = tup + match[1:]
                result.append(merged_tuple)
            if not already_match_flag:
                result.append(tup)
    return result


async def get_entity_type2samples(idxnms, kb_ids: list):
    if isinstance(idxnms, list):
        flattened_idxnms = []
        for idx in idxnms:
            if isinstance(idx, str):
                if idx:
                    flattened_idxnms.append(idx)
            elif isinstance(idx, list):
                flattened_idxnms.extend([name for name in idx if isinstance(name, str) and name])
        idxnms = flattened_idxnms
    es_res = await settings.retriever.search({"knowledge_graph_kwd": "ty2ents", "kb_id": kb_ids, "size": 10000, "fields": ["content_with_weight"]}, idxnms, kb_ids)

    res = defaultdict(list)
    for id in es_res.ids:
        smp = es_res.field[id].get("content_with_weight")
        if not smp:
            continue
        try:
            smp = json.loads(smp)
        except Exception as e:
            logging.exception(e)

        for ty, ents in smp.items():
            res[ty].extend(ents)
    return res


def flat_uniq_list(arr, key):
    res = []
    for a in arr:
        a = a[key]
        if isinstance(a, list):
            res.extend(a)
        else:
            res.append(a)
    return list(set(res))


async def rebuild_graph(tenant_id: str, kb_id: str, exclude_rebuild: str | list[str] | None = None) -> nx.Graph | None:
    """Reconstruct only from a complete, checked inventory of persisted subgraphs."""
    with db_connection() as db:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        index_name = search.index_name_one(tenant_id, kb.name)
    rows = await settled_io(graph_rows, index_name, kb_id, ["subgraph"])
    excluded = set(exclude_rebuild) if isinstance(exclude_rebuild, list) else {exclude_rebuild} if exclude_rebuild else set()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        sources = row.get("source_id", [])
        if isinstance(sources, str):
            sources = sources.split("\n") if sources else []
        if row.get("removed_kwd", "N") != "N" or excluded.intersection(sources):
            continue
        if len(sources) != 1:
            raise ValueError("Invalid per-document subgraph provenance")
        grouped.setdefault(sources[0], []).append(row)
    checkpoints: dict[str, dict[str, Any]] = {}
    for source, candidates in grouped.items():
        stable = chunk_id({"content_with_weight": f"subgraph::{source}", "kb_id": kb_id})
        selected = [row for row in candidates if row["pk"] == stable] or candidates
        if len(selected) != 1:
            raise ValueError("Ambiguous subgraph checkpoints during rebuild")
        checkpoints[source] = selected[0]

    graph = nx.Graph()
    for source, row in sorted(checkpoints.items()):
        next_graph = json_graph.node_link_graph(json.loads(row["content_with_weight"]), edges="edges")
        next_graph.graph["source_id"] = [source]
        merged_graph = nx.compose(graph, next_graph)
        merged_source = {node: sorted(set(graph.nodes[node]["source_id"] + next_graph.nodes[node]["source_id"])) for node in graph.nodes & next_graph.nodes}
        nx.set_node_attributes(merged_graph, merged_source, "source_id")
        merged_graph.graph["source_id"] = graph.graph.get("source_id", []) + [source]
        graph = merged_graph
    return graph if graph.number_of_nodes() else None
