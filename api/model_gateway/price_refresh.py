"""Keep the price table current without upgrading ``litellm``.

LiteLLM itself reads its cost map from its repository at run time and keeps
the copy bundled with the package as a backup. The gateway does the same when
``price_map_refresh_hours`` is set: it fetches the map, checks it the way
LiteLLM's ``GetModelCostMap.validate_model_cost_map`` does, and only then
replaces the table in this process. A failed or rejected fetch changes
nothing: the last good table stays, and before any success that is the
bundled one.

Costs already stored with request logs never change; a new table prices the
calls that end after it was installed.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from api.model_gateway import pricing
from common.app_config import ModelGatewayConfig

logger = logging.getLogger(__name__)

# LiteLLM's ``model_cost_map_url`` and validation thresholds (litellm/__init__.py, litellm/constants.py).
MODEL_COST_MAP_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MODEL_COST_MAP_MIN_MODEL_COUNT = 50
MODEL_COST_MAP_MAX_SHRINK_RATIO = 0.5
_FETCH_TIMEOUT_SECONDS = 15.0
# A map is a few megabytes; anything far larger is not a cost map.
_MAX_BYTES = 32 * 1024 * 1024
# After a failure the next try does not wait a whole interval, but neither does it hammer the source.
_RETRY_SECONDS = 900.0


def validate_cost_map(fetched: Any, current_count: int) -> str | None:
    """Why a fetched map must not replace the current one, or ``None`` when it may."""
    if not isinstance(fetched, dict) or not fetched:
        return "it is not a JSON object with entries"
    entries = sum(isinstance(entry, dict) for entry in fetched.values())
    if entries < MODEL_COST_MAP_MIN_MODEL_COUNT:
        return f"it lists {entries} models, fewer than {MODEL_COST_MAP_MIN_MODEL_COUNT}"
    if current_count > 0 and entries < current_count * MODEL_COST_MAP_MAX_SHRINK_RATIO:
        return f"it lists {entries} models, less than half of the {current_count} in use"
    return None


class PriceMapRefresher:
    def __init__(self, source: str, interval_seconds: float, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._source = source.strip() or MODEL_COST_MAP_URL
        self._interval = interval_seconds
        self._transport = transport
        self._next_at = 0.0
        self._task: asyncio.Task[bool] | None = None

    async def refresh_once(self) -> bool:
        """Fetch, check and install the map; ``False`` leaves the current table in place."""
        try:
            fetched = await self._fetch()
        except (httpx.HTTPError, OSError, ValueError) as error:
            logger.warning("model gateway: could not fetch the price map from %s (%s); keeping the current prices", self._source, type(error).__name__)
            return False
        rejected = validate_cost_map(fetched, len(pricing.default_table()))
        if rejected is not None:
            logger.warning("model gateway: the price map from %s was rejected: %s; keeping the current prices", self._source, rejected)
            return False
        pricing.install_table(fetched)
        logger.info("model gateway: price map refreshed from %s: %s models", self._source, len(pricing.default_table()))
        return True

    async def run_forever(self) -> None:
        while True:
            refreshed = await self.refresh_once()
            await asyncio.sleep(self._interval if refreshed else min(self._interval, _RETRY_SECONDS))

    def kick(self) -> None:
        """Start a refresh in the background when one is due; for processes without a refresh loop."""
        now = time.monotonic()
        if now < self._next_at or (self._task is not None and not self._task.done()):
            return
        self._next_at = now + min(self._interval, _RETRY_SECONDS)
        self._task = asyncio.get_running_loop().create_task(self._kicked())

    async def _kicked(self) -> bool:
        refreshed = await self.refresh_once()
        if refreshed:
            self._next_at = time.monotonic() + self._interval
        return refreshed

    async def _fetch(self) -> Any:
        if not self._source.lower().startswith(("http://", "https://")):
            # An internal mirror may simply be a file.
            return json.loads(await asyncio.to_thread(Path(self._source).read_text, encoding="utf-8"))
        async with httpx.AsyncClient(transport=self._transport, timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(self._source)
            response.raise_for_status()
            if len(response.content) > _MAX_BYTES:
                raise ValueError("the price map is too large")
            return response.json()


_shared: PriceMapRefresher | None = None


def refresher_of(settings: ModelGatewayConfig) -> PriceMapRefresher | None:
    """This process's refresher, or ``None`` while refreshing is off."""
    global _shared
    if settings.price_map_refresh_hours <= 0:
        return None
    if _shared is None:
        _shared = PriceMapRefresher(settings.price_map_url, settings.price_map_refresh_hours * 3600)
    return _shared
