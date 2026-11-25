"""Which deployment of a public name serves a request, and which are resting.

Several registered models may answer to one public name; each is a
*deployment* of that name. The rules follow LiteLLM's router so its
documentation applies:

- ``order``: the lowest order is tried first, higher orders only once the
  lower ones failed or are cooling down. A deployment without an order comes
  after every deployment that has one.
- ``weight``: among deployments of one order the pick is random, in
  proportion to weight (LiteLLM's ``simple-shuffle``).
- cooldown: a deployment that fails rests for ``cooldown_seconds``. The
  conditions are those of LiteLLM's ``_should_cooldown_deployment``.

Two choices differ from LiteLLM. A request never calls the same deployment
twice, so a retry always means another deployment. And when every deployment
of a name is cooling down they are all tried anyway, instead of refusing the
request: a wrong guess about health must not take a name offline.

State lives in this process, like the limit counters.
"""

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar, runtime_checkable

# LiteLLM's defaults (litellm/constants.py).
DEFAULT_NUM_RETRIES = 2
DEFAULT_COOLDOWN_TIME_SECONDS = 5.0
DEFAULT_FAILURE_THRESHOLD_PERCENT = 0.5
DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS = 5
SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD = 1000

# What the gateway records when the upstream gave no status at all.
STATUS_UNREACHABLE = 503
STATUS_TIMEOUT = 408


@runtime_checkable
class Deployment(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def weight(self) -> int: ...

    @property
    def order(self) -> int | None: ...


D = TypeVar("D", bound=Deployment)


def is_retryable(status_code: int) -> bool:
    """Whether another deployment may be tried after this upstream status.

    408, 409, 429 and 5xx are LiteLLM's ``_should_retry``; 401, 403 and 404 say
    this deployment is misconfigured, which another one may not be. Any other
    4xx is the upstream's verdict on the request itself.
    """
    return status_code in (401, 403, 404, 408, 409, 429) or status_code >= 500


def _cooldown_applies(status_code: int) -> bool:
    """LiteLLM's ``_is_cooldown_required``: of the 4xx only these say something about the deployment."""
    if 400 <= status_code < 500:
        return status_code in (401, 404, 408, 429)
    return True


def _should_retry_same(status_code: int) -> bool:
    """LiteLLM's ``_should_retry``: a later identical call might succeed."""
    return status_code in (408, 409, 429) or status_code >= 500


@dataclass(slots=True)
class _Minute:
    minute: int = -1
    successes: int = 0
    failures: int = 0


class Router:
    """Orders the deployments of a name and keeps their recent health."""

    def __init__(
        self,
        *,
        cooldown_seconds: float = DEFAULT_COOLDOWN_TIME_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._rng = rng or random.Random()
        self._minutes: dict[str, _Minute] = {}
        self._cooling_until: dict[str, float] = {}

    def plan(self, deployments: Sequence[D]) -> list[D]:
        """Every deployment once, in the order a request should try them."""
        if len(deployments) < 2:
            return list(deployments)
        healthy = [item for item in deployments if not self.cooling(item.model_id)]
        resting = [item for item in deployments if self.cooling(item.model_id)]
        return self._ordered(healthy) + self._ordered(resting)

    def cooling(self, model_id: str) -> bool:
        until = self._cooling_until.get(model_id)
        if until is None:
            return False
        if until <= self._clock():
            del self._cooling_until[model_id]
            return False
        return True

    def succeeded(self, model_id: str) -> None:
        self._minute(model_id).successes += 1

    def failed(self, model_id: str, status_code: int, *, deployments: int) -> bool:
        """Count a failure; returns whether the deployment now rests."""
        minute = self._minute(model_id)
        minute.failures += 1
        if self._cooldown_seconds <= 0 or not _cooldown_applies(status_code):
            return False
        single = deployments <= 1
        total = minute.successes + minute.failures
        percent_fails = minute.failures / total
        rests = (
            (status_code == 429 and not single)
            or (percent_fails == 1.0 and total >= SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD)
            or (percent_fails > DEFAULT_FAILURE_THRESHOLD_PERCENT and total >= DEFAULT_FAILURE_THRESHOLD_MINIMUM_REQUESTS and not single)
            or not _should_retry_same(status_code)
        )
        if rests:
            self._cooling_until[model_id] = self._clock() + self._cooldown_seconds
        return rests

    def _minute(self, model_id: str) -> _Minute:
        now = int(self._clock() // 60)
        minute = self._minutes.get(model_id)
        if minute is None or minute.minute != now:
            minute = _Minute(minute=now)
            self._minutes[model_id] = minute
        return minute

    def _ordered(self, deployments: Sequence[D]) -> list[D]:
        tiers: dict[float, list[D]] = {}
        for item in deployments:
            tiers.setdefault(item.order if item.order is not None else float("inf"), []).append(item)
        plan: list[D] = []
        for order in sorted(tiers):
            plan.extend(self._shuffled(tiers[order]))
        return plan

    def _shuffled(self, tier: list[D]) -> list[D]:
        """A weighted random order: each next pick is drawn from those left."""
        left, picked = list(tier), []
        while len(left) > 1:
            choice = self._rng.choices(range(len(left)), weights=[max(item.weight, 0) or 1 for item in left])[0]
            picked.append(left.pop(choice))
        return picked + left
