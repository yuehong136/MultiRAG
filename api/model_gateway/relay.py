"""HTTP relay for upstreams that speak the same protocol as the caller.

The request body is forwarded with three changes: the public model name
becomes the upstream model name, the caller's credential is replaced by the
upstream credential, and streaming chat requests ask the upstream to report
usage. The response is passed through as received; it is never re-serialised.

A public name may have several deployments. The router orders them; a failed
call moves on to the next one while nothing has been sent to the caller, up
to ``num_retries`` times. The answer of the last deployment tried is the
answer of the request, relayed exactly as a single deployment's would be.
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
from api.model_gateway.limits import Limiter, Subject
from api.model_gateway.router import STATUS_TIMEOUT, STATUS_UNREACHABLE, Router, is_retryable
from api.model_gateway.store import Attempt, Caller, GatewayStore, RequestLogEntry, ResolvedModel
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


def _key_subject(caller: Caller) -> Subject | None:
    if caller.limits or caller.quota is not None:
        return Subject(kind="key", id=caller.key_id, label="this API key", limits=caller.limits, quota=caller.quota)
    return None


def _model_subject(resolved: ResolvedModel) -> Subject | None:
    if resolved.limits:
        return Subject(kind="model", id=str(resolved.model_id), label=f"the model '{resolved.name}'", limits=resolved.limits)
    return None


def _attempt(resolved: ResolvedModel, status_code: int, error_code: str | None, since: float) -> Attempt:
    upstream = resolved.upstream
    return Attempt(
        model_id=upstream.model_id,
        model_name=upstream.model_name[:_MAX_END_USER_LENGTH],
        provider=upstream.factory,
        instance_name=resolved.instance_name,
        status_code=status_code,
        error_code=error_code,
        duration_ms=_elapsed_ms(since),
    )


class Relay:
    def __init__(self, client: httpx.AsyncClient, store: GatewayStore, settings: ModelGatewayConfig, limiter: Limiter, router: Router | None = None) -> None:
        self._router = router or Router(cooldown_seconds=settings.cooldown_time_seconds)
        self._client = client
        self._store = store
        self._settings = settings
        self._limiter = limiter
        self._background: set[asyncio.Task[None]] = set()
        # Requests that passed admission and still hold slots, by request id.
        self._admitted: dict[str, list[Subject]] = {}

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
            deployments = await self._store.resolve_deployments(caller, model_name, model_type)
            # A call refused before any deployment was tried is logged under the first one.
            entry.upstream_model_id = deployments[0].upstream.model_id
            entry.upstream_model_name = deployments[0].upstream.model_name[:_MAX_END_USER_LENGTH]
            key_subject = _key_subject(caller)
            if key_subject is not None:
                await self._limiter.admit(request_id, [key_subject])
                self._admitted[request_id] = [key_subject]
            return await self._route(request, payload, deployments, entry, started)
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

    async def _route(self, request: Request, payload: dict[str, Any], deployments: list[ResolvedModel], entry: RequestLogEntry, started: float) -> Response:
        """Try the deployments in the router's order until one answers or none may be tried."""
        plan = self._router.plan(deployments)
        deadline = started + self._settings.total_timeout_seconds
        calls_left = 1 + self._settings.num_retries
        refusal: GatewayError | None = None
        for index, resolved in enumerate(plan):
            attempt_started = time.monotonic()
            entry.upstream_model_id = resolved.upstream.model_id
            entry.upstream_model_name = resolved.upstream.model_name[:_MAX_END_USER_LENGTH]
            entry.price = resolved.price
            subject = _model_subject(resolved)
            if subject is not None:
                try:
                    await self._limiter.admit(entry.id, [subject])
                except GatewayError as error:
                    # A deployment at its own limit is passed over without an upstream call.
                    entry.attempts.append(_attempt(resolved, error.status_code, error.code, attempt_started))
                    refusal = error
                    continue
                self._admitted.setdefault(entry.id, []).append(subject)
            calls_left -= 1
            may_retry = calls_left > 0 and index < len(plan) - 1 and time.monotonic() < deadline
            try:
                response, caller_asked_for_usage = await self._send(request, payload, resolved, entry)
            except GatewayError as error:
                status = STATUS_TIMEOUT if error.code == "upstream_timeout" else STATUS_UNREACHABLE
                self._router.failed(resolved.model_id, status, deployments=len(plan))
                entry.attempts.append(_attempt(resolved, error.status_code, error.code, attempt_started))
                if not may_retry:
                    raise
                await self._release(entry.id, subject)
                continue
            if response.status_code >= 400:
                self._router.failed(resolved.model_id, response.status_code, deployments=len(plan))
                entry.attempts.append(_attempt(resolved, response.status_code, None, attempt_started))
                if may_retry and is_retryable(response.status_code):
                    await response.aclose()
                    await self._release(entry.id, subject)
                    continue
                return await self._relay_failure(response, response.headers.get("content-type", "application/json"), entry, started)
            self._router.succeeded(resolved.model_id)
            entry.attempts.append(_attempt(resolved, response.status_code, None, attempt_started))
            return await self._deliver(response, caller_asked_for_usage, entry, started, deadline)
        # Only deployments at their limit were left.
        assert refusal is not None
        raise refusal

    async def _release(self, request_id: str, subject: Subject | None) -> None:
        """Give back the slot of a deployment the request moved on from."""
        if subject is None:
            return
        admitted = self._admitted.get(request_id)
        if admitted is not None and subject in admitted:
            admitted.remove(subject)
        await self._limiter.settle(request_id, [subject], None)

    async def _send(self, request: Request, payload: dict[str, Any], resolved: ResolvedModel, entry: RequestLogEntry) -> tuple[httpx.Response, bool]:
        """Call one deployment and return its response once the status line is in."""
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
        try:
            async with asyncio.timeout(self._settings.first_response_timeout_seconds):
                return await self._client.send(upstream_request, stream=True), caller_asked_for_usage
        except (TimeoutError, httpx.TimeoutException):
            raise errors.upstream_timeout() from None
        except httpx.HTTPError:
            raise errors.upstream_unreachable() from None

    async def _deliver(self, response: httpx.Response, caller_asked_for_usage: bool, entry: RequestLogEntry, started: float, deadline: float) -> Response:
        content_type = response.headers.get("content-type", "application/json")
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
                if tap.finished:
                    # The terminator is delivered. SDKs hang up on it at once and
                    # some upstreams keep the connection open, so stop here.
                    outcome = "completed"
                    break
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
            # A caller that hangs up right after the terminator received everything.
            if outcome == "client_closed" and tap.finished:
                outcome = "completed"
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
        subjects = self._admitted.pop(entry.id, None)
        if subjects:
            # Settle before the log write so a slow database does not hold a slot.
            try:
                await self._limiter.settle(entry.id, subjects, entry.usage.total_tokens)
            except Exception:
                logger.exception("model gateway: failed to settle limits for request %s", entry.id)
        try:
            await self._store.record(entry)
        except Exception:
            # The response is already decided; losing a log row must not change it.
            logger.exception("model gateway: failed to write request log %s", entry.id)

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
