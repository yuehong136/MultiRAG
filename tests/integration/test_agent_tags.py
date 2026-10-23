"""PostgreSQL migration, real HTTP auth, token filtering and zero-write denials."""

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.orm import Session

from api.db.db_models import Tenant, UserTenant
from tests.support.agent_tags import read_agent, seed_agent, tags_http_api
from tests.support.database import _alembic_config, scratch_database


@pytest.fixture(scope="module")
def tags_api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    evidence = Path(os.environ.get("MULTIRAG_AGENT_TAGS_EVIDENCE", str(tmp_path_factory.mktemp("agent-tags"))))
    with scratch_database(evidence) as engine, tags_http_api(engine, evidence) as env:
        yield env


def get(env: dict[str, Any], suffix: str = "", **params: Any) -> Any:
    response = env["client"].get(env["base"] + "/api/v1/agents" + suffix, params=params, timeout=30)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retcode"] == 0, body
    return body["data"]


def put(env: dict[str, Any], agent_id: str, tags: Any, caller: int = 0) -> Any:
    return env["client"].put(env["base"] + f"/api/v1/agents/{agent_id}/tags", json={"tags": tags}, headers={"Authorization": f"Bearer {env['jwts'][caller]}"}, timeout=30)


def test_tags_persist_and_normalize_without_touching_dsl(tags_api: dict[str, Any]) -> None:
    env = tags_api
    agent_id = seed_agent(env)
    before = read_agent(env, agent_id)
    response = put(env, agent_id, [" ML ", "ml", "Data,Science", "x" * 70, "X" * 64, "中文"])
    assert response.status_code == 200 and response.json()["data"] is True and response.json()["retcode"] == 0
    after = read_agent(env, agent_id)
    assert after["tags"] == "ML,Data Science," + "x" * 64 + ",中文"
    assert after["dsl"] == before["dsl"] and after["release"] == before["release"] and after["permission"] == before["permission"]
    detail = env["client"].get(env["base"] + f"/api/v1/agents/{agent_id}", timeout=30).json()
    assert detail["retcode"] == 0 and detail["data"]["tags"] == after["tags"]
    assert put(env, agent_id, [str(i) * 64 for i in range(20)]).json()["retcode"] == 0
    assert len(read_agent(env, agent_id)["tags"]) <= 512
    assert all(len(tag) <= 64 for tag in read_agent(env, agent_id)["tags"].split(","))
    assert put(env, agent_id, " a, b, A,, ").json()["retcode"] == 0
    assert read_agent(env, agent_id)["tags"] == "a,b"
    for empty in [[], ""]:
        assert put(env, agent_id, empty).json()["retcode"] == 0
        assert read_agent(env, agent_id)["tags"] == ""


def test_exact_tokens_or_and_existing_list_contract(tags_api: dict[str, Any]) -> None:
    env = tags_api
    keyword = uuid4().hex
    exact = seed_agent(env, tags="ml,100%,under_score", title=keyword + " A")
    partial = seed_agent(env, tags="ml-ops,100x,underXscore", title=keyword + " B")
    other = seed_agent(env, tags="data", title=keyword + " C")
    assert {row["id"] for row in get(env, tags="ml", keywords=keyword)["canvas"]} == {exact}
    assert {row["id"] for row in get(env, tags="ml,data", keywords=keyword)["canvas"]} == {exact, other}
    for tag in ["100%", "under_score"]:
        assert {row["id"] for row in get(env, tags=tag, keywords=keyword)["canvas"]} == {exact}
    result = get(env, tags="ml,ml-ops,data", keywords=keyword, orderby="title", desc=False, page=2, page_size=1, canvas_category="agent_canvas")
    assert result["total"] == 3 and [row["id"] for row in result["canvas"]] == [partial]
    assert result["canvas"][0]["tags"] == "ml-ops,100x,underXscore"
    assert get(env, tags="ml", keywords=keyword, canvas_category="dataflow_canvas")["total"] == 0
    assert get(env, tags="ml", keywords=keyword, owner_ids=env["owners"][0])["total"] == 1
    denied = env["client"].get(env["base"] + "/api/v1/agents", params={"owner_ids": env["owners"][3], "tags": "ml"}, timeout=30).json()
    assert denied["retcode"] != 0 and denied["data"] is False


