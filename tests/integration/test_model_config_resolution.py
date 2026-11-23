"""Model resolution and credential envelopes on an owned PostgreSQL database."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import core.llm as provider_models
from api.db.db_models import LLM, LLMFactories, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from tests.support.provider_instances import http as http
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.registered_model import model_owner, register_model
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration


@pytest.fixture
def credential_owner(bootstrapped_engine: Engine) -> Iterator[str]:
    owner = uuid.uuid4().hex
    journal = ResourceJournal("provider-credential-roundtrip")
    journal.event("reserve-sql-rows", database=bootstrapped_engine.url.database, owner=owner, table=TenantModel.__tablename__)
    try:
        yield owner
    finally:
        with Session(bootstrapped_engine) as writer:
            writer.execute(delete(TenantModel).where(model_owner() == owner))
            writer.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(select(func.count()).select_from(TenantModel).where(model_owner() == owner)) == 0
        journal.event("cleanup-readback", owner=owner, remaining=0)


def test_multicapability_registration_uses_requested_cv_adapter(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    owner = uuid.uuid4().hex
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VisionFixture"}, {"name": "TextFixture"}])

    class VisionModel:
        def __init__(self, key: str, model_name: str, lang: str, **kwargs: Any) -> None:
            self.model_name = model_name

        def describe_with_prompt(self, image: bytes, prompt: str) -> tuple[str, int]:
            return f"{self.model_name}: {prompt}", 1

    monkeypatch.setattr(provider_models, "CvModel", {"VisionFixture": VisionModel})
    identifiers = {}
    with Session(bootstrapped_engine) as db:
        for factory, tags in [("VisionFixture", "LLM, IMAGE2TEXT "), ("TextFixture", "LLM")]:
            db.add(LLMFactories(name=factory, tags="LLM", status="1"))
            db.add(LLM(fid=factory, llm_name="same-name", mdl_type="chat", tags=tags, is_tools=False, max_tokens=1024))
            capabilities = ("chat", "image2text") if factory == "VisionFixture" else ("chat",)
            identifiers[factory] = register_model(
                db, tenant_id=owner, provider_name=factory, model_name="same-name", model_types=capabilities, api_key="fixture", base_url="https://fixture.invalid"
            ).id
        db.commit()

    with Session(bootstrapped_engine) as db:
        config = get_model_config_by_type_and_name(db, owner, "image2text", identifiers["VisionFixture"])
        model = TenantLLMService.model_instance(db, owner, config)
        assert model.describe_with_prompt(b"image", "describe") == ("same-name: describe", 1)
        assert config["mdl_type"] == "image2text"
        assert get_model_config_by_type_and_name(db, owner, "chat", identifiers["VisionFixture"])["mdl_type"] == "chat"
        with pytest.raises(LookupError, match="requested capability"):
            get_model_config_by_type_and_name(db, owner, "image2text", identifiers["TextFixture"])
        with pytest.raises(LookupError):
            get_model_config_by_type_and_name(db, uuid.uuid4().hex, "image2text", identifiers["VisionFixture"])

    with Session(bootstrapped_engine) as db:
        rows = db.scalars(select(TenantModel).where(model_owner() == owner)).all()
        assert len(rows) == 2
        assert {row.model_type for row in rows} == {1, 9}


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
def test_provider_credentials_survive_probe_save_and_reload(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, factory: str, credentials: dict[str, Any], edit: dict[str, Any]) -> None:
    client, owner, engine, _ = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": factory, "llm": []}])
    retained = {} if factory == "OpenRouter" else {"opaque": {"retain": True}}
    original = {**credentials, **retained}
    edited = {**original, **edit}
    prefix = f"/api/v1/providers/{factory}/instances"
    base = "https://fixture.invalid/v1"
    candidate = {
        "api_key": json.dumps(original),
        "base_url": base,
        "model_ids": [],
        "custom_model": {"model_name": "fixture-model", "model_types": ["chat"], "max_tokens": 0, "is_tools": True},
    }
    assert client.post(f"/api/v1/providers/{factory}/connection", json=candidate).status_code == 200
    response = client.post(prefix, json={**candidate, "instance_name": "fixture"})
    assert response.status_code == 200, response.text
    identifier = client.get(prefix + "/fixture/models").json()["data"]["items"][0]["id"]
    for key in (original, edited):
        if key is edited:
            changed = client.put(prefix + "/fixture", json={"api_key": json.dumps(edited)})
            assert changed.status_code == 200, changed.text
        with Session(engine) as reader:
            row = reader.get(TenantModel, identifier)
            assert row.is_tools is True and row.max_tokens == 0
            assert json.loads(reader.get(TenantModelInstance, row.instance_id).api_key) == key
            runtime = get_model_config_by_id(reader, identifier, tenant_id=owner, model_type="chat")
            assert runtime["is_tools"] is True
            assert runtime["api_base"] == base


@pytest.mark.parametrize(
    "credential",
    [
        '{"api_key":"fixture-key","provider_order":["first"],"opaque":{"retain":true}}',
        '{"api_key":"fixture-key","provider_order":false}',
        '{"api_key":"fixture-key","api_key":"second-key"}',
    ],
    ids=["unknown-field", "invalid-order", "duplicate-key"],
)
def test_invalid_openrouter_credentials_fail_before_runtime_without_rewriting_storage(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, credential_owner: str, credential: str) -> None:
    constructed: list[str] = []

    class ForbiddenModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            constructed.append("OpenRouter")
            pytest.fail("Invalid credentials reached the provider constructor")

    monkeypatch.setattr(provider_models, "ChatModel", {"OpenRouter": ForbiddenModel})
    with Session(bootstrapped_engine) as writer:
        row = register_model(
            writer, tenant_id=credential_owner, provider_name="OpenRouter", model_name="fixture-model", model_types=("chat",), api_key=credential or "", used_tokens=0, status="active"
        )
        writer.add(row)
        writer.commit()
        identifier = row.id

    with Session(bootstrapped_engine) as db:
        with pytest.raises(ValueError, match=r"^Invalid OpenRouter credential$"):
            get_model_config_by_id(db, identifier, tenant_id=credential_owner)
        with pytest.raises(ValueError, match=r"^Invalid OpenRouter credential$"):
            get_model_config_by_type_and_name(db, credential_owner, "chat", identifier)
        with pytest.raises(ValueError, match=r"^Invalid OpenRouter credential$"):
            TenantLLMService.model_instance(db, credential_owner, {"id": identifier, "tenant_id": credential_owner, "mdl_type": "chat"})
        db.commit()

    assert constructed == []
    with Session(bootstrapped_engine) as reader:
        persisted = reader.get(TenantModel, identifier)
        assert reader.get(TenantModelInstance, persisted.instance_id).api_key == credential and persisted.used_tokens == 0
        assert reader.get(TenantModelProvider, persisted.provider_id).tenant_id == credential_owner and persisted.status == "active"
        assert reader.scalar(select(func.count()).select_from(TenantModel).where(model_owner() == credential_owner)) == 1


@pytest.mark.parametrize("limit,tools", [(0, False), (32768, True)])
def test_model_record_metadata_survives_commit_all_resolvers_and_bundle(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, limit: int, tools: bool) -> None:
    from api.db.db_models import Tenant, TenantModel, TenantModelInstance, TenantModelProvider
    from api.db.joint_services.tenant_model_service import get_tenant_default_model_by_type
    from api.db.services.llm_service import LLMBundle
    from api.db.services.tenant_llm_service import TenantLangfuseService
    from tests.support.provider_instances import tenant

    owner, foreign, pid, iid = (uuid.uuid4().hex for _ in range(4))
    journal = ResourceJournal("model-record-metadata")
    journal.event("reserve-sql-rows", database=bootstrapped_engine.url.database, owner=owner, provider=pid, instance=iid)
    try:
        with Session(bootstrapped_engine) as db:
            db.add(tenant(owner))
            db.add(TenantModelProvider(id=pid, tenant_id=owner, provider_name="OpenAI", status="active"))
            db.flush()
            db.add(TenantModelInstance(id=iid, provider_id=pid, instance_name="fixture", api_key=json.dumps({"api_key": "fixture", "is_tools": not tools}), base_url="", status="active"))
            db.flush()
            model = TenantModel(provider_id=pid, instance_id=iid, model_name="callable-model", model_type=9, extra={"max_tokens": limit, "is_tools": tools})
            db.add(model)
            db.flush()
            identifier = model.id
            db.get(Tenant, owner).tenant_llm_id = identifier
            db.commit()
        monkeypatch.setattr(TenantLLMService, "model_instance", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kwargs: None)
        with Session(bootstrapped_engine) as db:
            configs = [
                get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat"),
                get_model_config_by_type_and_name(db, owner, "image2text", str(identifier)),
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
            assert reader.get(TenantModel, identifier).max_tokens == limit
            assert reader.get(TenantModel, identifier).is_tools is tools
        journal.event("independent-metadata-readback", model_id=identifier, max_tokens=limit, is_tools=tools, resolver_count=len(configs))
    finally:
        with Session(bootstrapped_engine) as writer:
            writer.execute(delete(TenantModel).where(TenantModel.instance_id == iid))
            writer.execute(delete(TenantModel).where(model_owner() == owner))
            writer.execute(delete(TenantModelInstance).where(TenantModelInstance.id == iid))
            writer.execute(delete(TenantModelProvider).where(TenantModelProvider.id == pid))
            writer.execute(delete(Tenant).where(Tenant.id == owner))
            writer.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(select(func.count()).select_from(TenantModel).where(model_owner() == owner)) == 0
            assert reader.get(TenantModelInstance, iid) is None and reader.get(TenantModelProvider, pid) is None
        journal.event("cleanup-readback", remaining=0)
