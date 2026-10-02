import ast
import json
from copy import deepcopy
from io import BytesIO
from typing import Any
from uuid import uuid4

import pytest
import redis
import sqlalchemy as sa
import xxhash
from minio import Minio
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, Knowledgebase, Task, Tenant
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.task_service import TaskService
from common import resources, settings
from common.config_utils import CONFIGS
from common.metadata_utils import build_metadata_config
from core.graphrag import utils as cache_utils
from core.svr import task_executor


class MetadataModel:
    max_length = 8192

    def __init__(self) -> None:
        self.llm_name = f"scratch-metadata-{uuid4().hex}"
        self.schemas: list[dict[str, Any]] = []

    async def async_chat(self, system: str, messages: list[dict[str, Any]]) -> str:
        schema = ast.literal_eval(system.split("## Schema for extraction:\n", 1)[1].split("## Content to analyze:", 1)[0].strip())
        self.schemas.append(schema)
        values = {"author": "Ada", "year": 2026, "active": False, "source": "Paper", "category": "Research"}
        return json.dumps({key: values[key] for key in schema["properties"]})


class ScratchStorage:
    def __init__(self, client: Minio) -> None:
        self.client = client

    def get(self, bucket: str, name: str) -> bytes:
        response = self.client.get_object(bucket, name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()


@pytest.mark.parametrize("kind", ["schema", "legacy_list", "invalid_properties", "builtin_only", "empty"])
async def test_parse_task_metadata_roundtrip_in_scratch_services(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """Real task/config, MinIO text parsing, Redis cache and SQL writes; model is fake."""
    tenant_id, kb_id, doc_id, task_id = (uuid4().hex for _ in range(4))
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {"author": {"type": "string", "enum": ["Ada"]}, "year": {"type": "integer", "minimum": 2000}, "active": {"type": "boolean"}},
        "required": ["author", "year"],
        "additionalProperties": False,
    }
    configs: dict[str, Any] = {
        "schema": schema,
        "legacy_list": [{"key": "author", "description": None, "enum": None}],
        "invalid_properties": {"type": "object", "properties": None},
        "builtin_only": None,
        "empty": {"properties": None},
    }
    parser_config = {
        "enable_metadata": True,
        "metadata": configs[kind],
        "built_in_metadata": [] if kind == "empty" else [{"key": "source", "description": "Source", "enum": ["Paper"]}],
        "chunk_token_num": 128,
        "delimiter": "\n",
        "analyze_hyperlink": False,
    }
    original_config = deepcopy(parser_config)
    existing = {"owner": "Manual", "_version": 0, "_isCurrent": True, "outline": [{"title": "Existing", "depth": 0}], "tags": ["reviewed"]}
    model = MetadataModel()
    binary = b"Author: Ada. Year: 2026. Active: false. Source: Paper. Category: Research."
    filename = "metadata.txt"
    minio_config = CONFIGS["minio"]
    secure = str(minio_config.get("secure", False)).lower() in {"true", "1", "yes"}
    storage = Minio(minio_config["host"], access_key=minio_config["user"], secret_key=minio_config["password"], secure=secure)
    redis_config = CONFIGS["redis"]
    redis_host, redis_port = redis_config["host"].rsplit(":", 1)
    cache = redis.Redis(
        host=redis_host, port=int(redis_port), db=int(redis_config.get("db", 1)), username=redis_config.get("username") or None, password=redis_config.get("password") or None, decode_responses=True
    )
    assert cache.ping()
    cache_keys: set[str] = set()
    cache_calls: list[tuple[str, Any]] = []
    get_cache, set_cache = task_executor.get_llm_cache, task_executor.set_llm_cache

    def track_key(model_name: str, content: str, history: str, config: Any) -> str:
        key = xxhash.xxh64((str(model_name) + str(content) + str(history) + str(config)).encode()).hexdigest()
        cache_keys.add(key)
        return key

    def tracked_get(model_name: str, content: str, history: str, config: Any) -> str | None:
        track_key(model_name, content, history, config)
        cache_calls.append(("get", deepcopy(config)))
        return get_cache(model_name, content, history, config)

    def tracked_set(model_name: str, content: str, value: str, history: str, config: Any) -> None:
        key = track_key(model_name, content, history, config)
        cache_calls.append(("set", deepcopy(config)))
        set_cache(model_name, content, value, history, config)
        assert cache.get(key) == value
        assert 0 < cache.ttl(key) <= 86400

    def progress(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")
    monkeypatch.setitem(resources._state, "storage", ScratchStorage(storage))
    monkeypatch.setattr(cache_utils, "REDIS_CONN", cache)
    monkeypatch.setattr(task_executor, "get_llm_cache", tracked_get)
    monkeypatch.setattr(task_executor, "set_llm_cache", tracked_set)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: model)
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    storage.make_bucket(kb_id)
    try:
        storage.put_object(kb_id, filename, BytesIO(binary), len(binary), content_type="text/plain")
        with Session(bootstrapped_engine) as db:
            db.add(Tenant(id=tenant_id, llm_id="fake", embd_id="test", asr_id="test", img2txt_id="test", parser_ids="naive"))
            db.add(Knowledgebase(id=kb_id, tenant_id=tenant_id, name="metadata scratch", created_by=tenant_id, embd_id="test", parser_config=parser_config))
            db.add(Document(id=doc_id, kb_id=kb_id, parser_id="naive", type="doc", created_by=tenant_id, name=filename, location=filename, size=len(binary), parser_config=parser_config, run="1"))
            db.add(Task(id=task_id, doc_id=doc_id))
            db.commit()
            assert DocMetadataService.update_document_metadata(db, doc_id, existing)

        for _ in range(2):
            with Session(bootstrapped_engine) as db:
                task = TaskService.get_task(db, task_id)
                assert task and task["parser_config"] == original_config
                chunks = await task_executor.build_chunks(task, progress, db)
                assert chunks and "Author: Ada" in chunks[0]["content_with_weight"]
                assert all("metadata_obj" not in chunk and "metadata" not in chunk for chunk in chunks)

        with Session(bootstrapped_engine) as db:
            expected = {**existing}
            if kind != "empty":
                expected["source"] = "Paper"
                if kind in {"schema", "legacy_list"}:
                    expected["author"] = "Ada"
                if kind == "schema":
                    expected.update(year=2026, active=False)
            assert DocMetadataService.get_document_metadata(db, doc_id) == expected
            assert db.get(DocumentMetadata, doc_id).meta_fields == expected
            assert db.get(Document, doc_id).parser_config == original_config

        if kind == "empty":
            assert model.schemas == []
            assert not any(operation == "set" for operation, _ in cache_calls)
        else:
            assert len(model.schemas) == 1  # second parse hit the actual Redis cache
            effective = build_metadata_config(original_config)
            assert cache_calls == [("get", effective), ("set", effective), ("get", effective)]
            if kind == "schema":
                assert model.schemas[0]["required"] == schema["required"]
                assert model.schemas[0]["additionalProperties"] is False
                assert model.schemas[0]["properties"]["year"] == schema["properties"]["year"]
            changed = {**original_config, "built_in_metadata": original_config["built_in_metadata"] + [{"key": "category"}]}
            with Session(bootstrapped_engine) as db:
                DocumentService.update_parser_config(db, doc_id, changed)
                task = TaskService.get_task(db, task_id)
                assert task["parser_config"] == changed
                assert await task_executor.build_chunks(task, progress, db)
            assert len(model.schemas) == 2
            with Session(bootstrapped_engine) as db:
                assert db.get(DocumentMetadata, doc_id).meta_fields == {**expected, "category": "Research"}
    finally:
        with Session(bootstrapped_engine) as db:
            db.rollback()
            assert DocMetadataService.delete_document_metadata(db, doc_id, kb_id, tenant_id)
            for model_type, row_id in [(Task, task_id), (Document, doc_id), (Knowledgebase, kb_id), (Tenant, tenant_id)]:
                row = db.get(model_type, row_id)
                if row is not None:
                    db.delete(row)
            db.commit()
        with Session(bootstrapped_engine) as db:
            assert db.get(DocumentMetadata, doc_id) is None
            assert db.get(Task, task_id) is None
            assert db.get(Document, doc_id) is None
            assert db.get(Knowledgebase, kb_id) is None
            assert db.get(Tenant, tenant_id) is None
        if cache_keys:
            cache.delete(*cache_keys)
            assert all(cache.get(key) is None for key in cache_keys)
        cache.close()
        storage.remove_object(kb_id, filename)
        assert list(storage.list_objects(kb_id)) == []
        storage.remove_bucket(kb_id)
        assert not storage.bucket_exists(kb_id)
