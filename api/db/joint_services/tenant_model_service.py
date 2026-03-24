import enum
import os

from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService
from common import settings
from common.constants import LLMType


def get_model_config_by_id(db: Session, tenant_model_id: int) -> dict:
    tenant_model = TenantLLMService.get_or_none(db, id=tenant_model_id)
    if not tenant_model:
        raise LookupError(f"Tenant Model with id {tenant_model_id} not found")

    config_dict = tenant_model.to_dict()
    llms = LLMService.query(db, llm_name=config_dict["llm_name"])
    if llms:
        config_dict["is_tools"] = llms[0].is_tools
    return config_dict


def get_model_config_by_type_and_name(
    db: Session,
    tenant_id: str,
    model_type: str,
    model_name: str,
) -> dict:
    if not model_name:
        raise ValueError("Model Name is required")

    model_config = TenantLLMService.get_api_key(db, tenant_id, model_name)
    if not model_config:
        pure_model_name, fid = TenantLLMService.split_model_name_and_factory(model_name)
        if (
            model_type == LLMType.EMBEDDING.value
            and fid == "Builtin"
            and "tei-" in os.getenv("COMPOSE_PROFILES", "")
            and pure_model_name == os.getenv("TEI_MODEL", "")
        ):
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
        else:
            model_config = TenantLLMService.get_api_key(db, tenant_id, pure_model_name)
            if not model_config:
                raise LookupError(f"Tenant Model with name {model_name} not found")
            config_dict = model_config.to_dict()
    else:
        config_dict = model_config.to_dict()

    pure_model_name, fid = TenantLLMService.split_model_name_and_factory(config_dict["llm_name"])
    llms = LLMService.query(db, llm_name=pure_model_name, fid=fid) if fid else LLMService.query(db, llm_name=pure_model_name)
    if not llms and fid:
        llms = LLMService.query(db, llm_name=pure_model_name)
    if llms:
        config_dict["is_tools"] = llms[0].is_tools
    return config_dict


def get_tenant_default_model_by_type(
    db: Session,
    tenant_id: str,
    model_type: str | enum.Enum,
) -> dict:
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
        return get_model_config_by_id(db, tenant_model_id)
    if not model_name:
        raise ValueError(f"No default {model_type_val} model is set.")
    return get_model_config_by_type_and_name(db, tenant_id, model_type_val, model_name)
