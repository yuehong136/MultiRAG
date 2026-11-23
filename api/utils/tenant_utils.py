from typing import Any

from sqlalchemy.orm import Session

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
    "ocr_id": LLMType.OCR,
}


def ensure_tenant_model_id_for_params(
    db: Session,
    tenant_id: str,
    param_dict: dict[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    for key, model_type in _KEY_TO_MODEL_TYPE.items():
        tenant_key = f"tenant_{key}"
        if key not in param_dict:
            continue
        if param_dict[key]:
            llm_setting = param_dict.get("llm_setting")
            if key == "llm_id" and isinstance(llm_setting, dict) and llm_setting.get("model_type") == LLMType.IMAGE2TEXT.value:
                model_type = LLMType.IMAGE2TEXT
            tenant_model = TenantLLMService.get_api_key(db, tenant_id, param_dict[key], model_type)
            if strict and not tenant_model:
                raise ArgumentException(f"Tenant model {param_dict[key]} of type {model_type.value} not found.")
            param_dict[tenant_key] = tenant_model.id if tenant_model else None
        else:
            param_dict[tenant_key] = None
    return param_dict
