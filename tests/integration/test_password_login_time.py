"""Password login commits its timestamp only after real authentication succeeds."""

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from api.db import UserAccountKind
from api.db.db_models import User, get_async_db
from api.db.services.user_service import UserService
from api.utils.crypt import crypt
from common import resources, settings
from common.constants import RetCode
from tests.support.resource_journal import ResourceJournal


def login_state(engine: sa.Engine, user_id: str) -> dict[str, Any]:
    with Session(engine) as reader:
        user = reader.get(User, user_id)
        assert user is not None
        return {name: getattr(user, name) for name in ("last_login_time", "access_token", "update_time", "update_date")}


@pytest.fixture
async def password_login(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, sa.Engine, str, ResourceJournal]]:
    # This test owns a router and SQL session. Other application resources are
    # never needed; supply the signer before importing the application package.
    monkeypatch.setattr(settings, "SECRET_KEY", "password-login-test-only-signing-key")
    from api.apps.restful_apis import user_api

    monkeypatch.setattr(settings, "DISABLE_PASSWORD_LOGIN", False)
    journal = ResourceJournal("password-login-timestamp")
    user_id = uuid4().hex
    journal.event("reserve-login-user", database=bootstrapped_engine.url.database, table=User.__table__.fullname, user_id=user_id)
    sessions = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)

    async def request_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/v1")
    app.dependency_overrides[get_async_db] = request_db
    before_resources = dict(resources._state)
    try:
        with Session(bootstrapped_engine) as seed:
            seed.add(
                User(
                    id=user_id,
                    email=user_id + "@login.example.test",
                    nickname="Login timestamp",
                    password=UserService.hash_password("correct-password"),
                    account_kind=UserAccountKind.LOCAL.value,
                    is_active=True,
                    status="1",
                    access_token="old-test-access-token",
                    last_login_time=datetime(2020, 1, 2, 3, 4, 5),
                    update_time=123,
                    update_date=datetime(2020, 1, 2, 3, 4, 5),
                )
            )
            seed.commit()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://login.example.test") as client:
            yield client, bootstrapped_engine, user_id, journal
    finally:
        app.dependency_overrides.clear()
        with Session(bootstrapped_engine) as cleanup:
            cleanup.execute(sa.delete(User).where(User.id == user_id))
            cleanup.commit()
        with Session(bootstrapped_engine) as reader:
            absent = reader.get(User, user_id) is None
        journal.event("login-user-cleanup-readback", database=bootstrapped_engine.url.database, user_id=user_id, absent=absent)
        assert absent
        assert resources._state == before_resources


async def test_successful_password_login_persists_time_and_rotates_token(password_login: tuple[httpx.AsyncClient, sa.Engine, str, ResourceJournal], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.restful_apis import user_api

    client, engine, user_id, journal = password_login
    before = login_state(engine, user_id)
    expected = datetime(2026, 6, 15, 10, 6, 24)
    monkeypatch.setattr(user_api, "get_format_time", lambda: expected)
    journal.event("reserve-login-update", user_id=user_id, expected="successful-authentication")
    response = await client.post("/api/v1/auth/login", json={"username": user_id + "@login.example.test", "password": crypt("correct-password")})
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.headers.get("authorization")
    after = login_state(engine, user_id)
    assert after["last_login_time"] == expected
    assert after["access_token"] != before["access_token"]
    assert after["update_time"] > before["update_time"]
    journal.event("login-sql-readback", user_id=user_id, successful=True, timestamp_persisted=True, token_rotated=True)


@pytest.mark.parametrize("failure", ["password", "ciphertext", "disabled-account", "disabled-login", "missing-user", "commit"])
async def test_failed_password_login_leaves_all_login_fields_unchanged(
    password_login: tuple[httpx.AsyncClient, sa.Engine, str, ResourceJournal], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    client, engine, user_id, journal = password_login
    username = user_id + "@login.example.test"
    password = crypt("correct-password")
    expected_code = RetCode.FORBIDDEN
    if failure == "password":
        password, expected_code = crypt("wrong-password"), RetCode.AUTHENTICATION_ERROR
    elif failure == "ciphertext":
        password, expected_code = "invalid-ciphertext", RetCode.SERVER_ERROR
    elif failure == "missing-user":
        username, expected_code = "missing-" + username, RetCode.AUTHENTICATION_ERROR
    elif failure == "disabled-account":
        journal.event("reserve-login-account-disable", user_id=user_id)
        with engine.begin() as writer:
            writer.execute(sa.update(User).where(User.id == user_id).values(is_active=False))
    elif failure == "disabled-login":
        monkeypatch.setattr(settings, "DISABLE_PASSWORD_LOGIN", True)
    elif failure == "commit":

        async def rejected_commit(self: AsyncSession) -> None:
            await self.flush()
            raise RuntimeError("controlled failure after UPDATE, before COMMIT")

        monkeypatch.setattr(AsyncSession, "commit", rejected_commit)
    before = login_state(engine, user_id)
    journal.event("reserve-login-update", user_id=user_id, expected=failure)
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if failure == "commit":
        assert response.status_code == 500
    else:
        assert response.status_code == 200 and response.json()["retcode"] == expected_code, response.text
    assert "authorization" not in response.headers
    assert login_state(engine, user_id) == before
    journal.event("login-sql-readback", user_id=user_id, failure=failure, login_fields_unchanged=True)
