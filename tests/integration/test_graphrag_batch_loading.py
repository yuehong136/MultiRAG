"""HTTP config to real SQL/Milvus/Redis GraphRAG with controlled extraction."""

from copy import deepcopy
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.db_models import Document, Knowledgebase
from common import settings
from core.graphrag.general import index
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api
from tests.support.graphrag_acceptance import DocumentExtractor, Model, api, observe, worker
from tests.support.graphrag_acceptance import graphrag_api as graphrag_api


@pytest.mark.parametrize(("method", "budget", "expected_batches"), [("light", 512, 2), ("general", 4096, 1), ("ner", 512, 2)])
async def test_saved_batch_budget_reaches_worker_and_resume_avoids_source_io(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str, budget: int, expected_batches: int) -> None:
    env = graphrag_api
    doc_id = env["documents"][0]
    initial = env["reader"].query(env["collection"], filter=f'doc_id == "{doc_id}"', output_fields=["pk"], consistency_level="Strong")
    settings.docStoreConn.graph_delete_rows(env["collection"], env["kb"], [row["pk"] for row in initial])
    source = [" alpha" * 300, " beta" * 300]
    rows = [
        {"id": uuid4().hex, "doc_id": doc_id, "kb_id": env["kb"], "page_num_int": page, "content_with_weight": content, "available_int": 1, "q_2_vec": [0.25, 0.75]}
        for page, content in enumerate(source)
    ]
    assert settings.docStoreConn.insert(rows, env["collection"], env["kb"]) == []
    parser_config = deepcopy(env["parser_config"])
    parser_config["graphrag"].update(method=method, batch_chunk_token_size=budget, resolution=False, community=False)
    saved = request_api(env, "PUT", f"/datasets/{env['kb']}", credential=env["jwt"][0], payload={"parser_config": parser_config})
    assert saved.status_code == 200 and saved.json()["code"] == 0, saved.text
    with env["engine"].connect() as db:
        stored = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == env["kb"]))
    assert stored["graphrag"]["batch_chunk_token_size"] == budget
    captured: list[list[str]] = []

    class RecordingExtractor(DocumentExtractor):
        async def __call__(self, doc: str, chunks: list[str], callback: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            captured.append(list(chunks))
            return await super().__call__(doc, chunks, callback, **kwargs)

    for name in ["LightKGExt", "GeneralKGExt", "NerKGExt"]:
        monkeypatch.setattr(index, name, RecordingExtractor)
    reads = Mock(wraps=settings.docStoreConn.graph_source_rows)
    monkeypatch.setattr(settings.docStoreConn, "graph_source_rows", reads)
    task_id = api(env, "POST")["data"]["task_id"]
    await worker(env, task_id, Model())
    assert len(captured) == 1 and len(captured[0]) == expected_batches and "".join(captured[0]) == "".join(source)
    if method == "ner":
        assert captured[0] == source
    assert reads.call_count == 1
    first = observe(env, "first batch-budget run")
    assert any(row.get("knowledge_graph_kwd") == "graph" for row in first["rows"]) and not first["writer_active"]
    assert next(row for row in first["task_rows"] if row["id"] == task_id)["progress"] == 1
    # The second real queued invocation reuses the committed graph and performs
    # neither a source read nor extraction, even with zero new chunk batches.
    resumed_id = api(env, "POST")["data"]["task_id"]
    await worker(env, resumed_id, Model())
    assert reads.call_count == 1 and len(captured) == 1
    resumed = observe(env, "resumed without source read")
    assert resumed["rows"] == first["rows"] and not resumed["writer_active"]
    env["record"]["batch_budget"] = {"method": method, "saved": budget, "batch_sizes": [len(batch) for batch in captured[0]], "source_reads_after_resume": reads.call_count}


async def test_large_ner_source_snapshot_is_complete_without_generic_chunk_pagination(graphrag_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = graphrag_api
    doc_id, count = env["documents"][0], 10005
    original = env["reader"].query(env["collection"], filter=f'doc_id == "{doc_id}"', output_fields=["pk"], consistency_level="Strong")
    settings.docStoreConn.graph_delete_rows(env["collection"], env["kb"], [row["pk"] for row in original])
    rows = [
        {"id": uuid4().hex, "kb_id": env["kb"], "doc_id": doc_id, "page_num_int": page, "content_with_weight": f"source-{page}", "available_int": 1, "q_2_vec": [0.25, 0.75]} for page in range(count)
    ]
    for offset in range(0, count, 512):
        assert settings.docStoreConn.insert(rows[offset : offset + 512], env["collection"], env["kb"]) == []
    # Independent native iterator proves the actual committed set, rather than
    # the bounded convenience native_rows() observation used by smaller tests.
    iterator = env["reader"].query_iterator(collection_name=env["collection"], filter=f'doc_id == "{doc_id}"', output_fields=["pk"], batch_size=512, consistency_level="Strong")
    stored: set[str] = set()
    try:
        while batch := iterator.next():
            stored.update(row["pk"] for row in batch)
    finally:
        iterator.close()
    assert stored == {row["id"] for row in rows}
    complete = settings.retriever.chunk_list(doc_id, env["tenant"], [env["kb"]], fields=["content_with_weight"], sort_by_position=True, retrieve_all=True)
    assert len(complete) == count and [row["content_with_weight"] for row in complete] == [f"source-{page}" for page in range(count)]
    with env["engine"].begin() as db:
        parser_config = deepcopy(env["parser_config"])
        parser_config["graphrag"].update(method="ner", batch_chunk_token_size=512, resolution=False, community=False)
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(parser_config=parser_config, chunk_num=count))
        db.execute(sa.update(Document).where(Document.id == doc_id).values(chunk_num=count))
    seen: list[str] = []

    class LargeSourceExtractor(DocumentExtractor):
        async def __call__(self, doc: str, chunks: list[str], callback: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            seen.extend(chunks)
            return await super().__call__(doc, chunks, callback, **kwargs)

    monkeypatch.setattr(index, "NerKGExt", LargeSourceExtractor)
    task_id = api(env, "POST")["data"]["task_id"]
    await worker(env, task_id, Model())
    assert seen == [f"source-{page}" for page in range(count)]
    graphs = env["reader"].query(env["collection"], filter='knowledge_graph_kwd == "graph"', output_fields=["source_id"], consistency_level="Strong")
    assert len(graphs) == 1
    sources = graphs[0]["source_id"]
    assert (sources.split("\n") if isinstance(sources, str) else sources) == [doc_id]
    env["record"]["large_source_snapshot"] = {"committed_rows": len(stored), "extracted_chunks": len(seen), "complete_order": True, "generic_chunk_list_used": False}
