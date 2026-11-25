"""Shared helpers for exercising the model gateway against an owned database.

The management routes and the data plane each run in their own in-process
application, wired to the given engine. The caller side is the real OpenAI
SDK; only the upstream transport differs between controlled and live runs.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx
import openai
import sqlalchemy as sa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import GatewayRequestLog, TenantModelInstance, TenantModelProvider, get_async_db
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.model_gateway.app import create_app
from api.model_gateway.limits import MemoryLimiter
from api.model_gateway.store import SqlGatewayStore
from common.app_config import ModelGatewayConfig
from tests.support.registered_model import register_model as register_tenant_model

MANAGEMENT_PREFIX = "/api/v1/model-gateway"
# psycopg's async driver needs the selector loop on Windows.
_BACKEND = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


def manager(owner: str) -> Any:
    """An interactively logged-in owner, as the management routes require."""
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=owner, display_name="Gateway operator"),
        membership=TenantMembershipEvidence(platform_user_id=owner, tenant_id=owner),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )


def register_model(db: Session, owner: str, factory: str, model_name: str, model_type: str, base_url: str, api_key: str, *, instance_name: str = "default") -> str:
    return register_tenant_model(db, tenant_id=owner, provider_name=factory, model_name=model_name, model_types=(model_type,), base_url=base_url, api_key=api_key, instance_name=instance_name).id


@contextmanager
def management(engine: Engine, owner: str) -> Iterator[TestClient]:
    """The management routes on a small application, with the gateway enabled."""
    from api.apps.restful_apis import model_gateway_api
    from api.utils.api_utils import async_current_user

    async_engine = create_async_engine(engine.url)
    factory = async_sessionmaker(async_engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(model_gateway_api.router, prefix="/api/v1")
    app.dependency_overrides[get_async_db] = session
    app.dependency_overrides[async_current_user] = lambda: manager(owner)
    app.dependency_overrides[model_gateway_api.require_enabled] = lambda: None
    # Pooled connections are created and disposed on this client's portal loop.
    with TestClient(app, backend_options=_BACKEND) as client:
        try:
            yield client
        finally:
            assert client.portal is not None
            client.portal.call(async_engine.dispose)


@contextmanager
def data_plane(engine: Engine, transport: httpx.AsyncBaseTransport | None, *, clock: Callable[[], datetime] | None = None, **settings: Any) -> Iterator[TestClient]:
    """The data plane with SQL persistence. ``transport=None`` reaches real upstreams.

    ``clock`` pins the limiter's time, so limit windows do not roll during a test.
    """
    async_engine = create_async_engine(engine.url)
    store = SqlGatewayStore(async_sessionmaker(async_engine, expire_on_commit=False))
    limiter = MemoryLimiter(usage_since=store.tokens_used_since, clock=clock) if clock is not None else None
    app = create_app(store=store, transport=transport, config=ModelGatewayConfig(**{"enabled": True, **QUIET, **settings}), limiter=limiter)
    with TestClient(app, backend_options=_BACKEND) as client:
        try:
            yield client
        finally:
            assert client.portal is not None
            client.portal.call(async_engine.dispose)


class _InProcess(httpx.BaseTransport):
    """Carry the SDK's httpx requests into the gateway application.

    The SDK only accepts an ``httpx.Client``; the test client is built on a
    different HTTP library, so requests are handed over one by one.
    """

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        headers = {name: value for name, value in request.headers.items() if name.lower() not in {"host", "content-length"}}
        response = self._client.request(request.method, str(request.url), headers=headers, content=request.read())
        relayed = [(name, value) for name, value in response.headers.items() if name.lower() not in {"content-length", "content-encoding", "transfer-encoding"}]
        return httpx.Response(response.status_code, headers=relayed, content=response.content, request=request)


def sdk(client: TestClient, key: str) -> openai.OpenAI:
    """The real OpenAI SDK pointed at the in-process gateway, without SDK retries."""
    return openai.OpenAI(api_key=key, base_url="http://testserver/v1", http_client=httpx.Client(transport=_InProcess(client)), max_retries=0)


# What tests switch off unless they ask for it: Redis state, background health checks, fetching prices
# from the network, and importing LiteLLM's adapter at start.
QUIET: dict[str, Any] = {"state_store": "memory", "health_check_interval_seconds": 0.0, "price_map_refresh_hours": 0.0, "messages_api_warm_up": False}


def anthropic_sdk(client: TestClient, key: str) -> anthropic.Anthropic:
    """The real Anthropic SDK pointed at the in-process gateway, without SDK retries."""
    return anthropic.Anthropic(api_key=key, base_url="http://testserver", http_client=httpx.Client(transport=_InProcess(client)), max_retries=0)


def logs(engine: Engine) -> list[GatewayRequestLog]:
    """Request logs read back on an independent connection, oldest first."""
    with Session(engine) as reader:
        return list(reader.scalars(sa.select(GatewayRequestLog).order_by(GatewayRequestLog.started_at, GatewayRequestLog.create_time)).all())


def instance_id(db: Session, owner: str, factory: str, instance_name: str = "default") -> str:
    """The id of one of the owner's provider instances (connections)."""
    return db.scalars(
        sa.select(TenantModelInstance.id)
        .join(TenantModelProvider, TenantModelInstance.provider_id == TenantModelProvider.id)
        .where(TenantModelProvider.tenant_id == owner, TenantModelProvider.provider_name == factory, TenantModelInstance.instance_name == instance_name)
    ).one()
