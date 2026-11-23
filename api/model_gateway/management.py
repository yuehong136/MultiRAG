"""Management operations of the model gateway.

Every function works inside the caller's transaction and scopes its queries
to one owner tenant; commit, rollback and HTTP mapping stay with the route.
There is no publishing: the catalog lists every registered model, and an
operator only switches models off, names them or limits them.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Integer, and_, case, cast, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import GatewayKey, GatewayModelSetting, GatewayRequestLog, GatewaySetting
from api.model_gateway import catalog
from api.model_gateway.directory import CALLABLE, Directory, DirectoryModel, load_directory
from api.model_gateway.keys import issue_key, redact_key
from api.model_gateway.limits import Quota
from api.model_gateway.schemas import CatalogInstancePatch, CatalogModelPatch, GatewayKeyCreate, GatewayKeyPatch, GatewaySettingsPatch
from common.app_config import ModelGatewayConfig

USAGE_GROUPS = ("key", "model", "day", "end_user")
OVERVIEW_DAYS = (1, 7, 30)
_RECENT_WINDOW = timedelta(days=7)
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})
_HEALTH_TIMEOUT_SECONDS = 1.0


class GatewayNotFound(LookupError):
    """The gateway resource does not exist for this owner."""


class GatewayConflict(Exception):
    """The requested change contradicts the resource's current state."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# --- catalog ---------------------------------------------------------------


def _catalog_model_view(model: DirectoryModel, requests: dict[str, int]) -> dict[str, Any]:
    return {
        "model_id": model.model_id,
        "model_name": model.model_name,
        "provider": model.provider,
        "instance_id": model.instance_id,
        "instance_name": model.instance_name,
        "model_types": list(model.model_types),
        "servable_types": list(model.servable_types),
        "state": model.state,
        "reason": model.reason,
        "enabled": model.enabled,
        "enabled_by_default": model.enabled_by_default,
        "call_name": model.call_name,
        "names": list(model.names),
        "alias": model.alias,
        "preferred": model.preferred,
        "shares_name": model.shares_name,
        "description": model.description,
        "rpm_limit": model.limits.rpm,
        "tpm_limit": model.limits.tpm,
        "max_parallel_requests": model.limits.max_parallel,
        "requests_7d": requests.get(model.model_id, 0),
    }


async def _requests_by_model(db: AsyncSession, tenant_id: str, since: datetime) -> dict[str, int]:
    log = GatewayRequestLog
    query = select(log.upstream_model_id, func.count()).where(log.tenant_id == tenant_id, log.started_at >= since, log.upstream_model_id.is_not(None)).group_by(log.upstream_model_id)
    return {model_id: int(count) for model_id, count in (await db.execute(query)).all()}


async def _catalog_of(db: AsyncSession, tenant_id: str, directory: Directory) -> dict[str, Any]:
    requests = await _requests_by_model(db, tenant_id, datetime.now(UTC) - _RECENT_WINDOW)
    return {
        "new_models_enabled": directory.new_models_enabled,
        "connections": [{"instance_id": item.instance_id, "instance_name": item.instance_name, "provider": item.provider} for item in directory.connections],
        "models": [_catalog_model_view(model, requests) for model in directory.models],
    }


async def catalog_view(db: AsyncSession, tenant_id: str) -> dict[str, Any]:
    return await _catalog_of(db, tenant_id, await load_directory(db, tenant_id, with_connections=True))


async def _settings_rows(db: AsyncSession, tenant_id: str, actor_id: str, models: list[DirectoryModel]) -> dict[str, GatewayModelSetting]:
    """Each model's settings row, created with its current state where missing."""
    if models:
        values = [{"id": uuid.uuid4().hex, "tenant_id": tenant_id, "model_id": model.model_id, "enabled": model.enabled, "created_by": actor_id} for model in models]
        await db.execute(insert(GatewayModelSetting).values(values).on_conflict_do_nothing(index_elements=["tenant_id", "model_id"]))
    ids = [model.model_id for model in models]
    rows = (
        await db.scalars(
            select(GatewayModelSetting).where(GatewayModelSetting.tenant_id == tenant_id, GatewayModelSetting.model_id.in_(ids)).with_for_update().execution_options(populate_existing=True)
        )
    ).all()
    return {row.model_id: row for row in rows}


