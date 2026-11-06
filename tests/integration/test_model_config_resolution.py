"""Model resolution and credential envelopes on an owned PostgreSQL database."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import core.llm as provider_models
from api.db.db_models import LLM, LLMFactories, TenantLLM, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.tenant_llm_service import TenantLLMService
from common import resources, settings

pytestmark = pytest.mark.integration


def test_image_fallback_reloads_chat_registration_as_cv(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = uuid.uuid4().hex
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VisionFixture"}, {"name": "TextFixture"}])

    class VisionModel:
        def __init__(self, key: str, model_name: str, lang: str, **kwargs: Any) -> None:
            self.model_name = model_name

        def describe_with_prompt(self, image: bytes, prompt: str) -> tuple[str, int]:
            return f"{self.model_name}: {prompt}", 1

    monkeypatch.setattr(provider_models, "CvModel", {"VisionFixture": VisionModel})
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


@pytest.mark.parametrize(
    ("factory", "credentials", "edit"),
    [
        ("Bedrock", {"auth_mode": "access_key", "bedrock_ak": "fixture-ak", "bedrock_sk": "fixture-sk", "bedrock_region": "us-east-1", "aws_role_arn": ""}, {"bedrock_region": "eu-west-1"}),
        (
            "Bedrock",
            {"auth_mode": "assume_role", "bedrock_ak": "", "bedrock_sk": "", "bedrock_region": "us-east-1", "aws_role_arn": "arn:aws:iam::123456789012:role/fixture"},
            {"auth_mode": "access_key", "bedrock_ak": "new-ak", "bedrock_sk": "new-sk"},
        ),
        ("Azure-OpenAI", {"api_key": "fixture-key", "api_version": "2025-01-01-preview"}, {"api_version": "2025-02-01-preview"}),
        ("OpenRouter", {"api_key": "fixture-key", "provider_order": ["first", "second"]}, {"provider_order": ["second"]}),
        ("VolcEngine", {"ark_api_key": "fixture-key", "endpoint_id": "fixture-endpoint"}, {"endpoint_id": "new-endpoint"}),
    ],
)
def test_provider_credentials_survive_probe_save_and_reload(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, factory: str, credentials: dict[str, Any], edit: dict[str, Any]) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setitem(resources._state, "secret_key", "model-config-test-signing-key")
    from api.apps import llm_app

    owner = uuid.uuid4().hex
    probed: list[dict[str, Any]] = []

    class ProbeModel:
        def __init__(self, key: str, model_name: str, **kwargs: Any) -> None:
            probed.append(json.loads(key))

        async def async_chat_streamly(self, *args: Any, **kwargs: Any) -> AsyncIterator[str | int]:
            yield "fixture answer"
            yield 1

    monkeypatch.setattr(provider_models, "ChatModel", {factory: ProbeModel})
    monkeypatch.setattr(llm_app, "get_allowed_llm_factories", lambda db: [SimpleNamespace(name=factory)])
    app = FastAPI()
    app.include_router(llm_app.router, prefix="/v1/llm")

    def session() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[llm_app.manager] = lambda: SimpleNamespace(id=owner)
    request = {"llm_factory": factory, "llm_name": "fixture-model", "mdl_type": "chat"}
    with TestClient(app) as client:
        response = client.post("/v1/llm/add_llm", json={**request, **credentials, "is_tools": True})
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        assert probed == [credentials]
        with Session(bootstrapped_engine) as db:
            row = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner)).one()
            assert json.loads(row.api_key) == {**credentials, "is_tools": True}
            # Simulate a stored field unknown to the current UI, then edit without api_key.
            row.api_key = json.dumps({**credentials, "opaque": {"retain": True}, "is_tools": True})
            db.commit()
            row_id = row.id
        response = client.post("/v1/llm/add_llm", json={**request, **edit})
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text

    expected = {**credentials, "opaque": {"retain": True}, **edit}
    assert probed[-1] == expected
    with Session(bootstrapped_engine) as db:
        row = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner)).one()
        assert row.id == row_id
        assert json.loads(row.api_key) == {**expected, "is_tools": True}
        config = get_model_config_by_id(db, row_id, tenant_id=owner)
        assert config["is_tools"] is True
        assert json.loads(config["api_key_payload"]) == expected


@pytest.mark.parametrize("limit,tools", [(0, False), (32768, True)])
def test_model_record_metadata_survives_commit_all_resolvers_and_bundle(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, limit: int, tools: bool) -> None:
    from sqlalchemy import delete, func

    from api.db.db_models import Tenant, TenantModel, TenantModelInstance, TenantModelProvider
    from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
    from api.db.services.llm_service import LLMBundle
    from api.db.services.tenant_llm_service import TenantLangfuseService
    from tests.support.provider_instances import tenant
    from tests.support.resource_journal import ResourceJournal

    owner, foreign, pid, iid = (uuid.uuid4().hex for _ in range(4))
    journal = ResourceJournal("model-record-metadata")
    journal.event("reserve-sql-rows", database=bootstrapped_engine.url.database, owner=owner, provider=pid, instance=iid)
    try:
        with Session(bootstrapped_engine) as db:
            db.add(tenant(owner))
            db.add(TenantModelProvider(id=pid, tenant_id=owner, provider_name="OpenAI", status="active"))
            db.flush()
            db.add(TenantModelInstance(id=iid, provider_id=pid, instance_name="fixture", api_key=json.dumps({"api_key": "fixture", "is_tools": not tools}), base_url="", status="active"))
            row = TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="opaque-record", mdl_type="chat", max_tokens=limit, api_key="old-key", status="1")
            db.add(row)
            db.flush()
            identifier = row.id
            db.add(TenantModel(id=identifier, instance_id=iid, model_name="callable-model", model_types=["chat", "image2text"], is_tools=tools))
            db.get(Tenant, owner).tenant_llm_id = identifier
            db.commit()
        monkeypatch.setattr(TenantLLMService, "model_instance", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kwargs: None)
        with Session(bootstrapped_engine) as db:
            configs = [
                get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat"),
                get_model_config_by_type_and_name(db, owner, "image2text", str(identifier)),
                get_model_config_by_type_and_name(db, owner, "chat", "callable-model@fixture@OpenAI"),
                get_tenant_default_model_by_type(db, owner, "chat"),
            ]
            for config in configs:
                assert config["max_tokens"] == limit and config["is_tools"] is tools
                assert config["tenant_id"] == owner and config["api_key"] == "fixture"
                bundle = LLMBundle(db, owner, config)
                assert bundle.max_length == limit and bundle.is_tools is tools
            with pytest.raises(LookupError):
                get_model_config_by_id(db, identifier, tenant_id=foreign, model_type="chat")
            with pytest.raises(LookupError):
                get_model_config_by_id(db, identifier, allowed_tenant_ids=set(), model_type="chat")
            with pytest.raises(LookupError, match="capability is required"):
                get_model_config_by_id(db, identifier, tenant_id=owner)
        with Session(bootstrapped_engine) as reader:
            assert reader.get(TenantLLM, identifier).max_tokens == limit
            assert reader.get(TenantModel, identifier).is_tools is tools
        journal.event("independent-metadata-readback", model_id=identifier, max_tokens=limit, is_tools=tools, resolver_count=len(configs))
    finally:
        with Session(bootstrapped_engine) as writer:
            writer.execute(delete(TenantModel).where(TenantModel.instance_id == iid))
            writer.execute(delete(TenantLLM).where(TenantLLM.tenant_id == owner))
            writer.execute(delete(TenantModelInstance).where(TenantModelInstance.id == iid))
            writer.execute(delete(TenantModelProvider).where(TenantModelProvider.id == pid))
            writer.execute(delete(Tenant).where(Tenant.id == owner))
            writer.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(select(func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == owner)) == 0
            assert reader.get(TenantModelInstance, iid) is None and reader.get(TenantModelProvider, pid) is None
        journal.event("cleanup-readback", remaining=0)
