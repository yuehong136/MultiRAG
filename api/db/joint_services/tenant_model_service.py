import asyncio
import enum
import os
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Tenant, TenantModel, TenantModelInstance, TenantModelProvider, db_connection
from api.db.model_contract import DEFAULT_MODEL_FIELDS, MODEL_TYPE_BITS, parse_model_id
from api.db.services.llm_service import LLMBundle
from api.db.services.provider_model_resolution import model_config, model_for_id, model_records
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from common import settings
from common.constants import LLMType


def get_ocr_model_reference(db: Session, tenant_id: str, provider_name: str) -> str | None:
    """Prefer the configured active model, then atomically import env config."""
    query = (
        model_records()
        .where(
            TenantModelProvider.tenant_id == tenant_id,
            TenantModelProvider.provider_name == provider_name,
            TenantModelProvider.status == "active",
            TenantModelInstance.status == "active",
            TenantModel.status == "active",
            TenantModel.model_type.bitwise_and(MODEL_TYPE_BITS["ocr"]) != 0,
        )
        .order_by(TenantModel.create_time, TenantModel.id)
    )
    candidates = [model.id for model, _, _ in db.execute(query)]
    owner = db.get(Tenant, tenant_id)
    if owner is None:
        raise LookupError("Tenant not found")
    if owner.tenant_ocr_id in candidates:
        return str(owner.tenant_ocr_id)
    if candidates:
        return str(candidates[0])
    ensure = {"MinerU": TenantLLMService.ensure_mineru_from_env, "PaddleOCR": TenantLLMService.ensure_paddleocr_from_env, "OpenDataLoader": TenantLLMService.ensure_opendataloader_from_env}.get(
        provider_name
    )
    if ensure is None:
        raise ValueError("Unsupported PDF OCR provider")
    return ensure(db, tenant_id)


