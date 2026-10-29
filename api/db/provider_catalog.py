"""Provider catalog identity, ordering and instance transport normalization."""

import json
from typing import Any
from urllib.parse import urlsplit

from common import settings
from core.llm.openrouter_auth import decode_openrouter_key


def public_factories() -> list[dict[str, Any]]:
    # This name supplies regional metadata; it has no independently registered driver.
    return [item for item in settings.FACTORY_LLM_INFOS if item["name"] not in {"siliconflow_intl", "FastEmbed"}]


def provider_order(name: str) -> tuple[int, str]:
    factory: dict[str, Any] = next((item for item in settings.FACTORY_LLM_INFOS if item["name"] == name), {})
    rank = factory.get("rank", 500)
    try:
        rank = int(rank) if type(rank) in {str, int} else 500
    except (TypeError, ValueError, OverflowError):
        rank = 500
    return -rank, name


def regional_provider(provider: str, region: str) -> str:
    if provider != "SILICONFLOW":
        return provider
    if region not in {"", "default", "cn", "intl"}:
        raise ValueError("Unsupported SiliconFlow region")
    return "siliconflow_intl" if region == "intl" else provider


def _openrouter_transport_key(key: str) -> str:
    """Use one known envelope for the chat and vision runtime adapters."""
    return json.dumps(decode_openrouter_key(key), ensure_ascii=False)


def provider_transport(provider: str, key: str, base_url: str, region: str, *, stored: bool = False) -> tuple[str, str, str]:
    if provider == "OpenRouter":
        return _openrouter_transport_key(key), base_url, region
    if provider != "SILICONFLOW":
        return key, base_url, region
    try:
        envelope = json.loads(key)
    except (TypeError, ValueError):
        envelope = None
    if isinstance(envelope, dict):
        if not stored or set(envelope) - {"api_key", "source_fid", "is_tools"} or not isinstance(envelope.get("api_key"), str):
            raise ValueError("SiliconFlow requires a raw API key and separate region")
        key = envelope["api_key"]
        if not region and envelope.get("source_fid") == "siliconflow_intl":
            region = "intl"
    target = regional_provider(provider, region)
    expected = "api.siliconflow.com" if target == "siliconflow_intl" else "api.siliconflow.cn"
    if base_url:
        host = urlsplit(base_url).hostname
        if host in {"api.siliconflow.com", "api.siliconflow.cn"} and host != expected:
            raise ValueError("SiliconFlow base URL conflicts with region")
    else:
        base_url = f"https://{expected}/v1"
    return key, base_url, region


def normalize_model_transport(config: dict[str, Any]) -> dict[str, Any]:
    config = dict(config)
    if config.get("llm_factory") == "OpenRouter":
        config["api_key"] = _openrouter_transport_key(config.get("api_key", ""))
    if config.get("llm_factory") == "SILICONFLOW":
        try:
            envelope = json.loads(config.get("api_key", ""))
        except (TypeError, ValueError):
            envelope = None
        if isinstance(envelope, dict) and "is_tools" in envelope:
            if type(envelope["is_tools"]) is not bool:
                raise ValueError("Stored is_tools must be a boolean")
            config.setdefault("is_tools", envelope["is_tools"])
        key, base, region = provider_transport("SILICONFLOW", config.get("api_key", ""), config.get("api_base", ""), config.get("region", ""), stored=True)
        config.update(api_key=key, api_base=base, region=region)
        config.pop("api_key_payload", None)
    return config
