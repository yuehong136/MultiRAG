"""Poll only explicitly submitted maintenance executions from the SQL queue."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


async def _close_connections(connectors: list[Any], async_engine: Any, engine: Any) -> None:
    """Attempt every owned close even if an earlier resource close fails."""
    async with AsyncExitStack() as cleanup:
        cleanup.push_async_callback(asyncio.to_thread, engine.dispose)
        if async_engine is not None:
            cleanup.push_async_callback(async_engine.dispose)
        for connector in connectors:
            close = getattr(connector, "close", None)
            if callable(close):
                cleanup.push_async_callback(asyncio.to_thread, close)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--once", action="store_true", help="Reconcile known execution metadata and process at most one queued submission")
    command.add_argument("--worker-id", default="maintenance-" + uuid4().hex)
    command.add_argument("--poll-interval", type=float, default=2)
    command.add_argument("--claim-timeout-seconds", type=int, default=60, help="Only a pre-model claimed dispatch can expire; running attempts require reconciliation")
    command.add_argument("--heartbeat-seconds", type=float, default=10)
    return command


async def run_worker(
    sessionmaker: Any,
    jobs_factory: Any,
    *,
    worker_id: str,
    once: bool = False,
    poll_interval: float = 2,
    claim_timeout_seconds: int = 60,
    heartbeat_seconds: float = 10,
    stop: asyncio.Event | None = None,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from memory.maintenance.execution import ExecutionBookkeepingUnknown, process_next
    from memory.revisions.domain import RevisionPersistenceUnknown

    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            processed = await process_next(sessionmaker, jobs_factory, worker_id=worker_id, claim_timeout_seconds=claim_timeout_seconds, heartbeat_seconds=heartbeat_seconds)
        except ExecutionBookkeepingUnknown:
            logger.error("Maintenance terminal bookkeeping is unconfirmed; stopping this worker")
            raise
        except (DBAPIError, RevisionPersistenceUnknown) as error:
            logger.warning("Maintenance queue transaction unconfirmed (%s)", type(error).__name__)
            processed = False
        if once:
            return
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                pass


async def _runtime(options: argparse.Namespace) -> None:
    from api.apps.services.memory_maintenance_service import jobs
    from api.db.db_models import async_engine, async_session_factory, engine
    from common import resources
    from common.asyncio_utils import finish_owned_work

    if async_session_factory is None:
        raise RuntimeError("Maintenance worker requires PostgreSQL async sessions")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, stop.set)
    try:
        await run_worker(
            async_session_factory,
            jobs,
            worker_id=options.worker_id,
            once=options.once,
            poll_interval=options.poll_interval,
            claim_timeout_seconds=options.claim_timeout_seconds,
            heartbeat_seconds=options.heartbeat_seconds,
            stop=stop,
        )
    finally:
        # Only connections owned by this standalone process are closed. There
        # is no schema initialization, migration, source scan, or worker restart.
        await finish_owned_work(_close_connections([resources.msg_store(), resources.doc_store()], async_engine, engine))


def main(argv: list[str] | None = None) -> int:
    command = parser()
    options = command.parse_args(argv)
    if (
        not 0.05 <= options.poll_interval <= 60
        or not 5 <= options.claim_timeout_seconds <= 3600
        or not 0 < options.heartbeat_seconds < options.claim_timeout_seconds
        or not options.worker_id
        or len(options.worker_id) > 128
    ):
        command.error("Worker intervals/identity are outside their bounded ranges")
    from common.bootstrap import ensure_initialized

    ensure_initialized()
    asyncio.run(_runtime(options))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
