"""HTTP relay for upstreams that speak the same protocol as the caller.

The request body is forwarded with three changes: the public model name
becomes the upstream model name, the caller's credential is replaced by the
upstream credential, and streaming chat requests ask the upstream to report
usage. The response is passed through as received; it is never re-serialised.
One request makes one upstream call: no retries, no fallback.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse

from api.model_gateway import errors
from api.model_gateway.errors import GatewayError
from api.model_gateway.store import GatewayStore, RequestLogEntry, ResolvedModel
from api.model_gateway.usage import SSEUsageTap, usage_from_payload
from common.app_config import ModelGatewayConfig

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
# nginx's convention for "client closed the connection before the response ended".
CLIENT_CLOSED_STATUS = 499
_MAX_NAME_LENGTH = 128
_MAX_END_USER_LENGTH = 255
# Upstream error bodies are relayed, but a broken upstream must not flood memory.
_MAX_ERROR_BODY_BYTES = 1 << 20


def request_id_of(request: Request) -> str:
    """The server-generated id of this request, created on first use."""
    request_id = getattr(request.state, "request_id", None)
    if request_id is None:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
    return request_id


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _sse_error(error: GatewayError) -> bytes:
    return b"data: " + json.dumps(error.body()).encode("utf-8") + b"\n\n"


def _encode_body(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates in the input survive only as escapes.
        return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _upstream_error_code(raw: bytes) -> str | None:
    try:
        error = json.loads(raw).get("error")
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code)[:64] if isinstance(code, (str, int)) and not isinstance(code, bool) else None


class Relay:
    def __init__(self, client: httpx.AsyncClient, store: GatewayStore, settings: ModelGatewayConfig) -> None:
        self._client = client
        self._store = store
        self._settings = settings
        self._background: set[asyncio.Task[None]] = set()

    async def handle(self, request: Request, *, endpoint: str, model_type: str) -> Response:
        request_id = request_id_of(request)
        started_at, started = datetime.now(UTC), time.monotonic()
        # Unauthenticated requests are rejected here and leave no request log.
        caller = await self._store.authenticate(request.headers.get("authorization"))
        entry = RequestLogEntry(
            id=request_id,
            tenant_id=caller.tenant_id,
            key_id=caller.key_id,
            key_name=caller.key_name,
            tags=caller.tags,
            endpoint=endpoint,
            started_at=started_at,
            client_ip=request.client.host if request.client else None,
        )
        try:
            payload = await self._read_json(request)
            model_name = payload.get("model")
            if not isinstance(model_name, str) or not model_name.strip():
                raise errors.invalid_request("You must provide a model parameter.", param="model", code="missing_model")
            entry.model = model_name[:_MAX_NAME_LENGTH]
            entry.stream = payload.get("stream") is True
            end_user = payload.get("user")
            entry.end_user = end_user[:_MAX_END_USER_LENGTH] if isinstance(end_user, str) and end_user else None
            resolved = await self._store.resolve_model(caller, model_name, model_type)
            entry.gateway_model_id = resolved.gateway_model_id
            entry.upstream_model_id = resolved.upstream.model_id
            entry.upstream_model_name = resolved.upstream.model_name[:_MAX_END_USER_LENGTH]
            return await self._forward(request, payload, resolved, entry, started)
        except GatewayError as error:
            entry.status_code, entry.error_type, entry.error_code = error.status_code, error.error_type, error.code
            await self._finish(entry, started)
            raise

    async def drain(self) -> None:
        """Wait for request logs of streams that ended just before shutdown."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def _read_json(self, request: Request) -> dict[str, Any]:
        limit = self._settings.max_request_bytes
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise errors.request_too_large(limit)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > limit:
                raise errors.request_too_large(limit)
        try:
            payload = json.loads(bytes(body))
        except (ValueError, UnicodeDecodeError):
            raise errors.invalid_request("The request body is not valid JSON.", code="invalid_json") from None
        if not isinstance(payload, dict):
            raise errors.invalid_request("The request body must be a JSON object.", code="invalid_json")
        return payload

    async def _forward(self, request: Request, payload: dict[str, Any], resolved: ResolvedModel, entry: RequestLogEntry, started: float) -> Response:
        upstream = resolved.upstream
        body = dict(payload)
        body["model"] = upstream.model_name
        caller_asked_for_usage = True
        if resolved.model_type == "chat" and entry.stream:
            options = body.get("stream_options")
            options = dict(options) if isinstance(options, dict) else {}
            caller_asked_for_usage = options.get("include_usage") is True
            options["include_usage"] = True
            body["stream_options"] = options
        # Compression would make the relayed bytes depend on content-encoding handling.
        headers = {"content-type": "application/json", "accept": request.headers.get("accept") or "application/json", "accept-encoding": "identity", REQUEST_ID_HEADER: entry.id}
        authorization = upstream.authorization()
        if authorization:
            headers["authorization"] = authorization
        upstream_request = self._client.build_request("POST", upstream.endpoint_url(resolved.model_type), content=_encode_body(body), headers=headers)
        deadline = started + self._settings.total_timeout_seconds
        try:
            async with asyncio.timeout(self._settings.first_response_timeout_seconds):
                response = await self._client.send(upstream_request, stream=True)
        except (TimeoutError, httpx.TimeoutException):
            raise errors.upstream_timeout() from None
        except httpx.HTTPError:
            raise errors.upstream_unreachable() from None

        content_type = response.headers.get("content-type", "application/json")
        if response.status_code >= 400:
            return await self._relay_failure(response, content_type, entry, started)
        if "text/event-stream" in content_type:
            tap = SSEUsageTap(hide_usage_only_chunk=not caller_asked_for_usage)
            return StreamingResponse(
                self._stream(response, tap, entry, started, deadline),
                status_code=response.status_code,
                media_type=content_type,
                headers={REQUEST_ID_HEADER: entry.id, "cache-control": "no-cache", "x-accel-buffering": "no"},
            )
        try:
            async with asyncio.timeout(max(deadline - time.monotonic(), 0.001)):
                raw = await response.aread()
        except (TimeoutError, httpx.TimeoutException):
            raise errors.upstream_timeout() from None
        except httpx.HTTPError:
            raise errors.upstream_unreachable() from None
        finally:
            await response.aclose()
        try:
            entry.usage = usage_from_payload(json.loads(raw))
        except (ValueError, UnicodeDecodeError):
            pass
        entry.status_code = response.status_code
        await self._finish(entry, started)
        return Response(content=raw, status_code=response.status_code, media_type=content_type, headers={REQUEST_ID_HEADER: entry.id})

    async def _relay_failure(self, response: httpx.Response, content_type: str, entry: RequestLogEntry, started: float) -> Response:
        status = response.status_code
        try:
            raw = bytearray()
            if response.is_stream_consumed:
                # A transport that buffers the body hands it over already read.
                raw += response.content
            else:
                async for chunk in response.aiter_raw():
                    raw += chunk
                    if len(raw) > _MAX_ERROR_BODY_BYTES:
                        break
        except httpx.HTTPError:
            raise errors.upstream_unreachable() from None
        finally:
            await response.aclose()
        if status in (401, 403):
            raise errors.upstream_auth_error()
        if status >= 500 or len(raw) > _MAX_ERROR_BODY_BYTES:
            raise errors.upstream_error(status)
        # Other client errors are the upstream's verdict on the request itself.
        entry.status_code, entry.error_type, entry.error_code = status, "upstream_rejected", _upstream_error_code(bytes(raw))
        await self._finish(entry, started)
        headers = {REQUEST_ID_HEADER: entry.id}
        if "retry-after" in response.headers:
            headers["retry-after"] = response.headers["retry-after"]
        return Response(content=bytes(raw), status_code=status, media_type=content_type, headers=headers)

    async def _stream(self, response: httpx.Response, tap: SSEUsageTap, entry: RequestLogEntry, started: float, deadline: float) -> AsyncIterator[bytes]:
        # The status line is already sent, so failures are reported as a final
        # SSE error event. "client_closed" stands until the stream ends itself.
        outcome = "client_closed"
        try:
            async for chunk in response.aiter_raw():
                if time.monotonic() > deadline:
                    outcome = "upstream_timeout"
                    yield _sse_error(errors.upstream_timeout())
                    break
                forwarded = tap.feed(chunk)
                if forwarded:
                    if entry.first_token_ms is None:
                        entry.first_token_ms = _elapsed_ms(started)
                    yield forwarded
            else:
                rest = tap.flush()
                if rest:
                    yield rest
                outcome = "completed"
        except httpx.TimeoutException:
            outcome = "upstream_timeout"
            yield _sse_error(errors.upstream_timeout())
        except httpx.HTTPError:
            outcome = "upstream_stream_error"
            yield _sse_error(errors.upstream_unreachable())
        finally:
            # A disconnecting client cancels this generator; awaits here would be
            # cancelled with it, so cleanup and the log write run in their own task.
            self._spawn(self._finish_stream(response, tap, entry, started, outcome))

    async def _finish_stream(self, response: httpx.Response, tap: SSEUsageTap, entry: RequestLogEntry, started: float, outcome: str) -> None:
        try:
            await response.aclose()
        except httpx.HTTPError:
            logger.debug("model gateway: closing the upstream stream failed", exc_info=True)
        entry.usage = tap.usage
        entry.status_code = response.status_code
        if outcome == "client_closed":
            entry.status_code, entry.error_type, entry.error_code = CLIENT_CLOSED_STATUS, "client_closed", None
        elif outcome != "completed":
            entry.error_type, entry.error_code = "upstream_error", outcome
        await self._finish(entry, started)

    async def _finish(self, entry: RequestLogEntry, started: float) -> None:
        entry.duration_ms = _elapsed_ms(started)
        try:
            await self._store.record(entry)
        except Exception:
            # The response is already decided; losing a log row must not change it.
            logger.exception("model gateway: failed to write request log %s", entry.id)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
