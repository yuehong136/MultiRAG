"""One credential contract for VolcEngine chat, vision and embeddings."""

import json
from typing import Any


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate VolcEngine credential field")
        result[name] = value
    return result


def decode_volc_credentials(key: str | dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Accept a raw Ark key or an object envelope, without echoing secrets."""
    payload: Any = key
    if isinstance(key, str):
        if not key or key != key.strip():
            raise ValueError("Invalid VolcEngine API key")
        try:
            payload = json.loads(key, object_pairs_hook=_object)
        except json.JSONDecodeError:
            if key[0] in '{["' or key in {"NaN", "Infinity", "-Infinity"}:
                raise ValueError("Invalid VolcEngine credential JSON") from None
            payload = {"ark_api_key": key}
    if not isinstance(payload, dict):
        raise ValueError("VolcEngine credentials must be an object or raw Ark key")
    api_key = payload.get("ark_api_key")
    if not isinstance(api_key, str) or not api_key or api_key != api_key.strip() or any(ord(char) < 32 for char in api_key):
        raise ValueError("VolcEngine requires a nonempty ark_api_key string")
    endpoints: list[str] = []
    for field in ("ep_id", "endpoint_id"):
        if field in payload:
            value = payload[field]
            if not isinstance(value, str) or not value or value != value.strip() or any(ord(char) < 32 for char in value):
                raise ValueError("VolcEngine endpoint must be a nonempty string")
            endpoints.append(value)
    if len(set(endpoints)) > 1:
        raise ValueError("Conflicting VolcEngine endpoint fields")
    return api_key, dict(payload)


def decode_volc_key(key: str | dict[str, Any], model_name: str, *, use_endpoint: bool = True) -> tuple[str, str, dict[str, Any]]:
    """Resolve an explicit runtime model independently of the API credential."""
    api_key, payload = decode_volc_credentials(key)
    endpoints = [payload[field] for field in ("ep_id", "endpoint_id") if field in payload]
    selected = endpoints[0] if endpoints and use_endpoint else model_name
    if not isinstance(selected, str) or not selected or selected != selected.strip() or any(ord(char) < 32 for char in selected):
        raise ValueError("VolcEngine requires an explicit model name or endpoint")
    return api_key, selected, dict(payload)
