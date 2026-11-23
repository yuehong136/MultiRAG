"""Real JWT/profile/registration HTTP with independent PostgreSQL readback."""

import importlib
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import File, Tenant, TenantModel, TenantModelInstance, TenantModelProvider, User, UserTenant
from api.db.model_contract import ModelRegistration, parse_model_id
from api.db.services import provider_verification
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.crypt import crypt
from common import settings
from common.constants import MINERU_ENV_KEYS, OPENDATALOADER_ENV_KEYS, PADDLEOCR_ENV_KEYS, RetCode
from tests.support.registered_model import register_model
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def profile_state(env: dict[str, Any]) -> dict[str, Any]:
    with Session(env["engine"]) as reader:
        user = reader.get(User, env["owners"][0])
        assert user is not None
        return {key: getattr(user, key) for key in ("nickname", "timezone", "password", "access_token", "update_time")}


def test_profile_and_registration_nickname_http(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = runtime_upload_api
    base = env["base"] + "/api/v1"
    headers = {"Authorization": "Bearer " + env["jwt"]}
    journal = env["journal"]
    user_api = sys.modules["api.apps.restful_apis.user"]
    monkeypatch.setattr(settings, "REGISTER_ENABLED", 1)
    for name in ("CHAT_CFG", "EMBEDDING_CFG", "ASR_CFG", "IMAGE2TEXT_CFG", "RERANK_CFG"):
        monkeypatch.setattr(settings, name, {"model": "", "factory": "OpenAI", "api_key": "", "base_url": ""})
    for name in ("CHAT_MDL", "EMBEDDING_MDL", "ASR_MDL", "IMAGE2TEXT_MDL", "RERANK_MDL"):
        monkeypatch.setattr(settings, name, "")
    for key in [*MINERU_ENV_KEYS, *PADDLEOCR_ENV_KEYS, *OPENDATALOADER_ENV_KEYS]:
        monkeypatch.delenv(key, raising=False)
    writes: list[str] = []

    def observe(conn: sa.Connection, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if conn.engine.url.database != env["engine"].url.database:
            return
        if getattr(context, "isinsert", False):
            table = context.compiled.statement.table
            for values in context.compiled_parameters:
                journal.event("reserve-profile-sql-row", database=conn.engine.url.database, table=table.fullname, id=str(values.get("id", "")))
        if statement.lstrip().split(" ", 1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    sa.event.listen(sa.Engine, "before_cursor_execute", observe)
    registration_id = uuid4().hex
    journal.event("reserve-profile-registration", database=env["engine"].url.database, user_id=registration_id)
    identifiers = iter([uuid4().hex, registration_id])
    monkeypatch.setattr(user_api, "get_uuid", lambda: next(identifiers))
    email = registration_id + "@nickname.example.test"
    try:
        unauthorized = requests.patch(base + "/users/me", json={"nickname": "Valid"}, timeout=15)
        assert unauthorized.status_code == 401
        for name in ["\ufeff 张三 한국어 \ufeff", "𐐀" * 100]:
            response = requests.patch(base + "/users/me", headers=headers, json={"nickname": name}, timeout=15)
            assert response.status_code == 200 and response.json()["data"] is True, response.text
            expected = name.strip("\ufeff ")
            assert profile_state(env)["nickname"] == expected
            assert requests.get(base + "/users/me", headers=headers, timeout=15).json()["data"]["nickname"] == expected
        before = profile_state(env)
        response = requests.patch(base + "/users/me", headers=headers, json={"timezone": "Asia/Shanghai"}, timeout=15)
        assert response.json()["data"] is True
        assert profile_state(env)["nickname"] == before["nickname"]
        before = profile_state(env)
        for name in [None, "", " ", "bad!", "a" * 101, "𐐀" * 101, "John\tDoe", "Jose\u0301", 123]:
            for method, suffix, body in [
                ("PATCH", "/users/me", {"nickname": name, "password": "bad", "new_password": "bad"}),
                ("POST", "/users", {"nickname": name, "email": email, "password": "bad"}),
            ]:
                writes.clear()
                response = requests.request(method, base + suffix, headers=headers, json=body, timeout=15)
                assert response.status_code == 422, response.text
                assert writes == []
                assert profile_state(env) == before
                with Session(env["engine"]) as reader:
                    assert reader.get(User, registration_id) is None
        registered = requests.post(base + "/users", json={"email": email, "nickname": "\ufeff 新用户 한국어 \ufeff", "password": crypt("test-password")}, timeout=15)
        assert registered.status_code == 200 and registered.json()["retcode"] == 0, registered.text
        with Session(env["engine"]) as reader:
            user = reader.get(User, registration_id)
            assert user is not None and user.nickname == "新用户 한국어"
            assert reader.get(Tenant, registration_id) is not None
            assert reader.scalar(sa.select(sa.func.count()).select_from(File).where(File.tenant_id == registration_id)) == 1
            assert reader.scalar(sa.select(sa.func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id == registration_id)) == 0
        journal.event("profile-http-readback", user_id=registration_id, nickname_trimmed=True, invalid_dml=0, unicode_100=True)
    finally:
        sa.event.remove(sa.Engine, "before_cursor_execute", observe)
        with Session(env["engine"]) as db:
            for model, column in [(File, File.tenant_id), (UserTenant, UserTenant.user_id), (Tenant, Tenant.id), (User, User.id)]:
                db.execute(sa.delete(model).where(column == registration_id))
            db.commit()
        with Session(env["engine"]) as reader:
            remaining = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(column == registration_id))
                for model, column in [(File, File.tenant_id), (UserTenant, UserTenant.user_id), (Tenant, Tenant.id), (User, User.id)]
            }
        journal.event("profile-registration-cleanup-readback", database=env["engine"].url.database, remaining=remaining)
        assert not any(remaining.values()), remaining


def test_model_default_patch_uses_owned_model_ids_and_rolls_back(runtime_upload_api: dict[str, Any]) -> None:
    env = runtime_upload_api
    owner, foreign_owner = env["owners"]
    url = env["base"] + "/api/v1/users/me/models"
    headers = {"Authorization": "Bearer " + env["jwt"]}

    def patch(values: dict[str, str]) -> requests.Response:
        return requests.patch(url, headers=headers, json={"tenant_id": owner, **values}, timeout=15)

    def defaults() -> tuple[str, str | None, str, str | None]:
        with Session(env["engine"]) as db:
            row = db.get(Tenant, owner)
            assert row is not None
            return row.llm_id, row.tenant_llm_id, row.embd_id, row.tenant_embd_id

    try:
        with Session(env["engine"]) as db:
            first = register_model(db, tenant_id=owner, provider_name="OpenAI", model_name="first")
            next_model = register_model(db, tenant_id=owner, provider_name="OpenAI", model_name="next")
            embed = register_model(db, tenant_id=owner, provider_name="OpenAI", model_name="embed", model_types=("embedding",))
            foreign_embed = register_model(db, tenant_id=foreign_owner, provider_name="OpenAI", model_name="embed", model_types=("embedding",))
            first_id, next_id, embed_id, foreign_id = first.id, next_model.id, embed.id, foreign_embed.id
            db.commit()

        assert patch({"llm_id": first_id, "embd_id": embed_id}).json()["retcode"] == RetCode.SUCCESS
        assert defaults() == (first_id, first_id, embed_id, embed_id)
        assert patch({"tenant_id": foreign_owner, "embd_id": foreign_id}).json()["retcode"] == RetCode.FORBIDDEN
        assert patch({"llm_id": "obsolete-model-name"}).status_code == 422
        assert patch({"llm_id": next_id, "embd_id": foreign_id}).json()["retcode"] == RetCode.ARGUMENT_ERROR
        assert defaults() == (first_id, first_id, embed_id, embed_id)
        assert patch({"llm_id": next_id}).json()["retcode"] == RetCode.SUCCESS
        assert defaults() == (next_id, next_id, embed_id, embed_id)
        with Session(env["engine"]) as reader:
            assert reader.get(Tenant, foreign_owner).tenant_embd_id is None
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Tenant).where(Tenant.id.in_([owner, foreign_owner])).values(llm_id="", tenant_llm_id=None, embd_id="", tenant_embd_id=None))
            for identifier in (owner, foreign_owner):
                TenantLLMService.delete_by_tenant_id(db, identifier)
            db.commit()
        with Session(env["engine"]) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id.in_([owner, foreign_owner]))) == 0


