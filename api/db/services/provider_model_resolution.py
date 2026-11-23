"""Resolve a registered tenant model and its provider instance."""

import json
from typing import Any

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import MODEL_TYPE_BITS, parse_model_id

ModelRecord = tuple[TenantModel, TenantModelInstance, TenantModelProvider]


def model_records() -> Select[ModelRecord]:
    return (
        select(TenantModel, TenantModelInstance, TenantModelProvider)
        .join(TenantModelInstance, and_(TenantModel.instance_id == TenantModelInstance.id, TenantModel.provider_id == TenantModelInstance.provider_id))
        .join(TenantModelProvider, TenantModel.provider_id == TenantModelProvider.id)
    )


def model_for_id(db: Session, identifier: str, tenant_id: str | None = None, *, allowed_tenant_ids: set[str] | None = None) -> ModelRecord:
    if parse_model_id(identifier) is None:
        raise LookupError("Invalid model ID")
    if allowed_tenant_ids is not None and (not allowed_tenant_ids or (tenant_id is not None and tenant_id not in allowed_tenant_ids)):
        raise LookupError("No authorization for tenant model.")
    query = model_records().where(TenantModel.id == identifier)
    if tenant_id is not None:
        query = query.where(TenantModelProvider.tenant_id == tenant_id)
    if allowed_tenant_ids is not None:
        query = query.where(TenantModelProvider.tenant_id.in_(allowed_tenant_ids))
    row = db.execute(query).one_or_none()
    if row is None:
        raise LookupError("Model not found or not authorized")
    return (row[0], row[1], row[2])


def model_config(model: TenantModel, instance: TenantModelInstance, provider: TenantModelProvider, *, capability: str | None = None) -> dict[str, Any]:
    if (model.status, instance.status, provider.status) != ("active", "active", "active"):
        raise LookupError("Model is unavailable")
    types = model.model_types
    if capability is None:
        if len(types) != 1:
            raise LookupError("Model capability is required for a model with multiple capabilities")
        capability = types[0]
    if not model.model_type & MODEL_TYPE_BITS.get(capability, 0):
        raise LookupError("Model does not support the requested capability")
    credential = instance.api_key
    try:
        payload = json.loads(credential)
    except ValueError:
        payload = None
    if isinstance(payload, dict) and "is_tools" in payload:
        payload.pop("is_tools")
        credential = json.dumps(payload, ensure_ascii=False)
    return {
        "id": model.id,
        "tenant_id": provider.tenant_id,
        "llm_name": model.model_name,
        "llm_factory": provider.provider_name,
        "mdl_type": capability,
        "model_types": types,
        "status": "1",
        "max_tokens": model.max_tokens,
        "used_tokens": model.used_tokens,
        "is_tools": model.is_tools,
        "api_key": credential,
        "api_base": instance.base_url,
        "region": instance.region,
        "instance_id": instance.id,
        "instance_name": instance.instance_name,
        "reference": model.id,
    }


def resolve_model_config(db: Session, config: dict[str, Any]) -> dict[str, Any]:
    identifier, tenant_id = config.get("id"), config.get("tenant_id")
    if identifier is None or not tenant_id:
        return config
    return {**config, **model_config(*model_for_id(db, identifier, tenant_id), capability=config.get("mdl_type"))}
