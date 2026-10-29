"""Default/auto table metadata through worker build and a real SQL commit."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, DocumentMetadata, Knowledgebase, Task
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_task_service import write_task_table_metadata
from common import settings
from core.app import table
from core.svr import task_executor as worker
from core.utils.table_metadata import TABLE_METADATA, TABLE_ROW_KEY


@pytest.mark.parametrize("mode", [None, "auto"])
async def test_default_auto_worker_metadata_commits_lossless_values(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
    tenant, dataset, document, task_id = [uuid4().hex for _ in range(4)]
    config: dict[str, Any] = {} if mode is None else {"table_column_mode": mode}
    binary = b"title,country,score,notes\nFirst,Brazil,0,\nSecond,Japan,2,Keep entire cell\n"
    expected = {
        "title": ["First", "Second"],
        "country": ["Brazil", "Japan"],
        "score": [0, 2],
        "notes": ["Keep entire cell"],
    }
    monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    async def read(*args: Any, **kwargs: Any) -> bytes:
        return binary

    monkeypatch.setattr(table, "db_connection", scratch_db)
    monkeypatch.setattr(worker.File2DocumentService, "get_storage_address", lambda *a, **kw: ("controlled", "rows.csv"))
    monkeypatch.setattr(worker, "get_storage_binary", read)
    task: dict[str, Any] = {
        "id": task_id,
        "doc_id": document,
        "kb_id": dataset,
        "tenant_id": tenant,
        "name": "rows.csv",
        "location": "rows.csv",
        "size": len(binary),
        "from_page": 0,
        "to_page": 10,
        "language": "English",
        "parser_id": "table",
        "parser_config": {},
        "kb_parser_config": config.copy(),
    }
    try:
        with Session(bootstrapped_engine) as db:
            db.add(Knowledgebase(id=dataset, tenant_id=tenant, created_by=tenant, name="Auto table SQL scratch", embd_id="test", parser_config=config))
            db.add(Document(id=document, kb_id=dataset, created_by=tenant, name="rows.csv", parser_id="table", type="doc", run="1"))
            db.add(Task(id=task_id, doc_id=document, digest="auto-range", progress=0))
            db.commit()
            assert DocMetadataService.update_document_metadata(db, document, {"owner": "Ada", "country": ["manual"]})
        with Session(bootstrapped_engine) as db:
            chunks = await worker.build_chunks(task, lambda *a, **kw: None, db)
        assert len(chunks) == 2
        assert all(TABLE_METADATA not in ck and TABLE_ROW_KEY not in ck for ck in chunks)
        assert task["table_metadata"] == expected
        assert "table_column_names" not in task["kb_parser_config"]  # snapshot stays stale
        write_task_table_metadata(bootstrapped_engine, task_id, document, dataset, task["table_metadata"])
        with Session(bootstrapped_engine) as reader:
            assert DocMetadataService.get_document_metadata(reader, document) == {"owner": "Ada", **expected, "country": ["manual", "Brazil", "Japan"]}
            state = reader.get(DocumentMetadata, document)
            assert state is not None and state.table_metadata_state["tasks"]["auto-range"] == expected
            kb = reader.get(Knowledgebase, dataset)
            assert kb is not None and kb.parser_config["table_column_names"] == ["title", "country", "score", "notes"]
    finally:
        with bootstrapped_engine.begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == document))
            db.execute(sa.delete(Task).where(Task.id == task_id))
            db.execute(sa.delete(Document).where(Document.id == document))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == dataset))