async def update_catalog_model(db: AsyncSession, tenant_id: str, actor_id: str, model_id: str, body: CatalogModelPatch) -> dict[str, Any]:
    directory = await load_directory(db, tenant_id)
    model = directory.model(model_id)
    if model is None:
        raise GatewayNotFound(str(model_id))
    changes = body.model_dump(exclude_unset=True)
    for required in ("enabled", "preferred"):
        if changes.get(required) is None:
            changes.pop(required, None)
    alias = changes.get("alias")
    if alias is not None:
        holder = directory.lookup(alias)
        if holder is not None and holder.model_id != model_id:
            raise GatewayConflict("Another model already answers to this name")
    row = (await _settings_rows(db, tenant_id, actor_id, [model]))[model_id]
    if changes.get("preferred"):
        rivals = [item.model_id for item in directory.models if item.model_name == model.model_name and item.model_id != model_id]
        if rivals:
            await db.execute(update(GatewayModelSetting).where(GatewayModelSetting.tenant_id == tenant_id, GatewayModelSetting.model_id.in_(rivals)).values(preferred=False, updated_by=actor_id))
    for name, value in changes.items():
        setattr(row, name, value)
    row.updated_by = actor_id
    await db.flush()
    updated = (await load_directory(db, tenant_id)).model(model_id)
    assert updated is not None
    return _catalog_model_view(updated, await _requests_by_model(db, tenant_id, datetime.now(UTC) - _RECENT_WINDOW))


async def update_catalog_instance(db: AsyncSession, tenant_id: str, actor_id: str, instance_id: str, body: CatalogInstancePatch) -> dict[str, Any]:
    directory = await load_directory(db, tenant_id, with_connections=True)
    if not any(item.instance_id == instance_id for item in directory.connections):
        raise GatewayNotFound(instance_id)
    models = [model for model in directory.models if model.instance_id == instance_id]
    for row in (await _settings_rows(db, tenant_id, actor_id, models)).values():
        row.enabled = body.enabled
        row.updated_by = actor_id
    await db.flush()
    return await catalog_view(db, tenant_id)


async def update_settings(db: AsyncSession, tenant_id: str, actor_id: str, body: GatewaySettingsPatch) -> dict[str, Any]:
    """Change the default for models nobody switched; models that exist now keep their state."""
    directory = await load_directory(db, tenant_id)
    if body.new_models_enabled != directory.new_models_enabled:
        await _settings_rows(db, tenant_id, actor_id, [model for model in directory.models if model.enabled_by_default])
    statement = insert(GatewaySetting).values(id=uuid.uuid4().hex, tenant_id=tenant_id, new_models_enabled=body.new_models_enabled, updated_by=actor_id)
    await db.execute(statement.on_conflict_do_update(index_elements=["tenant_id"], set_={"new_models_enabled": body.new_models_enabled, "updated_by": actor_id}))
    await db.flush()
    return await catalog_view(db, tenant_id)


# --- keys --------------------------------------------------------------------


def _key_view(row: GatewayKey, quota_used: int | None = None) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tags": row.tags,
        "redacted_key": redact_key(row.id),
        "status": row.status,
        "expires_at": _iso(row.expires_at),
        "allow_all_models": row.allow_all_models,
        "allowed_model_ids": row.allowed_model_ids,
        "allowed_instance_ids": [str(item) for item in row.allowed_instance_ids or []],
        "rpm_limit": row.rpm_limit,
        "tpm_limit": row.tpm_limit,
        "max_parallel_requests": row.max_parallel_requests,
        "token_quota": row.token_quota,
        "quota_period": row.quota_period,
        # Tokens used in the current period; None for keys without a quota.
        "quota_used": quota_used,
        "last_used_at": _iso(row.last_used_at),
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


