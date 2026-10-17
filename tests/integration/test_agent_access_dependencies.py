"""Real HTTP authorization, path binding and denial side effects for Agent APIs."""

from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db import UserTenantRole
from api.db.db_models import API4Conversation
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import denied_membership, join_member, message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api


def create_resources(env: dict[str, Any]) -> tuple[str, str, str, str]:
    result = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": f"Access {uuid4().hex}", "dsl": message_dsl("access")}, timeout=30).json()
    assert result["retcode"] == 0, result
    agent_id = result["data"]["id"]
    update(env, agent_id, {"release": True, "permission": "team"})
    result = env["client"].post(f"{env['base']}/api/v1/agents/{agent_id}/sessions", json={"release": True}, timeout=30).json()
    assert result["retcode"] == 0, result
    version_id = read_state(env, agent_id)["versions"][0]["id"]
    message_id = uuid4().hex
    env["task_ids"].append(message_id)
    assert REDIS_CONN.set_obj(f"{agent_id}-{message_id}-logs", {"steps": ["authorized"]})
    return agent_id, result["data"]["id"], version_id, message_id


def conversations(env: dict[str, Any], agent_id: str) -> list[dict[str, Any]]:
    with Session(env["engine"]) as db:
        return [row.to_dict() for row in db.scalars(sa.select(API4Conversation).where(API4Conversation.dialog_id == agent_id).order_by(API4Conversation.id))]


@pytest.mark.parametrize("credential", ["jwts", "keys"])
@pytest.mark.parametrize("caller", ["owner", "normal", "admin", "invite", "inactive_member", "inactive_admin", "foreign", "private", "missing"])
def test_http_access_dependency_matrix(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, credential: str, caller: str) -> None:
    env = release_api
    agent_id, session_id, version_id, message_id = create_resources(env)
    if caller in {"normal", "admin", "private"}:
        join_member(env, monkeypatch, 1, UserTenantRole.ADMIN if caller == "admin" else UserTenantRole.NORMAL)
        index = 1
    elif caller in {"invite", "inactive_member", "inactive_admin"}:
        denied_membership(env, monkeypatch, caller)
        index = 2
    else:
        index = 0 if caller in {"owner", "missing"} else 2
    if caller == "private":
        update(env, agent_id, {"permission": "me"})
    before = read_state(env, agent_id), conversations(env, agent_id)
    log_key = f"{agent_id}-{message_id}-logs"
    log_before = REDIS_CONN.get(log_key)
    requested = uuid4().hex if caller == "missing" else agent_id
    prefix = f"{env['base']}/api/v1/agents/{requested}"
    headers = {"Authorization": f"Bearer {env[credential][index]}"}
    allowed = caller in {"owner", "normal", "admin"}
    for suffix in ["", "/sessions", f"/sessions/{session_id}", "/versions", f"/versions/{version_id}", f"/logs/{message_id}"]:
        response = env["client"].get(prefix + suffix, headers=headers, params={"user_id": env["owners"][0], "tenant_id": env["owners"][0]}, timeout=30)
        assert response.status_code == 200, response.text
        assert (response.json()["retcode"] == 0) is allowed, response.text
        if allowed and suffix.startswith("/logs/"):
            assert response.json()["data"] == {"steps": ["authorized"]}
        assert (read_state(env, agent_id), conversations(env, agent_id)) == before
        assert REDIS_CONN.get(log_key) == log_before
    response = env["client"].delete(prefix + f"/sessions/{session_id}", headers=headers, timeout=30)
    assert (response.json()["retcode"] == 0) is allowed, response.text
    if allowed:
        assert type(response.json()["data"]) is int and response.json()["data"] == 1
    assert read_state(env, agent_id) == before[0]
    assert conversations(env, agent_id) == ([] if allowed else before[1])
    assert REDIS_CONN.get(log_key) == log_before


@pytest.mark.parametrize("credential", ["jwts", "keys"])
def test_nested_resources_are_bound_to_path_agent(release_api: dict[str, Any], credential: str) -> None:
    env = release_api
    agent_id, session_id, version_id, _ = create_resources(env)
    other, _, _, _ = create_resources(env)
    before = read_state(env, agent_id), conversations(env, agent_id)
    headers = {"Authorization": f"Bearer {env[credential][0]}"}
    for method, suffix in [("GET", f"/sessions/{session_id}"), ("DELETE", f"/sessions/{session_id}"), ("GET", f"/versions/{version_id}"), ("GET", f"/sessions/{uuid4().hex}")]:
        result = env["client"].request(method, f"{env['base']}/api/v1/agents/{other}{suffix}", headers=headers, timeout=30)
        assert result.status_code == 200 and result.json()["retcode"] != 0, result.text
        assert (read_state(env, agent_id), conversations(env, agent_id)) == before
    with Session(env["engine"]) as db:
        db.execute(sa.update(API4Conversation).where(API4Conversation.id == session_id).values(source="dialog"))
        db.commit()
    for method in ["GET", "DELETE"]:
        result = env["client"].request(method, f"{env['base']}/api/v1/agents/{agent_id}/sessions/{session_id}", headers=headers, timeout=30)
        assert result.json()["retcode"] != 0, result.text
        with Session(env["engine"]) as db:
            assert db.get(API4Conversation, session_id) is not None


def test_authentication_and_log_failure_are_not_success(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = release_api
    agent_id, session_id, version_id, message_id = create_resources(env)
    before = read_state(env, agent_id), conversations(env, agent_id)
    prefix = f"{env['base']}/api/v1/agents/{agent_id}"
    for token in ["", "invalid"]:
        for method, suffix in [
            ("GET", "/sessions"),
            ("GET", f"/sessions/{session_id}"),
            ("DELETE", f"/sessions/{session_id}"),
            ("GET", "/versions"),
            ("GET", f"/versions/{version_id}"),
            ("GET", f"/logs/{message_id}"),
        ]:
            response = env["client"].request(method, prefix + suffix, headers={"Authorization": token}, timeout=30)
            assert response.status_code == 401, response.text
            assert (read_state(env, agent_id), conversations(env, agent_id)) == before
    assert REDIS_CONN.REDIS is not None
    REDIS_CONN.REDIS.set(f"{agent_id}-{message_id}-logs", "invalid-json")
    response = env["client"].get(prefix + f"/logs/{message_id}", timeout=30)
    assert response.json()["retcode"] != 0, response.text
    assert (read_state(env, agent_id), conversations(env, agent_id)) == before

    def unavailable(key: str) -> None:
        raise ConnectionError("test log storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(REDIS_CONN.REDIS, "get", unavailable)
        response = env["client"].get(prefix + f"/logs/{message_id}", timeout=30)
        assert response.json()["retcode"] != 0, response.text
    assert (read_state(env, agent_id), conversations(env, agent_id)) == before
    # Check FastAPI's actual route schema, including all path variables and no
    # decorator-created phantom query arguments.
    from api.apps import app

    paths = app.openapi()["paths"]
    for suffix in ["/sessions", "/sessions/{session_id}", "/versions", "/versions/{version_id}", "/logs/{message_id}"]:
        params = paths["/api/v1/agents/{canvas_id}" + suffix]["get"]["parameters"]
        assert {param["name"] for param in params if param["in"] == "path"} == {"canvas_id", *([suffix.split("{")[1][:-1]] if "{" in suffix else [])}
        assert not any(param["name"] in {"agent_id", "tenant_id", "args", "kwargs"} for param in params)
    smoke = env["client"].get(f"{env['base']}/api/v1/system/ping", timeout=30)
    assert smoke.status_code == 200
