from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from common.constants import LLMType


_KEY_TO_MODEL_TYPE = {
    "llm_id": LLMType.CHAT,
    "embd_id": LLMType.EMBEDDING,
    "asr_id": LLMType.SPEECH2TEXT,
    "img2txt_id": LLMType.IMAGE2TEXT,
    "rerank_id": LLMType.RERANK,
    "tts_id": LLMType.TTS,
}


def ensure_tenant_model_id_for_params(db: Session, tenant_id: str, param_dict: dict) -> dict:
    for key in ["llm_id", "embd_id", "asr_id", "img2txt_id", "rerank_id", "tts_id"]:
        tenant_key = f"tenant_{key}"
        if key not in param_dict:
            continue
        if param_dict[key] and not param_dict.get(tenant_key):
            tenant_model = TenantLLMService.get_api_key(db, tenant_id, param_dict[key], _KEY_TO_MODEL_TYPE[key])
            param_dict[tenant_key] = tenant_model.id if tenant_model else None
        elif not param_dict[key]:
            param_dict[tenant_key] = None
    return param_dict
