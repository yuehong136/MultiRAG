"""MiniMax credentials and query options shared by both chat backends."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit


def decode_minimax_key(key: str | dict[str, Any]) -> tuple[str, str]:
    payload: Any = key
    if isinstance(key, str):
        try:
            payload = json.loads(key)
        except json.JSONDecodeError:
            return key, ""
    if not isinstance(payload, dict):
        if isinstance(key, str):
            return key, ""
        raise ValueError("MiniMax credentials must be a string or object")
    api_key = payload.get("api_key", "")
    group_id = payload.get("group_id") or ""
    if not isinstance(api_key, str) or not isinstance(group_id, str | int) or isinstance(group_id, bool):
        raise ValueError("Invalid MiniMax credential field type")
    return api_key, str(group_id)


def minimax_request_options(base_url: str, group_id: str, extra_query: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Keep query values out of the base path that the SDK extends with its endpoint."""
    parsed = urlsplit(base_url.strip())
    query: dict[str, Any] = {key: values[0] if len(values) == 1 else values for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
    query.update(extra_query or {})
    if group_id:
        query["GroupId"] = group_id
    elif isinstance(query.get("GroupId"), list):
        query["GroupId"] = query["GroupId"][-1] if query["GroupId"] else None
    return urlunsplit(parsed._replace(path=parsed.path.rstrip("/"), query="", fragment="")), query
