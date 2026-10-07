"""The retained management boundary and live published execution permission matrix."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db import UserTenantRole
from api.db.db_models import API4Conversation, UserCanvas, UserCanvasVersion
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import denied_membership, join_member, message_dsl, read_state, update
from tests.support.agent_update_release import release_api as release_api


@pytest.mark.parametrize("caller", ["owner", "normal", "admin", "invite", "inactive_member", "foreign"])
def test_retained_management_and_runtime_permissions(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, caller: str) -> None:
    env = release_api
    result = env["client"].post(f"{env['base']}/api/v1/agents", json={"title": "Permission matrix", "dsl": message_dsl("published matrix")}, timeout=30).json()
    assert result["retcode"] == 0
    agent_id = result["data"]["id"]
    update(env, agent_id, {"release": True, "permission": "team"})
    if caller in {"normal", "admin"}:
        join_member(env, monkeypatch, 1, UserTenantRole.NORMAL if caller == "normal" else UserTenantRole.ADMIN)
        index = 1
    elif caller in {"invite", "inactive_member"}:
        denied_membership(env, monkeypatch, caller)
        index = 2
    else:
        index = 0 if caller == "owner" else 2
    headers = {"Authorization": f"Bearer {env['keys'][index]}"}
    before = read_state(env, agent_id)
    keys_before = {key: REDIS_CONN.get(key) for key in REDIS_CONN.REDIS.keys(f"canvas:replica:{agent_id}:*")}
    forms = env["client"].get(f"{env['base']}/api/v1/agents/{agent_id}/components/begin/input-form", headers=headers, timeout=30)
    edited = env["client"].put(f"{env['base']}/api/v1/agents/{agent_id}", headers=headers, json={"title": "owner edited"}, timeout=30)
    if caller == "owner":
        assert forms.json()["retcode"] == edited.json()["retcode"] == 0
        assert read_state(env, agent_id)["title"] == "owner edited"
        assert read_state(env, agent_id)["versions"] == before["versions"]
    else:
        assert forms.json()["retcode"] != 0 and edited.json()["retcode"] != 0
        deleted = env["client"].delete(f"{env['base']}/api/v1/agents/{agent_id}", headers=headers, timeout=30)
        assert deleted.json()["retcode"] != 0 and read_state(env, agent_id) == before
    assert {key: REDIS_CONN.get(key) for key in keys_before} == keys_before
    # A body runtime user cannot promote the caller to the resource owner.
    draft = env["client"].post(f"{env['base']}/api/v1/agents/chat/completions", headers=headers, json={"agent_id": agent_id, "user_id": env["owners"][0], "query": "draft"}, timeout=30)
    if caller == "owner":
        assert '"event": "message_end"' in draft.text
    else:
        assert draft.json()["retcode"] != 0
        assert {key: REDIS_CONN.get(key) for key in keys_before} == keys_before
    published = env["client"].post(
        f"{env['base']}/api/v1/agents/chat/completions", headers=headers, json={"agent_id": agent_id, "release": True, "stream": False, "query": "published", "user_id": env["owners"][0]}, timeout=30
    )
    if caller in {"owner", "normal", "admin"}:
        assert published.status_code == 200 and published.json()["retcode"] == 0, published.text
        session_id = published.json()["data"]["session_id"]
        with Session(env["engine"]) as db:
            conversation = db.get(API4Conversation, session_id)
            assert conversation is not None and conversation.dialog_id == agent_id and conversation.user_id == env["owners"][index]
        assert env["runtime_tenants"][-1] == env["owners"][0]
    else:
        assert published.status_code == 403 and published.json()["retcode"] != 0
        with Session(env["engine"]) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id == agent_id)) == 0
    if caller == "owner":
        deleted = env["client"].delete(f"{env['base']}/api/v1/agents/{agent_id}", headers=headers, timeout=30)
        assert deleted.json()["retcode"] == 0
        with Session(env["engine"]) as db:
            assert db.get(UserCanvas, agent_id) is None
            # The existing delete route removes only the canvas; clean this
            # fixture's associated rows explicitly, without claiming cascades.
            db.execute(sa.delete(API4Conversation).where(API4Conversation.dialog_id == agent_id))
            db.execute(sa.delete(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == agent_id))
            db.commit()
