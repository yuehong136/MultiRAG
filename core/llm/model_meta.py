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
# Complete /models inventories are independent of API page and create sizes.
MAX_INVENTORY_MODELS = 2000
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


def access_token(key: str, *, openrouter: bool = False) -> str:
    """Resolve the known connection envelope before constructing a Bearer header."""
    if key.startswith(("{", "[", '"')):
        try:
            payload = json.loads(key)
        except ValueError:
            raise ValueError("Invalid discovery credential") from None
        allowed = {"api_key", "is_tools"} | ({"provider_order"} if openrouter else set())
        if not isinstance(payload, dict) or set(payload) - allowed or "api_key" not in payload:
            raise ValueError("Invalid discovery credential")
        if "is_tools" in payload and type(payload["is_tools"]) is not bool:
            raise ValueError("Invalid discovery credential")
        order = payload.get("provider_order", "")
        if not isinstance(order, str) and (not isinstance(order, list) or any(not isinstance(item, str) for item in order)):
            raise ValueError("Invalid discovery credential")
        key = payload["api_key"]
    if not isinstance(key, str) or any(not 33 <= ord(char) <= 126 for char in key):
        raise ValueError("Invalid discovery credential")
    return key


def authorization(key: str, base: str, *, openrouter: bool = False, loopback_only: bool = False) -> dict[str, str]:
    token = access_token(key, openrouter=openrouter)
    if token:
        parsed = urlsplit(base)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if not loopback and (loopback_only or parsed.scheme != "https"):
            raise ValueError("Authenticated discovery requires HTTPS or a literal loopback origin")
    return {"Authorization": "Bearer " + token} if token else {}


def context_length(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise DiscoveryError("Invalid model context length")
        return value
    return 8192


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DiscoveryError("Invalid model modalities or parameters")
    return value


def inventory(raw: dict[str, Any], key: str = "data", *, limit: int = MAX_MODELS) -> list[dict[str, Any]]:
    models = raw.get(key)
    if not isinstance(models, list) or len(models) > limit or any(not isinstance(item, dict) for item in models):
        raise DiscoveryError("Invalid or excessive model inventory")
    # These protocols return a complete inventory, never silently accept a page.
    if raw.get("has_more", False) is not False or raw.get("next") or raw.get("next_page"):
        raise DiscoveryError("Incomplete model inventory")
    total = raw.get("total", len(models))
    if type(total) is not int or total != len(models):
        raise DiscoveryError("Inconsistent model inventory total")
    names = [identifier(item.get("id" if key == "data" else "name")) for item in models]
    if len(names) != len(set(names)):
        raise DiscoveryError("Duplicate model identity in remote inventory")
    return models


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
        headers = authorization(self.api_key, base, loopback_only=True)
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


class OpenAIAPICompatible(Base):
    _FACTORY_NAME = "OpenAI-API-Compatible"
    _SUPPORTED = {"chat", "image2text", "embedding", "rerank"}

    @staticmethod
    def _infer_model_types(name: str) -> list[str]:
        name = name.lower()
        for hints, capability in (
            (("rerank",), "rerank"),
            (("embed", "bge"), "embedding"),
            (("asr", "stt", "transcrib", "whisper"), "speech2text"),
            (("tts", "text-to-speech"), "tts"),
        ):
            if any(hint in name for hint in hints):
                return [capability]
        vision = ("vl", "vision", "llava", "minicpm-v", "gpt-4o", "glm-4v", "qvq", "pixtral")
        return ["chat", "image2text"] if any(hint in name for hint in vision) else ["chat"]

    def _metadata(self, model: dict[str, Any]) -> ModelMetadata | None:
        name = identifier(model.get("id"))
        types = [value for value in self._infer_model_types(name) if value in self._SUPPORTED]
        context = context_length(*(model.get(key) for key in ("max_tokens", "max_completion_tokens", "context_length", "max_model_len")))
        return ModelMetadata(name, types, context, [], name) if types else None

    async def get_model_list(self) -> list[ModelMetadata]:
        base = endpoint(self.base_url)
        headers = authorization(self.api_key, base)
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS), self._session() as client:
                raw = await self._request(client, "GET", base + "/v1/models", headers=headers)
                return [metadata for model in inventory(raw, limit=MAX_INVENTORY_MODELS) if (metadata := self._metadata(model)) is not None]
        except TimeoutError:
            raise DiscoveryError("Model discovery timed out") from None


class VLLM(OpenAIAPICompatible):
    _FACTORY_NAME = "VLLM"


class LMStudio(OpenAIAPICompatible):
    _FACTORY_NAME = "LM-Studio"
    # The registered rerank class is an unimplemented placeholder.
    _SUPPORTED = {"chat", "image2text", "embedding"}


