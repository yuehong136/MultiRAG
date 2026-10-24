"""Shared TTS audio cache for the Canvas and dialog entry points."""

import hashlib
import json
import logging
import os
import re
from collections.abc import Iterator
from typing import Any, Protocol

from core.utils.redis_conn import REDIS_CONN

_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60
_KEY_PREFIX = "tts:cache:v1:"
_BOOKKEEPING_FIELDS = {"used_tokens", "create_time", "create_date", "update_time", "update_date", "status"}


class TTSModel(Protocol):
    model_config: dict[str, Any]
    tenant_id: str

    def tts(self, text: str) -> Iterator[bytes]: ...


def _ttl_seconds() -> int:
    raw = os.environ.get("RAGFLOW_TTS_CACHE_TTL_SECONDS")
    if not raw:
        return _DEFAULT_TTL_SECONDS
    try:
        return max(int(raw), 0)
    except ValueError:
        logging.warning("Invalid RAGFLOW_TTS_CACHE_TTL_SECONDS, using default")
        return _DEFAULT_TTL_SECONDS


def _build_key(tts_mdl: TTSModel, text: str) -> str | None:
    config = getattr(tts_mdl, "model_config", None)
    if not isinstance(config, dict):
        return None
    tenant_id = getattr(tts_mdl, "tenant_id", None) or config.get("tenant_id")
    model_id = config.get("id") or config.get("llm_name") or config.get("model_name")
    if not tenant_id or not model_id:
        return None
    # Config includes provider, endpoint and structured voice/reference settings.
    # Hash credentials too, without exposing them or speech text in Redis keys.
    # Usage and timestamps change independently of synthesized audio.
    identity = {"tenant_id": tenant_id, "config": {key: value for key, value in config.items() if key not in _BOOKKEEPING_FIELDS}}
    try:
        serialized = json.dumps(identity, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    model_digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}{model_digest}:{text_digest}"


def _to_hex_string(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str) and re.fullmatch(r"(?:[0-9a-fA-F]{2})+", value):
        return value
    return None


def synthesize_with_cache(tts_mdl: TTSModel | None, cleaned_text: str) -> str | None:
    """Return complete hex audio, or None on empty/failed synthesis.

    A hit skips the bundle's provider call and usage recording. Nonpositive TTL
    disables both reads and writes. Redis failures preserve uncached synthesis;
    concurrent misses may synthesize independently, as without the cache.
    """
    if not tts_mdl or not cleaned_text:
        return None
    ttl = _ttl_seconds()
    key = _build_key(tts_mdl, cleaned_text) if ttl else None
    if key:
        try:
            cached = _to_hex_string(REDIS_CONN.get(key))
            if cached:
                return cached
        except Exception as error:
            logging.warning("TTS cache lookup failed (%s)", type(error).__name__)

    audio = bytearray()
    try:
        for chunk in tts_mdl.tts(cleaned_text):
            if not isinstance(chunk, (bytes, bytearray)):
                raise TypeError("TTS returned a non-audio chunk")
            audio.extend(chunk)
    except Exception as error:
        logging.error("TTS failed (%s; text length=%d)", type(error).__name__, len(cleaned_text))
        return None
    if not audio:
        return None
    hex_value = audio.hex()
    if key:
        try:
            REDIS_CONN.set(key, hex_value, exp=ttl)
        except Exception as error:
            logging.warning("TTS cache store failed (%s)", type(error).__name__)
    return hex_value
