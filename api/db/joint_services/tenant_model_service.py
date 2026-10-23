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


def resolve_model_type(db: Session, tenant_id: str, model_ref: str) -> list[str]:
    """Resolve types from the tenant's registration, preserving provider identity."""
    if not tenant_id or not model_ref:
        raise LookupError("Tenant and model reference are required")
    name, factory = TenantLLMService.split_model_name_and_factory(model_ref)

    def find(model_name: str) -> list[TenantLLM]:
        statement = select(TenantLLM).where(TenantLLM.tenant_id == tenant_id, TenantLLM.llm_name == model_name)
        if factory:
            statement = statement.where(TenantLLM.llm_factory == factory)
        return list(db.scalars(statement.limit(2)))

    models = find(name)
    if not models and factory and (suffix := TenantLLMService._FACTORY_SUFFIX.get(factory)):
        models = find(name + suffix)
    if not models:
        raise LookupError(f"Tenant model {model_ref} not found")
    if len(models) != 1:
        raise LookupError(f"Model {model_ref} is ambiguous; include its provider")
    model = models[0]
    if model.status != "1":
        raise LookupError(f"Model {model_ref} is disabled")
    if not model.mdl_type:
        raise LookupError(f"Model {model_ref} has no model type")
    return [model.mdl_type]


def get_model_config_by_id(
    db: Session,
    tenant_model_id: int,
    *,
    tenant_id: str | None = None,
    allowed_tenant_ids: set[str] | None = None,
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

    return TenantLLMService.resolve_model_config(db, tenant_model.to_dict())


def get_model_config_by_type_and_name(
    db: Session,
    tenant_id: str,
    model_type: str,
    model_name: str,
) -> dict[str, Any]:
    if not model_name:
        raise ValueError("Model Name is required")

    model_type_value = model_type.value if hasattr(model_type, "value") else model_type
    model_config = TenantLLMService.get_api_key(db, tenant_id, model_name, model_type_value)
    if not model_config:
        pure_model_name, fid = TenantLLMService.split_model_name_and_factory(model_name)
        compose_profiles = os.getenv("COMPOSE_PROFILES", "")
        is_tei_builtin_embedding = model_type_value == LLMType.EMBEDDING.value and "tei-" in compose_profiles and pure_model_name == os.getenv("TEI_MODEL", "") and (fid == "Builtin" or fid is None)
        if is_tei_builtin_embedding:
            embedding_cfg = settings.EMBEDDING_CFG
            config_dict = {
                "id": None,
                "llm_factory": "Builtin",
                "api_key": embedding_cfg["api_key"],
                "llm_name": pure_model_name,
                "api_base": embedding_cfg["base_url"],
                "mdl_type": LLMType.EMBEDDING.value,
                "max_tokens": embedding_cfg.get("max_tokens", 8192),
            }
        elif model_type_value == LLMType.CHAT.value:
            model_config = TenantLLMService.get_api_key(db, tenant_id, pure_model_name, LLMType.CHAT.value)
            if not model_config:
                model_config = TenantLLMService.get_api_key(db, tenant_id, pure_model_name, LLMType.IMAGE2TEXT.value)
            if not model_config:
                raise LookupError(f"Tenant Model with name {model_name} and type {model_type_value} not found")
            config_dict = model_config.to_dict()
        elif model_type_value == LLMType.IMAGE2TEXT.value:
            model_config = TenantLLMService.get_api_key(db, tenant_id, model_name, LLMType.CHAT.value)
            if model_config:
                models = LLMService.query(db, fid=model_config.llm_factory, llm_name=model_config.llm_name)
                tags = {tag.strip() for tag in (models[0].tags or "").split(",")} if models else set()
                if "IMAGE2TEXT" not in tags:
                    model_config = None
            if not model_config:
                raise LookupError(f"Tenant Model with name {model_name} and type {model_type_value} not found")
            config_dict = model_config.to_dict()
            # Select CvModel without changing the persisted CHAT registration.
            config_dict["mdl_type"] = LLMType.IMAGE2TEXT.value
        else:
            model_config = TenantLLMService.get_api_key(db, tenant_id, pure_model_name, model_type_value)
            if not model_config:
                raise LookupError(f"Tenant Model with name {model_name} and type {model_type_value} not found")
            config_dict = model_config.to_dict()
    else:
        config_dict = model_config.to_dict()

    config_model_type = config_dict.get("mdl_type") or config_dict.get("model_type")
    config_model_type = config_model_type.value if hasattr(config_model_type, "value") else config_model_type
    if config_model_type != model_type_value and not (model_type_value == LLMType.CHAT.value and config_model_type == LLMType.IMAGE2TEXT.value):
        raise LookupError(f"Tenant Model with name {model_name} has type {config_model_type}, expected {model_type_value}")

    return TenantLLMService.resolve_model_config(db, config_dict)


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
            raise ValueError("OCR model name is required")
        case _:
            raise ValueError(f"Unknown model type {model_type_val}")

    if tenant_model_id:
        return get_model_config_by_id(db, tenant_model_id, tenant_id=tenant_id)
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
    **kwargs: Any,
) -> LLMBundle:
    """Build by ID with a request session, or a short session for task callers."""

    def build(session: Session) -> LLMBundle:
        if allowed_tenant_ids is None:
            model_config = get_model_config_by_id(session, tenant_model_id, tenant_id=tenant_id)
        else:
            model_config = get_model_config_by_id(session, tenant_model_id, allowed_tenant_ids=allowed_tenant_ids)
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
