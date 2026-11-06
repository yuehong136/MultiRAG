"""Real JWT/profile/registration HTTP with independent PostgreSQL readback."""

import sys
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import File, Tenant, TenantLLM, TenantModelProvider, User, UserTenant
from api.utils.crypt import crypt
from common import settings
from common.constants import MINERU_ENV_KEYS, OPENDATALOADER_ENV_KEYS, PADDLEOCR_ENV_KEYS
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
            for model in (TenantLLM, TenantModelProvider):
                assert reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == registration_id)) == 0
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
