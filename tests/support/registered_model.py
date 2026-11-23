"""Register an isolated model fixture through the same storage used at runtime."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import InstrumentedAttribute, Session
from sqlalchemy.sql.selectable import ScalarSelect

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import MODEL_TYPE_BITS


def register_model(
    db: Session,
    *,
    tenant_id: str,
    provider_name: str,
    model_name: str,
    model_types: tuple[str, ...] = ("chat",),
    api_key: str = "",
    base_url: str = "",
    instance_name: str = "fixture",
    max_tokens: int = 8192,
    is_tools: bool = False,
    status: str = "active",
    identifier: str | None = None,
    used_tokens: int = 0,
) -> TenantModel:
    provider = db.scalar(select(TenantModelProvider).where(TenantModelProvider.tenant_id == tenant_id, TenantModelProvider.provider_name == provider_name))
    if provider is None:
        provider = TenantModelProvider(id=uuid4().hex, tenant_id=tenant_id, provider_name=provider_name, status="active")
        db.add(provider)
        db.flush()
    instance = db.scalar(select(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id, TenantModelInstance.instance_name == instance_name))
    if instance is None:
        instance = TenantModelInstance(id=uuid4().hex, provider_id=provider.id, instance_name=instance_name, api_key=api_key, base_url=base_url, region="", status="active")
        db.add(instance)
        db.flush()
    elif (instance.api_key, instance.base_url) != (api_key, base_url):
        raise ValueError("Use distinct instance names for distinct connection configurations")
    model = TenantModel(
        id=identifier,
        provider_id=provider.id,
        instance_id=instance.id,
        model_name=model_name,
        model_type=sum(MODEL_TYPE_BITS[value] for value in model_types),
        extra={"max_tokens": max_tokens, "is_tools": is_tools},
        status=status,
        used_tokens=used_tokens,
    )
    db.add(model)
    db.flush()
    return model


def model_owner() -> ScalarSelect[str]:
    return select(TenantModelProvider.tenant_id).where(TenantModelProvider.id == TenantModel.provider_id).correlate(TenantModel).scalar_subquery()


def model_provider() -> ScalarSelect[str]:
    return select(TenantModelProvider.provider_name).where(TenantModelProvider.id == TenantModel.provider_id).correlate(TenantModel).scalar_subquery()


def model_attribute(column: InstrumentedAttribute[str]) -> ScalarSelect[str]:
    return select(column).select_from(TenantModelInstance).where(TenantModelInstance.id == TenantModel.instance_id).correlate(TenantModel).scalar_subquery()
