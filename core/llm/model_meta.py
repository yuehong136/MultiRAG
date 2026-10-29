"""Read-only model discovery with bounded I/O and explicit failure semantics."""

import asyncio
import ipaddress
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from common.asyncio_utils import finish_owned_work

MAX_MODELS = 100
MAX_BODY_BYTES = 1024 * 1024
CONCURRENCY = 4
DISCOVERY_SECONDS = 30


class DiscoveryError(ValueError):
    """The remote inventory could not be read completely and safely."""


@dataclass
class ModelMetadata:
    name: str
    model_types: list[str]
    max_tokens: int = 8192
    features: list[str] = field(default_factory=list)
    display_name: str = ""


def fish_access_token(key: str) -> str:
    """Accept known structured credentials without using a JSON blob as a token."""
    if not key or key != key.strip() or any(ord(char) < 32 for char in key):
        raise ValueError("Invalid Fish Audio credential")
    if not key.startswith(("{", "[", '"')):
        return key
    try:
        payload = json.loads(key)
    except ValueError:
        raise ValueError("Invalid Fish Audio credential") from None
    if not isinstance(payload, dict):
        raise ValueError("Invalid Fish Audio credential")
    tokens = [payload[name] for name in ("fish_audio_ak", "access_token", "api_key") if name in payload]
    if not tokens or any(not isinstance(token, str) or not token or token != token.strip() or any(ord(char) < 32 for char in token) for token in tokens) or len(set(tokens)) != 1:
        raise ValueError("Invalid Fish Audio credential")
    return tokens[0]


def endpoint(base_url: str) -> str:
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid discovery base URL") from None
    if (
        not base_url
        or base_url != base_url.strip()
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or "\\" in base_url
        or any(ord(char) < 32 for char in base_url)
        or (port is not None and not 0 < port <= 65535)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or "%" in parsed.path
    ):
        raise ValueError("Invalid discovery base URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and (address.is_link_local or address.is_multicast or address.is_unspecified):
        raise ValueError("Invalid discovery host")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 128 or value != value.strip() or "@" in value or any(ord(char) < 32 for char in value):
        raise DiscoveryError("Invalid model identity in remote inventory")
    return value


class Base(ABC):
    def __init__(self, api_key: str, base_url: str = "") -> None:
        self.api_key = api_key
        self.base_url = base_url

    @abstractmethod
    async def get_model_list(self) -> list[ModelMetadata]:
        raise NotImplementedError

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with client.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    raise DiscoveryError("Model discovery returned an unsuccessful response")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BODY_BYTES:
                        raise DiscoveryError("Model discovery response exceeds its size limit")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise DiscoveryError("Invalid model discovery response")
            return value
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, DiscoveryError):
                raise
            # Remote exceptions may include URL, Authorization or response bodies.
            raise DiscoveryError("Model discovery transport or response failed") from None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), limits=httpx.Limits(max_connections=CONCURRENCY), follow_redirects=False, trust_env=False)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[httpx.AsyncClient]:
        client = self._client()
        try:
            yield client
        finally:
            await finish_owned_work(client.aclose())


