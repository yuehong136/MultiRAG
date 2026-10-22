"""Registered password reset against scratch SQL and real Redis verification state."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.apps.restful_apis.user_api import verified_key
from api.db.db_models import User
from api.db.services.user_service import UserService
from common.constants import RetCode
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import release_api as release_api


def test_password_reset_rejections_and_success_sql_readback(release_api: dict[str, Any]) -> None:
    env = release_api
    email = f"{env['owners'][0]}@release.example.com"
    key = verified_key(email)
    redis = REDIS_CONN.REDIS

    def state() -> Any:
        with Session(env["engine"]) as db:
            user = db.get(User, env["owners"][0])
            return user.to_dict(), redis.get(key)

    def post(body: dict[str, Any], *, path: str = "/auth/password/reset") -> Any:
        return env["client"].post(env["base"] + "/api/v1" + path, json=body, timeout=30)

    valid = {"email": email, "new_password": "new-plain-password", "confirm_new_password": "new-plain-password"}
    try:
        redis.set(key, "1", ex=300)
        before = state()
        for field in valid:
            for value in ["", None]:
                response = post(dict(valid, **{field: value}))
                if value == "":
                    assert response.json()["retcode"] == RetCode.ARGUMENT_ERROR
                else:
                    assert response.status_code == 422
                assert state() == before
            body = dict(valid)
            body.pop(field)
            assert post(body).status_code == 422
            assert state() == before
        response = post(dict(valid, confirm_new_password="different"))
        assert response.json()["retcode"] == RetCode.ARGUMENT_ERROR
        assert state() == before
        redis.delete(key)
        before = state()
        for password in ["new-plain-password", "invalid-rsa-ciphertext!"]:
            response = post(dict(valid, new_password=password, confirm_new_password=password))
            assert response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR
            assert state() == before
        response = post({"email": email, "otp": "INVALID"}, path="/auth/password/forgot/otp/verify")
        assert response.json()["retcode"] == RetCode.NOT_EFFECTIVE
        assert state() == before
        redis.set(key, "1", ex=300)
        response = post(valid)
        assert response.status_code == 200 and response.json()["retcode"] == 0
        with Session(env["engine"]) as db:
            password_hash = db.scalar(sa.select(User.password).where(User.id == env["owners"][0]))
        assert UserService.verify_password(valid["new_password"], password_hash)
        assert redis.get(key) is None
        before = state()
        assert post(valid).json()["retcode"] == RetCode.AUTHENTICATION_ERROR
        assert state() == before
    finally:
        redis.delete(key)
        assert not redis.exists(key)
