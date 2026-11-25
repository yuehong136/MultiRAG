"""Background health checks of the models the gateway can serve.

Off unless ``health_check_interval_seconds`` is set, like LiteLLM's
``background_health_checks``. Each round sends every callable model the
smallest real request — LiteLLM's probe: one user message and
``max_tokens: 1`` for chat, one short input for embeddings — and records
whether the upstream answered. The results are stored for the management
page, and the router tries unhealthy deployments last until a later round
finds them healthy again.

A probe is a real, billed upstream call. It is not written to the request
logs and does not count towards any limit.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from api.model_gateway.catalog import UpstreamTarget
from api.model_gateway.router import Router

logger = logging.getLogger(__name__)

# LiteLLM's defaults (litellm/constants.py).
DEFAULT_HEALTH_CHECK_INTERVAL = 300
HEALTH_CHECK_TIMEOUT_SECONDS = 60
HEALTH_CHECK_PROMPT = "test"

HEALTHY = "healthy"
UNHEALTHY = "unhealthy"
# Probes run side by side, but not so many that a round looks like a burst of traffic.
_CONCURRENCY = 8
_MAX_ERROR_LENGTH = 500
_STARTUP_DELAY_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class HealthTarget:
    """One model to probe, with the capability the probe uses."""

    tenant_id: str
    model_id: str
    model_type: str
    upstream: UpstreamTarget


@dataclass(frozen=True, slots=True)
class HealthResult:
    tenant_id: str
    model_id: str
    status: str
    checked_at: datetime
    response_time_ms: int
    status_code: int | None = None
    error_message: str | None = None


@runtime_checkable
class HealthStore(Protocol):
    async def health_targets(self) -> list[HealthTarget]: ...

    async def record_health(self, results: Sequence[HealthResult]) -> None: ...


def probe_body(target: HealthTarget) -> dict[str, Any]:
    if target.model_type == "embedding":
        return {"model": target.upstream.model_name, "input": HEALTH_CHECK_PROMPT}
    return {"model": target.upstream.model_name, "messages": [{"role": "user", "content": HEALTH_CHECK_PROMPT}], "max_tokens": 1}


class HealthChecker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: HealthStore,
        router: Router,
        *,
        interval_seconds: float,
        timeout_seconds: float = HEALTH_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._store = store
        self._router = router
        self._interval = interval_seconds
        self._timeout = timeout_seconds

    async def run_forever(self) -> None:
        await asyncio.sleep(min(_STARTUP_DELAY_SECONDS, self._interval))
        while True:
            try:
                await self.run_once()
            except Exception:
                # A failed round must not end the checks; the next one starts from scratch.
                logger.exception("model gateway: health check round failed")
            await asyncio.sleep(self._interval)

    async def run_once(self) -> list[HealthResult]:
        targets = await self._store.health_targets()
        gate = asyncio.Semaphore(_CONCURRENCY)

        async def guarded(target: HealthTarget) -> HealthResult:
            async with gate:
                return await self.probe(target)

        results = list(await asyncio.gather(*(guarded(target) for target in targets)))
        self._router.set_unhealthy({result.model_id for result in results if result.status == UNHEALTHY})
        if results:
            await self._store.record_health(results)
        unhealthy = sum(result.status == UNHEALTHY for result in results)
        logger.info("model gateway: health check round done: %s models, %s unhealthy", len(results), unhealthy)
        return results

    async def probe(self, target: HealthTarget) -> HealthResult:
        upstream = target.upstream
        headers = {"content-type": "application/json", "accept": "application/json"}
        authorization = upstream.authorization()
        if authorization:
            headers["authorization"] = authorization
        started, checked_at = time.monotonic(), datetime.now(UTC)
        status_code: int | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(self._timeout):
                response = await self._client.post(upstream.endpoint_url(target.model_type), json=probe_body(target), headers=headers)
            status_code = response.status_code
            if status_code >= 400:
                error = response.text[:_MAX_ERROR_LENGTH] or None
        except (TimeoutError, httpx.TimeoutException):
            error = "The model service did not respond in time."
        except httpx.HTTPError as failure:
            error = f"The model service could not be reached: {type(failure).__name__}"
        healthy = status_code is not None and status_code < 400
        return HealthResult(
            tenant_id=target.tenant_id,
            model_id=target.model_id,
            status=HEALTHY if healthy else UNHEALTHY,
            checked_at=checked_at,
            response_time_ms=int((time.monotonic() - started) * 1000),
            status_code=status_code,
            error_message=error,
        )
