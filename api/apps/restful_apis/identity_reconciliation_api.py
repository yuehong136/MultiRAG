"""API-process lifecycle composition for EIM-I8 identity reconciliation.

This route module deliberately exposes no HTTP operation.  Route discovery
includes its router only to merge the lifespan into every API process.  The
feature remains disabled by default; when disabled, startup does not inspect
the database lifecycle or construct an enterprise identity provider.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import APIRouter, FastAPI

from api.identity.reconciliation.runtime import IdentityReconciliationWorker
from common.app_config import IdentityReconciliationConfig, get_app_config

LOGGER = logging.getLogger(__name__)
_STATE_KEY = "_multirag_identity_reconciliation"


class IdentityReconciliationRuntimeUnavailableError(RuntimeError):
    """Raised when enabled reconciliation cannot bind to the API DB lifecycle."""


@dataclass(slots=True)
class _IdentityReconciliationLifecycleHandle:
    stop_event: asyncio.Event
    task: asyncio.Task[None]


def _build_identity_reconciliation_worker(
    config: IdentityReconciliationConfig,
) -> IdentityReconciliationWorker:
    """Compose I8 lazily after the enabled gate has passed."""

    config.require_enabled()

    # Delayed imports keep module discovery and the disabled path free of DB,
    # secret-store, provider-SDK, and provider-registry side effects.
    from api.db import db_models
    from api.identity.reconciliation.repository import (
        SqlAlchemyIdentityReconciliationRepository,
    )
    from api.identity.reconciliation.runtime import (
        IdentityReconciliationRuntimeLimits,
    )
    from api.identity.reconciliation.service import (
        IdentityReconciliationLimits,
        IdentityReconciliationService,
    )
    from api.identity.reconciliation.telemetry import (
        PROCESS_RECONCILIATION_TELEMETRY,
    )
    from api.identity_adapters.provider_runtime import (
        get_identity_provider_registry,
    )

    session_factory = db_models.async_session_factory
    if session_factory is None:
        raise IdentityReconciliationRuntimeUnavailableError(
            "enabled identity reconciliation requires the async database lifecycle",
        )

    repository = SqlAlchemyIdentityReconciliationRepository(session_factory)
    provider_registry = get_identity_provider_registry(session_factory)
    service = IdentityReconciliationService(
        repository,
        provider_registry,
        IdentityReconciliationLimits(
            lease_seconds=config.lease_seconds,
            probe_safety_margin_seconds=config.probe_safety_margin_seconds,
            cycle_interval_seconds=config.cycle_interval_seconds,
            active_window_seconds=config.active_window_seconds,
            backoff_initial_seconds=config.backoff_initial_seconds,
            backoff_max_seconds=config.backoff_max_seconds,
            not_found_confirmation_seconds=(config.not_found_confirmation_seconds),
            degrade_after_failures=config.degrade_after_failures,
            max_tighten_per_cycle=config.max_tighten_per_cycle,
        ),
        telemetry=PROCESS_RECONCILIATION_TELEMETRY,
    )
    return IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(
            poll_seconds=config.poll_seconds,
            seed_interval_seconds=config.seed_interval_seconds,
        ),
        enabled=True,
        telemetry=PROCESS_RECONCILIATION_TELEMETRY,
    )


def _observe_worker_exit(
    task: asyncio.Task[None],
    *,
    stop_event: asyncio.Event,
) -> None:
    """Consume unexpected task completion using only a closed error surface."""

    if stop_event.is_set():
        return
    if task.cancelled():
        LOGGER.error(
            "identity_reconciliation_event=worker_exit result=failed error_code=IDENTITY_RECONCILIATION_WORKER_CANCELLED",
        )
        return
    error = task.exception()
    if error is None:
        LOGGER.error(
            "identity_reconciliation_event=worker_exit result=failed error_code=IDENTITY_RECONCILIATION_WORKER_STOPPED",
        )
        return
    LOGGER.error(
        "identity_reconciliation_event=worker_exit result=failed error_code=IDENTITY_RECONCILIATION_WORKER_CRASHED error_type=%s",
        type(error).__name__,
    )


@asynccontextmanager
async def _identity_reconciliation_lifespan(
    app: FastAPI,
) -> AsyncIterator[None]:
    """Own one serial worker per API process; durable leases coordinate peers."""

    config = get_app_config().identity.reconciliation
    if not config.enabled:
        yield
        return

    worker = _build_identity_reconciliation_worker(config)
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        worker.run(stop_event),
        name="identity-reconciliation-worker",
    )
    task.add_done_callback(
        lambda completed: _observe_worker_exit(
            completed,
            stop_event=stop_event,
        ),
    )
    handle = _IdentityReconciliationLifecycleHandle(
        stop_event=stop_event,
        task=task,
    )
    setattr(app.state, _STATE_KEY, handle)
    LOGGER.info(
        "identity_reconciliation_event=worker_start result=ok error_code=",
    )
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()
        stopped_cleanly = True
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            stopped_cleanly = False
            LOGGER.warning(
                "identity_reconciliation_event=worker_stop result=failed error_code=IDENTITY_RECONCILIATION_WORKER_STOP_FAILED error_type=%s",
                type(exc).__name__,
            )
        if getattr(app.state, _STATE_KEY, None) is handle:
            delattr(app.state, _STATE_KEY)
        if stopped_cleanly:
            LOGGER.info(
                "identity_reconciliation_event=worker_stop result=ok error_code=",
            )


router = APIRouter(lifespan=_identity_reconciliation_lifespan)


__all__ = [
    "IdentityReconciliationRuntimeUnavailableError",
    "router",
]