def detached_bundle(db: Session, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> LLMBundle:
    """Construct in a sync callback without returning its session facade."""
    bundle = LLMBundle(db, tenant_id, model_config, **kwargs)
    bundle.db = None
    return bundle


def resolve_model_type(db: Session, tenant_id: str, model_ref: str) -> list[str]:
    """Resolve types from the tenant's registration, preserving provider identity."""
    if not tenant_id or not model_ref:
        raise LookupError("Tenant and model reference are required")
    model, instance, provider = model_for_id(db, model_ref, tenant_id)
    if (model.status, instance.status, provider.status) != ("active", "active", "active"):
        raise LookupError("Model is unavailable")
    capabilities = model.model_types
    if not capabilities:
        raise LookupError("Tenant model has no usable capability")
    return capabilities


def get_model_config_by_id(
    db: Session,
    tenant_model_id: str,
    *,
    tenant_id: str | None = None,
    allowed_tenant_ids: set[str] | None = None,
    model_type: str | None = None,
) -> dict[str, Any]:
    """Resolve model credentials only within the authorized tenant scope."""
    row = model_for_id(db, tenant_model_id, tenant_id, allowed_tenant_ids=allowed_tenant_ids)
    return TenantLLMService.decode_model_config(model_config(*row, capability=model_type))


def get_model_config_by_type_and_name(
    db: Session,
    tenant_id: str,
    model_type: str,
    model_name: str,
) -> dict[str, Any]:
    if not model_name:
        raise ValueError("Model Name is required")
    expected = model_type.value if hasattr(model_type, "value") else model_type
    if parse_model_id(model_name):
        return get_model_config_by_id(db, model_name, tenant_id=tenant_id, model_type=expected)
    name, factory = TenantLLMService.split_model_name_and_factory(model_name)
    if expected == LLMType.EMBEDDING.value and "tei-" in os.getenv("COMPOSE_PROFILES", "") and name == os.getenv("TEI_MODEL", "") and factory in {"Builtin", None}:
        embedding = settings.EMBEDDING_CFG
        return {
            "id": None,
            "llm_factory": "Builtin",
            "api_key": embedding["api_key"],
            "llm_name": name,
            "api_base": embedding["base_url"],
            "mdl_type": expected,
            "max_tokens": embedding.get("max_tokens", 8192),
        }
    raise LookupError(f"Model reference {model_name!r} is unavailable for {expected}")


def get_tenant_default_model_by_type(
    db: Session,
    tenant_id: str,
    model_type: str | enum.Enum,
) -> dict[str, Any]:
    tenant = TenantService.get_or_none(db, id=tenant_id)
    if not tenant:
        raise LookupError("Tenant not found")

    model_type_val = model_type if isinstance(model_type, str) else model_type.value
    field = DEFAULT_MODEL_FIELDS.get(model_type_val)
    if field is None:
        raise ValueError(f"Unknown model type {model_type_val}")
    identifier = getattr(tenant, f"tenant_{field}")
    if identifier:
        return get_model_config_by_id(db, identifier, tenant_id=tenant_id, model_type=model_type_val)
    if model_type_val == LLMType.EMBEDDING.value and (reference := getattr(tenant, field)):
        return get_model_config_by_type_and_name(db, tenant_id, model_type_val, reference)
    raise LookupError(f"No default {model_type_val} model is set.")


# ---------------------------------------------------------------------------
# 事件循环上的 LLMBundle 构造（Canvas/Agent 组件等自开连接的调用方）
#
# 构造是纯同步 DB（模型配置查询 + LLMBundle 内部的租户模型/Langfuse 查询），不持有
# 调用方的 Session——整体入线程池即可解除事件循环阻塞，无需沿调用链传 session。
# 构造用的短会话在返回前关闭，故一律剥离 `bundle.db`（不得随 bundle 逸出，AGENTS.md 规约）。
# ---------------------------------------------------------------------------


async def build_default_bundle_async(tenant_id: str, model_type: str | enum.Enum, **kwargs) -> LLMBundle:
    """按租户默认模型构造 LLMBundle（事件循环安全）。"""

    def _build() -> LLMBundle:
        with db_connection() as db:
            model_config = get_tenant_default_model_by_type(db, tenant_id, model_type)
            bundle = LLMBundle(db, tenant_id, model_config, **kwargs)
        bundle.db = None
        return bundle

    return await asyncio.to_thread(_build)


async def build_named_bundle_async(tenant_id: str, model_type: str, llm_id: str, **kwargs) -> LLMBundle:
    """按模型名构造 LLMBundle（事件循环安全）。"""

    def _build() -> LLMBundle:
        with db_connection() as db:
            model_config = get_model_config_by_type_and_name(db, tenant_id, model_type, llm_id)
            bundle = LLMBundle(db, tenant_id, model_config, **kwargs)
        bundle.db = None
        return bundle

    return await asyncio.to_thread(_build)


async def build_bundle_by_id_async(
    tenant_id: str,
    tenant_model_id: str,
    *,
    db: AsyncSession | None = None,
    allowed_tenant_ids: set[str] | None = None,
    model_type: str | None = None,
    **kwargs: Any,
) -> LLMBundle:
    """Build by ID with a request session, or a short session for task callers."""

    def build(session: Session) -> LLMBundle:
        if allowed_tenant_ids is None:
            model_config = get_model_config_by_id(session, tenant_model_id, tenant_id=tenant_id, model_type=model_type)
        else:
            model_config = get_model_config_by_id(session, tenant_model_id, allowed_tenant_ids=allowed_tenant_ids, model_type=model_type)
        # Usage and tracing follow the authorized model's actual owner.
        bundle = LLMBundle(session, model_config.get("tenant_id", tenant_id), model_config, **kwargs)
        bundle.db = None
        return bundle

    if db is not None:
        return await db.run_sync(build)  # TODO(async-phase4): legacy model construction

    def build_with_connection() -> LLMBundle:
        with db_connection() as session:
            return build(session)

    return await asyncio.to_thread(build_with_connection)


def resolve_parser_reference(db: Session, tenant_id: str, reference: str) -> tuple[str, str | None]:
    """Dispatch registered OCR IDs and built-in parser modes."""

    if parse_model_id(reference) is None:
        return reference, None
    types = resolve_model_type(db, tenant_id, reference)
    if "ocr" not in types:
        return reference, None
    config = get_model_config_by_type_and_name(db, tenant_id, "ocr", reference)
    factory = config["llm_factory"]
    if factory not in {"MinerU", "PaddleOCR", "OpenDataLoader"}:
        raise LookupError("This OCR provider does not have a PDF parser adapter")
    return factory, reference


def rebind_model_config(db: Session, bundle: LLMBundle, model_type: str) -> dict[str, Any]:
    """Rebuild a worker bundle by its identity, never by its display name."""
    config = bundle.model_config
    if config.get("id") is not None:
        return get_model_config_by_id(db, config["id"], tenant_id=bundle.tenant_id, model_type=model_type)
    reference = config.get("reference") or f"{bundle.llm_name}@{config['llm_factory']}"
    return get_model_config_by_type_and_name(db, bundle.tenant_id, model_type, reference)
