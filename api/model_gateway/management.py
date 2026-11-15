"""Management operations of the model gateway.

Every function works inside the caller's transaction and scopes its queries
to one owner tenant; commit, rollback and HTTP mapping stay with the route.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Integer, and_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import GatewayKey, GatewayModel, GatewayRequestLog
from api.model_gateway.catalog import ModelUnavailable, describe_models, resolve_upstream
from api.model_gateway.keys import issue_key, redact_key
from api.model_gateway.limits import Quota
from api.model_gateway.schemas import GatewayKeyCreate, GatewayKeyPatch, GatewayModelCreate, GatewayModelPatch

USAGE_GROUPS = ("key", "model", "day", "end_user")

_UNAVAILABLE_REASONS = {
    "provider_not_supported": "The model's provider cannot be served through the gateway yet",
    "endpoint_not_supported": "This model type cannot be served through the gateway yet",
    "missing_base_url": "The model has no base URL configured",
    "missing_model_name": "The model has no name configured",
    "model_not_available": "The model does not exist, is disabled, or lacks this capability",
}


class GatewayNotFound(LookupError):
    """The gateway resource does not exist for this owner."""


class GatewayConflict(Exception):
    """The requested change contradicts the resource's current state."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _model_view(row: GatewayModel, described: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "model_id": row.model_id,
        "model_type": row.model_type,
        "status": row.status,
        "description": row.description,
        "rpm_limit": row.rpm_limit,
        "tpm_limit": row.tpm_limit,
        "max_parallel_requests": row.max_parallel_requests,
        # None when the registered model no longer exists for this owner.
        "target": described.get(row.model_id),
        "created_by": row.created_by,
        "updated_by": row.updated_by,
        "create_time": row.create_time,
        "update_time": row.update_time,
    }


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
        "allowed_model_ids": list(row.allowed_model_ids or []),
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


async def _servable(db: AsyncSession, tenant_id: str, model_id: int, model_type: str) -> None:
    try:
        await resolve_upstream(db, tenant_id, model_id, model_type)
    except ModelUnavailable as error:
        raise ValueError(_UNAVAILABLE_REASONS.get(error.reason, "The model cannot be served through the gateway")) from None


async def _owned_model(db: AsyncSession, tenant_id: str, gateway_model_id: str) -> GatewayModel:
    row = await db.scalar(select(GatewayModel).where(GatewayModel.tenant_id == tenant_id, GatewayModel.id == gateway_model_id))
    if row is None:
        raise GatewayNotFound(gateway_model_id)
    return row


async def _owned_key(db: AsyncSession, tenant_id: str, key_id: str) -> GatewayKey:
    row = await db.scalar(select(GatewayKey).where(GatewayKey.tenant_id == tenant_id, GatewayKey.id == key_id))
    if row is None:
        raise GatewayNotFound(key_id)
    return row


async def _checked_model_ids(db: AsyncSession, tenant_id: str, model_ids: list[str]) -> list[str]:
    unique = list(dict.fromkeys(model_ids))
    if unique:
        known = set((await db.scalars(select(GatewayModel.id).where(GatewayModel.tenant_id == tenant_id, GatewayModel.id.in_(unique)))).all())
        if known != set(unique):
            raise ValueError("allowed_model_ids contains an unknown published model")
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


async def list_models(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    rows = (await db.scalars(select(GatewayModel).where(GatewayModel.tenant_id == tenant_id).order_by(GatewayModel.name))).all()
    described = await describe_models(db, tenant_id) if rows else {}
    return [_model_view(row, described) for row in rows]


async def create_model(db: AsyncSession, tenant_id: str, actor_id: str, body: GatewayModelCreate) -> dict[str, Any]:
    await _servable(db, tenant_id, body.model_id, body.model_type)
    row = GatewayModel(
        id=uuid.uuid4().hex,
        tenant_id=tenant_id,
        name=body.name,
        model_id=body.model_id,
        model_type=body.model_type,
        status="active",
        description=body.description,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        max_parallel_requests=body.max_parallel_requests,
        created_by=actor_id,
    )
    db.add(row)
    await db.flush()
    return _model_view(row, await describe_models(db, tenant_id))


async def update_model(db: AsyncSession, tenant_id: str, actor_id: str, gateway_model_id: str, body: GatewayModelPatch) -> dict[str, Any]:
    row = await _owned_model(db, tenant_id, gateway_model_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name") is None:
        changes.pop("name", None)
    if changes.get("status") is None:
        changes.pop("status", None)
    if changes.get("status") == "active" and row.status != "active":
        # Re-enabling is a fresh publication: the target may have changed meanwhile.
        await _servable(db, tenant_id, row.model_id, row.model_type)
    for name, value in changes.items():
        setattr(row, name, value)
    row.updated_by = actor_id
    await db.flush()
    return _model_view(row, await describe_models(db, tenant_id))


async def delete_model(db: AsyncSession, tenant_id: str, gateway_model_id: str) -> None:
    await db.delete(await _owned_model(db, tenant_id, gateway_model_id))
    await db.flush()


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
    for required in ("name", "allow_all_models", "allowed_model_ids", "status"):
        if changes.get(required) is None:
            changes.pop(required, None)
    if "allowed_model_ids" in changes:
        changes["allowed_model_ids"] = await _checked_model_ids(db, tenant_id, changes["allowed_model_ids"])
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
