"""Real ES/OpenSearch ranking, payload and citation reads on owned resources."""

import asyncio
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import numpy as np
import pytest
from elasticsearch import Elasticsearch
from opensearchpy import OpenSearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import Document, Knowledgebase
from api.db.services import dialog_service
from common import settings
from common.doc_store.doc_store_base import MatchDenseExpr, OrderByExpr
from core.nlp import search
from core.nlp.search import Dealer
from core.utils.es_conn import ESConnection
from core.utils.opensearch_conn import OSConnection
from tests.support.database import scratch_database


@pytest.fixture(params=["elasticsearch", "opensearch"])
def scored_store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, postgres_service: None) -> Iterator[dict[str, Any]]:
    backend = request.param
    url = os.environ.get("METADATA_TEST_ES_URL" if backend == "elasticsearch" else "METADATA_TEST_OS_URL")
    assert url, "Prepare an owned ES/OS listener before collection"
    evidence = Path(os.environ.get("SCORING_EVIDENCE", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    with scratch_database(evidence) as engine, ExitStack() as cleanup:
        owner, kb = uuid4().hex, uuid4().hex
        name = "scoring_" + uuid4().hex
        index_name = search.index_name_one(owner, name)
        ids = {key: uuid4().hex for key in ("semantic", "lexical", "negative", "missing", "hidden", "foreign")}
        client_type = Elasticsearch if backend == "elasticsearch" else OpenSearch
        transport = {"request_timeout": 60} if backend == "elasticsearch" else {"timeout": 60}
        client = client_type(url, **transport)
        reader = client_type(url, **transport)
        cleanup.callback(client.close)
        cleanup.callback(reader.close)
        cls = next(cell.cell_contents for cell in (ESConnection if backend == "elasticsearch" else OSConnection).__closure__ if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        if backend == "elasticsearch":
            store.es = client
            store.logger = logging.getLogger(__name__)
            vector = {"type": "dense_vector", "dims": 512, "index": True, "similarity": "cosine"}
        else:
            store.os = client
            vector = {"type": "knn_vector", "dimension": 512, "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}}
        properties = {
            **{field: {"type": "keyword"} for field in ["id", "kb_id", "doc_id", "docnm_kwd"]},
            **{field: {"type": "text", "analyzer": "whitespace"} for field in ["content_ltks", "content_with_weight"]},
            "available_int": {"type": "integer"},
            "q_512_vec": vector,
        }
        body = {"settings": {"number_of_shards": 1, "number_of_replicas": 0, **({"index.knn": True} if backend == "opensearch" else {})}, "mappings": {"properties": properties}}
        manifest = {"backend": backend, "url": url, "index": index_name, "database": engine.url.database, "version": dict(reader.info())["version"], "cleaned": False}
        manifest_file = evidence / f"{index_name}.json"
        manifest_file.write_text(json.dumps(manifest, indent=2))

        def remove() -> None:
            # A create timeout has an unknown outcome. Drain queued cluster
            # state operations before verifying/removing the exact owned name.
            reader.cluster.health(wait_for_events="languid")
            if reader.indices.exists(index=index_name):
                reader.indices.delete(index=index_name)
            assert not reader.indices.exists(index=index_name)
            manifest["cleaned"] = True
            manifest_file.write_text(json.dumps(manifest, indent=2))

        cleanup.callback(remove)
        client.indices.create(index=index_name, body=body, master_timeout="60s")
        rows = {}
        with Session(engine) as db:
            db.add(Knowledgebase(id=kb, tenant_id=owner, created_by=owner, name=name, parser_config={}, parser_id="naive", embd_id="controlled"))
            for key, identifier in ids.items():
                db.add(Document(id=identifier, kb_id=kb, name=key, created_by=owner, parser_id="naive", parser_config={}, type="txt", location="", auth=""))
            db.commit()
        for key, identifier in ids.items():
            cosine = {"semantic": 1.0, "lexical": 0.1, "negative": -1.0, "missing": 0.0, "hidden": 1.0, "foreign": 1.0}[key]
            rows[identifier] = {
                "id": identifier,
                "kb_id": "foreign-kb" if key == "foreign" else kb,
                "doc_id": identifier,
                "docnm_kwd": key,
                "content_ltks": "rivet" if key in {"lexical", "negative", "missing"} else "fastener",
                "content_with_weight": "rivet" if key in {"lexical", "negative", "missing"} else "fastener",
                "q_512_vec": [cosine, math.sqrt(max(0.0, 1 - cosine * cosine))] + [0.0] * 510,
                "available_int": 0 if key == "hidden" else 1,
            }
            if key == "missing":
                rows[identifier].pop("q_512_vec")
            client.index(index=index_name, id=identifier, body=rows[identifier], refresh=True)
            assert reader.get(index=index_name, id=identifier)["_source"] == rows[identifier]
        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        @asynccontextmanager
        async def connection() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        @contextmanager
        def sync_connection() -> Iterator[Session]:
            with Session(engine) as db:
                yield db

        monkeypatch.setattr(search, "async_db_connection", connection)
        monkeypatch.setattr(search, "db_connection", sync_connection)
        dealer = Dealer(store)
        # Keep the term signal deterministic while using the production vector,
        # candidate, scoping, pruning, rerank, pagination and threshold paths.
        dealer.qryr.flds = ["content_ltks"]
        yield {
            "dealer": dealer,
            "store": store,
            "reader": reader,
            "index": index_name,
            "kb": kb,
            "owner": owner,
            "name": name,
            "ids": ids,
            "rows": rows,
            "manifest": manifest,
            "manifest_file": manifest_file,
            "engine": engine,
            "sessions": sessions,
        }


class _Embedding:
    def encode_queries(self, _question: str) -> tuple[list[float], int]:
        return [1.0] + [0.0] * 511, 1

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        return np.array([[1.0] + [0.0] * 511 for _ in texts]), len(texts)


class _CitationModel(_Embedding):
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.db = None
        self.max_length = 8192

    def chat(self, *_args: Any, **_kwargs: Any) -> str:
        return "A rivet is a fastener."

    async def async_chat(self, *_args: Any, **_kwargs: Any) -> str:
        return self.chat()

    async def async_chat_streamly_delta(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        yield self.chat()

    def chat_streamly(self, *_args: Any, **_kwargs: Any) -> Iterator[str]:
        yield self.chat()


async def test_exact_server_cosines_include_negative_and_omit_vector_payload(scored_store: dict[str, Any]) -> None:
    env = scored_store
    dealer, ids = env["dealer"], env["ids"]
    chosen = [ids[key] for key in ["semantic", "lexical", "negative", "missing"]]
    result = Dealer.SearchResult(4, chosen, [1.0] + [0.0] * 511, {identifier: env["rows"][identifier] for identifier in chosen})
    scores = await dealer._knn_scores(result, [env["index"]], [env["kb"]], {"available_int": 1, "doc_id": chosen})
    # The chunk without a stored vector has no cosine to report; it ranks as 0.0.
    assert scores[ids["missing"]] is None
    assert {key: value for key, value in scores.items() if key != ids["missing"]} == pytest.approx(dict(zip(chosen[:3], [1.0, 0.1, -1.0], strict=True)), abs=1e-6)
    raw = env["store"].search([], [], {"id": chosen}, [MatchDenseExpr("q_512_vec", [1.0] + [0.0] * 511, "float", "cosine", 4, {"exact": True})], OrderByExpr(), 0, 4, [env["index"]], [env["kb"]])
    ordinary = env["store"].search([], [], {"id": chosen}, [], OrderByExpr(), 0, 4, [env["index"]], [env["kb"]])
    assert all("_source" in hit for hit in ordinary["hits"]["hits"])
    assert all("_source" not in hit and "fields" not in hit for hit in raw["hits"]["hits"])
    with_vectors = env["store"].search(["q_512_vec"], [], {"id": chosen}, [], OrderByExpr(), 0, 4, [env["index"]], [env["kb"]])
    sizes = {"headersBytes": len(json.dumps(dict(raw))), "vectorBytes": len(json.dumps(dict(with_vectors)))}
    assert sizes["vectorBytes"] > sizes["headersBytes"] * 5
    env["manifest"].update(scores=scores, payload=sizes, scoringCalls=1)
    env["manifest_file"].write_text(json.dumps(env["manifest"], indent=2))


async def test_live_retrieval_modes_threshold_weights_external_rerank_and_hydration(scored_store: dict[str, Any]) -> None:
    env, embedding = scored_store, _Embedding()
    dealer, ids = env["dealer"], env["ids"]
    kwargs = {"similarity_threshold": 0.05, "kb_ids": [env["kb"]], "rank_feature": {}}

    async def retrieve(mode: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return await dealer.retrieval("rivet", "", embedding, env["owner"], [env["name"]], 1, 10, search_mode=mode, **{**kwargs, **extra})

    dense = await retrieve({"dense": {}})
    assert [chunk["chunk_id"] for chunk in dense["chunks"]] == [ids["semantic"], ids["lexical"]]
    assert [chunk["vector_similarity"] for chunk in dense["chunks"]] == pytest.approx([1.0, 0.1], abs=1e-6)
    assert all(chunk["vector"] == [0.0] * 512 for chunk in dense["chunks"])
    hybrid = await retrieve({"hybrid": {"weight_sparse": 0.95, "weight_dense": 0.05}})
    assert hybrid["chunks"][0]["chunk_id"] == ids["lexical"]
    sparse = await retrieve({"sparse": {}})
    assert {chunk["chunk_id"] for chunk in sparse["chunks"]} == {ids["lexical"], ids["negative"], ids["missing"]}
    threshold = await retrieve({"dense": {}}, similarity_threshold=0.5)
    assert [chunk["chunk_id"] for chunk in threshold["chunks"]] == [ids["semantic"]]
    reranker = Mock()
    reranker.similarity.side_effect = lambda _question, texts: (np.array([0.9 if "rivet" in text else 0.2 for text in texts]), 1)
    external = await retrieve({"dense": {}}, rerank_mdl=reranker, vector_similarity_weight=1.0)
    assert external["chunks"][0]["chunk_id"] == ids["lexical"]
    assert reranker.similarity.call_count == 1
    vectors = await dealer.fetch_chunk_vectors([ids["semantic"], ids["hidden"], ids["foreign"]], env["owner"], [env["kb"]], 512, kb_names=[env["name"]], doc_ids=[ids["semantic"]])
    assert vectors == {ids["semantic"]: [1.0] + [0.0] * 511}
    missing = await dealer.fetch_chunk_vectors([ids["missing"]], env["owner"], [env["kb"]], 512, kb_names=[env["name"]], doc_ids=[ids["missing"]])
    assert missing == {ids["missing"]: [0.0] * 512}
    # Cosine indexes reject zero vectors at ingestion; they cannot be silently
    # admitted as valid cosine documents by either real engine.
    with pytest.raises(Exception, match=r"400|zero|vector"):
        env["store"].es.index(index=env["index"], id="zero", body={**env["rows"][ids["semantic"]], "q_512_vec": [0.0] * 512}, refresh=True) if hasattr(env["store"], "es") else env["store"].os.index(
            index=env["index"], id="zero", body={**env["rows"][ids["semantic"]], "q_512_vec": [0.0] * 512}, refresh=True
        )
    assert not env["reader"].exists(index=env["index"], id="zero")
    empty = await retrieve({"dense": {}}, kb_ids=[])
    assert empty["chunks"] == [] and empty["total"] == 0


async def test_real_storage_scoring_failure_propagates_without_writes(scored_store: dict[str, Any]) -> None:
    env = scored_store
    bad = Dealer.SearchResult(1, [env["ids"]["semantic"]], [1.0, 0.0], {env["ids"]["semantic"]: {"kb_id": env["kb"]}})
    before = env["reader"].count(index=env["index"])["count"]
    with pytest.raises(Exception, match=r"400|script|dimension"):
        await env["dealer"]._knn_scores(bad, [env["index"]], [env["kb"]], {})
    assert env["reader"].count(index=env["index"])["count"] == before
    assert env["reader"].indices.stats(index=env["index"])["indices"][env["index"]]["total"]["search"]["open_contexts"] == 0


@pytest.mark.parametrize("consumer", ["chat-stream", "chat-full", "ask", "sync-chat", "sync-ask", "no-citations"])
async def test_actual_chat_and_ask_finalize_citations_from_real_storage(scored_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, consumer: str) -> None:
    env = scored_store
    dealer = env["dealer"]
    monkeypatch.setattr(settings, "DOC_ENGINE", env["manifest"]["backend"])
    monkeypatch.setitem(settings.__dict__, "retriever", dealer)
    monkeypatch.setattr(dialog_service, "resolve_model_type", lambda *_: ["chat"])
    monkeypatch.setattr(dialog_service, "_resolve_dialog_primary_model_config", lambda *_: {"llm_factory": "controlled", "max_tokens": 8192})
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda *_: {"llm_name": "controlled"})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda *_: {"llm_name": "controlled"})
    monkeypatch.setattr(dialog_service, "LLMBundle", _CitationModel)
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", lambda *_a, **_k: None)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda *_: {})
    monkeypatch.setattr(dialog_service, "label_question", lambda *_: {})

    def get_models(db: Session, _dialog: Any) -> tuple[Any, ...]:
        return dialog_service.KnowledgebaseService.get_by_ids(db, [env["kb"]]), _CitationModel(), None, _CitationModel(), None

    monkeypatch.setattr(dialog_service, "get_models", get_models)
    hydrated = AsyncMock(wraps=dealer.fetch_chunk_vectors)
    monkeypatch.setattr(dealer, "fetch_chunk_vectors", hydrated)
    dialog = SimpleNamespace(
        tenant_id=env["owner"],
        kb_ids=[env["kb"]],
        llm_id="controlled",
        tenant_llm_id=None,
        prompt_config={"system": "Answer using {knowledge}", "parameters": [{"key": "knowledge", "optional": False}], "quote": consumer != "no-citations"},
        llm_setting={},
        meta_data_filter=None,
        top_n=3,
        similarity_threshold=0.05,
        vector_similarity_weight=0.3,
        search_mode=None,
    )
    messages = [{"role": "user", "content": "rivet"}]
    if consumer.startswith("sync-"):

        def sync_consumer() -> list[dict[str, Any]]:
            with Session(env["engine"]) as db:
                if consumer == "sync-chat":
                    return list(dialog_service.chat(dialog, messages, db, stream=False))
                return list(dialog_service.ask(db, "rivet", [env["kb"]], env["owner"]))

        events = await asyncio.to_thread(sync_consumer)
    else:
        async with env["sessions"]() as db:
            if consumer == "ask":
                events = [event async for event in dialog_service.async_ask(db, "rivet", [env["kb"]], env["owner"])]
            else:
                events = [event async for event in dialog_service.async_chat(dialog, messages, db, stream=consumer == "chat-stream")]
    assert events[-1]["answer"]
    if consumer == "no-citations":
        hydrated.assert_not_awaited()
        assert events[-1]["reference"] == []
    else:
        assert hydrated.await_count == 1
        assert "[ID:" in events[-1]["answer"] or "##" in events[-1]["answer"]
        assert events[-1]["reference"]["chunks"]
        assert "vector" not in events[-1]["reference"]["chunks"][0]


async def test_parent_keeps_child_citation_vector_in_real_index(scored_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scored_store
    identifier = env["ids"]["semantic"]
    parent_id = uuid4().hex
    parent = {**env["rows"][identifier], "id": parent_id, "available_int": 0, "content_with_weight": "Parent paragraph"}
    parent.pop("q_512_vec")
    writer = env["store"].es if hasattr(env["store"], "es") else env["store"].os
    writer.index(index=env["index"], id=parent_id, body=parent, refresh=True)
    child = {"chunk_id": identifier, "mom_id": parent_id, "kb_id": env["kb"], "doc_id": identifier, "content_ltks": "rivet", "similarity": 1.0, "vector": [0.0] * 512}
    parents = env["dealer"].retrieval_by_children([child], [env["owner"]])
    assert len(parents) == 1 and parents[0]["chunk_id"] == parent_id
    monkeypatch.setattr(settings, "DOC_ENGINE", env["manifest"]["backend"])
    await dialog_service._hydrate_chunk_vectors(env["dealer"], parents, [SimpleNamespace(id=env["kb"], name=env["name"], tenant_id=env["owner"])])
    assert parents[0]["vector"] == [1.0] + [0.0] * 511


def test_complete_chunk_list_crosses_result_window_and_closes_failed_scroll(scored_store: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scored_store
    doc_id, count = env["ids"]["semantic"], 10005
    writer = env["store"].es if hasattr(env["store"], "es") else env["store"].os
    operations = []
    identifiers = []
    for number in range(1, count + 1):
        identifier = uuid4().hex
        identifiers.append(identifier)
        operations.extend(
            [
                {"index": {"_index": env["index"], "_id": identifier}},
                {"id": identifier, "kb_id": env["kb"], "doc_id": doc_id, "content_with_weight": f"source {number}", "page_num_int": number, "available_int": 1},
            ]
        )
    reply = writer.bulk(body=operations, refresh=True)
    assert reply["errors"] is False
    assert env["reader"].count(index=env["index"], body={"query": {"term": {"doc_id": doc_id}}})["count"] == count + 1
    fields = ["doc_id", "content_with_weight"]
    assert len(env["dealer"].chunk_list(doc_id, env["owner"], [env["kb"]], max_count=130, fields=fields, sort_by_position=True)) == 130
    complete = env["dealer"].chunk_list(doc_id, env["owner"], [env["kb"]], fields=fields, sort_by_position=True, retrieve_all=True)
    assert len(complete) == count + 1 and len({row["id"] for row in complete}) == count + 1
    assert {row["id"] for row in complete} == {doc_id, *identifiers}
    assert complete[-1]["content_with_weight"] == "source 10005"
    assert all(row["doc_id"] == doc_id for row in complete)
    tail = env["dealer"].chunk_list(doc_id, env["owner"], [env["kb"]], offset=10000, fields=fields, sort_by_position=True, retrieve_all=True)
    assert [row["id"] for row in tail] == [row["id"] for row in complete[10000:]]
    with monkeypatch.context() as scope:
        original_request = writer.transport.perform_request

        def fail_scroll(method: str, target: str, *args: Any, **kwargs: Any) -> Any:
            if method in {"GET", "POST"} and target.split("?", 1)[0] == "/_search/scroll":
                raise RuntimeError("injected mid-scroll failure")
            return original_request(method, target, *args, **kwargs)

        # ES helpers clone clients through options(); the underlying transport
        # is the real common boundary. DELETE remains live for cursor cleanup.
        scope.setattr(writer.transport, "perform_request", fail_scroll)
        with pytest.raises(RuntimeError, match="mid-scroll"):
            env["dealer"].chunk_list(doc_id, env["owner"], [env["kb"]], fields=fields, retrieve_all=True)
    assert env["reader"].indices.stats(index=env["index"])["indices"][env["index"]]["total"]["search"]["open_contexts"] == 0
    env["manifest"]["retrieve_all"] = {"source_rows": count + 1, "legacy_cap": 130, "offset_tail": len(tail), "failed_scroll_closed": True}
    env["manifest_file"].write_text(json.dumps(env["manifest"], indent=2))