def test_cli_uses_model_registry_over_authenticated_http(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = runtime_upload_api
    owner = env["owners"][0]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    cli_module = importlib.import_module("multirag_client")
    transport_module = importlib.import_module("http_client")
    endpoint = urlsplit(env["base"])
    transport = transport_module.HttpClient(host=endpoint.hostname, port=endpoint.port)
    transport.login_token = "Bearer " + env["jwt"]
    cli = cli_module.MultiRAGClient(transport, "user")
    tables: list[Any] = []
    cli._print_table_simple = tables.append

    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "OpenAI", "llm": [{"llm_name": "controlled-chat", "mdl_type": "chat"}]}])

    async def probe(provider: str, api_key: str, base_url: str, declaration: ModelRegistration) -> dict[str, Any]:
        assert (provider, api_key, declaration.model_name) == ("OpenAI", "fixture-key", "controlled-chat")
        return {"success": True}

    monkeypatch.setattr(provider_verification, "verify_model", probe)
    try:
        cli.create_model_provider({"provider_name": "OpenAI", "provider_key": "fixture-key"})
        cli.list_user_model_providers({})
        model_id = tables[-1][0]["model_id"]
        assert parse_model_id(model_id) == model_id
        cli.set_default_model({"model_type": "llm_id", "model_id": model_id})
        cli.list_user_default_models({})
        assert tables[-1][0]["model_id"] == model_id
        cli.drop_model_provider({"provider_name": "OpenAI"})
        with Session(env["engine"]) as reader:
            model = reader.get(TenantModel, model_id)
            assert model is not None
            assert reader.get(TenantModelInstance, model.instance_id).instance_name == "default"
            assert reader.get(TenantModelProvider, model.provider_id).tenant_id == owner
            assert reader.get(Tenant, owner).tenant_llm_id == model_id
            identities = model.id, model.instance_id, model.provider_id
        cli.reset_default_model({"model_type": "llm_id"})
        cli.drop_model_provider({"provider_name": "OpenAI"})
        with Session(env["engine"]) as reader:
            assert reader.get(Tenant, owner).tenant_llm_id is None
            for table, identifier in zip((TenantModel, TenantModelInstance, TenantModelProvider), identities, strict=True):
                assert reader.get(table, identifier) is None
        env["journal"].event("cli-model-registry-readback", owned_model=model_id, blocked_delete_verified=True, registry_cleanup_verified=True)
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.update(Tenant).where(Tenant.id == owner).values(llm_id="", tenant_llm_id=None))
            TenantLLMService.delete_by_tenant_id(db, owner)
            db.commit()
