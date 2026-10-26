import asyncio
import enum
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM, db_connection
from api.db.services.llm_service import LLMBundle, LLMService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from common import settings
from common.constants import LLMType


def detached_bundle(db: Session, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> LLMBundle:
    """Construct in a sync callback without returning its session facade."""
    bundle = LLMBundle(db, tenant_id, model_config, **kwargs)
    bundle.db = None
    return bundle


def resolve_model_type(db: Session, tenant_id: str, model_ref: str) -> list[str]:
    """Resolve types from the tenant's registration, preserving provider identity."""
    if not tenant_id or not model_ref:
        raise LookupError("Tenant and model reference are required")
    row = TenantLLMService.get_api_key(db, tenant_id, model_ref)
    if row is None:
        raise LookupError("Tenant model not found")
    config = TenantLLMService.resolve_model_config(db, row.to_dict())
    return list(config.get("model_types", [row.mdl_type]))


def get_model_config_by_id(
    db: Session,
    tenant_model_id: int,
    *,
    tenant_id: str | None = None,
    allowed_tenant_ids: set[str] | None = None,
    model_type: str | None = None,
) -> dict[str, Any]:
    """Apply the trusted tenant scope in SQL, before loading model credentials.

    Management callers may retain their existing unscoped lookup. Retrieval
    callers must pass the authenticated tenant and authorized dataset owners.
    An explicit empty scope never falls back to an unscoped lookup.
    """
    if allowed_tenant_ids is not None:
        scope = {identifier for identifier in allowed_tenant_ids if identifier}
        if tenant_id is not None:
            scope &= {tenant_id}
        if not scope:
            raise LookupError("No authorization for tenant model.")
        tenant_model = db.scalar(select(TenantLLM).where(TenantLLM.id == tenant_model_id, TenantLLM.tenant_id.in_(scope)))
    else:
        filters: dict[str, Any] = {"id": tenant_model_id}
        if tenant_id is not None:
            filters["tenant_id"] = tenant_id
        tenant_model = TenantLLMService.get_or_none(db, **filters)
    if not tenant_model:
        reason = "not found or not authorized" if allowed_tenant_ids is not None else "not found"
        raise LookupError(f"Tenant Model with id {tenant_model_id} {reason}")

    from api.db.services.provider_model_resolution import linked_model, resolve_linked_config

    raw_config = tenant_model.to_dict()
    link = linked_model(db, tenant_model_id, raw_config["tenant_id"]) if raw_config.get("tenant_id") else None
    if link and len(link[0].model_types) > 1 and model_type is None:
        raise LookupError("Model capability is required for a model with multiple capabilities")
    config = resolve_linked_config(db, raw_config, expected_type=model_type)
    if model_type is not None and config["mdl_type"] != model_type:
        raise LookupError("Model does not support the requested capability")
    return TenantLLMService.resolve_model_config(db, config)


def get_model_config_by_type_and_name(
    db: Session,
    tenant_id: str,
    model_type: str,
    model_name: str,
) -> dict[str, Any]:
    if not model_name:
        raise ValueError("Model Name is required")
    expected = model_type.value if hasattr(model_type, "value") else model_type
    model = TenantLLMService.get_api_key(db, tenant_id, model_name, expected)
    if model is not None:
        config = TenantLLMService.resolve_model_config(db, model.to_dict())
        config["mdl_type"] = expected
        return config
    # Retain the existing, declared legacy CHAT/vision fallback without dropping
    # a requested provider or changing its persisted registration.
    if expected in {LLMType.CHAT.value, LLMType.IMAGE2TEXT.value}:
        fallback_type = LLMType.IMAGE2TEXT.value if expected == LLMType.CHAT.value else LLMType.CHAT.value
        model = TenantLLMService.get_api_key(db, tenant_id, model_name, fallback_type)
        if model is not None:
            config = TenantLLMService.resolve_model_config(db, model.to_dict())
            if expected == LLMType.CHAT.value and config["mdl_type"] == LLMType.IMAGE2TEXT.value:
                return config
            if expected == LLMType.IMAGE2TEXT.value and config["mdl_type"] == LLMType.CHAT.value and "model_types" not in config:
                catalog = LLMService.query(db, fid=config["llm_factory"], llm_name=config["llm_name"])
                if catalog and "IMAGE2TEXT" in {tag.strip() for tag in (catalog[0].tags or "").split(",")}:
                    config["mdl_type"] = expected
                    return config
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
    raise LookupError(f"Tenant Model with name {model_name} and type {expected} not found")


def get_tenant_default_model_by_type(
    db: Session,
    tenant_id: str,
    model_type: str | enum.Enum,
) -> dict[str, Any]:
    tenant = TenantService.get_or_none(db, id=tenant_id)
    if not tenant:
        raise LookupError("Tenant not found")

    model_type_val = model_type if isinstance(model_type, str) else model_type.value
    model_name = ""
    tenant_model_id: int | None = None

    match model_type_val:
        case LLMType.EMBEDDING.value:
            model_name = tenant.embd_id
            tenant_model_id = tenant.tenant_embd_id
        case LLMType.SPEECH2TEXT.value:
            model_name = tenant.asr_id
            tenant_model_id = tenant.tenant_asr_id
        case LLMType.IMAGE2TEXT.value:
            model_name = tenant.img2txt_id
            tenant_model_id = tenant.tenant_img2txt_id
        case LLMType.CHAT.value:
            model_name = tenant.llm_id
            tenant_model_id = tenant.tenant_llm_id
        case LLMType.RERANK.value:
            model_name = tenant.rerank_id
            tenant_model_id = tenant.tenant_rerank_id
        case LLMType.TTS.value:
            model_name = tenant.tts_id
            tenant_model_id = tenant.tenant_tts_id
        case LLMType.OCR.value:
            model_name = tenant.ocr_id
            tenant_model_id = tenant.tenant_ocr_id
        case _:
            raise ValueError(f"Unknown model type {model_type_val}")

    if tenant_model_id:
        return get_model_config_by_id(db, tenant_model_id, tenant_id=tenant_id, model_type=model_type_val)
    if not model_name:
        raise ValueError(f"No default {model_type_val} model is set.")
    return get_model_config_by_type_and_name(db, tenant_id, model_type_val, model_name)


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
    tenant_model_id: int,
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
    """Dispatch numeric OCR references without encoding provider suffixes."""
    from common.parser_config_utils import normalize_layout_recognizer

    if not reference.isdecimal():
        return normalize_layout_recognizer(reference)
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