class Ollama(Base):
    _FACTORY_NAME = "Ollama"

    async def get_model_list(self) -> list[ModelMetadata]:
        base = endpoint(self.base_url)
        if self.api_key:
            try:
                loopback = ipaddress.ip_address(urlsplit(base).hostname or "").is_loopback
            except ValueError:
                loopback = False
            if not loopback:
                raise ValueError("Authenticated Ollama discovery requires a literal loopback origin")
            if self.api_key != self.api_key.strip() or any(ord(char) < 32 for char in self.api_key):
                raise ValueError("Invalid Ollama credential")
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS), self._session() as client:
                tags = await self._request(client, "GET", base + "/api/tags", headers=headers)
                models = tags.get("models")
                if not isinstance(models, list) or len(models) > MAX_MODELS:
                    raise DiscoveryError("Invalid or excessive Ollama inventory")
                names = [identifier(model.get("name")) if isinstance(model, dict) else identifier(None) for model in models]
                if len(names) != len(set(names)):
                    raise DiscoveryError("Duplicate model identity in remote inventory")
                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def detail(name: str) -> ModelMetadata:
                    async with semaphore:
                        raw = await self._request(client, "POST", base + "/api/show", headers=headers, json={"model": name})
                    capabilities, info = raw.get("capabilities"), raw.get("model_info")
                    details = raw.get("details", {})
                    if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities) or not isinstance(info, dict) or not isinstance(details, dict):
                        raise DiscoveryError("Invalid Ollama model metadata")
                    types = {"completion": "chat", "vision": "image2text", "embedding": "embedding"}
                    model_types = list(dict.fromkeys(types[value] for value in capabilities if value in types))
                    if not model_types:
                        raise DiscoveryError("Unsupported Ollama model capabilities")
                    family = info.get("general.architecture", details.get("family", ""))
                    context = info.get(f"{family}.context_length", 8192)
                    if type(context) is not int or not 0 <= context <= 2147483647:
                        raise DiscoveryError("Invalid Ollama model context length")
                    features = [feature for value, feature in (("tools", "is_tools"), ("thinking", "thinking")) if value in capabilities]
                    return ModelMetadata(name, model_types, context, features, name)

                tasks = [asyncio.create_task(detail(name)) for name in names]
                try:
                    return await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await finish_owned_work(asyncio.gather(*tasks, return_exceptions=True))
        except TimeoutError:
            raise DiscoveryError("Model discovery timed out") from None


class FishAudio(Base):
    _FACTORY_NAME = "Fish Audio"

    async def get_model_list(self) -> list[ModelMetadata]:
        token = fish_access_token(self.api_key)
        base = endpoint(self.base_url or "https://api.fish.audio")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or parsed.hostname != "api.fish.audio" or parsed.port not in {None, 443} or parsed.path not in {"", "/tts", "/v1/tts"}:
            raise ValueError("Fish Audio discovery requires its official HTTPS origin")
        result: list[ModelMetadata] = []
        seen: set[str] = set()
        expected_total: int | None = None
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS), self._session() as client:
                for page_number in range(1, 12):
                    raw = await self._request(
                        client, "GET", "https://api.fish.audio/model", headers={"Authorization": "Bearer " + token}, params={"page_size": 10, "page_number": page_number, "self": "true"}
                    )
                    items, total = raw.get("items"), raw.get("total")
                    if not isinstance(items, list) or len(items) > 10 or type(total) is not int or total < 0 or total > MAX_MODELS:
                        raise DiscoveryError("Invalid or excessive Fish Audio inventory")
                    if expected_total is not None and total != expected_total:
                        raise DiscoveryError("Fish Audio inventory changed during pagination")
                    expected_total = total
                    for item in items:
                        if not isinstance(item, dict):
                            raise DiscoveryError("Invalid Fish Audio model metadata")
                        name = identifier(item.get("_id"))
                        if len(name) not in {24, 32} or any(char not in "0123456789abcdefABCDEF" for char in name) or name in seen:
                            raise DiscoveryError("Invalid or duplicate Fish Audio model identity")
                        title = item.get("title", name)
                        if not isinstance(title, str) or not title.strip() or len(title) > 512 or any(ord(char) < 32 for char in title):
                            raise DiscoveryError("Invalid Fish Audio model title")
                        seen.add(name)
                        result.append(ModelMetadata(name, ["tts"], 8192, [], title))
                    if len(result) == total:
                        return result
                    if not items or len(result) > total:
                        raise DiscoveryError("Inconsistent Fish Audio inventory pagination")
                raise DiscoveryError("Fish Audio inventory exceeds its page limit")
        except TimeoutError:
            raise DiscoveryError("Model discovery timed out") from None