async def _owned_key(db: AsyncSession, tenant_id: str, key_id: str) -> GatewayKey:
    row = await db.scalar(select(GatewayKey).where(GatewayKey.tenant_id == tenant_id, GatewayKey.id == key_id))
    if row is None:
        raise GatewayNotFound(key_id)
    return row


async def _checked_model_ids(db: AsyncSession, tenant_id: str, model_ids: list[str]) -> list[str]:
    unique = list(dict.fromkeys(model_ids))
    if unique and not set(unique) <= {model.model_id for model in await catalog.registered_models(db, tenant_id)}:
        raise ValueError("allowed_model_ids contains a model this tenant has not registered")
    return unique


async def _checked_instance_ids(db: AsyncSession, tenant_id: str, instance_ids: list[str]) -> list[str]:
    unique = list(dict.fromkeys(instance_ids))
    if unique and not set(unique) <= {item.instance_id for item in await catalog.connections(db, tenant_id)}:
        raise ValueError("allowed_instance_ids contains a connection this tenant does not have")
    return unique


async def _quota_usage(db: AsyncSession, rows: list[GatewayKey]) -> dict[str, int]:
    """Tokens each quota-bearing key has used in its current period, from the request logs."""
    now = datetime.now(UTC)
    usage: dict[str, int] = {}
    for period in ("day", "month"):
        key_ids = [row.id for row in rows if row.token_quota is not None and row.quota_period == period]
        if not key_ids:
            continue
        since = Quota(tokens=1, period=period).period_start(now)
        query = (
            select(GatewayRequestLog.key_id, func.coalesce(func.sum(GatewayRequestLog.total_tokens), 0))
            .where(GatewayRequestLog.key_id.in_(key_ids), GatewayRequestLog.started_at >= since)
            .group_by(GatewayRequestLog.key_id)
        )
        usage.update(dict.fromkeys(key_ids, 0))
        usage.update({key_id: int(used) for key_id, used in (await db.execute(query)).all()})
    return usage


async def _key_with_usage(db: AsyncSession, row: GatewayKey) -> dict[str, Any]:
    return _key_view(row, (await _quota_usage(db, [row])).get(row.id))


