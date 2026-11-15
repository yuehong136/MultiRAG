"""Owned SQL maintenance worker for HTTP/consumer integration fixtures."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.apps.services.memory_maintenance_service import jobs
from memory.maintenance.execution import process_next


@contextmanager
def maintenance_worker(env: dict[str, Any]) -> Iterator[None]:
    """Use the production dispatcher, with independent SQL sessions and ownership."""
    stop, started, polled = threading.Event(), threading.Event(), threading.Event()
    enabled = env.setdefault("worker_enabled", threading.Event())
    enabled.set()
    errors: list[BaseException] = []
    env["worker_errors"] = errors
    identity = "http-maintenance-" + uuid4().hex
    journal = env["journal"]
    journal.event("reserve-maintenance-worker", worker_id=identity, pid=os.getpid(), database=env["engine"].url.database, daemon=False)

    async def consume() -> None:
        journal.event("reserve-worker-sql-engine", worker_id=identity, database=env["engine"].url.database, pool="NullPool")
        engine = create_async_engine(env["engine"].url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        def reserve_insert(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            compiled = getattr(context, "compiled", None)
            if compiled is None or not getattr(context, "isinsert", False):
                return
            for values in context.compiled_parameters:
                journal.event(
                    "reserve-worker-sql-row",
                    worker_id=identity,
                    database=env["engine"].url.database,
                    table=compiled.statement.table.name,
                    identities={key: str(values[key]) for key in ("memory_id", "job_id", "operation_id", "candidate_id", "input_id") if key in values},
                )

        sa.event.listen(engine.sync_engine, "before_cursor_execute", reserve_insert)
        started.set()
        try:
            while not stop.is_set():
                if enabled.is_set():
                    await process_next(factory, jobs, worker_id=identity, claim_timeout_seconds=5, heartbeat_seconds=0.2)
                    polled.set()
                await asyncio.sleep(0.05)
        finally:
            sa.event.remove(engine.sync_engine, "before_cursor_execute", reserve_insert)
            await engine.dispose()
            journal.event("worker-sql-engine-disposed", worker_id=identity)

    def run() -> None:
        try:
            asyncio.run(consume())
        except BaseException as error:
            errors.append(error)
            started.set()

    thread = threading.Thread(target=run, name=identity, daemon=False)
    thread.start()
    assert started.wait(20), "maintenance worker did not initialize"
    env["worker_stop"] = stop
    env["worker_thread"] = thread
    env["worker_polled"] = polled
    try:
        assert not errors, errors
        yield
    finally:
        if "analysis_release" in env:
            env["analysis_release"].set()
        stop.set()
        thread.join(timeout=40)
        journal.event("maintenance-worker-cleanup-readback", worker_id=identity, thread_alive=thread.is_alive(), error_types=[type(error).__name__ for error in errors])
        assert not thread.is_alive(), "owned maintenance worker did not drain"
        assert not errors, errors
