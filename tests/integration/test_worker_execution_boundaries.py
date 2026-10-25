"""Real isolated SQL/queue/object/index worker acceptance; parser/provider controlled."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task
from api.db.services.document_source_recovery import source_recovery_key
from api.db.services.document_task_service import SupersededDocumentTask, increment_task_document, put_task_image, write_task_metadata
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from common import settings
from common.constants import SVR_CONSUMER_GROUP_NAME
from common.doc_store.document_history import document_history
from core.svr import task_executor
from core.utils.redis_conn import REDIS_CONN
from tests.support.rest_document_contract import rest_documents as rest_documents


def test_worker_queue_commit_cancel_retry_and_stale_write_fences(rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = rest_documents
    journal = env["journal"]
    dataset, owner, index = env["datasets"][0], env["owners"][0], env["collections"][0]
    docs = {name: uuid4().hex for name in ("normal", "canceled", "retry", "exhausted")}
    tasks = {name: uuid4().hex for name in docs}
    replacement = uuid4().hex
    keys = [key for task_id in [*tasks.values(), replacement] for key in (f"{task_id}-cancel", f"task-runtime:v1:{task_id}", f"document-task-images:{task_id}")]
    keys.extend(source_recovery_key(document_id, tasks[name]) for name, document_id in docs.items())
    journal.event("reserve-worker-cases", documents=docs, tasks=tasks, replacement_task=replacement, redis_keys=keys, queue=env["queue"], index=index)
    models: list[Any] = []
    calls: list[list[str]] = []

    class Provider:
        max_length = 8192

        def __init__(self) -> None:
            self.closed = False
            models.append(self)

        def encode(self, texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
            assert not self.closed
            calls.append(texts)
            return np.asarray([[0.1] * 768 for _ in texts]), len(texts)

        def close(self) -> None:
            self.closed = True

    config = {"llm_name": "controlled", "llm_factory": "Builtin", "mdl_type": "embedding", "max_tokens": 8192, "api_key": "", "api_base": ""}
    monkeypatch.setattr(TenantLLMService, "model_instance", lambda *args, **kwargs: Provider())
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args, **kwargs: config)
    monkeypatch.setattr(task_executor, "get_tenant_default_model_by_type", lambda *args, **kwargs: config)
    monkeypatch.setattr(task_executor, "CONSUMER_NAME", "worker-boundary-" + journal.run_id)
    monkeypatch.setattr(task_executor, "UNACKED_ITERATOR", None)

    def parse(name: str, *, binary: bytes, **kwargs: Any) -> list[dict[str, Any]]:
        assert binary == b"source bytes"
        return [{"content_with_weight": name, "content_ltks": name, "docnm_kwd": name, "title_tks": "worker", "page_num_int": [0], "top_int": [0], "position_int": []}]

    monkeypatch.setitem(task_executor.FACTORY, "naive", SimpleNamespace(chunk=parse))
    store = settings.docStoreConn
    original_insert = store.insert
    fail_insert = False

    def insert(rows: list[dict[str, Any]], *args: Any, **kwargs: Any) -> list[str]:
        if fail_insert:
            raise RuntimeError("controlled index failure before mutation")
        return original_insert(rows, *args, **kwargs)

    monkeypatch.setattr(store, "insert", insert)
    try:
        with Session(env["engine"]) as db:
            for name, document_id in docs.items():
                db.add(Document(id=document_id, kb_id=dataset, name=name + ".txt", location=name, size=12, type="doc", parser_id="naive", parser_config={}, created_by=owner, run="1", status="1"))
                db.add(Task(id=tasks[name], doc_id=document_id, from_page=0, to_page=1, retry_count=3 if name == "exhausted" else 0))
            db.commit()
        for name in docs:
            settings.STORAGE_IMPL.put(dataset, name, b"source bytes")

        def enqueue(name: str) -> None:
            assert REDIS_CONN.queue_product(env["queue"], {"id": tasks[name], "doc_id": docs[name]})

        def work() -> None:
            asyncio.run(task_executor.handle_task())
            assert REDIS_CONN.REDIS.xpending(env["queue"], SVR_CONSUMER_GROUP_NAME)["pending"] == 0

        enqueue("normal")
        work()
        normal_rows = document_history(store, index, dataset, docs["normal"])
        assert len(normal_rows) == 1 and normal_rows[0]["content_with_weight"] == "normal.txt"
        with Session(env["engine"]) as db:
            normal_task = db.get(Task, tasks["normal"])
            assert normal_task.progress == 1 and normal_task.chunk_ids
            assert db.get(Document, docs["normal"]).chunk_num == 1
            assert db.get(Document, docs["normal"]).token_num > 0
            normal_tokens = db.get(Knowledgebase, dataset).token_num
        assert models and all(model.closed for model in models)
        before_calls = deepcopy(calls)
        # A committed task redelivered before the original XACK must not run again.
        enqueue("normal")
        work()
        assert calls == before_calls
        with Session(env["engine"]) as db:
            assert db.get(Knowledgebase, dataset).token_num == normal_tokens

        REDIS_CONN.REDIS.set(f"{tasks['canceled']}-cancel", "owned-cancel")
        for name in ("canceled", "exhausted"):
            enqueue(name)
            work()
            assert document_history(store, index, dataset, docs[name]) == []
        assert calls == before_calls

        fail_insert = True
        enqueue("retry")
        work()
        assert document_history(store, index, dataset, docs["retry"]) == []
        with Session(env["engine"]) as db:
            failed = db.get(Task, tasks["retry"])
            assert failed.progress < 0
            assert db.get(Document, docs["retry"]).chunk_num == 0
            assert db.get(Knowledgebase, dataset).token_num == normal_tokens
            failed.progress = 0
            db.commit()
        fail_insert = False
        enqueue("retry")
        work()
        assert len(document_history(store, index, dataset, docs["retry"])) == 1
        assert all(model.closed for model in models)

        # Replacing the task invalidates old metadata, image and count writes.
        with Session(env["engine"]) as db:
            db.delete(db.get(Task, tasks["retry"]))
            db.add(Task(id=replacement, doc_id=docs["retry"], from_page=0, to_page=1))
            db.commit()
        before = document_history(store, index, dataset, docs["retry"])
        for action in (
            lambda: write_task_metadata(env["engine"], tasks["retry"], docs["retry"], dataset, {"stale": True}),
            lambda: increment_task_document(env["engine"], tasks["retry"], docs["retry"], dataset, 999, 999, 0),
            lambda: put_task_image(tasks["retry"], docs["retry"], dataset, owner, bucket=dataset, fnm="stale-image", binary=b"stale"),
        ):
            with pytest.raises(SupersededDocumentTask):
                action()
        assert document_history(store, index, dataset, docs["retry"]) == before
        assert not any(item.object_name.endswith("stale-image") for item in env["storage"].list_objects(env["bucket"], recursive=True))
        with Session(env["engine"]) as db:
            assert db.get(Document, docs["retry"]).chunk_num == 1
            assert db.get(Task, replacement).progress == 0
        journal.event(
            "worker-independent-readback",
            completed_redelivery_no_model=True,
            canceled_and_exhausted_no_model=True,
            failed_index_no_count=True,
            retry_succeeded=True,
            stale_writes_rejected=True,
            models_closed=len(models),
        )
    finally:
        REDIS_CONN.REDIS.delete(*keys)
        assert not any(REDIS_CONN.REDIS.exists(key) for key in keys)
        journal.event("worker-key-cleanup", keys=keys, absent=True)
