"""Real beta-token SQL and registered HTTP object scope, with controlled IO."""

import asyncio
import hashlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, Knowledgebase, Search, Task, Tenant, User, UserCanvas, UserTenant, get_async_db, get_db
from common import resources
from tests.support.resource_journal import ResourceJournal


def test_beta_share_credentials_and_resource_scope(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from api.apps import app, datav_app, manager
    from api.apps.restful_apis import bot as route
    from api.db.services import task_service

    engine = bootstrapped_engine
    monkeypatch.setattr(engine, "hide_parameters", True)
    journal = ResourceJournal("beta-share-scope")
    roles = ["owner", "foreign", "inactive", "disabled_member"]
    owners = {role: uuid4().hex for role in roles}
    memberships = {role: uuid4().hex for role in [*roles, "invited", "disabled"]}
    datasets = {role: uuid4().hex for role in ["owner", "foreign"]}
    searches = {role: uuid4().hex for role in ["owner", "foreign", "disabled_member"]}
    agents = {role: uuid4().hex for role in ["owner", "foreign"]}
    tokens = {role: uuid4().hex for role in roles}
    beta = {role: uuid4().hex for role in [*roles, "duplicate"]}
    names = "bs-" + uuid4().hex[:12]
    evidence = Path(os.environ.get("F_BETA_EVIDENCE", str(tmp_path))) / "beta-share-scope.json"
    journal.event(
        "reserve-sql-rows",
        database=engine.url.database,
        host=engine.url.host,
        port=engine.url.port,
        users=owners,
        memberships=memberships,
        datasets=datasets,
        searches=searches,
        agents=agents,
        api_token_name=names,
        evidence=str(evidence),
        borrowed="PostgreSQL service only; no Redis/storage/provider/listener created",
    )
    report: dict[str, Any] = {"database": engine.url.database, "checks": [], "cleanup": False, "controlled_io": True}
    tables = [User, Tenant, UserTenant, APIToken, Knowledgebase, Search, UserCanvas, Document, Task]

    def readback() -> str:
        with Session(engine) as db:
            rows = {model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key.columns)).mappings()] for model in tables}
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

    def save() -> None:
        evidence.write_text(json.dumps(report, indent=2))
        evidence.chmod(0o600)

    async_engine = create_async_engine(engine.url, poolclass=NullPool)
    async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    def sync_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    async def async_db() -> AsyncIterator[AsyncSession]:
        async with async_sessions() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, sync_db)
    monkeypatch.setitem(app.dependency_overrides, get_async_db, async_db)
    for dependency in [route.beta_token_required, route.async_beta_token_required]:
        assert dependency not in app.dependency_overrides
    writes: list[str] = []

    def capture_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        verb = statement.lstrip().split()[0].upper()
        if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE"}:
            writes.append(verb)

    business_reads: list[str] = []
    original_search, original_agent = route.SearchService.get_detail, route.UserCanvasService.get_by_id
    original_accessible = route.KnowledgebaseService.accessible

    def search_detail(db: Session, search_id: str) -> dict[str, Any]:
        business_reads.append("search")
        return original_search(db, search_id)

    def agent_detail(db: Session, agent_id: str) -> Any:
        business_reads.append("agent")
        return original_agent(db, agent_id)

    def dataset_visible(db: Session, kb_id: str, user_id: str) -> bool:
        business_reads.append("dataset")
        return original_accessible(db, kb_id, user_id)

    monkeypatch.setattr(route.SearchService, "get_detail", search_detail)
    monkeypatch.setattr(route.UserCanvasService, "get_by_id", agent_detail)
    monkeypatch.setattr(route.KnowledgebaseService, "accessible", dataset_visible)
    fetch = Mock(return_value=[{"title": "Owned candidate"}])
    mindmap = AsyncMock(return_value={"id": "controlled-root"})
    forbidden = Mock(side_effect=AssertionError("Denied shared request reached provider, retrieval or queue"))
    monkeypatch.setattr(datav_app, "fetch_persondata_list", fetch)
    monkeypatch.setattr(route, "gen_mindmap", mindmap)
    for name in ["async_ask", "LLMBundle", "get_model_config_by_type_and_name", "get_tenant_default_model_by_type", "build_named_bundle_async", "build_bundle_by_id_async"]:
        monkeypatch.setattr(route, name, forbidden)
    monkeypatch.setattr(task_service, "queue_tasks", forbidden)
    storage = Mock()
    monkeypatch.setitem(resources._state, "storage", storage)
    monkeypatch.setitem(resources._state, "retriever", forbidden)
    client: TestClient | None = None
    conditions = [
        (APIToken, APIToken.name == names),
        (Knowledgebase, Knowledgebase.id.in_(list(datasets.values()))),
        (Search, Search.id.in_(list(searches.values()))),
        (UserCanvas, UserCanvas.id.in_(list(agents.values()))),
        (UserTenant, UserTenant.id.in_(list(memberships.values()))),
        (Tenant, Tenant.id.in_(list(owners.values()))),
        (User, User.id.in_(list(owners.values()))),
    ]
    try:
        with Session(engine) as db:
            for role, identifier in owners.items():
                db.add(User(id=identifier, email=identifier + "@beta-scope.test", nickname="Beta scope", password="unused", access_token="active"))
                db.add(Tenant(id=identifier, name="Beta scope", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive", status="0" if role == "inactive" else "1"))
                db.add(UserTenant(id=memberships[role], user_id=identifier, tenant_id=identifier, role="owner", invited_by=identifier))
                db.add(APIToken(tenant_id=identifier, token=tokens[role], beta=beta[role], name=names))
            db.add(UserTenant(id=memberships["invited"], user_id=owners["owner"], tenant_id=owners["foreign"], role="invite", invited_by=owners["foreign"]))
            db.add(UserTenant(id=memberships["disabled"], user_id=owners["owner"], tenant_id=owners["disabled_member"], role="normal", invited_by=owners["disabled_member"], status="0"))
            for role, identifier in datasets.items():
                db.add(Knowledgebase(id=identifier, tenant_id=owners[role], created_by=owners[role], name="Beta " + role, permission="me", embd_id="embedding", parser_config={}))
            for role, identifier in searches.items():
                db.add(
                    Search(
                        id=identifier,
                        tenant_id=owners[role],
                        created_by=owners[role],
                        name="Beta " + role,
                        search_config={"chat_id": "controlled", "kb_ids": [datasets.get(role, datasets["foreign"])]},
                    )
                )
            for role, identifier in agents.items():
                db.add(UserCanvas(id=identifier, user_id=owners[role], title="Beta " + role, dsl={}))
            for role in ["owner", "foreign"]:
                db.add(APIToken(tenant_id=owners[role], token=uuid4().hex, beta=beta["duplicate"], name=names))
            db.commit()
        before = readback()
        for target in [engine, async_engine.sync_engine]:
            sa.event.listen(target, "before_cursor_execute", capture_write)
        client = TestClient(app)
        invalid: list[tuple[str, Any]] = [
            ("missing", {}),
            ("invalid", {"Authorization": "Bearer unknown"}),
            ("wrong-scheme", {"Authorization": "Basic " + beta["owner"]}),
            ("extra-part", {"Authorization": "Bearer " + beta["owner"] + " extra"}),
            ("duplicate-beta-rows", {"Authorization": "Bearer " + beta["duplicate"]}),
            ("inactive-tenant", {"Authorization": "Bearer " + beta["inactive"]}),
            ("API-token", {"Authorization": "Bearer " + tokens["owner"]}),
            ("JWT", {"Authorization": "Bearer " + manager.create_access_token(data={"sub": owners["owner"] + "@beta-scope.test"})}),
            ("duplicate-headers", [("Authorization", "Bearer " + beta["owner"]), ("Authorization", "Bearer " + beta["foreign"])]),
        ]
        for label, headers in invalid:
            for method, path, body in [
                ("GET", f"/api/v1/agentbots/{agents['owner']}/persondataList", None),
                ("POST", "/api/v1/searchbots/mindmap", {"question": "q", "kb_ids": [datasets["owner"]]}),
            ]:
                response = client.request(method, path, json=body, headers=headers)
                assert response.status_code == 401 and response.json()["retcode"] == 109, (label, response.text)
                assert readback() == before and not writes and not business_reads
                fetch.assert_not_called()
                mindmap.assert_not_awaited()
                forbidden.assert_not_called()
                assert not storage.mock_calls
                report["checks"].append({"case": label, "method": method, "status": 401, "code": 109, "sql_unchanged": True, "handler_io": 0})
        headers = {"Authorization": "Bearer " + beta["owner"]}
        response = client.get(f"/api/v1/agentbots/{agents['foreign']}/persondataList", headers=headers)
        assert response.status_code == 200 and response.json()["code"] == 102
        fetch.assert_not_called()
        report["checks"].append({"case": "foreign-agent", "code": 102, "redis_calls": 0})
        for role in ["foreign", "disabled_member"]:
            for method, path, body in [
                ("GET", f"/api/v1/searchbots/detail?search_id={searches[role]}", None),
                ("POST", "/api/v1/searchbots/related_questions", {"question": "q", "search_id": searches[role]}),
                ("POST", "/api/v1/searchbots/ask", {"question": "q", "kb_ids": [datasets["owner"]], "search_id": searches[role]}),
                ("POST", "/api/v1/searchbots/mindmap", {"question": "q", "kb_ids": [datasets["owner"]], "search_id": searches[role]}),
            ]:
                response = client.request(method, path, json=body, headers=headers)
                assert response.status_code == 200 and response.json()["code"] == 109, response.text
                assert "text/event-stream" not in response.headers["content-type"]
                report["checks"].append({"case": role + "-search", "method": method, "code": 109, "provider_calls": 0})
        for selected in [[], [datasets["foreign"]], [datasets["owner"], datasets["foreign"]]]:
            for path in ["ask", "mindmap"]:
                response = client.post("/api/v1/searchbots/" + path, json={"question": "q", "kb_ids": selected}, headers=headers)
                assert response.status_code == 200 and response.json()["code"] == 109, response.text
                assert "text/event-stream" not in response.headers["content-type"]
                report["checks"].append({"case": path + "-denied-datasets", "count": len(selected), "code": 109, "provider_calls": 0})
        forbidden.assert_not_called()
        fetch.assert_not_called()
        mindmap.assert_not_awaited()
        assert readback() == before and not writes and not storage.mock_calls
        response = client.get(f"/api/v1/searchbots/detail?search_id={searches['owner']}", headers=headers)
        assert response.status_code == 200 and response.json()["retcode"] == 0 and response.json()["data"]["id"] == searches["owner"]
        response = client.get(f"/api/v1/agentbots/{agents['owner']}/persondataList", headers=headers)
        assert response.status_code == 200 and response.json()["data"] == [{"title": "Owned candidate"}]
        fetch.assert_called_once_with(agents["owner"])
        response = client.post("/api/v1/searchbots/mindmap", json={"question": "q", "kb_ids": [datasets["owner"]], "search_id": searches["owner"]}, headers=headers)
        assert response.status_code == 200 and response.json()["data"] == {"id": "controlled-root"}
        mindmap.assert_awaited_once()
        assert mindmap.call_args.args[2:4] == ([datasets["owner"]], owners["owner"])
        assert readback() == before and not writes and not storage.mock_calls
        forbidden.assert_not_called()
        report.update(request_sql_unchanged=True, sql_hash=before, controlled_persondata_calls=fetch.call_count, controlled_mindmap_calls=mindmap.await_count)
    finally:
        if client is not None:
            client.close()
        for target in [engine, async_engine.sync_engine]:
            if sa.event.contains(target, "before_cursor_execute", capture_write):
                sa.event.remove(target, "before_cursor_execute", capture_write)
        asyncio.run(async_engine.dispose())
        with Session(engine) as db:
            for model, condition in conditions:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        with Session(engine) as db:
            remaining = {model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in conditions}
            assert not any(remaining.values()), remaining
        journal.event("sql-cleanup-readback", database=engine.url.database, owned_rows_absent=True, remaining=remaining)
        report.update(cleanup=True, remaining=remaining)
        save()
