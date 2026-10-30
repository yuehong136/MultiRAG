"""Real Milvus path readback and KGSearch consumption; not OceanBase DDL proof."""

import json
from typing import Any

import networkx as nx
import numpy as np

from common import settings
from core.graphrag import utils
from core.graphrag.search import KGSearch
from tests.support.dataset_management_http import management_api as management_api
from tests.support.graphrag_acceptance import Embedding, native_rows
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api


class QueryEmbedding(Embedding):
    def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
        assert text == "ALICE"
        return np.array([0.25, 0.75]), 0


async def test_entity_rank_and_paths_reach_real_keyword_retrieval(graphrag_api: dict[str, Any]) -> None:
    env = graphrag_api
    graph = nx.Graph()
    graph.graph["source_id"] = [env["documents"][0]]
    for name in ["ALICE", "BOB", "CAROL"]:
        graph.add_node(name, entity_type="person", description=name, source_id=graph.graph["source_id"])
    graph.add_edge("ALICE", "BOB", weight=3, description="knows", keywords=["knows"], source_id=graph.graph["source_id"])
    graph.add_edge("BOB", "CAROL", weight=7, description="knows", keywords=["knows"], source_id=graph.graph["source_id"])
    nx.set_node_attributes(graph, nx.pagerank(graph), "pagerank")
    change = utils.GraphChange(added_updated_nodes=set(graph.nodes), added_updated_edges=set(graph.edges))
    await utils.set_graph(env["tenant"], env["kb"], QueryEmbedding(), graph, change, None)
    # A separate native client reads committed rows, outside producer output.
    persisted = {row["entity_kwd"]: row for row in native_rows(env) if row.get("knowledge_graph_kwd") == "entity"}
    expected = [{"path": ["ALICE", "BOB", "CAROL"], "weights": [3.0, 7.0]}]
    assert json.loads(persisted["ALICE"]["n_hop_with_weight"]) == expected
    # rank_flt is a Milvus FLOAT; compare its exact float32 representation.
    assert np.float32(persisted["ALICE"]["rank_flt"]) == np.float32(graph.nodes["ALICE"]["pagerank"]) > 0
    result = await KGSearch(settings.docStoreConn).get_relevant_ents_by_keywords(["ALICE"], {}, [env["collection"]], [env["kb"]], QueryEmbedding())
    assert result["ALICE"]["n_hop_ents"] == expected
    assert result["ALICE"]["pagerank"] == persisted["ALICE"]["rank_flt"]
    assert result["ALICE"]["sim"] > 0.99
    env["record"]["entity_path_consumer"] = {"backend": "milvus", "persisted": persisted, "consumer": result}