async def list_keys(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    rows = list((await db.scalars(select(GatewayKey).where(GatewayKey.tenant_id == tenant_id).order_by(GatewayKey.create_time.desc(), GatewayKey.id))).all())
    usage = await _quota_usage(db, rows)
    return [_key_view(row, usage.get(row.id)) for row in rows]


async def create_key(db: AsyncSession, tenant_id: str, actor_id: str, body: GatewayKeyCreate) -> dict[str, Any]:
    issued = issue_key()
    row = GatewayKey(
        id=issued.key_id,
        tenant_id=tenant_id,
        name=body.name,
        description=body.description,
        tags=body.tags,
        secret_hash=issued.secret_hash,
        status="active",
        expires_at=body.expires_at,
        allow_all_models=body.allow_all_models,
        allowed_model_ids=await _checked_model_ids(db, tenant_id, body.allowed_model_ids),
        allowed_instance_ids=await _checked_instance_ids(db, tenant_id, body.allowed_instance_ids),
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        max_parallel_requests=body.max_parallel_requests,
        token_quota=body.token_quota,
        quota_period=body.quota_period,
        created_by=actor_id,
    )
    db.add(row)
    await db.flush()
    # The only place the plaintext ever leaves the process.
    return {**_key_view(row, 0 if row.token_quota is not None else None), "key": issued.plaintext}


async def update_key(db: AsyncSession, tenant_id: str, actor_id: str, key_id: str, body: GatewayKeyPatch) -> dict[str, Any]:
    row = await _owned_key(db, tenant_id, key_id)
    if row.status == "revoked":
        raise GatewayConflict("A revoked key cannot be changed")
    changes = body.model_dump(exclude_unset=True)
    for required in ("name", "allow_all_models", "allowed_model_ids", "allowed_instance_ids", "status"):
        if changes.get(required) is None:
            changes.pop(required, None)
    if "allowed_model_ids" in changes:
        changes["allowed_model_ids"] = await _checked_model_ids(db, tenant_id, changes["allowed_model_ids"])
    if "allowed_instance_ids" in changes:
        changes["allowed_instance_ids"] = await _checked_instance_ids(db, tenant_id, changes["allowed_instance_ids"])
    for name, value in changes.items():
        setattr(row, name, value)
    if (row.token_quota is None) != (row.quota_period is None):
        raise ValueError("token_quota and quota_period must be set together")
    row.updated_by = actor_id
    await db.flush()
    return await _key_with_usage(db, row)


async def revoke_key(db: AsyncSession, tenant_id: str, actor_id: str, key_id: str) -> dict[str, Any]:
    row = await _owned_key(db, tenant_id, key_id)
    row.status = "revoked"
    row.updated_by = actor_id
    await db.flush()
    return await _key_with_usage(db, row)


# --- overview ----------------------------------------------------------------


def public_base_url(config: ModelGatewayConfig) -> str | None:
    """Where callers reach the data plane, when the server can tell."""
    if config.public_base_url.strip():
        return config.public_base_url.strip().rstrip("/")
    if config.host.strip() in _WILDCARD_HOSTS:
        return None
    host = f"[{config.host}]" if ":" in config.host else config.host
    return f"http://{host}:{config.http_port}/v1"


async def data_plane_status(config: ModelGatewayConfig, transport: httpx.AsyncBaseTransport | None = None) -> str:
    """``up`` when the data plane answers its health check from this host."""
    host = "127.0.0.1" if config.host.strip() in _WILDCARD_HOSTS else config.host
    host = f"[{host}]" if ":" in host else host
    try:
        async with httpx.AsyncClient(transport=transport, timeout=_HEALTH_TIMEOUT_SECONDS) as client:
            response = await client.get(f"http://{host}:{config.http_port}/healthz")
    except httpx.HTTPError:
        return "down"
    return "up" if response.status_code == 200 else "down"


def _window(days: int, now: datetime) -> tuple[str, datetime, datetime]:
    """Bucket size and the bucket-aligned start of a window ending now (UTC)."""
    if days == 1:
        return "hour", now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23), now
    return "day", now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1), now


