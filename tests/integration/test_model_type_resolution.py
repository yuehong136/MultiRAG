"""Model types must come from one tenant/provider registration, never global names."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM
from api.db.joint_services.tenant_model_service import resolve_model_type
from common import settings

pytestmark = pytest.mark.integration


def test_same_name_across_tenants_and_factories(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    owner, other = uuid.uuid4().hex, uuid.uuid4().hex
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "TextFixture"}, {"name": "VisionFixture"}])
    with Session(bootstrapped_engine) as db:
        db.add_all(
            [
                TenantLLM(tenant_id=owner, llm_factory="TextFixture", llm_name="same-model", mdl_type="chat", status="1"),
                TenantLLM(tenant_id=other, llm_factory="TextFixture", llm_name="same-model", mdl_type="embedding", status="1"),
                TenantLLM(tenant_id=owner, llm_factory="VisionFixture", llm_name="same-model", mdl_type="image2text", status="1"),
            ]
        )
        db.commit()

    with Session(bootstrapped_engine) as db:
        assert resolve_model_type(db, owner, "same-model@TextFixture") == ["chat"]
        assert resolve_model_type(db, owner, "same-model@VisionFixture") == ["image2text"]
        assert resolve_model_type(db, other, "same-model") == ["embedding"]
        with pytest.raises(LookupError, match="ambiguous"):
            resolve_model_type(db, owner, "same-model")
        with pytest.raises(LookupError, match="not found"):
            resolve_model_type(db, uuid.uuid4().hex, "same-model@TextFixture")
        assert not db.new and not db.dirty and not db.deleted

    with Session(bootstrapped_engine) as db:
        assert len(db.scalars(select(TenantLLM).where(TenantLLM.tenant_id.in_([owner, other]))).all()) == 3


def test_local_type_overrides_catalog_and_retains_compatible_suffix(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = uuid.uuid4().hex
    monkeypatch.setattr(
        settings,
        "FACTORY_LLM_INFOS",
        [
            {"name": "OpenAI-API-Compatible", "llm": [{"llm_name": "custom-model", "mdl_type": "chat"}]},
        ],
    )
    with Session(bootstrapped_engine) as db:
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI-API-Compatible", llm_name="custom-model___OpenAI-API", mdl_type="image2text", status="1"))
        db.commit()
    with Session(bootstrapped_engine) as db:
        assert resolve_model_type(db, owner, "custom-model@OpenAI-API-Compatible") == ["image2text"]
        assert resolve_model_type(db, owner, "custom-model___OpenAI-API") == ["image2text"]
        with pytest.raises(LookupError, match="not found"):
            resolve_model_type(db, uuid.uuid4().hex, "custom-model@OpenAI-API-Compatible")


@pytest.mark.parametrize("status,model_type", [("0", "chat"), ("1", None)])
def test_unusable_registration_is_rejected(bootstrapped_engine: Engine, status: str, model_type: str | None) -> None:
    owner = uuid.uuid4().hex
    with Session(bootstrapped_engine) as db:
        db.add(TenantLLM(tenant_id=owner, llm_factory="Fixture", llm_name="unusable-model", mdl_type=model_type, status=status))
        db.commit()
    with Session(bootstrapped_engine) as db, pytest.raises(LookupError):
        resolve_model_type(db, owner, "unusable-model")
