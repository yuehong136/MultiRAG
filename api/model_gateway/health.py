"""Background health checks of the models the gateway can serve.

Each round finds out, for every callable model, whether its upstream can
serve it, and says *why not* when it cannot. The design borrows from three
gateways rather than inventing rules:

- **Probe** (LiteLLM, new-api): the smallest real request — one user message
  and ``max_tokens: 1`` for chat, one short input for embeddings. OpenAI's
  reasoning models refuse ``max_tokens`` and cannot answer in one token, so a
  rejected chat probe is sent once more with ``max_completion_tokens: 16``,
  the value new-api's channel test uses for them.
- **Classification** (Bifrost's ``failureclass.go``): a failure is a rate
  limit, a rejected credential, an exhausted quota, a model the upstream does
  not have, a server error, a timeout, or an unreachable host. Quota wording
  is recognised by new-api's ``AutomaticDisableKeywords``, because providers
  report an empty balance as 429 or 403 as often as 402.
- **What counts against a deployment** (new-api, Portkey, Envoy): being rate
  limited does not. It is a state of the account that passes by itself, so a
  rate-limited model is shown as such and keeps its place in routing; live
  429s are already handled by the router's cooldown. Only real failures make
  the router try a deployment last.

To spend as little of the upstream's allowance as possible, a model that
served a real call during the last interval is not probed at all (Bifrost
tracks health from traffic alone), and a rate-limited model is left alone
for as long as the upstream asked, at least until the round after next.

A probe is a real, billed upstream call. It is not written to the request
logs and does not count towards any limit.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
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
# new-api's channel test: what a reasoning model needs to answer at all.
REASONING_PROBE_TOKENS = 16

HEALTHY = "healthy"
RATE_LIMITED = "rate_limited"
UNHEALTHY = "unhealthy"

# Why a model is not healthy; the names follow Bifrost's failure classes.
RATE_LIMIT = "rate_limit"
CREDENTIAL = "credential"
QUOTA = "quota"
MODEL_GONE = "model_gone"
TRANSIENT = "transient"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"

# Where a result comes from: a probe, or real calls that succeeded since the last round.
PROBE = "probe"
TRAFFIC = "traffic"

# new-api's default AutomaticDisableKeywords (setting/operation_setting), plus OpenAI's error code for the same.
QUOTA_KEYWORDS = (
    "your credit balance is too low",
    "this organization has been disabled",
    "you exceeded your current quota",
    "insufficient_quota",
    "insufficient balance",
    "arrearage",
)
# The upstream answered and took the credential, but not this request.
_REQUEST_REJECTED = (400, 422)
# Probes run side by side, but not so many that a round looks like a burst of traffic.
_CONCURRENCY = 8
_MAX_ERROR_LENGTH = 500
_STARTUP_DELAY_SECONDS = 15.0
# However long an upstream asks to be left alone, a model is looked at again within the hour.
_MAX_BACKOFF_SECONDS = 3600.0


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
    failure_class: str | None = None
    source: str = PROBE


@runtime_checkable
class HealthStore(Protocol):
    async def health_targets(self) -> list[HealthTarget]: ...

    async def record_health(self, results: Sequence[HealthResult]) -> None: ...


def probe_body(target: HealthTarget, *, reasoning: bool = False) -> dict[str, Any]:
    if target.model_type == "embedding":
        return {"model": target.upstream.model_name, "input": HEALTH_CHECK_PROMPT}
    body: dict[str, Any] = {"model": target.upstream.model_name, "messages": [{"role": "user", "content": HEALTH_CHECK_PROMPT}]}
    if reasoning:
        body["max_completion_tokens"] = REASONING_PROBE_TOKENS
    else:
        body["max_tokens"] = 1
    return body


def classify(status_code: int, body: str = "") -> tuple[str, str | None]:
    """What an upstream's answer to a probe says about the model: ``(status, failure_class)``."""
    if status_code < 400 or status_code in _REQUEST_REJECTED:
        # A malformed-request answer means the probe does not fit the model, not that the model is down.
        return HEALTHY, None
    text = body.lower()
    if status_code == 402 or any(keyword in text for keyword in QUOTA_KEYWORDS):
        return UNHEALTHY, QUOTA
    if status_code == 429:
        return RATE_LIMITED, RATE_LIMIT
    if status_code in (401, 403):
        return UNHEALTHY, CREDENTIAL
    if status_code == 404:
        return UNHEALTHY, MODEL_GONE
    if status_code == 408:
        return UNHEALTHY, TIMEOUT
    return UNHEALTHY, TRANSIENT


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None


