from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMService
from api.db.services.tenant_llm_service import TenantLLMService
from common.constants import LLMType
from common.exceptions import ArgumentException

_KEY_TO_MODEL_TYPE = {
    "llm_id": LLMType.CHAT,
    "embd_id": LLMType.EMBEDDING,
    "asr_id": LLMType.SPEECH2TEXT,
    "img2txt_id": LLMType.IMAGE2TEXT,
    "rerank_id": LLMType.RERANK,
    "tts_id": LLMType.TTS,
}


def _supports_model_type(db: Session, tenant_model: object, model_type: LLMType) -> bool:
    model_name = getattr(tenant_model, "llm_name", "")
    factory = getattr(tenant_model, "llm_factory", "")
    models = LLMService.query(db, llm_name=model_name, fid=factory) if factory else LLMService.query(db, llm_name=model_name)
    return any(model_type.value.upper() in {tag.strip().upper() for tag in (model.tags or "").split(",")} for model in models)


def ensure_tenant_model_id_for_params(
    db: Session,
    tenant_id: str,
    param_dict: dict,
    *,
    strict: bool = False,
) -> dict:
    for key in ["llm_id", "embd_id", "asr_id", "img2txt_id", "rerank_id", "tts_id"]:
        tenant_key = f"tenant_{key}"
        if key not in param_dict:
            continue
        if param_dict[key] and not param_dict.get(tenant_key):
            model_type = _KEY_TO_MODEL_TYPE[key]
            tenant_model = TenantLLMService.get_api_key(db, tenant_id, param_dict[key], model_type)
            if not tenant_model and model_type == LLMType.CHAT:
                fallback_model = TenantLLMService.get_api_key(db, tenant_id, param_dict[key])
                if fallback_model and _supports_model_type(db, fallback_model, model_type):
                    tenant_model = fallback_model
            if strict and not tenant_model:
                raise ArgumentException(f"Tenant Model with name {param_dict[key]} and type {model_type.value} not found")
            param_dict[tenant_key] = tenant_model.id if tenant_model else None
        elif not param_dict[key]:
            param_dict[tenant_key] = None
    return param_dict
