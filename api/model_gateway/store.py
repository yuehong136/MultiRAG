"""What the data plane needs from persistence, and its SQL implementation.

The relay depends on the ``GatewayStore`` protocol only, so its behaviour is
testable without a database. Sessions here are short: none is held while an
upstream call or a stream is in progress.
"""

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import GatewayDailyUsage, GatewayImportedCredential, GatewayKey, GatewayModelHealth, GatewayRequestLog, Tenant
from api.model_gateway import errors
from api.model_gateway.catalog import ModelUnavailable, UpstreamTarget, resolve_upstream
from api.model_gateway.directory import CALLABLE, load_directory
from api.model_gateway.health import HealthResult, HealthTarget
from api.model_gateway.keys import import_lookup_digest, parse_key_id, verify_key
from api.model_gateway.limits import NO_LIMITS, Limits, Quota
from api.model_gateway.pricing import ModelPrice, resolve_price
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
    allowed_model_ids: frozenset[str] = frozenset()
    allowed_instance_ids: frozenset[str] = frozenset()
    tags: dict[str, Any] | None = None
    limits: Limits = NO_LIMITS
    quota: Quota | None = None

    def may_use(self, model_id: str, instance_id: str) -> bool:
        return self.allow_all_models or model_id in self.allowed_model_ids or instance_id in self.allowed_instance_ids


@dataclass(frozen=True, slots=True)
class PublicModel:
    name: str
    created: int


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A registered model a request may call; ``name`` is its call name."""

    model_id: str
    name: str
    model_type: str
    upstream: UpstreamTarget
    limits: Limits = NO_LIMITS
    # What a token of this model costs; ``None`` when nobody priced it.
    price: ModelPrice | None = None
    # Routing among the deployments of the requested name.
    weight: int = 1
    order: int | None = None
    # The connection it is registered under; tells deployments of one provider apart.
    instance_name: str = ""


@dataclass(frozen=True, slots=True)
class Attempt:
    """One upstream call of a request, or a deployment its own limit kept it from."""

    model_id: str
    model_name: str
    provider: str
    instance_name: str
    status_code: int
    error_code: str | None
    duration_ms: int

    def view(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "model_name": self.model_name,
            "provider": self.provider,
            "instance_name": self.instance_name,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "duration_ms": self.duration_ms,
        }


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
    upstream_model_id: str | None = None
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
    # The price in force for this call; not stored, only the resulting cost is.
    price: ModelPrice | None = None
    # Deployments tried before the one that answered, then that one.
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.error_type is not None or self.status_code >= 400

    @property
    def cost(self) -> Decimal | None:
        """USD for this call; ``None`` without a price or without usage."""
        if self.price is None:
            return None
        return self.price.cost(self.usage.prompt_tokens, self.usage.completion_tokens, self.usage.cache_read_tokens)


def daily_usage_upsert(entry: RequestLogEntry) -> Any:
    """Add one call to its UTC day's row; the row is created by the first call of the bucket."""
    usage, cost = entry.usage, entry.cost
    added = {
        "prompt_tokens": usage.prompt_tokens or 0,
        "completion_tokens": usage.completion_tokens or 0,
        "cache_read_input_tokens": usage.cache_read_tokens or 0,
        "total_tokens": usage.total_tokens or 0,
        "spend": cost if cost is not None else Decimal(0),
        "api_requests": 1,
        "successful_requests": 0 if entry.failed else 1,
        "failed_requests": 1 if entry.failed else 0,
        "usage_unknown_requests": 1 if usage.source == "unavailable" else 0,
        "total_response_time_ms": entry.duration_ms,
    }
    statement = insert(GatewayDailyUsage).values(
        id=uuid.uuid4().hex,
        tenant_id=entry.tenant_id,
        date=entry.started_at.astimezone(UTC).date(),
        key_id=entry.key_id,
        key_name=entry.key_name,
        model_id=entry.upstream_model_id or "",
        model_name=entry.upstream_model_name,
        endpoint=entry.endpoint,
        **added,
    )
    table = GatewayDailyUsage.__table__.c
    return statement.on_conflict_do_update(
        constraint="uq_gateway_daily_usage_bucket",
        set_={
            **{name: table[name] + statement.excluded[name] for name in added},
            # Names are the latest snapshot; a refused call has no model name to offer.
            "key_name": statement.excluded.key_name,
            "model_name": func.coalesce(statement.excluded.model_name, table.model_name),
            "update_date": statement.excluded.update_date,
            "update_time": statement.excluded.update_time,
        },
    )


