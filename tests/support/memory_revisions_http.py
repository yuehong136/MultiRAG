"""Shared owned memory revisions http integration fixtures and helpers."""

from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.services import memory_revision_service
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.services.tenant_llm_service import TenantLLMService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_authorization import insert_read_messages
from tests.support.memory_authorization import memory_api as memory_api
from tests.support.resource_journal import ResourceJournal

_TABLES = (MemoryPublicationOutbox, MemoryRevisionSource, MemoryEntryRevision, MemoryMutationOperation, MemoryEntry, MemorySourceState, ManagedMemoryState)


class RevisionEmbedding:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.extend(texts)
        return [[1.0, 0.0] for _ in texts], 0

    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0, 0.0], 0

    def close(self) -> None:
        pass


@pytest.fixture
def revision_api(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = memory_api
    calls: list[str] = []
    counter = env["queue"] + ":revision-ids"
    journal = ResourceJournal("memory-revision-http")
    journal.event("reserve-memory-revision-state", database=env["engine"].url.database, memory_ids=list(env["memories"].values()), counter=counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)
    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(lambda cls, *args, **kwargs: RevisionEmbedding(calls)))
    insert_read_messages(env, [{"message_id": 910, "content": "A project uses port 80. A separate fact remains."}])
    try:
        yield dict(env, embedding_calls=calls, counter=counter, memory_id=env["memories"]["personal"])
    finally:
        with Session(env["engine"]) as db:
            for model in _TABLES:
                db.execute(sa.delete(model).where(model.memory_id.in_(list(env["memories"].values()))))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id.in_(list(env["memories"].values())))) for model in _TABLES}
            assert not any(remaining.values()), remaining
        journal.event("memory-revision-sql-cleanup-readback", database=env["engine"].url.database, remaining=remaining)
