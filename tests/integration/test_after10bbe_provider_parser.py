"""SQL proof for configured defaults, env OCR and complete parser identities."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import LLM, File, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, User, UserTenant
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_ocr_model_reference, resolve_parser_reference
from api.db.services.llm_service import initialize_tenant_models
from api.db.services.model_provider_service import catalog, models_for_owner
from api.db.services.tenant_llm_service import TenantLLMService
from common import resources, settings
from common.constants import MINERU_ENV_KEYS, OPENDATALOADER_ENV_KEYS, PADDLEOCR_ENV_KEYS
from common.parser_config_utils import normalize_layout_recognizer
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.provider_instances import tenant

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(resources._state, "secret_key", "test-only-provider-init-key")
    for key in [*MINERU_ENV_KEYS, *PADDLEOCR_ENV_KEYS, *OPENDATALOADER_ENV_KEYS]:
        monkeypatch.delenv(key, raising=False)
    for name in ("CHAT_CFG", "EMBEDDING_CFG", "ASR_CFG", "IMAGE2TEXT_CFG", "RERANK_CFG"):
        monkeypatch.setattr(settings, name, {"model": "", "factory": "OpenAI", "api_key": "", "base_url": ""})
    for name in ("CHAT_MDL", "EMBEDDING_MDL", "ASR_MDL", "IMAGE2TEXT_MDL", "RERANK_MDL"):
        monkeypatch.setattr(settings, name, "")


def counts(engine: Engine, owner: str) -> list[int]:
    with Session(engine) as reader:
        return [
            reader.scalar(select(func.count()).select_from(model).where(getattr(model, field) == owner))
            for model, field in ((User, "id"), (Tenant, "id"), (UserTenant, "tenant_id"), (File, "tenant_id"), (TenantLLM, "tenant_id"), (TenantModelProvider, "tenant_id"))
        ]


def test_actual_catalog_scalar_projection_and_atomic_registration(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.restful_apis import user_api
    from api.db import init_data

    monkeypatch.setattr(
        settings,
        "FACTORY_LLM_INFOS",
        [
            {
                "name": "OpenAI",
                "tags": "LLM",
                "llm": [
                    {"llm_name": "configured", "mdl_type": ["chat", "image2text"], "max_tokens": 0, "tags": "LLM", "is_tools": False},
                    {"llm_name": "unselected", "mdl_type": "chat", "max_tokens": 8192, "tags": "LLM"},
                ],
            }
        ],
    )
    configured = {"model": "configured@OpenAI", "factory": "OpenAI", "api_key": "fixture-key", "base_url": "http://fixture.invalid/v1"}
    for name in ("CHAT_CFG", "IMAGE2TEXT_CFG"):
        monkeypatch.setattr(settings, name, configured)
    for name in ("CHAT_MDL", "IMAGE2TEXT_MDL"):
        monkeypatch.setattr(settings, name, configured["model"])
    with Session(owned_engine) as db:
        init_data.init_llm_factory(db)
    with Session(owned_engine) as reader:
        legacy = reader.get(LLM, ("OpenAI", "configured"))
        assert legacy.mdl_type == "chat" and legacy.max_tokens == 0 and "IMAGE2TEXT" in legacy.tags
    owner = uuid4().hex
    account = {"nickname": "Configured", "email": owner + "@example.test", "password": "fixture-password"}
    with Session(owned_engine) as db:
        created = user_api.user_register(db, owner, account)
        assert len(created) == 1
    assert counts(owned_engine, owner) == [1, 1, 1, 1, 1, 1]
    with Session(owned_engine) as reader:
        row = reader.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner)).one()
        model = reader.get(TenantModel, row.id)
        stored = reader.get(Tenant, owner)
        assert model.model_types == ["chat", "image2text"] and model.is_tools is False and row.max_tokens == 0
        assert stored.tenant_llm_id == stored.tenant_img2txt_id == row.id
        assert get_model_config_by_type_and_name(reader, owner, "image2text", str(row.id))["mdl_type"] == "image2text"
        initialize_tenant_models(reader, owner)
        reader.commit()
    assert counts(owned_engine, owner) == [1, 1, 1, 1, 1, 1]
    duplicate = uuid4().hex
    with Session(owned_engine) as db, pytest.raises(HTTPException):
        user_api.user_register(db, duplicate, account)
    assert counts(owned_engine, duplicate) == [0] * 6

    from api.db import model_backfill

    original = model_backfill.backfill

    def fail_after_link(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("controlled-after-link-failure")

    monkeypatch.setattr(model_backfill, "backfill", fail_after_link)
    failed = uuid4().hex
    with Session(owned_engine) as db, pytest.raises(HTTPException):
        user_api.user_register(db, failed, {**account, "email": failed + "@example.test"})
    assert counts(owned_engine, failed) == [0] * 6
    assert counts(owned_engine, owner) == [1, 1, 1, 1, 1, 1]


def test_catalog_replacement_failure_keeps_committed_catalog(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.init_data import init_llm_factory

    with Session(owned_engine) as db:
        db.add(LLM(fid="preserved", llm_name="previous", mdl_type="chat", max_tokens=0, tags="LLM"))
        db.commit()
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "OpenAI", "tags": "LLM", "llm": [{"llm_name": "bad", "mdl_type": [], "max_tokens": 0, "tags": "LLM"}]}])
    with Session(owned_engine) as db, pytest.raises(ValueError):
        init_llm_factory(db)
    with Session(owned_engine) as reader:
        assert reader.get(LLM, ("preserved", "previous")) is not None
        assert reader.get(LLM, ("OpenAI", "bad")) is None


@pytest.mark.parametrize("provider,environment", [("MinerU", "MINERU_SERVER_URL"), ("PaddleOCR", "PADDLEOCR_API_URL"), ("OpenDataLoader", "OPENDATALOADER_APISERVER")])
def test_env_import_is_modern_visible_idempotent_and_rollback_safe(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch, provider: str, environment: str) -> None:
    monkeypatch.setenv(environment, "http://127.0.0.1:39411/controlled")
    owner, aborted = uuid4().hex, uuid4().hex
    with Session(owned_engine) as db:
        db.add_all([tenant(owner), tenant(aborted)])
        db.commit()
    ensure = {"MinerU": TenantLLMService.ensure_mineru_from_env, "PaddleOCR": TenantLLMService.ensure_paddleocr_from_env, "OpenDataLoader": TenantLLMService.ensure_opendataloader_from_env}[provider]
    with Session(owned_engine) as db:
        reference = ensure(db, owner)
        assert reference is not None and reference.isdecimal()
        assert ensure(db, owner) == reference
        db.commit()
    with Session(owned_engine) as reader:
        model = reader.get(TenantModel, int(reference))
        instance = reader.get(TenantModelInstance, model.instance_id)
        assert model.model_types == ["ocr"] and reader.get(TenantLLM, int(reference)).max_tokens == 0
        composite = f"{model.model_name}@{instance.instance_name}@{provider}"
        assert normalize_layout_recognizer(composite) == (provider, composite)
        assert resolve_parser_reference(reader, owner, reference) == (provider, reference)
        assert get_model_config_by_type_and_name(reader, owner, "ocr", composite)["instance_id"] == instance.id
        with pytest.raises(LookupError):
            get_model_config_by_type_and_name(reader, aborted, "ocr", composite)
        assert get_ocr_model_reference(reader, owner, provider) == reference
        instance.status = "inactive"
        reader.commit()
    with Session(owned_engine) as reader:
        assert get_ocr_model_reference(reader, owner, provider) is None
        assert reader.scalar(select(func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == owner)) == 1
        with pytest.raises(LookupError, match="disabled"):
            get_model_config_by_type_and_name(reader, owner, "ocr", reference)
    with Session(owned_engine) as db:
        assert ensure(db, aborted) is not None
        db.rollback()
    assert counts(owned_engine, aborted)[4:] == [0, 0]

    async def modern_readback() -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(owned_engine.url)
        try:
            async with async_sessionmaker(engine)() as session:
                models = await models_for_owner(session, owner)
                assert len(models) == 1 and models[0][0].id == int(reference)
        finally:
            await engine.dispose()

    asyncio.run(modern_readback())


def test_all_target_catalog_models_have_valid_typed_capabilities() -> None:
    from common.app_config import get_factory_llm_infos

    for factory in get_factory_llm_infos():
        for model in catalog(factory["name"]):
            assert isinstance(model.model_types, list) and len(model.model_types) == len(set(model.model_types))
            assert type(model.max_tokens) is int and model.max_tokens >= 0
    assert "chat" not in next(model for model in catalog("Mistral") if model.model_name == "mistral-ocr-latest").model_types


def test_startup_and_admin_registration_keep_env_identity_atomic(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from admin.server.auth import add_tenant_for_admin
    from api.db import model_backfill
    from api.db.init_data import init_superuser
    from api.db.joint_services.user_account_service import create_new_user
    from api.db.services.user_service import UserService

    monkeypatch.setenv("PADDLEOCR_API_URL", "http://127.0.0.1:39412/controlled")
    email = uuid4().hex + "@example.test"
    with Session(owned_engine) as db:
        init_superuser(db, nickname="Startup fixture", email=email, password="fixture-password")
        owner = db.scalar(select(User.id).where(User.email == email))
        init_superuser(db, nickname="Startup fixture", email=email, password="fixture-password")
    assert counts(owned_engine, owner) == [1, 1, 1, 0, 1, 1]
    admin_owner = uuid4().hex
    with Session(owned_engine) as db:
        UserService.save(db, id=admin_owner, email=admin_owner + "@example.test", nickname="Admin fixture", password="fixture-password")
        add_tenant_for_admin(db, {"id": admin_owner, "nickname": "Admin fixture"}, "owner")
    assert counts(owned_engine, admin_owner) == [1, 1, 1, 0, 1, 1]
    with Session(owned_engine) as db:
        result = create_new_user(db, {"nickname": "Account fixture", "email": uuid4().hex + "@example.test", "password": "fixture-password"})
        account_owner = result["user_info"]["id"]
    assert counts(owned_engine, account_owner) == [1, 1, 1, 1, 1, 1]
    with Session(owned_engine) as db:
        TenantLLMService.delete_by_tenant_id(db, account_owner)
    assert counts(owned_engine, account_owner) == [1, 1, 1, 1, 0, 0]
    with Session(owned_engine) as reader:
        assert reader.scalar(select(func.count()).select_from(TenantModel)) == 2
        assert reader.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
    original = model_backfill.backfill

    def fail_after_link(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("controlled startup linking failure")

    monkeypatch.setattr(model_backfill, "backfill", fail_after_link)
    failed_email = uuid4().hex + "@example.test"
    with Session(owned_engine) as db, pytest.raises(RuntimeError):
        init_superuser(db, nickname="Failed fixture", email=failed_email, password="fixture-password")
    with Session(owned_engine) as reader:
        assert reader.scalar(select(User).where(User.email == failed_email)) is None
        assert reader.scalar(select(func.count()).select_from(TenantModel)) == 2


@pytest.mark.parametrize("provider", ["MinerU", "PaddleOCR", "OpenDataLoader"])
def test_naive_and_pipeline_keep_complete_instance_reference(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager
    from types import SimpleNamespace

    from core.app import naive
    from core.flow.parser import parser as pipeline

    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        parent = TenantModelProvider(id=uuid4().hex, tenant_id=owner, provider_name=provider, status="active")
        db.add(parent)
        db.flush()
        instances = [TenantModelInstance(id=uuid4().hex, provider_id=parent.id, instance_name=name, api_key="fixture-" + name, base_url="", region="", status="active") for name in ("first", "second")]
        db.add_all(instances)
        db.flush()
        ids = []
        for instance in instances:
            row = TenantLLM(tenant_id=owner, llm_factory=provider, llm_name="model:" + uuid4().hex, mdl_type="ocr", api_key=instance.api_key, api_base="", max_tokens=0)
            db.add(row)
            db.flush()
            db.add(TenantModel(id=row.id, instance_id=instance.id, model_name="shared-name", model_types=["ocr"], is_tools=False))
            ids.append(row.id)
        db.commit()

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(owned_engine) as db:
            yield db

    class ControlledParser:
        outlines: list[Any] = []

        def __init__(self, identifier: int) -> None:
            self.identifier = identifier

        def parse_pdf(self, *args: Any, **kwargs: Any) -> tuple[list[tuple[str, str, str]], list[Any]]:
            return [(str(self.identifier), "text", "")], []

        def extract_positions(self, *args: Any) -> list[Any]:
            return []

        def crop(self, *args: Any) -> None:
            return None

    def bundle(_db: Session, _owner: str, config: dict[str, Any], **kwargs: Any) -> Any:
        return SimpleNamespace(mdl=ControlledParser(config["id"]))

    for module in (naive, pipeline):
        monkeypatch.setattr(module, "db_connection", connection)
        monkeypatch.setattr(module, "LLMBundle", bundle)
    outputs: dict[str, Any] = {}
    config = {"parse_method": f"shared-name@second@{provider}", "output_format": "json", "preprocess": []}
    process = SimpleNamespace(
        _param=SimpleNamespace(setups={"pdf": config}), _canvas=SimpleNamespace(_tenant_id=owner), callback=lambda *args: None, set_output=lambda key, value: outputs.__setitem__(key, value)
    )
    pipeline.Parser._pdf(process, "fixture.pdf", b"controlled-pdf")
    assert outputs["json"][0]["text"] == str(ids[1])
    method = getattr(naive, "by_" + provider.lower())
    options = {provider.lower() + "_llm_name": config["parse_method"]}
    sections, _, _ = method("fixture.pdf", binary=b"controlled-pdf", tenant_id=owner, **options)
    assert sections[0][0] == str(ids[1])


def test_default_admin_first_start_model_failure_rolls_back_user(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from admin.server.auth import init_default_admin
    from api.db import model_backfill

    monkeypatch.setenv("PADDLEOCR_API_URL", "http://127.0.0.1:39413/controlled")
    original = model_backfill.backfill
    calls = 0

    def fail_after_env_link(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("controlled first-admin post-env failure")

    with monkeypatch.context() as patch:
        patch.setattr(model_backfill, "backfill", fail_after_env_link)
        with Session(owned_engine) as db, pytest.raises(RuntimeError, match="post-env"):
            init_default_admin(db)
    assert calls == 2
    with Session(owned_engine) as reader:
        assert all(reader.scalar(select(func.count()).select_from(model)) == 0 for model in (User, Tenant, UserTenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider))
    with Session(owned_engine) as db:
        init_default_admin(db)
        init_default_admin(db)
    with Session(owned_engine) as reader:
        user = reader.scalars(select(User)).one()
        assert user.email == "admin@datav.com" and user.is_superuser is True
        assert reader.get(Tenant, user.id) is not None
        assert reader.scalars(select(UserTenant)).one().role == "owner"
        assert reader.scalars(select(TenantLLM)).one().max_tokens == 0
        assert reader.scalars(select(TenantModel)).one().model_types == ["ocr"]
