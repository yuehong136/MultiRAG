"""Real Canvas Iteration, scoped SQL metadata, and owned Elasticsearch recall."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from elasticsearch import Elasticsearch
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from agent.canvas import Canvas
from agent.tools import retrieval as module
from agent.tools.retrieval import Retrieval
from api.db.db_models import Document, DocumentMetadata, Knowledgebase, UserTenant
from common import settings
from core.nlp import search
from core.nlp.search import Dealer
from core.utils.es_conn import ESConnection
from tests.support.database import scratch_database


class ControlledEmbedding:
    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0] + [0.0] * 511, 1


@pytest.fixture
def scoped_recall(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    url = os.environ.get("METADATA_TEST_ES_URL")
    assert url, "Provide a prepared ES8 listener; a missing engine is not a skip"
    token = os.environ.get("G_RETRIEVAL_TEST_TOKEN") or uuid4().hex
    suffix = {
        "test_canvas_iteration_retrieves_new_metadata_value_each_round": "iteration",
        "test_typed_manual_constants_and_no_match_reach_real_search": "typed",
        "test_denied_dataset_prevents_model_metadata_and_search_side_effects": "denied",
    }[request.node.name]
    name = f"manual_{suffix}"
    index = search.index_name_one(token, name)
    manifest = {"index": index, "database": None, "created": False, "absent": False}
    record = tmp_path / "search-resource.json"
    record.write_text(json.dumps(manifest, indent=2))
    with scratch_database(tmp_path / "database") as engine, ExitStack() as cleanup:
        client = cleanup.enter_context(Elasticsearch(url, request_timeout=20))
        reader = cleanup.enter_context(Elasticsearch(url, request_timeout=20))
        assert reader.info()["version"]["number"].startswith("8.")
        assert not reader.indices.exists(index=index)
        manifest["database"] = engine.url.database
        record.write_text(json.dumps(manifest, indent=2))

        def remove_index() -> None:
            reader.cluster.health(wait_for_events="languid")
            if reader.indices.exists(index=index):
                reader.indices.delete(index=index)
            assert not reader.indices.exists(index=index)
            manifest["absent"] = True
            record.write_text(json.dumps(manifest, indent=2))

        cleanup.callback(remove_index)
        mapping = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
            "mappings": {
                "properties": {
                    **{field: {"type": "keyword"} for field in ["id", "kb_id", "doc_id", "docnm_kwd"]},
                    "content_ltks": {"type": "text", "analyzer": "whitespace"},
                    "content_with_weight": {"type": "text"},
                    "available_int": {"type": "integer"},
                    "q_512_vec": {"type": "dense_vector", "dims": 512, "index": True, "similarity": "cosine"},
                }
            },
        }
        client.indices.create(index=index, body=mapping)
        manifest["created"] = True
        record.write_text(json.dumps(manifest, indent=2))
        ids = {key: uuid4().hex for key in ["actor", "outsider", "kb", "foreign_kb", "alice", "bob", "foreign"]}
        with Session(engine) as db:
            db.add(UserTenant(id=uuid4().hex, tenant_id=token, user_id=ids["actor"], role="owner", invited_by=ids["actor"], status="1"))
            for key in ["kb", "foreign_kb"]:
                db.add(Knowledgebase(id=ids[key], tenant_id=token, created_by=ids["actor"], name=name if key == "kb" else "foreign", embd_id="controlled", permission="me", status="1"))
            for key, tag, typed in [("alice", "Alice", 0), ("bob", "Bob", False), ("foreign", "Alice", 0)]:
                kb_id = ids["foreign_kb"] if key == "foreign" else ids["kb"]
                db.add(Document(id=ids[key], kb_id=kb_id, created_by=ids["actor"], name=key, parser_id="naive", type="txt", status="1"))
                db.add(DocumentMetadata(id=ids[key], tenant_id=token, kb_id=kb_id, meta_fields={"tag": tag, "typed": typed}))
            db.commit()
        rows = {}
        for key in ["alice", "bob", "foreign"]:
            row = {
                "id": ids[key],
                "kb_id": ids["foreign_kb"] if key == "foreign" else ids["kb"],
                "doc_id": ids[key],
                "docnm_kwd": key,
                "content_ltks": "rivet",
                "content_with_weight": f"rivet {key}",
                "available_int": 1,
                "q_512_vec": [1.0] + [0.0] * 511,
            }
            client.index(index=index, id=ids[key], document=row, refresh=True)
            assert reader.get(index=index, id=ids[key])["_source"] == row
            rows[ids[key]] = row

        cls = next(cell.cell_contents for cell in ESConnection.__closure__ if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store.es = client
        store.logger = logging.getLogger(__name__)
        dealer = Dealer(store)
        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        active_sessions = 0

        @contextmanager
        def connection() -> Iterator[Session]:
            nonlocal active_sessions
            with Session(engine) as db:
                active_sessions += 1
                try:
                    yield db
                finally:
                    active_sessions -= 1

        @asynccontextmanager
        async def async_connection() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        built: list[str] = []

        async def embedding(tenant_id: str, *args: Any) -> ControlledEmbedding:
            assert active_sessions == 0
            built.append(tenant_id)
            return ControlledEmbedding()

        calls: list[dict[str, Any]] = []
        retrieve = dealer.retrieval

        async def observed_retrieval(*args: Any, **kwargs: Any) -> dict[str, Any]:
            assert active_sessions == 0
            result = await retrieve(*args, **kwargs)
            calls.append({"doc_ids": deepcopy(kwargs["doc_ids"]), "chunks": deepcopy(result["chunks"])})
            return result

        monkeypatch.setattr(module, "db_connection", connection)
        monkeypatch.setattr(search, "db_connection", connection)
        monkeypatch.setattr(search, "async_db_connection", async_connection)
        monkeypatch.setattr(module, "build_named_bundle_async", embedding)
        monkeypatch.setattr(Retrieval, "_rank_feature", staticmethod(lambda *_: {}))
        monkeypatch.setattr(dealer, "retrieval", observed_retrieval)
        monkeypatch.setitem(vars(settings), "retriever", dealer)
        # Use the production SQL metadata store, while the production Dealer
        # and ES adapter execute real chunk recall independently.
        monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")
        yield {"engine": engine, "ids": ids, "owner": token, "reader": reader, "index": index, "rows": rows, "built": built, "calls": calls}
        with Session(engine) as db:
            assert dict(db.execute(sa.select(DocumentMetadata.id, DocumentMetadata.meta_fields)).all()) == {
                ids["alice"]: {"tag": "Alice", "typed": 0},
                ids["bob"]: {"tag": "Bob", "typed": False},
                ids["foreign"]: {"tag": "Alice", "typed": 0},
            }
        assert reader.count(index=index)["count"] == len(rows)
        for identifier, row in rows.items():
            assert reader.get(index=index, id=identifier)["_source"] == row


def _canvas(env: dict[str, Any], config: dict[str, Any], *, iteration: bool = False, denied: bool = False) -> Canvas:
    def node(name: str, params: dict[str, Any] | None = None, *, parent: str | None = None, upstream: list[str] | None = None, downstream: list[str] | None = None) -> dict[str, Any]:
        return {"obj": {"component_name": name, "params": params or {}}, "parent_id": parent, "upstream": upstream or [], "downstream": downstream or []}

    params = {"query": "rivet", "dataset_ids": [env["ids"]["kb"]], "meta_data_filter": config, "top_n": 3, "similarity_threshold": 0, "empty_response": "No results"}
    components = {"begin": node("Begin", downstream=["Iteration:outer"] if iteration else ["Retrieval:child"])}
    components["Retrieval:child"] = node("Retrieval", params, parent="Iteration:outer" if iteration else None, upstream=["IterationItem:outer"] if iteration else ["begin"])
    if iteration:
        components.update(
            {
                "Iteration:outer": node("Iteration", {"items_ref": "env.items"}, upstream=["begin"], downstream=["Message:done"]),
                "IterationItem:outer": node("IterationItem", parent="Iteration:outer", downstream=["Retrieval:child"]),
                "Message:done": node("Message", {"content": ["done"], "stream": False}, upstream=["Iteration:outer"]),
            }
        )
    globals_ = {"env.items": ["Alice", "Bob"], "sys.query": "", "sys.user_id": "", "sys.conversation_turns": 0, "sys.files": [], "sys.history": []}
    return Canvas(json.dumps({"components": components, "globals": globals_, "retrieval": [], "path": []}), tenant_id=env["ids"]["outsider" if denied else "actor"])


async def test_canvas_iteration_retrieves_new_metadata_value_each_round(scoped_recall: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scoped_recall
    config = {"method": "manual", "manual": [{"key": "tag", "op": "in", "value": "{IterationItem:outer@item}"}]}
    canvas = _canvas(env, config, iteration=True)
    monkeypatch.setattr(canvas, "is_canceled", lambda: False)
    monkeypatch.setattr(canvas.get_component_obj("Message:done"), "_save_to_memory", lambda *_: None)
    try:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        finished = [event["data"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Retrieval:child"]
        assert len(finished) == 2 and all(not event["error"] for event in finished)
        assert [call["doc_ids"] for call in env["calls"]] == [[env["ids"]["alice"]], [env["ids"]["bob"]]]
        assert [[chunk["doc_id"] for chunk in call["chunks"]] for call in env["calls"]] == [[env["ids"]["alice"]], [env["ids"]["bob"]]]
        assert canvas.get_component_obj("Retrieval:child")._param.meta_data_filter == config
    finally:
        canvas._thread_pool.shutdown(wait=True)


async def test_typed_manual_constants_and_no_match_reach_real_search(scoped_recall: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scoped_recall
    for value, expected in [([0], [env["ids"]["alice"]]), ([False], [env["ids"]["bob"]]), (["missing"], [])]:
        config = {"method": "manual", "manual": [{"key": "typed", "op": "in", "value": value}]}
        canvas = _canvas(env, config)
        monkeypatch.setattr(canvas, "is_canceled", lambda: False)
        try:
            await canvas.get_component_obj("Retrieval:child")._retrieve_kb("rivet")
            assert [chunk["doc_id"] for chunk in env["calls"][-1]["chunks"]] == expected
            assert env["calls"][-1]["doc_ids"] == (expected or ["-999"])
            assert canvas.get_component_obj("Retrieval:child")._param.meta_data_filter == config
        finally:
            canvas._thread_pool.shutdown(wait=True)


async def test_denied_dataset_prevents_model_metadata_and_search_side_effects(scoped_recall: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = scoped_recall
    config = {"method": "manual", "manual": [{"key": "tag", "op": "in", "value": "{env.current}"}]}
    canvas = _canvas(env, config, denied=True)
    tool = canvas.get_component_obj("Retrieval:child")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Denied dataset reached variable, model, metadata or search IO")

    monkeypatch.setattr(canvas, "get_variable_value", forbidden)
    monkeypatch.setattr(module, "build_named_bundle_async", forbidden)
    monkeypatch.setattr(module, "apply_meta_data_filter", forbidden)
    monkeypatch.setattr(settings.retriever, "retrieval", forbidden)
    try:
        with pytest.raises(PermissionError, match="missing or inaccessible"):
            await tool._retrieve_kb("rivet")
        assert not env["calls"] and not env["built"]
        assert tool._param.meta_data_filter == config
    finally:
        canvas._thread_pool.shutdown(wait=True)
