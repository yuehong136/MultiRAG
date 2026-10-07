"""Owned GraphRAG HTTP/SQL/Milvus/Redis resources and controlled model boundary."""

import asyncio
import json
import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import redis
import sqlalchemy as sa
from pymilvus import MilvusClient
from sqlalchemy.orm import Session

from api.apps.services import dataset_api_service
from api.db.db_models import Document, File, File2Document, Knowledgebase, Task
from api.db.services import document_service
from common import settings
from common.config_utils import CONFIGS
from core.graphrag import utils
from core.graphrag.general import extractor, index
from core.graphrag.persistence import control_key, writer_key
from core.graphrag.phase_markers import ALL_PHASES, phase_key
from core.nlp import search
from tests.support.dataset_management_http import PARSER, request_api
from tests.support.dataset_management_http import management_api as management_api


class Embedding:
    llm_name = "graphrag-acceptance"

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        return np.array([[0.25, 0.75] for _ in texts]), 0


class DocumentExtractor(extractor.Extractor):
    calls: list[str] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __call__(self, doc: str, chunks: list[str], callback: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        self.calls.append(doc)
        entities = [{"entity_name": name, "entity_type": "person", "description": name, "source_id": [doc]} for name in ["ALPHA", "ALPHAS", "X"]]
        edges = [{"src_id": a, "tgt_id": b, "description": a + b, "weight": 1, "keywords": ["related"]} for a, b in [("ALPHA", "ALPHAS"), ("ALPHA", "X"), ("ALPHAS", "X")]]
        return entities, edges


@pytest.fixture
def graphrag_api(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    env = dict(management_api)
    name = "graphrag_acceptance_" + uuid4().hex
    kb, tenant = env["datasets"][0], env["users"][0]
    collection = search.index_name_one(tenant, name)
    queue = "graphrag-acceptance-" + uuid4().hex
    file_id, link_id = uuid4().hex, uuid4().hex
    tasks: list[str] = []
    files, links = [file_id], [link_id]
    parser_config = {**PARSER, "graphrag": {"use_graphrag": True, "resolution": True, "community": True, "entity_types": ["person"], "method": "light"}}
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    rcfg = CONFIGS["redis"]
    host, port = rcfg["host"].rsplit(":", 1)
    redis_reader = redis.Redis(host=host, port=int(port), password=rcfg.get("password"), db=rcfg.get("db", 1))
    directory = Path(os.environ.get("MULTIRAG_GRAPHRAG_EVIDENCE_DIR", str(tmp_path)))
    directory.mkdir(parents=True, exist_ok=True)
    record_path = directory / f"{kb}.json"
    record: dict[str, Any] = {"database": env["engine"].url.database, "collection": collection, "kb": kb, "tenant": tenant, "queue": queue, "tasks": tasks, "observations": []}
    env.update(
        kb=kb,
        tenant=tenant,
        collection=collection,
        reader=reader,
        redis=redis_reader,
        tasks=tasks,
        files=files,
        links=links,
        parser_config=parser_config,
        queue=queue,
        record=record,
        graph_record_path=record_path,
    )

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    def new_task_id() -> str:
        identifier = uuid4().hex
        tasks.append(identifier)
        return identifier

    monkeypatch.setattr(document_service, "get_uuid", new_task_id)
    for module in [dataset_api_service, index, utils, search]:
        monkeypatch.setattr(module, "db_connection", scratch_db)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    monkeypatch.setattr(index, "LightKGExt", DocumentExtractor)
    monkeypatch.setattr(index, "GeneralKGExt", DocumentExtractor)
    DocumentExtractor.calls = []
    # Cache writes are outside the model boundary under test; avoid anonymous
    # shared hash keys. Phase markers and the writer lease use real Redis.
    monkeypatch.setattr(extractor, "get_llm_cache", lambda *args: None)
    monkeypatch.setattr(extractor, "set_llm_cache", lambda *args: None)
    monkeypatch.setattr(utils, "get_embed_cache", lambda *args: None)
    monkeypatch.setattr(utils, "set_embed_cache", lambda *args: None)

    def cleanup() -> None:
        with env["engine"].begin() as db:
            db.execute(sa.delete(Task).where(Task.id.in_(tasks)))
            db.execute(sa.delete(File2Document).where(File2Document.id.in_(links)))
            db.execute(sa.delete(File).where(File.id.in_(files)))
        keys = [queue, writer_key(tenant, kb), control_key(kb), *[phase_key(kb, phase) for phase in ALL_PHASES], *[task + "-cancel" for task in tasks]]
        redis_reader.delete(*keys)
        if reader.has_collection(collection):
            reader.drop_collection(collection)
        assert not reader.has_collection(collection) and not any(redis_reader.exists(key) for key in keys)
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.id.in_(tasks))) == 0
        record["cleanup"] = {"collection_absent": True, "redis_keys_absent": True, "tasks_absent": True}

    with ExitStack() as owned:
        owned.callback(reader.close)
        owned.callback(redis_reader.close)
        owned.callback(lambda: record_path.write_text(json.dumps(record, default=str, indent=2)))
        owned.callback(cleanup)
        assert not reader.has_collection(collection)
        assert settings.docStoreConn.create_idx(collection, kb, 2)
        with env["engine"].begin() as db:
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == kb).values(name=name, chunk_num=1, doc_num=1, parser_config=parser_config))
            db.execute(sa.update(Document).where(Document.id == env["documents"][0]).values(chunk_num=1, progress=1, run="3"))
            db.execute(sa.insert(File).values(id=file_id, parent_id=tenant, tenant_id=tenant, created_by=tenant, name="source.txt", type="txt"))
            db.execute(sa.insert(File2Document).values(id=link_id, file_id=file_id, document_id=env["documents"][0]))
        rows = [{"id": uuid4().hex, "kb_id": kb, "doc_id": env["documents"][0], "content_with_weight": "GraphRAG source", "available_int": 1, "q_2_vec": [0.25, 0.75]}]
        assert settings.docStoreConn.insert(rows, collection, kb) == []
        yield env


