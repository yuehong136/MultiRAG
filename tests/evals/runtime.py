"""Owned runtime and report locations for independent quality evaluation."""

import asyncio
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from pymilvus import DataType, MilvusClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from scripts.provision_eval_assets import DIRECTORY, MODEL, verify
from tests.evals.contracts import CORPUS


@pytest.fixture
def quality_runtime(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    verify()
    from api.db import db_models
    from api.db.db_models import Document, Knowledgebase, Tenant
    from common import settings
    from common.bootstrap import ensure_initialized
    from common.config_utils import CONFIGS
    from core.app import naive
    from core.llm.embedding import DefaultEmbedding
    from core.nlp import rag_tokenizer, search

    ensure_initialized()
    model = DefaultEmbedding(None, MODEL, model_path=str(DIRECTORY), local_files_only=True, query_instruction="")
    owner, dataset = uuid4().hex, uuid4().hex
    name = f"eval_{uuid4().hex}"
    collection = search.index_name_one(owner, name)
    ids = {path.name: uuid4().hex for path in CORPUS.glob("*.md")}
    cfg = CONFIGS["milvus"]
    with ExitStack() as cleanup:
        reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
        cleanup.callback(reader.close)
        assert not reader.has_collection(collection)

        def drop_collection() -> None:
            if reader.has_collection(collection):
                reader.drop_collection(collection, timeout=60)
            assert not reader.has_collection(collection)

        cleanup.callback(drop_collection)

        def drop_rows() -> None:
            with bootstrapped_engine.begin() as db:
                db.execute(sa.delete(Document).where(Document.id.in_(list(ids.values()))))
                db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == dataset))
                db.execute(sa.delete(Tenant).where(Tenant.id == owner))

        cleanup.callback(drop_rows)
        with Session(bootstrapped_engine) as db:
            db.add(Tenant(id=owner, llm_id="", embd_id=MODEL, asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(Knowledgebase(id=dataset, tenant_id=owner, created_by=owner, name=name, embd_id=MODEL))
            for filename, doc_id in ids.items():
                db.add(Document(id=doc_id, kb_id=dataset, created_by=owner, name=filename, type="doc", parser_id="naive"))
            db.commit()
        schema = reader.create_schema(auto_id=False, enable_dynamic_field=True)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=512)
        for field in ["kb_id", "doc_id", "docnm_kwd", "tenant_id"]:
            schema.add_field(field, DataType.VARCHAR, max_length=512)
        text_fields = ["content_with_weight", "title_tks", "title_sm_tks", "important_kwd", "important_tks", "question_tks", "content_ltks", "content_sm_ltks"]
        for field in text_fields:
            schema.add_field(field, DataType.VARCHAR, max_length=65535)
        schema.add_field("available_int", DataType.INT64)
        for field in ["vector", "q_512_vec"]:
            schema.add_field(field, DataType.FLOAT_VECTOR, dim=512)
        indexes = reader.prepare_index_params()
        for field in ["vector", "q_512_vec"]:
            indexes.add_index(field, index_type="AUTOINDEX", metric_type="COSINE")
        reader.create_collection(collection, schema=schema, index_params=indexes, consistency_level="Strong", timeout=60)
        parsed = {}
        rows = []
        for filename, doc_id in ids.items():
            chunks = naive.chunk(
                filename, binary=(CORPUS / filename).read_bytes(), lang="Chinese", callback=lambda *args, **kwargs: None, parser_config={"chunk_token_num": 256, "analyze_hyperlink": False}
            )
            parsed[filename] = chunks
            vectors, _ = model.encode([chunk["content_with_weight"] for chunk in chunks])
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                identifier = f"{doc_id}-{index}"
                text = chunk["content_with_weight"]
                tokens = rag_tokenizer.tokenize(text)
                rows.append(
                    {
                        **dict.fromkeys(text_fields, ""),
                        **chunk,
                        "id": identifier,
                        "pk": identifier,
                        "doc_id": doc_id,
                        "kb_id": dataset,
                        "tenant_id": owner,
                        "docnm_kwd": filename,
                        "content_ltks": tokens,
                        "content_sm_ltks": tokens,
                        "available_int": 1,
                        "vector": vector.tolist(),
                        "q_512_vec": vector.tolist(),
                        "knowledge_graph_kwd": "",
                        "removed_kwd": "N",
                        "important_kwd": "",
                        "question_kwd": [],
                        "mom_id": "",
                    }
                )
        assert rows
        reader.insert(collection, rows, timeout=60)
        reader.flush(collection, timeout=60)
        assert reader.query(collection, filter='pk != ""', output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"] == len(rows)
        async_engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        monkeypatch.setattr(db_models, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False))

        @contextmanager
        def scratch_db() -> Iterator[Session]:
            with Session(bootstrapped_engine) as db:
                yield db

        monkeypatch.setattr(search, "db_connection", scratch_db)
        yield {"model": model, "dealer": search.Dealer(settings.docStoreConn), "owner": owner, "dataset": dataset, "name": name, "parsed": parsed}


def _report_path(request: pytest.FixtureRequest, name: str) -> Path:
    directory = request.config.getoption("--test-report-dir")
    assert directory, "Use make eval to retain evaluation evidence"
    return Path(directory) / name