@runtime_checkable
class GatewayStore(Protocol):
    async def authenticate(self, authorization: str | None) -> Caller: ...

    async def visible_models(self, caller: Caller) -> list[PublicModel]: ...

    async def resolve_deployments(self, caller: Caller, name: str, model_type: str) -> list[ResolvedModel]:
        """The models that may serve this call to ``name``; never empty."""
        ...

    async def record(self, entry: RequestLogEntry) -> None: ...

    async def tokens_used_since(self, key_id: str, since: datetime) -> int: ...


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
        presented = token or authorization
        if not presented:
            raise errors.invalid_api_key()
        key_id = parse_key_id(token) if token else None
        query = select(GatewayKey, Tenant.status).join(Tenant, Tenant.id == GatewayKey.tenant_id)
        if key_id is not None:
            query = query.where(GatewayKey.id == key_id)
        else:
            digest = import_lookup_digest(presented)
            if digest is None:
                raise errors.invalid_api_key()
            query = query.join(GatewayImportedCredential, GatewayImportedCredential.key_id == GatewayKey.id).where(GatewayImportedCredential.digest == digest)
            if token is None:
                query = query.where(GatewayImportedCredential.allow_raw_authorization.is_(True))
        now = datetime.now(UTC)
        async with self._sessions() as db:
            row = (await db.execute(query)).first()
            if row is None:
                raise errors.invalid_api_key()
            key, tenant_status = row[0], row[1]
            accepted = [key.secret_hash]
            if key.previous_secret_hash and key.previous_secret_expires_at and key.previous_secret_expires_at > now:
                accepted.append(key.previous_secret_hash)
            # An indexed spelling proves possession. Its original credential must still be current or in grace.
            if key_id is None:
                valid = key.import_digest in accepted
            else:
                valid = any(verify_key(presented, digest) for digest in accepted)
            if not valid:
                raise errors.invalid_api_key()
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
                allowed_model_ids=frozenset(key.allowed_model_ids),
                allowed_instance_ids=frozenset(str(item) for item in key.allowed_instance_ids or []),
                tags=dict(key.tags) if key.tags else None,
                limits=Limits(rpm=key.rpm_limit, tpm=key.tpm_limit, max_parallel=key.max_parallel_requests),
                quota=Quota(tokens=key.token_quota, period=key.quota_period) if key.token_quota is not None and key.quota_period is not None else None,
            )

    async def visible_models(self, caller: Caller) -> list[PublicModel]:
        async with self._sessions() as db:
            directory = await load_directory(db, caller.tenant_id)
        return [PublicModel(name=model.call_name, created=model.created // 1000) for model in directory.models if model.state == CALLABLE and caller.may_use(model.model_id, model.instance_id)]

    async def resolve_deployments(self, caller: Caller, name: str, model_type: str) -> list[ResolvedModel]:
        async with self._sessions() as db:
            # A closed model is as invisible to callers as an unknown name.
            models = [model for model in (await load_directory(db, caller.tenant_id)).deployments(name) if model.enabled]
            if not models:
                raise errors.model_not_found(name)
            # A key's scope holds for every deployment: a call never leaves the connections it was given.
            models = [model for model in models if caller.may_use(model.model_id, model.instance_id)]
            if not models:
                raise errors.model_not_allowed(name)
            models = [model for model in models if model_type in model.model_types]
            if not models:
                raise errors.invalid_request(f"The model '{name}' is not available on this endpoint.", param="model", code="model_endpoint_mismatch")
            deployments = []
            for model in models:
                try:
                    upstream = await resolve_upstream(db, caller.tenant_id, model.model_id, model_type)
                except ModelUnavailable:
                    continue
                price = resolve_price(model.provider, upstream.model_name, model.custom_price)
                deployments.append(
                    ResolvedModel(
                        model_id=model.model_id,
                        name=name,
                        model_type=model_type,
                        upstream=upstream,
                        limits=model.limits,
                        price=price,
                        weight=model.weight,
                        order=model.order,
                        instance_name=model.instance_name,
                    )
                )
            if not deployments:
                raise errors.model_unavailable(name)
            return deployments

    async def tokens_used_since(self, key_id: str, since: datetime) -> int:
        """Tokens the key has used since ``since``.

        Quota periods start at a UTC midnight, where the daily sums answer
        without reading the logs; any other instant falls back to the logs.
        """
        since = since.astimezone(UTC)
        if since.timetz() == time(0, tzinfo=UTC):
            query: Any = select(func.coalesce(func.sum(GatewayDailyUsage.total_tokens), 0)).where(GatewayDailyUsage.key_id == key_id, GatewayDailyUsage.date >= since.date())
        else:
            query = select(func.coalesce(func.sum(GatewayRequestLog.total_tokens), 0)).where(GatewayRequestLog.key_id == key_id, GatewayRequestLog.started_at >= since)
        async with self._sessions() as db:
            used = await db.scalar(query)
        return int(used or 0)

    async def health_targets(self) -> list[HealthTarget]:
        """Every callable model of the owners that have a usable key, with the capability to probe."""
        now = datetime.now(UTC)
        targets: list[HealthTarget] = []
        async with self._sessions() as db:
            has_key = (
                select(GatewayKey.tenant_id)
                .join(Tenant, Tenant.id == GatewayKey.tenant_id)
                .where(GatewayKey.status == "active", Tenant.status == "1", or_(GatewayKey.expires_at.is_(None), GatewayKey.expires_at > now))
                .distinct()
            )
            for tenant_id in (await db.scalars(has_key)).all():
                for model in (await load_directory(db, tenant_id)).models:
                    if model.state != CALLABLE:
                        continue
                    model_type = model.servable_types[0]
                    try:
                        upstream = await resolve_upstream(db, tenant_id, model.model_id, model_type)
                    except ModelUnavailable:
                        continue
                    targets.append(HealthTarget(tenant_id=tenant_id, model_id=model.model_id, model_type=model_type, upstream=upstream))
        return targets

    async def record_health(self, results: Sequence[HealthResult]) -> None:
        """Replace each model's latest health check."""
        async with self._sessions() as db:
            for result in results:
                values = {
                    "status": result.status,
                    "status_code": result.status_code,
                    "error_message": result.error_message,
                    "response_time_ms": result.response_time_ms,
                    "checked_at": result.checked_at,
                    "failure_class": result.failure_class,
                    "source": result.source,
                }
                statement = insert(GatewayModelHealth).values(id=uuid.uuid4().hex, tenant_id=result.tenant_id, model_id=result.model_id, **values)
                await db.execute(statement.on_conflict_do_update(constraint="uq_gateway_model_health_tenant_model", set_=values))
            await db.commit()

    async def delete_logs_before(self, cutoff: datetime, limit: int) -> int:
        """Delete up to ``limit`` of the oldest request logs started before ``cutoff``."""
        expired = select(GatewayRequestLog.id).where(GatewayRequestLog.started_at < cutoff).order_by(GatewayRequestLog.started_at).limit(limit)
        async with self._sessions() as db:
            result = await db.execute(delete(GatewayRequestLog).where(GatewayRequestLog.id.in_(expired)))
            await db.commit()
        return int(getattr(result, "rowcount", 0) or 0)

    async def record(self, entry: RequestLogEntry) -> None:
        async with self._sessions() as db:
            db.add(
                GatewayRequestLog(
                    id=entry.id,
                    tenant_id=entry.tenant_id,
                    key_id=entry.key_id,
                    key_name=entry.key_name,
                    model=entry.model,
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
                    cache_read_input_tokens=entry.usage.cache_read_tokens,
                    cost=entry.cost,
                    usage_details=entry.usage.details,
                    attempts=[attempt.view() for attempt in entry.attempts] if len(entry.attempts) > 1 else None,
                    usage_source=entry.usage.source,
                    started_at=entry.started_at,
                    first_token_ms=entry.first_token_ms,
                    duration_ms=entry.duration_ms,
                    end_user=entry.end_user,
                    client_ip=entry.client_ip,
                    tags=entry.tags,
                )
            )
            # Same transaction: the daily sums never disagree with the logs.
            await db.execute(daily_usage_upsert(entry))
            await db.commit()
