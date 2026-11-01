"""Replicate credentials and request-owned SDK transport."""

import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from common.asyncio_utils import finish_owned_work


def normalize_replicate_key(key: str | dict[str, Any]) -> str:
    payload: Any = key
    if isinstance(key, str):
        try:
            decoded = json.loads(key)
        except ValueError:
            pass
        else:
            if isinstance(decoded, dict):
                payload = decoded
    if isinstance(payload, dict):
        payload = payload.get("api_key")
    if not isinstance(payload, str) or not payload or payload != payload.strip() or any(ord(char) < 32 for char in payload):
        raise ValueError("Invalid Replicate credential")
    return payload


@asynccontextmanager
async def replicate_session(key: str, base_url: str | None, timeout: float) -> AsyncIterator[Any]:
    """Use only the async SDK transport, closing it before releasing the request."""
    from replicate.client import Client

    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Replicate timeout must be finite and positive")
    client = Client(api_token=key, base_url=base_url or None, timeout=httpx.Timeout(timeout))
    # The pinned SDK exposes lazy transports but no client-level close method.
    transport = client._async_client
    try:
        yield client
    finally:
        await finish_owned_work(transport.aclose())
