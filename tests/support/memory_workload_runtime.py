"""Owned HTTP/worker/storage runtime for explicitly requested live evaluation."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.apps.services import memory_maintenance_service, memory_revision_service
from api.db.db_models import Base, Memory, Task, TenantModel, TenantModelProvider
from api.db.joint_services import memory_write_runtime
from api.db.services.tenant_llm_service import TenantLLMService
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import index_name
from tests.support.memory_maintenance_worker import maintenance_worker
from tests.support.memory_workload_models import ProviderObserver, configured_models, copy_models
from tests.support.memory_writes_http import api_request, result
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def memory_live_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Keep real provider calls and all writes inside an owned synthetic bank."""
    assert os.environ.get("MULTIRAG_MEMORY_LIVE") == "1", "explicit_live_memory_evaluation_required"
    env = release_api
    tenant = env["owners"][0]
    journal = ResourceJournal("memory-real-workload")
    observer = ProviderObserver(journal, capture_synthetic_content=os.environ.get("MULTIRAG_MEMORY_CAPTURE_SYNTHETIC_RESPONSES") == "1")
    observer.set_stage("source_embedding", phase="bank-initialization")
    identifiers = {"chat": os.environ["MULTIRAG_MEMORY_CHAT_MODEL_ID"], "embedding": os.environ["MULTIRAG_MEMORY_EMBEDDING_MODEL_ID"]}
    configs = configured_models(identifiers)
    provider_factory = os.environ.get("MULTIRAG_MEMORY_LIVE_PROVIDER", "Tongyi-Qianwen")
    if provider_factory == "OpenAI":
        assert configs["chat"]["llm_name"] == "gpt-6.1-terra"
        assert configs["embedding"]["llm_name"] in {"text-embedding-3-small", "text-embedding-3-large", "text-embedding-ada-002"}
    else:
        assert provider_factory == "Tongyi-Qianwen"
        assert configs["chat"]["llm_name"] == "qwen-flash"
        assert configs["embedding"]["llm_name"] == "text-embedding-v4"
    assert all(config["llm_factory"] == provider_factory for config in configs.values())
    identities: dict[str, str] = {}
    counter = "memory-live-ids-" + uuid4().hex
    queue = "memory-live-queue-" + uuid4().hex
    mids: list[str] = []
    journal.event("reserve-live-memory-scope", tenant_id=tenant, counter=counter, queue=queue)
    monkeypatch.setattr(memory_write_runtime, "_COUNTER_KEY", counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)
    monkeypatch.setattr(memory_maintenance_service, "_COUNTER_KEY", counter)
    asynchronous: AsyncEngine | None = None
    try:
        identities = copy_models(env["engine"], tenant, configs, journal)
        REDIS_CONN.REDIS.set(counter, str(2**53 + 100))
        journal.event("reserve-live-sql-engine", database=env["engine"].url.database)
        asynchronous = create_async_engine(env["engine"].url, poolclass=NullPool)
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)

        @asynccontextmanager
        async def runtime_db() -> AsyncIterator[AsyncSession]:
            async with factory() as db:
                yield db

        monkeypatch.setattr(memory_write_runtime, "async_db_connection", runtime_db)
        with monkeypatch.context() as patch, observer.install(patch, env["engine"], set(identities.values())):
            record = result(
                api_request(
                    env,
                    "POST",
                    "/memories",
                    actor=0,
                    json={
                        "name": "Synthetic real-provider memory workload",
                        "memory_type": ["raw"],
                        "embd_id": f"{configs['embedding']['llm_name']}@{provider_factory}",
                        "llm_id": f"{configs['chat']['llm_name']}@{provider_factory}",
                    },
                )
            )
            mid = record["id"]
            mids.append(mid)
            journal.event("reserve-memory-index", memory_id=mid, tenant_id=tenant, collection=index_name(tenant) + "_" + mid)
            child = dict(env, memory_id=mid, tenant_id=tenant, journal=journal, observer=observer, model_ids=identities)
            with maintenance_worker(child):
                yield child
    finally:
        original_error = sys.exception()
        cleanup_errors: list[BaseException] = []
        if asynchronous is not None:
            try:
                asyncio.run(asynchronous.dispose())
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            with Session(env["engine"]) as reader:
                # Recover a Memory create whose SQL commit acknowledgement was lost.
                mids[:] = sorted(set(mids) | set(reader.scalars(sa.select(Memory.id).where(Memory.tenant_id == tenant))))
        except BaseException as error:
            cleanup_errors.append(error)
        for mid in mids:
            try:
                asyncio.run(ManagedMessageStore(tenant, mid).drop())
                from common import settings

                assert not settings.msgStoreConn.index_exists_strict(index_name(tenant), mid)
            except BaseException as error:
                cleanup_errors.append(error)
        try:
            with Session(env["engine"]) as db:
                tables = [table for table in reversed(Base.metadata.sorted_tables) if table.schema == "usr_ai" and "memory_id" in table.c]
                for table in tables:
                    db.execute(sa.delete(table).where(table.c.memory_id.in_(mids)))
                db.execute(sa.delete(Task).where(Task.doc_id.in_(mids)))
                db.execute(sa.delete(Memory).where(Memory.id.in_(mids)))
                # Covers a commit whose acknowledgement was lost in copy_models.
                TenantLLMService.delete_by_tenant_id(db, tenant)
                db.commit()
            with Session(env["engine"]) as reader:
                remaining = {table.fullname: reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.c.memory_id.in_(mids))) for table in tables}
                assert not any(remaining.values()), remaining
                assert reader.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_(mids))) == 0
                assert (
                    reader.scalar(
                        sa.select(sa.func.count()).select_from(TenantModel).join(TenantModelProvider, TenantModel.provider_id == TenantModelProvider.id).where(TenantModelProvider.tenant_id == tenant)
                    )
                    == 0
                )
            journal.event("live-workload-sql-cleanup-readback", sql_remaining=remaining, model_rows_absent=True)
        except BaseException as error:
            cleanup_errors.append(error)
        try:
            REDIS_CONN.REDIS.delete(counter, queue)
            assert not REDIS_CONN.REDIS.exists(counter, queue)
            journal.event("live-workload-redis-cleanup-readback", keys=[counter, queue], absent=True)
        except BaseException as error:
            cleanup_errors.append(error)
        journal.event("live-workload-cleanup-result", memory_ids=mids, model_ids=list(identities.values()), errors=[type(error).__name__ for error in cleanup_errors])
        if cleanup_errors:
            raise BaseExceptionGroup("Owned live-workload cleanup failed", ([original_error] if original_error is not None else []) + cleanup_errors)