def test_visible_tag_counts_sort_category_and_inactive_membership(tags_api: dict[str, Any]) -> None:
    env = tags_api
    prefix = uuid4().hex
    a, b, c = [prefix + suffix for suffix in ("-a", "-b", "-c")]
    owned = seed_agent(env, tags=f"{b},{a}")
    team = seed_agent(env, owner=1, tags=f"{a},{c}", permission="team")
    private = seed_agent(env, owner=1, tags=prefix + "-private")
    foreign = seed_agent(env, owner=2, tags=prefix + "-foreign", permission="team")
    seed_agent(env, tags=a, category="dataflow_canvas")
    with Session(env["engine"]) as db:
        membership = uuid4().hex
        db.add(UserTenant(id=membership, tenant_id=env["owners"][1], user_id=env["owners"][0], invited_by=env["owners"][1], role="normal"))
        db.commit()
    visible = {row["id"] for row in get(env)["canvas"]}
    assert {owned, team} <= visible and private not in visible and foreign not in visible
    rows = [row for row in get(env, "/tags") if row["tag"].startswith(prefix)]
    assert rows == [{"tag": a, "count": 3}, {"tag": b, "count": 1}, {"tag": c, "count": 1}]
    rows = [row for row in get(env, "/tags", canvas_category="agent_canvas") if row["tag"].startswith(prefix)]
    assert rows == [{"tag": a, "count": 2}, {"tag": b, "count": 1}, {"tag": c, "count": 1}]
    before = read_agent(env, team)
    assert put(env, team, ["denied"]).json()["retcode"] != 0
    assert read_agent(env, team) == before
    with Session(env["engine"]) as db:
        for role, status in [("invite", "1"), ("normal", "0"), ("admin", "0")]:
            db.execute(sa.update(UserTenant).where(UserTenant.id == membership).values(role=role, status=status))
            db.commit()
            assert not any(row["tag"] == c for row in get(env, "/tags"))
            assert team not in {row["id"] for row in get(env)["canvas"]}
        db.execute(sa.update(UserTenant).where(UserTenant.id == membership).values(role="admin", status="1"))
        db.execute(sa.update(Tenant).where(Tenant.id == env["owners"][1]).values(status="0"))
        db.commit()
    assert not any(row["tag"] == c for row in get(env, "/tags"))
    with Session(env["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == env["owners"][1]).values(status="1"))
        db.execute(sa.delete(UserTenant).where(UserTenant.id == membership))
        db.commit()


def test_foreign_missing_invalid_and_unauthenticated_are_zero_write(tags_api: dict[str, Any]) -> None:
    env = tags_api
    agent_id = seed_agent(env, tags="protected")
    before = read_agent(env, agent_id)
    for caller, requested in [(1, agent_id), (2, agent_id), (0, uuid4().hex)]:
        response = put(env, requested, ["forbidden"], caller)
        assert response.status_code == 200 and response.json()["retcode"] != 0 and response.json()["data"] is False
        assert read_agent(env, agent_id) == before
    for body in [{}, {"tags": None}, {"tags": [1]}, {"tags": {}, "user_id": env["owners"][0]}]:
        response = env["client"].put(env["base"] + f"/api/v1/agents/{agent_id}/tags", json=body, timeout=30)
        assert response.status_code == 422 and read_agent(env, agent_id) == before
    for token in ["", "invalid"]:
        for method, suffix in [("GET", "/tags"), ("PUT", f"/{agent_id}/tags")]:
            response = env["client"].request(method, env["base"] + "/api/v1/agents" + suffix, headers={"Authorization": token}, json={"tags": []}, timeout=30)
            assert response.status_code == 401 and read_agent(env, agent_id) == before


def test_existing_row_migration_and_fresh_schema(tmp_path: Path) -> None:
    with scratch_database(tmp_path) as engine:
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns("t_ai_user_canvases", schema="usr_ai")}
        assert columns["tags"]["type"].length == 512 and columns["tags"]["nullable"] is False
        agent_id = uuid4().hex
        with engine.begin() as conn:
            conn.execute(sa.text("DROP INDEX usr_ai.ix_usr_ai_t_ai_user_canvases_tags"))
            conn.execute(sa.text("ALTER TABLE usr_ai.t_ai_user_canvases DROP COLUMN tags"))
            conn.execute(
                sa.text("INSERT INTO usr_ai.t_ai_user_canvases (id,user_id,title,permission,release,canvas_category) VALUES (:id,'old-owner','old','me',false,'agent_canvas')"), {"id": agent_id}
            )
            cfg = _alembic_config()
            cfg.attributes["connection"] = conn
            command.stamp(cfg, "e7a9b1c3d5f0")
            command.upgrade(cfg, "head")
        with engine.connect() as reader:
            assert reader.scalar(sa.text("SELECT tags FROM usr_ai.t_ai_user_canvases WHERE id=:id"), {"id": agent_id}) == ""
            assert reader.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == ScriptDirectory.from_config(_alembic_config()).get_current_head()
        index = next(index for index in sa.inspect(engine).get_indexes("t_ai_user_canvases", schema="usr_ai") if index["name"] == "ix_usr_ai_t_ai_user_canvases_tags")
        assert index["column_names"] == ["tags"]
