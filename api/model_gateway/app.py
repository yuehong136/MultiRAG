"""ASGI application of the model gateway data plane.

It serves ``/v1`` only and does not import ``api.apps``. Errors use the
OpenAI error envelope, and every response carries ``x-request-id``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.model_gateway import anthropic, errors
from api.model_gateway.errors import GatewayError
from api.model_gateway.health import HealthChecker, HealthStore
from api.model_gateway.limits import Limiter, MemoryLimiter
from api.model_gateway.price_refresh import refresher_of
from api.model_gateway.redis_state import RedisCooldowns, RedisLimiter, shared_redis
from api.model_gateway.relay import REQUEST_ID_HEADER, Relay, caller_credential, request_id_of
from api.model_gateway.retention import LogCleaner, LogPruner
from api.model_gateway.store import GatewayStore, SqlGatewayStore
from common.app_config import ModelGatewayConfig, get_app_config

logger = logging.getLogger(__name__)

_MAX_UPSTREAM_CONNECTIONS = 1000
_MAX_KEEPALIVE_CONNECTIONS = 100


def _speaks_anthropic(request: Request) -> bool:
    """Whether the caller expects Anthropic's shapes: its paths, or the header every Anthropic SDK sends."""
    return request.url.path.startswith("/v1/messages") or "anthropic-version" in request.headers


def _request_headers(request: Request) -> dict[str, str]:
    request_id = request_id_of(request)
    # ``request-id`` is the name Anthropic's SDKs read.
    return {REQUEST_ID_HEADER: request_id, "request-id": request_id} if _speaks_anthropic(request) else {REQUEST_ID_HEADER: request_id}


def _error_response(request: Request, error: GatewayError) -> JSONResponse:
    content = anthropic.gateway_error_body(error, request_id_of(request)) if _speaks_anthropic(request) else error.body()
    return JSONResponse(status_code=error.status_code, content=content, headers={**error.headers, **_request_headers(request)})


def create_app(
    *,
    store: GatewayStore | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    config: ModelGatewayConfig | None = None,
    limiter: Limiter | None = None,
) -> FastAPI:
    """Build the data plane. ``store``, ``transport`` and ``limiter`` are injectable for tests."""
    settings = config or get_app_config().model_gateway
    if store is None:
        from api.db.db_models import async_session_factory

        if async_session_factory is None:
            raise RuntimeError("The model gateway requires the PostgreSQL async engine")
        store = SqlGatewayStore(async_session_factory)
    gateway_store: GatewayStore = store
    redis = shared_redis(get_app_config().redis) if settings.state_store == "redis" else None
    cooldowns = RedisCooldowns(redis) if redis is not None else None
    gateway_limiter: Limiter
    if limiter is not None:
        gateway_limiter = limiter
    elif redis is not None:
        # A slot nobody gave back is free again once no call could still be running.
        gateway_limiter = RedisLimiter(redis, gateway_store.tokens_used_since, slot_ttl_seconds=settings.total_timeout_seconds + 60)
    else:
        gateway_limiter = MemoryLimiter(usage_since=gateway_store.tokens_used_since)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # read/write bound the gap between two pieces of data; the wait for the
        # response head and the whole call have their own limits in the relay.
        timeout = httpx.Timeout(
            connect=settings.connect_timeout_seconds,
            read=settings.stream_idle_timeout_seconds,
            write=settings.stream_idle_timeout_seconds,
            pool=settings.connect_timeout_seconds,
        )
        limits = httpx.Limits(max_connections=_MAX_UPSTREAM_CONNECTIONS, max_keepalive_connections=_MAX_KEEPALIVE_CONNECTIONS)
        async with httpx.AsyncClient(transport=transport, timeout=timeout, limits=limits, follow_redirects=False) as client:
            relay = Relay(client, gateway_store, settings, gateway_limiter, cooldowns=cooldowns)
            app.state.relay = relay
            cleanup: asyncio.Task[None] | None = None
            if settings.log_retention_days > 0 and isinstance(gateway_store, LogPruner):
                cleaner = LogCleaner(gateway_store, retention_days=settings.log_retention_days, interval_seconds=settings.log_cleanup_interval_seconds)
                cleanup = asyncio.create_task(cleaner.run_forever(), name="model-gateway-log-cleanup")
            checks: asyncio.Task[None] | None = None
            if settings.health_check_interval_seconds > 0 and isinstance(gateway_store, HealthStore):
                checker = HealthChecker(client, gateway_store, relay.router, interval_seconds=settings.health_check_interval_seconds, timeout_seconds=settings.health_check_timeout_seconds)
                checks = asyncio.create_task(checker.run_forever(), name="model-gateway-health-checks")
            # LiteLLM's adapter behind /v1/messages takes seconds to import; do it before the first request.
            warm = asyncio.create_task(anthropic.warm_up(), name="model-gateway-messages-adapter") if settings.messages_api_warm_up else None
            prices: asyncio.Task[None] | None = None
            refresher = refresher_of(settings)
            if refresher is not None:
                prices = asyncio.create_task(refresher.run_forever(), name="model-gateway-price-map")
            try:
                yield
            finally:
                for task in (cleanup, checks, prices, warm):
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                await relay.drain()
                if redis is not None:
                    await redis.aclose()

    app = FastAPI(title="MultiRAG Model Gateway", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(request: Request, error: GatewayError) -> JSONResponse:
        return _error_response(request, error)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, error: StarletteHTTPException) -> JSONResponse:
        if error.status_code == 404:
            return _error_response(request, errors.not_found())
        if error.status_code == 405:
            return _error_response(request, errors.method_not_allowed())
        return _error_response(request, GatewayError(error.status_code, str(error.detail), error_type="invalid_request_error", code="http_error"))

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, error: Exception) -> JSONResponse:
        logger.exception("model gateway: unhandled error for request %s", request_id_of(request))
        return _error_response(request, errors.internal_error())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request) -> JSONResponse:
        caller = await gateway_store.authenticate(caller_credential(request))
        models = await gateway_store.visible_models(caller)
        if _speaks_anthropic(request):
            # Claude Code and Anthropic's SDKs list models with their own page shape.
            return JSONResponse(content=anthropic.models_page([(model.name, model.created) for model in models]), headers=_request_headers(request))
        data = [{"id": model.name, "object": "model", "created": model.created, "owned_by": "multirag"} for model in models]
        return JSONResponse(content={"object": "list", "data": data}, headers={REQUEST_ID_HEADER: request_id_of(request)})

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        relay: Relay = request.app.state.relay
        return await relay.handle(request, endpoint="chat.completions", model_type="chat")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        relay: Relay = request.app.state.relay
        return await relay.handle(request, endpoint="embeddings", model_type="embedding")

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        """Anthropic's Messages API, served by the same chat models; errors use Anthropic's envelope."""
        relay: Relay = request.app.state.relay
        response = await relay.handle(request, endpoint="messages", model_type="chat", messages_api=True)
        response.headers["request-id"] = request_id_of(request)
        return response

    @app.post("/v1/messages/count_tokens")
    async def count_message_tokens(request: Request) -> JSONResponse:
        """Anthropic's token counting for a Messages request; nothing is sent upstream."""
        relay: Relay = request.app.state.relay
        return JSONResponse(content={"input_tokens": await relay.count_tokens(request)}, headers=_request_headers(request))

    return app
