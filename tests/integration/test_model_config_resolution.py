"""Model resolution and credential envelopes on an owned PostgreSQL database."""

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import LLM, LLMFactories, TenantLLM
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services import tenant_llm_service
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings

pytestmark = pytest.mark.integration


def test_image_fallback_reloads_chat_registration_as_cv(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = uuid.uuid4().hex
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VisionFixture"}, {"name": "TextFixture"}])

    class VisionModel:
        def __init__(self, key: str, model_name: str, lang: str, **kwargs: Any) -> None:
            self.model_name = model_name

        def describe_with_prompt(self, image: bytes, prompt: str) -> tuple[str, int]:
            return f"{self.model_name}: {prompt}", 1

    monkeypatch.setattr(tenant_llm_service, "CvModel", {"VisionFixture": VisionModel})
    with Session(bootstrapped_engine) as db:
        for factory, tags in [("VisionFixture", "LLM, IMAGE2TEXT "), ("TextFixture", "LLM")]:
            db.add(LLMFactories(name=factory, tags="LLM", status="1"))
            db.add(LLM(fid=factory, llm_name="same-name", mdl_type="chat", tags=tags, is_tools=False, max_tokens=1024))
            db.add(TenantLLM(tenant_id=owner, llm_factory=factory, llm_name="same-name", mdl_type="chat", api_key="fixture", api_base="https://fixture.invalid"))
        db.commit()

    with Session(bootstrapped_engine) as db:
        config = get_model_config_by_type_and_name(db, owner, "image2text", "same-name@VisionFixture")
        model = TenantLLMService.model_instance(db, owner, config)
        assert model.describe_with_prompt(b"image", "describe") == ("same-name: describe", 1)
        assert config["mdl_type"] == "image2text"
        assert get_model_config_by_type_and_name(db, owner, "chat", "same-name@VisionFixture")["mdl_type"] == "chat"
        with pytest.raises(LookupError, match="type image2text not found"):
            get_model_config_by_type_and_name(db, owner, "image2text", "same-name@TextFixture")
        with pytest.raises(LookupError):
            get_model_config_by_type_and_name(db, uuid.uuid4().hex, "image2text", "same-name@VisionFixture")

    with Session(bootstrapped_engine) as db:
        rows = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner)).all()
        assert len(rows) == 2
        assert {row.mdl_type for row in rows} == {"chat"}
