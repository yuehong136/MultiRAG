"""Real JWT/API-key list authorization, SQL pagination and denial readback."""

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Dialog, Search, Tenant, User, UserTenant, get_async_db
from common.constants import RetCode


@pytest.fixture(scope="module")
def app_lists(bootstrapped_engine: sa.Engine) -> Iterator[dict[str, Any]]:
    from api.apps import app, manager

    roles = ["personal", "owner", "admin", "normal", "invite", "revoked", "unknown", "disabled", "foreign"]
    ids = {role: uuid4().hex for role in roles}
    actor = ids["personal"]
    token = "list-scope-" + uuid4().hex
    with Session(bootstrapped_engine) as db:
        for role, tenant in ids.items():
            db.add(User(id=tenant, email=f"{tenant}@list.test", nickname=role, password="unused", access_token="active"))
            db.add(Tenant(id=tenant, name=role, status="0" if role == "disabled" else "1", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            if role != "foreign":
                db.add(
                    UserTenant(
                        id=uuid4().hex,
                        user_id=actor,
                        tenant_id=tenant,
                        role="owner" if role == "personal" else "normal" if role in {"revoked", "disabled"} else role,
                        status="0" if role == "revoked" else "1",
                        invited_by=actor,
                    )
                )
            for number in range(3):
                for model in [Dialog, Search]:
                    extra = {"llm_id": ""} if model is Dialog else {"created_by": tenant}
                    db.add(model(id=uuid4().hex, tenant_id=tenant, name=f"{role} App {number}", **extra))
            db.add(Dialog(id=uuid4().hex, tenant_id=tenant, llm_id="", name="Inactive", status="0"))
            db.add(Search(id=uuid4().hex, tenant_id=tenant, created_by=tenant, name="Inactive", status="0"))
        db.add(APIToken(tenant_id=actor, token=token, name="list-scope"))
        db.commit()
    engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scratch_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_async_db] = scratch_session
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield {
            "client": client,
            "ids": ids,
            "engine": bootstrapped_engine,
            "async_engine": engine,
            "credentials": {"jwt": manager.create_access_token(data={"sub": f"{actor}@list.test"}), "key": token},
        }
    finally:
        client.close()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        # NullPool closes each connection in the request; no cross-loop pool.


def snapshot(env: dict[str, Any]) -> dict[str, list[Any]]:
    with env["engine"].connect() as reader:
        return {model.__tablename__: list(reader.execute(sa.select(model.__table__).order_by(model.id))) for model in [Dialog, Search, UserTenant, APIToken]}


@pytest.mark.parametrize("credential", ["jwt", "key"])
@pytest.mark.parametrize("family,payload_key", [("chats", "chats"), ("searches", "search_apps")])
def test_authorized_scope_pagination_and_denied_zero_business_reads(app_lists: dict[str, Any], credential: str, family: str, payload_key: str) -> None:
    env, ids = app_lists, app_lists["ids"]
    before = snapshot(env)
    headers = {"Authorization": "Bearer " + env["credentials"][credential]}
    path = f"/api/v1/{family}"
    allowed = {ids[role] for role in ["personal", "owner", "admin", "normal"]}

    def request(params: Any = None) -> dict[str, Any]:
        response = env["client"].get(path, headers=headers, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    default = request()
    assert default["code"] == 0, default
    assert default["data"]["total"] == 12
    assert {row["tenant_id"] for row in default["data"][payload_key]} == allowed
    for role in ["owner", "admin", "normal"]:
        filtered = request([("owner_ids", ids[role]), ("owner_ids", ids[role]), ("page", "2"), ("page_size", "1"), ("orderby", "name"), ("desc", "false")])
        assert filtered["code"] == 0 and filtered["data"]["total"] == 3, filtered
        assert [row["name"] for row in filtered["data"][payload_key]] == [f"{role} App 1"]
        assert {row["tenant_id"] for row in filtered["data"][payload_key]} == {ids[role]}
    multi = request([("owner_ids", ids["owner"]), ("owner_ids", ids["normal"]), ("page", "2"), ("page_size", "2")])
    assert multi["code"] == 0 and multi["data"]["total"] == 6 and len(multi["data"][payload_key]) == 2
    assert request({"keywords": "nOrMaL aPP 2"})["data"]["total"] == 1
    assert request({"keywords": "missing"})["data"]["total"] == 0
    if family == "chats":
        row = default["data"][payload_key][0]
        for field in ["id", "name"]:
            exact = request({field: row[field], "keywords": "ignored"})
            assert exact["code"] == 0 and exact["data"]["total"] == 1
            assert exact["data"][payload_key][0]["id"] == row["id"]
    statements: list[str] = []
    connections: set[int] = set()

    def capture(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        statements.append(statement)
        connections.add(id(conn))

    sa.event.listen(env["async_engine"].sync_engine, "before_cursor_execute", capture)
    try:
        same_transaction = request({"owner_ids": ids["normal"], "page": 1, "page_size": 1})
        assert same_transaction["code"] == 0 and same_transaction["data"]["total"] == 3
        assert len(connections) == 1, "Authentication, membership, count and list must share one request connection"
        for role in ["invite", "revoked", "unknown", "disabled", "foreign"]:
            for owners in [[ids[role]], [ids["normal"], ids[role]]]:
                statements.clear()
                denied = request([*(("owner_ids", owner) for owner in owners), ("user_id", ids["foreign"])])
                assert denied == {"code": int(RetCode.OPERATING_ERROR), "message": "Only authorized owner_ids can be queried."}
                assert statements and not any(Dialog.__tablename__ in sql or Search.__tablename__ in sql for sql in statements), statements
    finally:
        sa.event.remove(env["async_engine"].sync_engine, "before_cursor_execute", capture)
    assert snapshot(env) == before


@pytest.mark.parametrize("credential", ["jwt", "key"])
def test_personal_membership_revocation_cannot_use_id_equality(app_lists: dict[str, Any], credential: str) -> None:
    env, actor = app_lists, app_lists["ids"]["personal"]
    with env["engine"].begin() as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == actor, UserTenant.tenant_id == actor).values(status="0"))
    before = snapshot(env)
    try:
        for family in ["chats", "searches"]:
            response = env["client"].get(f"/api/v1/{family}", headers={"Authorization": "Bearer " + env["credentials"][credential]})
            assert response.status_code == 401
            assert response.json()["code"] == int(RetCode.UNAUTHORIZED)
        assert snapshot(env) == before
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.update(UserTenant).where(UserTenant.user_id == actor, UserTenant.tenant_id == actor).values(status="1"))