async def overview(db: AsyncSession, tenant_id: str, *, days: int, config: ModelGatewayConfig, data_plane: str, now: datetime | None = None) -> dict[str, Any]:
    if days not in OVERVIEW_DAYS:
        raise ValueError(f"days must be one of {', '.join(map(str, OVERVIEW_DAYS))}")
    now = now or datetime.now(UTC)
    bucket, start, end = _window(days, now)
    log = GatewayRequestLog
    in_window = and_(log.tenant_id == tenant_id, log.started_at >= start, log.started_at < end)
    failed = _failed()

    totals = (
        await db.execute(
            select(
                func.count().label("requests"),
                func.coalesce(func.sum(cast(case((failed, 1), else_=0), Integer)), 0).label("errors"),
                func.coalesce(func.sum(log.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(log.completion_tokens), 0).label("completion_tokens"),
                func.coalesce(func.sum(log.total_tokens), 0).label("total_tokens"),
                func.coalesce(func.sum(cast(case((log.usage_source == "unavailable", 1), else_=0), Integer)), 0).label("usage_unknown"),
                func.percentile_cont(0.5).within_group(log.first_token_ms).filter(and_(log.stream.is_(True), log.first_token_ms.is_not(None))).label("first_token_p50_ms"),
            ).where(in_window)
        )
    ).one()
    previous_requests = await db.scalar(select(func.count()).select_from(log).where(log.tenant_id == tenant_id, log.started_at >= start - (end - start), log.started_at < start))

    code = func.coalesce(log.error_code, log.error_type)
    errors_by_code = (
        await db.execute(
            select(log.status_code, code, func.count().label("count")).where(in_window, failed).group_by(log.status_code, code).order_by(func.count().desc(), log.status_code, code).limit(3)
        )
    ).all()

    # Buckets are UTC, independent of the database session time zone.
    truncated = func.date_trunc(bucket, func.timezone("UTC", log.started_at))
    counted = {
        row[0].replace(tzinfo=UTC): (int(row[1]), int(row[2]))
        for row in (await db.execute(select(truncated, func.count(), func.coalesce(func.sum(cast(case((failed, 1), else_=0), Integer)), 0)).where(in_window).group_by(truncated))).all()
    }
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    series, cursor = [], start
    while cursor < end:
        requests, errors = counted.get(cursor, (0, 0))
        series.append({"start": cursor.isoformat(), "requests": requests, "errors": errors})
        cursor += step

    directory = await load_directory(db, tenant_id)
    top_models = []
    for model_id, upstream_name, requests, tokens in (
        await db.execute(
            select(log.upstream_model_id, func.max(log.upstream_model_name), func.count(), func.coalesce(func.sum(log.total_tokens), 0))
            .where(in_window, log.upstream_model_id.is_not(None))
            .group_by(log.upstream_model_id)
            .order_by(func.coalesce(func.sum(log.total_tokens), 0).desc(), func.count().desc())
            .limit(5)
        )
    ).all():
        model = directory.model(model_id)
        top_models.append(
            {"model_id": model_id, "name": model.call_name if model else upstream_name, "provider": model.provider if model else None, "requests": int(requests), "total_tokens": int(tokens)}
        )
    top_keys = [
        {"key_id": key_id, "key_name": key_name, "requests": int(requests), "total_tokens": int(tokens)}
        for key_id, key_name, requests, tokens in (
            await db.execute(
                select(log.key_id, func.max(log.key_name), func.count(), func.coalesce(func.sum(log.total_tokens), 0))
                .where(in_window)
                .group_by(log.key_id)
                .order_by(func.coalesce(func.sum(log.total_tokens), 0).desc(), func.count().desc())
                .limit(5)
            )
        ).all()
    ]
    recent_errors = (await db.scalars(select(log).where(log.tenant_id == tenant_id, failed).order_by(log.started_at.desc(), log.id).limit(5))).all()

    callable_models = [model for model in directory.models if model.state == CALLABLE]
    active_keys = await db.scalar(
        select(func.count()).select_from(GatewayKey).where(GatewayKey.tenant_id == tenant_id, GatewayKey.status == "active", or_(GatewayKey.expires_at.is_(None), GatewayKey.expires_at > now))
    )
    p50 = totals.first_token_p50_ms
    return {
        "base_url": public_base_url(config),
        "listen_port": config.http_port,
        "data_plane": data_plane,
        "counts": {"providers": len({model.provider for model in callable_models}), "callable_models": len(callable_models), "active_keys": int(active_keys or 0)},
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "totals": {
            "requests": int(totals.requests),
            "errors": int(totals.errors),
            "prompt_tokens": int(totals.prompt_tokens),
            "completion_tokens": int(totals.completion_tokens),
            "total_tokens": int(totals.total_tokens),
            "usage_unknown": int(totals.usage_unknown),
            "first_token_p50_ms": round(float(p50)) if p50 is not None else None,
        },
        "previous_requests": int(previous_requests or 0),
        "errors_by_code": [{"status_code": int(status), "code": error_code, "count": int(count)} for status, error_code, count in errors_by_code],
        "bucket": bucket,
        "series": series,
        "top_models": top_models,
        "top_keys": top_keys,
        "recent_errors": [_log_view(row) for row in recent_errors],
    }


# --- logs and usage ----------------------------------------------------------


def _failed() -> Any:
    return or_(GatewayRequestLog.error_type.is_not(None), GatewayRequestLog.status_code >= 400)


def _log_filters(
    tenant_id: str,
    *,
    key_id: str | None,
    model: str | None,
    end_user: str | None,
    outcome: str | None,
    start: datetime | None,
    end: datetime | None,
) -> list[Any]:
    filters: list[Any] = [GatewayRequestLog.tenant_id == tenant_id]
    if key_id:
        filters.append(GatewayRequestLog.key_id == key_id)
    if model:
        filters.append(GatewayRequestLog.model == model)
    if end_user:
        filters.append(GatewayRequestLog.end_user == end_user)
    if outcome == "error":
        filters.append(_failed())
    elif outcome == "success":
        filters.append(and_(GatewayRequestLog.error_type.is_(None), GatewayRequestLog.status_code < 400))
    if start is not None:
        filters.append(GatewayRequestLog.started_at >= start)
    if end is not None:
        filters.append(GatewayRequestLog.started_at < end)
    return filters


def _log_view(row: GatewayRequestLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "key_id": row.key_id,
        "key_name": row.key_name,
        "model": row.model,
        "gateway_model_id": row.gateway_model_id,
        "upstream_model_id": row.upstream_model_id,
        "upstream_model_name": row.upstream_model_name,
        "endpoint": row.endpoint,
        "stream": row.stream,
        "status_code": row.status_code,
        "error_type": row.error_type,
        "error_code": row.error_code,
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens,
        "usage_details": row.usage_details,
        "usage_source": row.usage_source,
        "started_at": _iso(row.started_at),
        "first_token_ms": row.first_token_ms,
        "duration_ms": row.duration_ms,
        "end_user": row.end_user,
        "client_ip": row.client_ip,
        "tags": row.tags,
    }


async def query_logs(
    db: AsyncSession,
    tenant_id: str,
    *,
    page: int,
    page_size: int,
    key_id: str | None = None,
    model: str | None = None,
    end_user: str | None = None,
    outcome: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    filters = _log_filters(tenant_id, key_id=key_id, model=model, end_user=end_user, outcome=outcome, start=start, end=end)
    total = await db.scalar(select(func.count()).select_from(GatewayRequestLog).where(*filters))
    rows = (await db.scalars(select(GatewayRequestLog).where(*filters).order_by(GatewayRequestLog.started_at.desc(), GatewayRequestLog.id).limit(page_size).offset((page - 1) * page_size))).all()
    return {"items": [_log_view(row) for row in rows], "total": int(total or 0), "page": page, "page_size": page_size}


async def usage_summary(
    db: AsyncSession,
    tenant_id: str,
    *,
    group_by: str,
    start: datetime,
    end: datetime,
    key_id: str | None = None,
    model: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate request logs. Token sums cover only calls whose usage is known;
    ``usage_unknown`` says how many calls are missing from those sums."""
    if group_by not in USAGE_GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(USAGE_GROUPS)}")
    log = GatewayRequestLog
    if group_by == "key":
        group: list[Any] = [log.key_id]
        labels: list[Any] = [log.key_id.label("key_id"), func.max(log.key_name).label("key_name")]
    elif group_by == "model":
        group, labels = [log.model], [log.model.label("model")]
    elif group_by == "end_user":
        group, labels = [log.end_user], [log.end_user.label("end_user")]
    else:
        # Days are UTC days, independent of the database session time zone.
        day = func.date_trunc("day", func.timezone("UTC", log.started_at))
        group, labels = [day], [day.label("day")]
    query = (
        select(
            *labels,
            func.count().label("requests"),
            func.coalesce(func.sum(cast(case((_failed(), 1), else_=0), Integer)), 0).label("errors"),
            func.coalesce(func.sum(log.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(log.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(log.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(cast(case((log.usage_source == "unavailable", 1), else_=0), Integer)), 0).label("usage_unknown"),
        )
        .where(*_log_filters(tenant_id, key_id=key_id, model=model, end_user=None, outcome=None, start=start, end=end))
        .group_by(*group)
        .order_by(*group)
    )
    summary: list[dict[str, Any]] = []
    for row in (await db.execute(query)).mappings().all():
        item = dict(row)
        if "day" in item:
            item["day"] = item["day"].date().isoformat() if item["day"] is not None else None
        for name in ("requests", "errors", "prompt_tokens", "completion_tokens", "total_tokens", "usage_unknown"):
            item[name] = int(item[name])
        summary.append(item)
    return summary