class HealthChecker:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: HealthStore,
        router: Router,
        *,
        interval_seconds: float,
        timeout_seconds: float = HEALTH_CHECK_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._store = store
        self._router = router
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._monotonic = monotonic
        # Rate-limited models and when they may be probed again; their last result stands meanwhile.
        self._resting: dict[str, tuple[float, HealthResult]] = {}
        # Probes sent in the round under way, for the log.
        self._probed = 0

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
        self._probed = 0

        async def guarded(target: HealthTarget) -> HealthResult:
            async with gate:
                return await self._check(target)

        results = list(await asyncio.gather(*(guarded(target) for target in targets)))
        known = {target.model_id for target in targets}
        self._resting = {model_id: rest for model_id, rest in self._resting.items() if model_id in known}
        self._router.set_unhealthy({result.model_id for result in results if result.status == UNHEALTHY})
        if results:
            await self._store.record_health(results)
        counts = {status: sum(result.status == status for result in results) for status in (UNHEALTHY, RATE_LIMITED)}
        logger.info("model gateway: health check round done: %s models (%s probed), %s unhealthy, %s rate limited", len(results), self._probed, counts[UNHEALTHY], counts[RATE_LIMITED])
        return results

    async def _check(self, target: HealthTarget) -> HealthResult:
        """Probe a model, unless real calls or a recent rate limit already say how it is."""
        now = self._monotonic()
        resting = self._resting.get(target.model_id)
        if resting is not None and resting[0] > now:
            # Probing a rate-limited model takes from the allowance it is short of.
            return resting[1]
        served = self._router.seconds_since_success(target.model_id)
        if served is not None and served < self._interval:
            self._resting.pop(target.model_id, None)
            return HealthResult(tenant_id=target.tenant_id, model_id=target.model_id, status=HEALTHY, checked_at=datetime.now(UTC), response_time_ms=0, source=TRAFFIC)
        self._probed += 1
        result, retry_after = await self._probe(target)
        if result.status == RATE_LIMITED:
            wait = min(max(retry_after or 0.0, 2 * self._interval), _MAX_BACKOFF_SECONDS)
            # A little less than two rounds, so the round after next is not missed by a few milliseconds.
            self._resting[target.model_id] = (now + wait - self._interval / 2, result)
        else:
            self._resting.pop(target.model_id, None)
        return result

    async def probe(self, target: HealthTarget) -> HealthResult:
        return (await self._probe(target))[0]

    async def _probe(self, target: HealthTarget) -> tuple[HealthResult, float | None]:
        upstream = target.upstream
        headers = {"content-type": "application/json", "accept": "application/json"}
        authorization = upstream.authorization()
        if authorization:
            headers["authorization"] = authorization
        started, checked_at = time.monotonic(), datetime.now(UTC)
        status_code: int | None = None
        error: str | None = None
        retry_after: float | None = None
        status: str = UNHEALTHY
        failure: str | None = UNREACHABLE
        try:
            async with asyncio.timeout(self._timeout):
                url = upstream.endpoint_url(target.model_type)
                response = await self._client.post(url, json=probe_body(target), headers=headers)
                if response.status_code in _REQUEST_REJECTED and target.model_type == "chat":
                    response = await self._client.post(url, json=probe_body(target, reasoning=True), headers=headers)
            status_code = response.status_code
            status, failure = classify(status_code, response.text if status_code >= 400 else "")
            if status != HEALTHY:
                error = response.text[:_MAX_ERROR_LENGTH] or None
                retry_after = _retry_after_seconds(response)
        except (TimeoutError, httpx.TimeoutException):
            failure, error = TIMEOUT, "The model service did not respond in time."
        except httpx.HTTPError as problem:
            error = f"The model service could not be reached: {type(problem).__name__}"
        result = HealthResult(
            tenant_id=target.tenant_id,
            model_id=target.model_id,
            status=status,
            checked_at=checked_at,
            response_time_ms=int((time.monotonic() - started) * 1000),
            status_code=status_code,
            error_message=error,
            failure_class=failure,
        )
        return result, retry_after
