"""What the data plane needs from persistence, and its SQL implementation.

The relay depends on the ``GatewayStore`` protocol only, so its behaviour is
testable without a database. Sessions here are short: none is held while an
upstream call or a stream is in progress.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import GatewayKey, GatewayModel, GatewayRequestLog, Tenant
from api.model_gateway import errors
from api.model_gateway.catalog import ModelUnavailable, UpstreamTarget, resolve_upstream
from api.model_gateway.keys import parse_key_id, verify_key
from api.model_gateway.usage import UNAVAILABLE, Usage

# last_used_at is a hint for operators, not an audit trail: refresh it rarely.
_LAST_USED_REFRESH = timedelta(minutes=5)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class Caller:
    """The authenticated key. Identity is ``key_id``; name and tags are metadata."""

    key_id: str
    tenant_id: str
    key_name: str
    allow_all_models: bool
    allowed_model_ids: frozenset[str]
    tags: dict[str, Any] | None = None

    def may_use(self, gateway_model_id: str) -> bool:
        return self.allow_all_models or gateway_model_id in self.allowed_model_ids


@dataclass(frozen=True, slots=True)
class PublicModel:
    id: str
    name: str
    model_type: str
    created: int


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    gateway_model_id: str
    name: str
    model_type: str
    upstream: UpstreamTarget


@dataclass(slots=True)
class RequestLogEntry:
    """Facts about one call, filled in as the request progresses."""

    id: str
    tenant_id: str
    key_id: str
    key_name: str
    endpoint: str
    started_at: datetime
    tags: dict[str, Any] | None = None
    model: str | None = None
    gateway_model_id: str | None = None
    upstream_model_id: int | None = None
    upstream_model_name: str | None = None
    stream: bool = False
    status_code: int = 500
    error_type: str | None = None
    error_code: str | None = None
    usage: Usage = field(default=UNAVAILABLE)
    first_token_ms: int | None = None
    duration_ms: int = 0
    end_user: str | None = None
    client_ip: str | None = None


@runtime_checkable
class GatewayStore(Protocol):
    async def authenticate(self, authorization: str | None) -> Caller: ...

    async def visible_models(self, caller: Caller) -> list[PublicModel]: ...

    async def resolve_model(self, caller: Caller, name: str, model_type: str) -> ResolvedModel: ...

    async def record(self, entry: RequestLogEntry) -> None: ...


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class SqlGatewayStore:
    """``GatewayStore`` backed by the application's async session factory."""

    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def authenticate(self, authorization: str | None) -> Caller:
        token = bearer_token(authorization)
        key_id = parse_key_id(token) if token else None
        if token is None or key_id is None:
            raise errors.invalid_api_key()
        now = datetime.now(UTC)
        async with self._sessions() as db:
            row = (await db.execute(select(GatewayKey, Tenant.status).join(Tenant, Tenant.id == GatewayKey.tenant_id).where(GatewayKey.id == key_id))).first()
            if row is None or not verify_key(token, row[0].secret_hash):
                raise errors.invalid_api_key()
            key, tenant_status = row[0], row[1]
            # The secret is proven from here on, so the reason may be specific.
            if key.status != "active" or tenant_status != "1":
                raise errors.api_key_disabled()
            if key.expires_at is not None and key.expires_at <= now:
                raise errors.api_key_expired()
            if key.last_used_at is None or now - key.last_used_at >= _LAST_USED_REFRESH:
                await db.execute(update(GatewayKey).where(GatewayKey.id == key.id).values(last_used_at=now))
                await db.commit()
            return Caller(
                key_id=key.id,
                tenant_id=key.tenant_id,
                key_name=key.name,
                allow_all_models=bool(key.allow_all_models),
                allowed_model_ids=frozenset(str(item) for item in key.allowed_model_ids or []),
                tags=dict(key.tags) if key.tags else None,
            )

    async def visible_models(self, caller: Caller) -> list[PublicModel]:
        async with self._sessions() as db:
            rows = (await db.scalars(select(GatewayModel).where(GatewayModel.tenant_id == caller.tenant_id, GatewayModel.status == "active").order_by(GatewayModel.name))).all()
        return [PublicModel(id=row.id, name=row.name, model_type=row.model_type, created=int((row.create_time or 0) // 1000)) for row in rows if caller.may_use(row.id)]

    async def resolve_model(self, caller: Caller, name: str, model_type: str) -> ResolvedModel:
        async with self._sessions() as db:
            row = await db.scalar(select(GatewayModel).where(GatewayModel.tenant_id == caller.tenant_id, GatewayModel.name == name))
            if row is None or row.status != "active":
                raise errors.model_not_found(name)
            if not caller.may_use(row.id):
                raise errors.model_not_allowed(name)
            if row.model_type != model_type:
                raise errors.invalid_request(f"The model '{name}' is not available on this endpoint.", param="model", code="model_endpoint_mismatch")
            try:
                upstream = await resolve_upstream(db, caller.tenant_id, row.model_id, row.model_type)
            except ModelUnavailable:
                raise errors.model_unavailable(name) from None
            return ResolvedModel(gateway_model_id=row.id, name=row.name, model_type=row.model_type, upstream=upstream)

    async def record(self, entry: RequestLogEntry) -> None:
        async with self._sessions() as db:
            db.add(
                GatewayRequestLog(
                    id=entry.id,
                    tenant_id=entry.tenant_id,
                    key_id=entry.key_id,
                    key_name=entry.key_name,
                    model=entry.model,
                    gateway_model_id=entry.gateway_model_id,
                    upstream_model_id=entry.upstream_model_id,
                    upstream_model_name=entry.upstream_model_name,
                    endpoint=entry.endpoint,
                    stream=entry.stream,
                    status_code=entry.status_code,
                    error_type=entry.error_type,
                    error_code=entry.error_code,
                    prompt_tokens=entry.usage.prompt_tokens,
                    completion_tokens=entry.usage.completion_tokens,
                    total_tokens=entry.usage.total_tokens,
                    usage_details=entry.usage.details,
                    usage_source=entry.usage.source,
                    started_at=entry.started_at,
                    first_token_ms=entry.first_token_ms,
                    duration_ms=entry.duration_ms,
                    end_user=entry.end_user,
                    client_ip=entry.client_ip,
                    tags=entry.tags,
                )
            )
            await db.commit()
