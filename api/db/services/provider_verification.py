"""Bounded, no-write probes of one declared model using the active adapters."""

import asyncio
import importlib
import inspect
import io
import math
import os
import sys
import tempfile
import wave
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from typing import Any
from uuid import uuid4

from api.db.model_contract import ModelRegistration
from common.asyncio_utils import finish_owned_work

_REGISTRIES = {"chat": "ChatModel", "embedding": "EmbeddingModel", "rerank": "RerankModel", "image2text": "CvModel", "tts": "TTSModel", "ocr": "OcrModel", "speech2text": "Seq2txtModel"}


async def _blocking(call: Callable[[], Any]) -> Any:
    # An executor cancellation cannot stop a synchronous SDK. Drain before
    # releasing its client; SDK/request transport timeouts remain authoritative.
    task = asyncio.create_task(asyncio.to_thread(call))
    try:
        return await asyncio.shield(task)
    except BaseException:
        try:
            await finish_owned_work(task)
        except BaseException:
            pass
        raise


async def _close(model: Any) -> None:
    close = getattr(model, "aclose", None) or getattr(model, "close", None)
    targets = [model] if callable(close) else [getattr(model, "client", None), getattr(model, "async_client", None)]
    targets.extend(getattr(model, "page_images", []))
    seen: set[int] = set()
    errors: list[Exception] = []
    for target in targets:
        if target is None or id(target) in seen:
            continue
        seen.add(id(target))
        close = getattr(target, "aclose", None) or getattr(target, "close", None)
        if callable(close):
            try:
                if inspect.iscoroutinefunction(close):
                    await close()
                else:
                    result = await _blocking(close)
                    if inspect.isawaitable(result):
                        await result
            except Exception as error:
                errors.append(error)
    if errors:
        raise ExceptionGroup("Failed to close probe resources", errors)


def _valid_text(answer: Any) -> bool:
    return isinstance(answer, str) and bool(answer.strip()) and "**ERROR**" not in answer


def _valid_vector(values: Any, *, matrix: bool) -> bool:
    if hasattr(values, "tolist"):
        values = values.tolist()
    if matrix:
        if not isinstance(values, list) or len(values) != 1:
            return False
        values = values[0]
    return isinstance(values, list) and bool(values) and all(type(value) in {float, int} and math.isfinite(value) for value in values)


def _own_chat_transport(model: Any, seconds: float) -> None:
    """Bypass LiteLLM's global client cache for this short-lived probe."""
    from core.llm.chat import LiteLLMBase

    if not isinstance(model, LiteLLMBase) or model.provider == "MiniMax":
        # MiniMax already owns its explicit SDK client inside _completion.
        return
    import httpx
    from openai import AsyncAzureOpenAI, AsyncOpenAI

    if model.prefix == "azure/":
        client: Any = AsyncAzureOpenAI(api_key=model.api_key, azure_endpoint=model.base_url, api_version=model.api_version, timeout=seconds, max_retries=0)
    elif model.prefix in {"openai/", "together_ai/", "nvidia_nim/", "deepinfra/"}:
        client = AsyncOpenAI(api_key=model.api_key or "empty", base_url=model.base_url or None, timeout=seconds, max_retries=0)
    else:
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        class OwnedHTTPHandler(AsyncHTTPHandler):
            def create_client(self, timeout: Any, event_hooks: Any, ssl_verify: Any = None, shared_session: Any = None) -> httpx.AsyncClient:
                return httpx.AsyncClient(timeout=timeout, event_hooks=event_hooks, follow_redirects=False)

        client = OwnedHTTPHandler(timeout=seconds)
    model.async_client = client
    original = model._completion

    @asynccontextmanager
    async def completion(options: dict[str, Any]) -> Any:
        async with original({**options, "client": client}) as response:
            yield response

    model._completion = completion


