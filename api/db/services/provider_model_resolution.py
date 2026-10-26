"""Synchronous compatibility bridge for existing model consumers."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import parse_model_id


def linked_model(db: Session, identifier: int, tenant_id: str) -> tuple[TenantModel, TenantModelInstance, TenantModelProvider] | None:
    query = (
        select(TenantModel, TenantModelInstance, TenantModelProvider)
        .join(TenantModelInstance, TenantModel.instance_id == TenantModelInstance.id)
        .join(TenantModelProvider, TenantModelInstance.provider_id == TenantModelProvider.id)
        .join(TenantLLM, TenantLLM.id == TenantModel.id)
        .where(TenantModel.id == identifier, TenantLLM.tenant_id == tenant_id, TenantModelProvider.tenant_id == tenant_id)
    )
    row = db.execute(query).first()
    return tuple(row) if row else None


def resolve_linked_config(db: Session, config: dict[str, Any], *, expected_type: str | None = None) -> dict[str, Any]:
    config = dict(config)
    if config.get("status", "1") != "1":
        raise LookupError("Model is disabled")
    identifier, tenant_id = config.get("id"), config.get("tenant_id")
    if identifier is None or not tenant_id:
        return config
    link = linked_model(db, identifier, tenant_id)
    if link is None:
        return config
    model, instance, provider = link
    if instance.status != "active" or provider.status != "active":
        raise LookupError("Model instance is disabled")
    capabilities = model.model_types
    selected = expected_type or config.get("mdl_type")
    if selected not in capabilities:
        raise LookupError("Model does not support the requested capability")
    credential = instance.api_key
    try:
        envelope = json.loads(credential)
    except (TypeError, ValueError):
        envelope = None
    if isinstance(envelope, dict) and "is_tools" in envelope:
        envelope.pop("is_tools")
        credential = json.dumps(envelope, ensure_ascii=False)
    config.update(
        llm_name=model.model_name,
        api_key=credential,
        api_base=instance.base_url,
        region=instance.region,
        mdl_type=selected,
        model_types=list(capabilities),
        is_tools=model.is_tools,
        instance_id=instance.id,
        instance_name=instance.instance_name,
        reference=str(identifier),
    )
    return config


def find_model_row(db: Session, tenant_id: str, reference: str, model_type: str | None = None) -> TenantLLM | None:
    query = select(TenantLLM).where(TenantLLM.tenant_id == tenant_id)
    legacy_name, factory = reference, None
    parts = reference.rsplit("@", 2)
    if len(parts) >= 2:
        factory = parts[-1]
        legacy_name = reference.rsplit("@", 1)[0]
    exact_query = query.where(TenantLLM.llm_name == legacy_name)
    if factory:
        exact_query = exact_query.where(TenantLLM.llm_factory == factory)
    exact = list(db.scalars(exact_query.limit(2)))
    try:
        identifier = parse_model_id(reference)
    except ValueError:
        if not exact:
            raise
        identifier = None
    if identifier is not None:
        numeric = db.scalar(query.where(TenantLLM.id == identifier))
        if numeric and any(row.id != numeric.id for row in exact):
            raise LookupError("Numeric model reference is ambiguous with a legacy name")
        candidates = [numeric] if numeric else exact
    elif exact:
        # A persisted legacy name is an exact alias for its original row. New
        # opaque storage keys cannot shadow it by registering the same raw name.
        candidates = exact
    else:
        raw_name = parts[0]
        joined = (
            query.join(TenantModel, TenantModel.id == TenantLLM.id)
            .join(TenantModelInstance, TenantModel.instance_id == TenantModelInstance.id)
            .join(TenantModelProvider, TenantModelInstance.provider_id == TenantModelProvider.id)
            .where(TenantModelProvider.tenant_id == tenant_id, TenantModel.model_name == raw_name)
        )
        if factory:
            joined = joined.where(TenantModelProvider.provider_name == factory)
        if len(parts) == 3:
            joined = joined.where(TenantModelInstance.instance_name == parts[1])
        candidates = list(db.scalars(joined))
    if len(candidates) > 1:
        raise LookupError("Model reference is ambiguous; select its numeric ID")
    if not candidates:
        return None
    row = candidates[0]
    config = resolve_linked_config(db, row.to_dict())
    capabilities = config.get("model_types", [row.mdl_type])
    if model_type is not None and model_type not in capabilities:
        return None
    return row


def require_legacy_write(db: Session, tenant_id: str, factory: str) -> None:
    linked = db.scalar(select(TenantModel.id).join(TenantLLM, TenantModel.id == TenantLLM.id).where(TenantLLM.tenant_id == tenant_id, TenantLLM.llm_factory == factory).limit(1))
    if linked is not None:
        raise ValueError("This provider uses instances; manage it through the provider instance API")
