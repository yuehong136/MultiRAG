from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from api.db.joint_services import tenant_model_service
from api.db.services.llm_service import LLMService
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
    monkeypatch.setattr(LLMService, "query", classmethod(lambda _cls, _db, **_kwargs: []))

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


@pytest.mark.parametrize("tags", ["LLM, IMAGE2TEXT ", "IMAGE2TEXT", None, "", "LLM", "NOT_IMAGE2TEXT", "image2text"])
def test_image_lookup_requires_declared_capability(monkeypatch: pytest.MonkeyPatch, db: Session, tags: str | None) -> None:
    persisted = {"id": 17, "llm_name": "vision-model", "llm_factory": "VisionFactory", "mdl_type": "chat", "api_key": "fixture-key", "api_base": "https://fixture.invalid"}
    record = SimpleNamespace(**persisted, to_dict=lambda: dict(persisted))
    calls: list[tuple[str, str]] = []

    def get_key(_cls: type, _db: Session, _tenant_id: str, name: str, kind: str) -> SimpleNamespace | None:
        calls.append((name, kind))
        return record if kind == "chat" else None

    def query(_cls: type, _db: Session, **filters: str) -> list[SimpleNamespace]:
        assert filters == {"fid": "VisionFactory", "llm_name": "vision-model"}
        return [SimpleNamespace(tags=tags, is_tools=False)]

    monkeypatch.setattr(tenant_model_service.TenantLLMService, "get_api_key", classmethod(get_key))
    monkeypatch.setattr(LLMService, "query", classmethod(query))
    if tags and "IMAGE2TEXT" in {tag.strip() for tag in tags.split(",")}:
        config = tenant_model_service.get_model_config_by_type_and_name(db, "tenant-1", LLMType.IMAGE2TEXT, "vision-model")
        assert config["mdl_type"] == "image2text"
        assert config["id"] == 17
        assert persisted["mdl_type"] == "chat"
    else:
        with pytest.raises(LookupError, match="type image2text not found"):
            tenant_model_service.get_model_config_by_type_and_name(db, "tenant-1", "image2text", "vision-model")
    assert calls == [("vision-model", "image2text"), ("vision-model", "chat")]


def test_image_lookup_keeps_factory_qualification(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    from common import settings

    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VisionFactory"}])
    calls: list[str] = []

    def get_key(_cls: type, _db: Session, _tenant_id: str, name: str, _kind: str) -> None:
        calls.append(name)

    monkeypatch.setattr(tenant_model_service.TenantLLMService, "get_api_key", classmethod(get_key))
    with pytest.raises(LookupError):
        tenant_model_service.get_model_config_by_type_and_name(db, "tenant-1", "image2text", "vision-model@VisionFactory")
    assert calls == ["vision-model@VisionFactory", "vision-model@VisionFactory"]