async def verify_model(provider: str, key: str, base_url: str, declaration: ModelRegistration) -> dict[str, Any]:
    capability = declaration.model_types[0]
    if capability not in _REGISTRIES:
        raise ValueError("Connection verification is unavailable for this model capability")
    model: Any = None
    try:
        seconds = float(os.environ.get("LLM_PROBE_TIMEOUT_SECONDS", "25"))
        if not math.isfinite(seconds) or not 0 < seconds <= 120:
            raise ValueError("Invalid probe timeout")
        async with asyncio.timeout(seconds):
            registry = getattr(importlib.import_module("core.llm"), _REGISTRIES[capability])
            cls = registry.get(provider)
            if cls is None:
                raise ValueError("Unsupported provider capability")
            # Keep a partially initialized adapter reachable for owned cleanup.
            model = cls.__new__(cls)
            options = {"key": key, "model_name": declaration.model_name, "base_url": base_url, "provider": provider, "max_retries": 0}
            signature = inspect.signature(cls.__init__)
            if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()):
                options = {name: value for name, value in options.items() if name in signature.parameters}
            await _blocking(lambda: cls.__init__(model, **options))

            async def safe_async_failure(_error: BaseException, _attempt: int) -> str:
                return "**ERROR** Provider connection failed"

            def safe_failure(_error: BaseException, _attempt: int) -> str:
                return "**ERROR** Provider connection failed"

            # Adapters normally log diagnostic exception bodies. Probe failures
            # never need those bodies; overrides belong only to this owned object.
            if hasattr(model, "_exceptions_async"):
                model._exceptions_async = safe_async_failure
            if hasattr(model, "_exceptions"):
                model._exceptions = safe_failure
            model.max_retries = 0
            if hasattr(model, "timeout"):
                model.timeout = seconds
            for name in ("client", "async_client"):
                client = getattr(model, name, None)
                if hasattr(client, "timeout"):
                    client.timeout = seconds
                if hasattr(client, "max_retries"):
                    client.max_retries = 0
            if capability == "chat":
                _own_chat_transport(model, seconds)
                answer, _usage = await model.async_chat("", [{"role": "user", "content": "Reply OK"}], {"max_tokens": 16})
                valid = _valid_text(answer)
            elif capability == "image2text":
                from core.utils.base64_image import test_image

                answer, _usage = await model.async_chat("", [{"role": "user", "content": "Describe this image briefly"}], {}, images=[test_image])
                valid = _valid_text(answer)
            elif capability == "embedding":
                vectors, _usage = await _blocking(lambda: model.encode(["Connection test"]))
                valid = _valid_vector(vectors, matrix=True)
            elif capability == "rerank":
                scores, _usage = await _blocking(lambda: model.similarity("Connection test", ["Connection test"]))
                valid = _valid_vector(scores, matrix=False) and len(scores) == 1
            elif capability == "ocr":

                def recognize() -> bool:
                    from PIL import Image, ImageDraw

                    data = io.BytesIO()
                    with Image.new("RGB", (240, 80), "white") as image:
                        ImageDraw.Draw(image).text((20, 30), "Connection test", fill="black")
                        image.save(data, format="PDF")
                    binary = data.getvalue()
                    # Configuration-only check_available is not a connection test.
                    result = model.parse_pdf("connection.pdf", binary=binary, request_timeout=max(1, int(seconds)))
                    return bool(result and result[0])

                valid = await _blocking(recognize)
            elif capability == "speech2text":

                def transcribe() -> bool:
                    data = io.BytesIO()
                    with wave.open(data, "wb") as audio:
                        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        audio.writeframes(b"\0\0" * 16000)
                    path = os.path.join(tempfile.gettempdir(), f"multirag-provider-probe-{uuid4().hex}.wav")
                    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            output.write(data.getvalue())
                        answer, _usage = model.transcription(path)
                        # Silence may correctly transcribe to an empty string.
                        return isinstance(answer, str) and "**ERROR**" not in answer
                    finally:
                        os.unlink(path)

                valid = await _blocking(transcribe)
            else:

                def synthesize() -> bool:
                    with closing(model.tts("Connection test")) as stream:
                        return any(isinstance(chunk, bytes) and bool(chunk) for chunk in stream)

                valid = await _blocking(synthesize)
            if not valid:
                raise ValueError("Invalid provider response")
        return {"verified": True, "model_name": declaration.model_name, "model_type": capability}
    except TimeoutError:
        raise ValueError("Provider connection verification timed out") from None
    except Exception:
        # Constructor, transport and provider diagnostics may all contain keys,
        # request URLs and payloads. Do not echo them or persist usage.
        raise ValueError("Provider connection verification failed") from None
    finally:
        body_failed = sys.exc_info()[0] is not None
        if model is not None:
            try:
                await finish_owned_work(_close(model))
            except asyncio.CancelledError:
                raise
            except Exception:
                # Cleanup failure must not turn a failed probe into a success.
                if not body_failed:
                    raise ValueError("Provider connection cleanup failed") from None
