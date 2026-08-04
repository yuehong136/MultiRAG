from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.db.joint_services import tenant_model_service
from common.constants import LLMType


def test_chat_lookup_falls_back_to_image2text_model(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    image_model = SimpleNamespace(
        to_dict=lambda: {
            "llm_name": "vision-model",
            "llm_factory": "VisionFactory",
            "mdl_type": LLMType.IMAGE2TEXT.value,
        }
    )

    def fake_get_api_key(_cls, _db, _tenant_id: str, model_name: str, model_type: str):
        calls.append((model_name, model_type))
        if model_type == LLMType.IMAGE2TEXT.value:
            return image_model
        return None

    monkeypatch.setattr(tenant_model_service.TenantLLMService, "get_api_key", classmethod(fake_get_api_key))
    monkeypatch.setattr(tenant_model_service.LLMService, "query", classmethod(lambda _cls, _db, **_kwargs: []))

    with Session() as db:
        config = tenant_model_service.get_model_config_by_type_and_name(
            db,
            "tenant-1",
            LLMType.CHAT.value,
            "vision-model",
        )

    assert config["mdl_type"] == LLMType.IMAGE2TEXT.value
    assert calls == [
        ("vision-model", LLMType.CHAT.value),
        ("vision-model", LLMType.CHAT.value),
        ("vision-model", LLMType.IMAGE2TEXT.value),
    ]