class Xinference(OpenAIAPICompatible):
    _FACTORY_NAME = "Xinference"

    def _metadata(self, model: dict[str, Any]) -> ModelMetadata | None:
        name = identifier(model.get("id"))
        kind = model.get("model_type", "LLM")
        if not isinstance(kind, str):
            raise DiscoveryError("Invalid Xinference model type")
        # Xinference image models generate images; vision belongs to LLM abilities.
        if kind not in {"LLM", "chat", "embedding", "rerank", "audio"}:
            return None
        abilities = model.get("model_ability")
        if abilities is not None and (not isinstance(abilities, list) or any(not isinstance(value, str) for value in abilities)):
            raise DiscoveryError("Invalid Xinference model abilities")
        types = {"embedding": ["embedding"], "rerank": ["rerank"]}.get(kind, [])
        if kind in {"LLM", "chat"}:
            types = ["chat"] if abilities is None or "chat" in abilities else []
            if abilities is not None and "vision" in abilities:
                types.append("image2text")
        if kind == "audio":
            types = [capability for ability, capability in (("text2audio", "tts"), ("audio2text", "speech2text")) if ability in (abilities or [])]
        if not types:
            return None
        abilities = abilities or []
        features = [feature for value, feature in (("tools", "is_tools"), ("reasoning", "thinking")) if value in abilities]
        context = context_length(model.get("context_length"), model.get("max_tokens"))
        return ModelMetadata(name, types, context, features, name) if types else None


class OpenRouter(OpenAIAPICompatible):
    _FACTORY_NAME = "OpenRouter"

    def _metadata(self, model: dict[str, Any]) -> ModelMetadata | None:
        name = identifier(model.get("id"))
        architecture, top = model.get("architecture"), model.get("top_provider", {})
        if not isinstance(architecture, dict) or not isinstance(top, dict):
            raise DiscoveryError("Invalid OpenRouter model metadata")
        inputs, outputs = _string_list(architecture.get("input_modalities")), _string_list(architecture.get("output_modalities"))
        parameters = _string_list(model.get("supported_parameters", []))
        # The active OpenRouter registry supports chat and vision only.
        types = (["chat"] + (["image2text"] if "image" in inputs else [])) if "text" in outputs else []
        features = (["is_tools"] if "tools" in parameters else []) + (["thinking"] if {"reasoning", "include_reasoning"} & set(parameters) else [])
        context = context_length(top.get("max_completion_tokens"), model.get("context_length"), top.get("context_length"))
        return ModelMetadata(name, types, context, features, name) if types else None

    async def get_model_list(self) -> list[ModelMetadata]:
        base = endpoint(self.base_url or "https://openrouter.ai/api/v1")
        parsed = urlsplit(base)
        if parsed.scheme != "https" or parsed.hostname != "openrouter.ai" or parsed.port not in {None, 443} or parsed.path not in {"", "/api"}:
            raise ValueError("OpenRouter discovery requires its official HTTPS origin")
        headers = authorization(self.api_key, base, openrouter=True)
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS), self._session() as client:
                raw = await self._request(client, "GET", "https://openrouter.ai/api/v1/models", headers=headers, params={"output_modalities": "all"})
                return [metadata for model in inventory(raw, limit=MAX_INVENTORY_MODELS) if (metadata := self._metadata(model)) is not None]
        except TimeoutError:
            raise DiscoveryError("Model discovery timed out") from None


class LocalAI(Ollama):
    _FACTORY_NAME = "LocalAI"

    async def get_model_list(self) -> list[ModelMetadata]:
        base = endpoint(self.base_url)
        headers = authorization(self.api_key, base)
        try:
            async with asyncio.timeout(DISCOVERY_SECONDS), self._session() as client:
                tags = inventory(await self._request(client, "GET", base + "/api/tags", headers=headers), "models")
                callable_models = inventory(await self._request(client, "GET", base + "/v1/models", headers=headers))
                callable_names = {model["id"] for model in callable_models}
                names: list[tuple[str, str]] = []
                for model in tags:
                    tagged = model["name"]
                    name = tagged if tagged in callable_names else tagged.removesuffix(":latest")
                    if name not in callable_names:
                        raise DiscoveryError("LocalAI tag has no callable model identity")
                    names.append((tagged, name))
                if len({name for _, name in names}) != len(names) or {name for _, name in names} != callable_names:
                    raise DiscoveryError("Ambiguous or incomplete LocalAI inventory")
                semaphore = asyncio.Semaphore(CONCURRENCY)

                async def detail(tagged: str, name: str) -> ModelMetadata:
                    async with semaphore:
                        raw = await self._request(client, "POST", base + "/api/show", headers=headers, json={"model": tagged})
                    capabilities, info = raw.get("capabilities"), raw.get("model_info")
                    if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities) or not isinstance(info, dict):
                        raise DiscoveryError("Invalid LocalAI model metadata")
                    types = {"completion": "chat", "vision": "image2text", "embedding": "embedding", "rerank": "rerank"}
                    model_types = list(dict.fromkeys(types[value] for value in capabilities if value in types))
                    if not model_types:
                        raise DiscoveryError("Unsupported LocalAI model capabilities")
                    features = [feature for value, feature in (("tools", "is_tools"), ("thinking", "thinking")) if value in capabilities]
                    return ModelMetadata(name, model_types, context_length(info.get("general.context_length")), features, name)

                tasks = [asyncio.create_task(detail(tagged, name)) for tagged, name in names]
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
