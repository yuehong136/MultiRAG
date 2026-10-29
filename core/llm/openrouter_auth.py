"""Strict credentials shared by OpenRouter runtime and discovery."""

import json
from typing import Any


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Invalid OpenRouter credential")
        result[name] = value
    return result


def decode_openrouter_key(key: str) -> dict[str, Any]:
    """Accept raw tokens and known envelopes without echoing credentials."""
    if not isinstance(key, str):
        raise ValueError("Invalid OpenRouter credential")
    payload: dict[str, Any] = {"api_key": key, "provider_order": ""}
    try:
        parsed = json.loads(key, object_pairs_hook=_object)
    except json.JSONDecodeError:
        if key.startswith(("{", "[", '"')) or key in {"NaN", "Infinity", "-Infinity"}:
            raise ValueError("Invalid OpenRouter credential") from None
    else:
        if not isinstance(parsed, dict) or "api_key" not in parsed or set(parsed) - {"api_key", "provider_order", "is_tools"}:
            raise ValueError("Invalid OpenRouter credential")
        payload.update(parsed)
    token, order = payload["api_key"], payload["provider_order"]
    if (
        not isinstance(token, str)
        or not token
        or any(not 33 <= ord(char) <= 126 for char in token)
        or (not isinstance(order, str) and (not isinstance(order, list) or any(not isinstance(item, str) for item in order)))
        or ("is_tools" in payload and type(payload["is_tools"]) is not bool)
    ):
        raise ValueError("Invalid OpenRouter credential")
    return payload
