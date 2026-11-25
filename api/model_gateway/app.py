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

from api.model_gateway import errors
from api.model_gateway.errors import GatewayError
from api.model_gateway.limits import Limiter, MemoryLimiter
from api.model_gateway.relay import REQUEST_ID_HEADER, Relay, request_id_of
from api.model_gateway.retention import LogCleaner, LogPruner
from api.model_gateway.store import GatewayStore, SqlGatewayStore
from common.app_config import ModelGatewayConfig, get_app_config

logger = logging.getLogger(__name__)

_MAX_UPSTREAM_CONNECTIONS = 1000
_MAX_KEEPALIVE_CONNECTIONS = 100


def _error_response(request: Request, error: GatewayError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.body(), headers={**error.headers, REQUEST_ID_HEADER: request_id_of(request)})


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
    gateway_limiter: Limiter = limiter or MemoryLimiter(usage_since=gateway_store.tokens_used_since)

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
            relay = Relay(client, gateway_store, settings, gateway_limiter)
            app.state.relay = relay
            cleanup: asyncio.Task[None] | None = None
            if settings.log_retention_days > 0 and isinstance(gateway_store, LogPruner):
                cleaner = LogCleaner(gateway_store, retention_days=settings.log_retention_days, interval_seconds=settings.log_cleanup_interval_seconds)
                cleanup = asyncio.create_task(cleaner.run_forever(), name="model-gateway-log-cleanup")
            try:
                yield
            finally:
                if cleanup is not None:
                    cleanup.cancel()
                    await asyncio.gather(cleanup, return_exceptions=True)
                await relay.drain()

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
        caller = await gateway_store.authenticate(request.headers.get("authorization"))
        models = await gateway_store.visible_models(caller)
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

    return app
