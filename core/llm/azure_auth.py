"""One credential contract for the active Azure embedding and vision adapters."""

import json
from typing import Any

DEFAULT_API_VERSION = "2024-02-01"


def resolve_azure_credentials(key: str | dict[str, Any]) -> tuple[str, str]:
    payload: Any = key
    if isinstance(key, str):
        try:
            payload = json.loads(key)
        except ValueError:
            return key, DEFAULT_API_VERSION
        if not isinstance(payload, dict):
            return key, DEFAULT_API_VERSION
    api_key = payload.get("api_key", "")
    api_version = payload.get("api_version", DEFAULT_API_VERSION)
    if not isinstance(api_key, str) or not isinstance(api_version, str) or not api_version or api_version != api_version.strip():
        raise ValueError("Invalid Azure credential envelope")
    return api_key, api_version