def native_rows(env: dict[str, Any]) -> list[dict[str, Any]]:
    return env["reader"].query(env["collection"], filter=f"kb_id == {json.dumps(env['kb'])}", output_fields=["*"], consistency_level="Strong", limit=1000)


def observe(env: dict[str, Any], label: str) -> dict[str, Any]:
    rows = native_rows(env)
    with env["engine"].connect() as db:
        tasks = [dict(row) for row in db.execute(sa.select(Task.__table__).where(Task.id.in_(env["tasks"]))).mappings()]
        kb = dict(db.execute(sa.select(Knowledgebase.__table__).where(Knowledgebase.id == env["kb"])).mappings().one())
    markers = {phase: env["redis"].get(phase_key(env["kb"], phase)) for phase in ALL_PHASES}
    result = {
        "label": label,
        "rows": rows,
        "task_rows": tasks,
        "dataset": kb,
        "markers": {key: value.decode() if value else None for key, value in markers.items()},
        "writer_active": bool(env["redis"].exists(writer_key(env["tenant"], env["kb"]))),
        "queue_messages": [json.loads(fields[b"message"]) for _, fields in env["redis"].xrange(env["queue"])],
    }
    env["record"]["observations"].append(result)
    return result


class Model:
    llm_name = "graphrag-acceptance"
    max_length = 16384

    def __init__(self) -> None:
        self.resolution = 0
        self.community = 0
        self.pause = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: Any = None, **kwargs: Any) -> str:
        if "When determining" in system:
            self.resolution += 1
            return "<|>1<|>&&Yes&&"
        self.community += 1
        if self.pause:
            self.started.set()
            await self.release.wait()
        return json.dumps({"title": "Acceptance community", "summary": "summary", "findings": [{"summary": "s", "explanation": "e"}], "rating": 1.0, "rating_explanation": "e"})


def api(env: dict[str, Any], method: str, *, wipe: bool | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"type": "graph"}
    if wipe is not None:
        params["wipe"] = str(wipe).lower()
    response = request_api(env, method, f"/datasets/{env['kb']}/index", credential=env["jwt"][0], params=params)
    assert response.status_code == 200
    return response.json()


async def worker(env: dict[str, Any], task_id: str, model: Model, docs: list[str] | None = None) -> None:
    # This is the production GraphRAG entry point in-process. The queue/Task rows
    # come from the real API; no resident worker or commercial provider is used.
    def callback(**kwargs: Any) -> None:
        env["record"].setdefault("messages", []).append(kwargs)
        if kwargs.get("prog") == -1:
            with env["engine"].begin() as db:
                db.execute(sa.update(Task).where(Task.id == task_id).values(progress=-1, progress_msg=kwargs["msg"]))

    queued = next(json.loads(fields[b"message"]) for _, fields in env["redis"].xrange(env["queue"]) if json.loads(fields[b"message"])["id"] == task_id)
    selected = queued["doc_ids"]
    if docs is not None:
        assert set(docs) == set(selected), "worker inputs must match the API's persisted queue payload"
    with env["engine"].connect() as db:
        parser_config = db.scalar(sa.select(Knowledgebase.parser_config).where(Knowledgebase.id == env["kb"]))
    graphrag = parser_config["graphrag"]
    assert graphrag["use_graphrag"]
    try:
        await index.run_graphrag_for_kb(
            {"tenant_id": env["tenant"], "kb_id": env["kb"], "id": task_id},
            selected,
            "English",
            parser_config,
            model,
            Embedding(),
            callback,
            with_resolution=graphrag["resolution"],
            with_community=graphrag["community"],
        )
    except Exception:
        with env["engine"].begin() as db:
            db.execute(sa.update(Task).where(Task.id == task_id).values(progress=-1))
        raise
    with env["engine"].begin() as db:
        db.execute(sa.update(Task).where(Task.id == task_id).values(progress=1))


def add_source_document(env: dict[str, Any], content: str = "New graph source") -> str:
    """Register source provenance/counters and persist actual source rows in owned resources."""
    doc_id, file_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    env["documents"].append(doc_id)
    env["files"].append(file_id)
    env["links"].append(link_id)
    with Session(env["engine"]) as db:
        db.add(Document(id=doc_id, kb_id=env["kb"], created_by=env["tenant"], name="new-source.txt", parser_id="naive", type="txt", chunk_num=1, progress=1, run="3", parser_config={}))
        db.add(File(id=file_id, parent_id=env["tenant"], tenant_id=env["tenant"], created_by=env["tenant"], name="new-source.txt", type="txt"))
        db.add(File2Document(id=link_id, file_id=file_id, document_id=doc_id))
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(chunk_num=Knowledgebase.chunk_num + 1, doc_num=Knowledgebase.doc_num + 1))
        db.commit()
    assert (
        settings.docStoreConn.insert(
            [{"id": uuid4().hex, "kb_id": env["kb"], "doc_id": doc_id, "content_with_weight": content, "available_int": 1, "q_2_vec": [0.25, 0.75]}], env["collection"], env["kb"]
        )
        == []
    )
    return doc_id
