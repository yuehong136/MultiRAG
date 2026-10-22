"""Installed NER models through real HTTP config, SQL and Milvus checkpoints."""

import json
from typing import Any

import pytest
import sqlalchemy as sa

from api.db.db_models import Knowledgebase
from common import settings
from core.graphrag import utils
from core.graphrag.general import index
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api
from tests.support.graphrag_acceptance import Embedding, api, native_rows, observe
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api


class NoChat:
    llm_name = "ner-no-chat"
    max_length = 8192

    async def async_chat(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("This small NER graph must not call the LLM")


@pytest.mark.parametrize(
    ("language", "content", "expected"),
    [
        ("English", "Barack Obama visited Microsoft in London.", {"BARACK OBAMA", "MICROSOFT", "LONDON"}),
        ("Chinese", "张三在北京大学工作。李四在上海工作。", {"北京大学", "上海"}),
    ],
)
async def test_real_ner_config_graph_storage_and_retrieval(graphrag_api: dict[str, Any], language: str, content: str, expected: set[str]) -> None:
    env, kb, doc = graphrag_api, graphrag_api["kb"], graphrag_api["documents"][0]
    cfg = {"use_graphrag": True, "method": "ner", "resolution": False, "community": False, "entity_types": ["organization", "person", "geo", "event", "category"]}
    response = request_api(env, "PUT", f"/datasets/{kb}", credential=env["jwt"][0], payload={"parser_config": {"graphrag": cfg}})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    reload = request_api(env, "GET", f"/datasets/{kb}", credential=env["jwt"][0]).json()
    assert reload["code"] == 0 and reload["data"]["parser_config"]["graphrag"]["method"] == "ner"
    with env["engine"].connect() as db:
        parser_config = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == kb))
    assert parser_config["graphrag"]["method"] == "ner"
    source = next(row for row in native_rows(env) if row["doc_id"] == doc)
    assert settings.docStoreConn.insert([{"id": source["pk"], "kb_id": kb, "doc_id": doc, "content_with_weight": content, "available_int": 1, "q_2_vec": [0.25, 0.75]}], env["collection"], kb) == []
    admission = api(env, "POST")
    assert admission["code"] == 0
    task_id = admission["data"]["task_id"]
    result = await index.run_graphrag_for_kb(
        {"tenant_id": env["tenant"], "kb_id": kb, "id": task_id}, [doc], language, parser_config, NoChat(), Embedding(), lambda **kw: None, with_resolution=False, with_community=False
    )
    assert result["ok_docs"] == [doc] and not result["failed_docs"], result
    graph = await utils.get_graph(env["tenant"], kb)
    assert graph is not None and expected <= set(graph.nodes) and graph.number_of_edges() > 0
    if language == "Chinese":
        assert "北京 大学" not in graph.nodes
    rows = native_rows(env)
    assert {"entity", "relation", "graph", "subgraph"} <= {r["knowledge_graph_kwd"] for r in rows if r.get("knowledge_graph_kwd")}
    assert all(r["content_with_weight"] for r in rows if r.get("knowledge_graph_kwd") in {"entity", "relation"})
    snapshot = observe(env, "ner-persisted")
    assert not snapshot["writer_active"]
    graph_http = request_api(env, "GET", f"/datasets/{kb}/graph", credential=env["jwt"][0])
    assert graph_http.status_code == 200 and graph_http.json()["code"] == 0, graph_http.text
    env["record"]["ner"] = {"language": language, "result": result, "expected": sorted(expected), "graph_http": graph_http.json(), "nodes": sorted(graph.nodes)}
    # Replay must use the published graph and must not duplicate identities.
    before = {r["pk"] for r in rows}
    await index.run_graphrag_for_kb(
        {"tenant_id": env["tenant"], "kb_id": kb, "id": task_id}, [doc], language, parser_config, NoChat(), Embedding(), lambda **kw: None, with_resolution=False, with_community=False
    )
    assert {r["pk"] for r in native_rows(env)} == before
    env["graph_record_path"].write_text(json.dumps(env["record"], default=str, indent=2))
