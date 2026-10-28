"""Shipped template creation and node binding through real API, SQL and Redis."""

import asyncio
import hashlib
import json
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.db.db_models import Knowledgebase, Tenant, TenantLLM, User, UserCanvas, UserCanvasVersion, UserTenant, get_db
from common import resources
from tests.support.resource_journal import ResourceJournal

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "agent/templates/smart_customer_service_specialist.json"
AGGREGATOR = "VariableAggregator:GentleLawsSneeze"
CATEGORIES = ("Product Feature Comparison", "Product Usage Guide", "Book Installation")


@pytest.fixture
def template_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    journal = ResourceJournal("customer-service-template-create-bind")
    ids = {name: uuid4().hex for name in ("owner", "foreign", "membership", "foreign_membership", "agent", "feature", "usage", "foreign_kb", "task")}
    messages = [uuid4().hex for _ in CATEGORIES]
    keys = (
        [f"canvas:replica:{ids['agent']}:{owner}:{owner}" for owner in (ids["owner"], ids["foreign"])]
        + [f"canvas:replica:lock:{ids['agent']}:{owner}:{owner}" for owner in (ids["owner"], ids["foreign"])]
        + [f"{ids['task']}-logs", f"{ids['task']}-cancel", *[f"{ids['task']}-{message}-logs" for message in messages]]
    )
    journal.event("reserve-sql-identities", database=bootstrapped_engine.url.database, host=bootstrapped_engine.url.host, port=bootstrapped_engine.url.port, ids=ids, numeric_models=[314, 315])
    journal.event("reserve-redis-keys", keys=keys)
    journal.event("shipped-template", path=str(TEMPLATE), sha256=hashlib.sha256(TEMPLATE.read_bytes()).hexdigest())
    journal.event("asgi-testclient", listener=None, lifespan=False, boundary="real routers and JWT; in-process ASGI transport")
    sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    canvas_inserts: list[str] = []

    def reserve_sql(_connection: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool) -> None:
        if statement.lstrip().split(maxsplit=1)[0].upper() not in {"INSERT", "UPDATE", "DELETE"}:
            return
        rows = parameters if isinstance(parameters, list) else [parameters]
        identifiers = [{key: value for key, value in row.items() if key in {"id", "user_canvas_id", "tenant_id", "user_id"}} for row in rows if isinstance(row, dict)]
        journal.event("reserve-sql", statement_sha256=hashlib.sha256(statement.encode()).hexdigest(), identifiers=identifiers)
        if statement.lstrip().startswith("INSERT INTO usr_ai.t_ai_user_canvases ("):
            assert all(row.get("id") == ids["agent"] for row in rows)
            canvas_inserts.extend(row["id"] for row in rows)

    sa.event.listen(bootstrapped_engine, "before_cursor_execute", reserve_sql)
    with ExitStack() as cleanup:
        cleanup.callback(sa.event.remove, bootstrapped_engine, "before_cursor_execute", reserve_sql)
        monkeypatch.setitem(resources._state, "secret_key", "template-witness-only-authentication-secret")
        from api import apps
        from api.db import db_models
        from core.utils import redis_conn

        REDIS_CONN = redis_conn.REDIS_CONN

        redis = REDIS_CONN.REDIS
        assert redis is not None and redis.ping()
        assert not any(redis.exists(key) for key in keys)
        original_set = REDIS_CONN.set_obj
        original_lock = redis_conn.Lock

        def registered_set(key: str, *args: Any, **kwargs: Any) -> Any:
            assert key in keys, "Unregistered Redis write"
            return original_set(key, *args, **kwargs)

        def registered_lock(client: Any, name: str, *args: Any, **kwargs: Any) -> Any:
            assert name in keys, "Unregistered Redis lock"
            return original_lock(client, name, *args, **kwargs)

        monkeypatch.setattr(REDIS_CONN, "set_obj", registered_set)
        monkeypatch.setattr(redis_conn, "Lock", registered_lock)

        def remove_redis() -> None:
            redis.delete(*keys)
            absent = not any(redis.exists(key) for key in keys)
            journal.event("redis-cleanup-readback", keys=keys, absent=absent)
            assert absent

        def remove_rows() -> None:
            selections = [
                (UserCanvasVersion, UserCanvasVersion.user_canvas_id, [ids["agent"]]),
                (UserCanvas, UserCanvas.id, [ids["agent"]]),
                (Knowledgebase, Knowledgebase.id, [ids["feature"], ids["usage"], ids["foreign_kb"]]),
                (TenantLLM, TenantLLM.id, [314, 315]),
                (UserTenant, UserTenant.id, [ids["membership"], ids["foreign_membership"]]),
                (Tenant, Tenant.id, [ids["owner"], ids["foreign"]]),
                (User, User.id, [ids["owner"], ids["foreign"]]),
            ]
            with sessions() as db:
                for model, column, values in selections:
                    db.execute(sa.delete(model).where(column.in_(values)))
                db.commit()
            with sessions() as reader:
                remaining = {model.__name__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(values))) for model, column, values in selections}
            journal.event("sql-cleanup-readback", remaining=remaining)
            assert not any(remaining.values())

        cleanup.callback(remove_redis)
        cleanup.callback(remove_rows)
        with sessions() as db:
            for name, model_id, membership in (("owner", 314, "membership"), ("foreign", 315, "foreign_membership")):
                owner = ids[name]
                db.add(User(id=owner, email=f"{owner}@template.example.com", nickname="Template witness", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name="Template witness", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=ids[membership], tenant_id=owner, user_id=owner, role="owner", invited_by=owner))
                db.add(TenantLLM(id=model_id, tenant_id=owner, llm_factory="OpenAI", llm_name="gpt-4o", mdl_type="chat", api_key="controlled-provider-boundary", max_tokens=16000))
            for name, owner in (("feature", ids["owner"]), ("usage", ids["owner"]), ("foreign_kb", ids["foreign"])):
                db.add(Knowledgebase(id=ids[name], tenant_id=owner, name="Template " + name, created_by=owner, embd_id="controlled-embedding", permission="me"))
            db.commit()

        monkeypatch.setattr(apps, "SessionLocal", sessions)
        monkeypatch.setattr(db_models, "SessionLocal", sessions)

        def scratch_db() -> Iterator[Session]:
            with sessions() as db:
                yield db

        monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
        routes = [context.original_route for context in iter_route_contexts(apps.app.routes) if context.path == "/api/v1/agents" and "POST" in context.methods]
        assert len(routes) == 1
        route = routes[0]
        active_module = sys.modules[route.endpoint.__module__]
        assert Path(active_module.__file__).resolve() == ROOT / "api/apps/restful_apis/agent_api.py"
        journal.event("registered-router", module=active_module.__name__, source=active_module.__file__)
        monkeypatch.setattr(active_module, "get_uuid", lambda: ids["agent"])
        client = TestClient(apps.app)
        cleanup.callback(client.close)
        client.headers["Authorization"] = "Bearer " + apps.manager.create_access_token(data={"sub": f"{ids['owner']}@template.example.com"})
        yield {
            "client": client,
            "ids": ids,
            "engine": bootstrapped_engine,
            "journal": journal,
            "messages": messages,
            "redis": redis,
            "canvas_inserts": canvas_inserts,
            "foreign_jwt": apps.manager.create_access_token(data={"sub": f"{ids['foreign']}@template.example.com"}),
        }


def read_dsl(env: dict[str, Any]) -> dict[str, Any]:
    with Session(env["engine"]) as reader:
        row = reader.get(UserCanvas, env["ids"]["agent"])
        assert row is not None and row.user_id == env["ids"]["owner"]
        versions = list(reader.scalars(sa.select(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == row.id)))
        assert versions and any(version.dsl == row.dsl for version in versions)
        return deepcopy(row.dsl)


def assert_empty_references(dsl: dict[str, Any]) -> None:
    forms = [component["obj"]["params"] for component in dsl["components"].values()] + [node["data"].get("form", {}) for node in dsl["graph"]["nodes"]]
    for form in forms:
        if "llm_id" in form:
            assert form["llm_id"] == ""
        if "kb_ids" in form:
            assert form["kb_ids"] == []


def test_shipped_template_create_edit_readback_and_tenant_bound_canvas(template_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = template_api
    client, ids = env["client"], env["ids"]
    shipped = json.loads(TEMPLATE.read_text())["dsl"]
    response = client.post("/api/v1/agents", json={"title": "Customer service from template", "dsl": shipped})
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.json()["data"]["id"] == ids["agent"]
    assert env["canvas_inserts"] == [ids["agent"]]
    saved = read_dsl(env)
    assert saved == shipped
    assert_empty_references(saved)
    detail = client.get(f"/api/v1/agents/{ids['agent']}")
    assert detail.status_code == 200 and detail.json()["retcode"] == 0, detail.text
    assert detail.json()["data"]["dsl"] == saved
    replica_key = f"canvas:replica:{ids['agent']}:{ids['owner']}:{ids['owner']}"
    assert json.loads(env["redis"].get(replica_key))["dsl"] == saved

    edited = deepcopy(detail.json()["data"]["dsl"])
    bindings = {"Retrieval:EightyDaysHappen": ids["feature"], "Retrieval:EagerTipsFeel": ids["usage"]}
    forms = [(name, component["obj"]["params"]) for name, component in edited["components"].items()] + [(node["id"], node["data"].get("form", {})) for node in edited["graph"]["nodes"]]
    for name, form in forms:
        if "llm_id" in form:
            form["llm_id"] = "314"
        if name in bindings:
            # Current Web editor serializes the selected IDs to both fields.
            form["dataset_ids"] = form["kb_ids"] = [bindings[name]]
    response = client.put(f"/api/v1/agents/{ids['agent']}", json={"dsl": edited})
    assert response.status_code == 200 and response.json()["retcode"] == 0 and response.json()["data"] is True, response.text
    assert read_dsl(env) == edited
    with Session(env["engine"]) as reader:
        versions = list(reader.scalars(sa.select(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == ids["agent"])))
        assert any(version.dsl == shipped for version in versions)
        assert any(version.dsl == edited for version in versions)
    detail = client.get(f"/api/v1/agents/{ids['agent']}")
    assert detail.status_code == 200 and detail.json()["retcode"] == 0, detail.text
    assert detail.json()["data"]["dsl"] == edited
    assert json.loads(env["redis"].get(replica_key))["dsl"] == edited
    for method, body in (("get", None), ("put", {"title": "Foreign mutation"})):
        response = getattr(client, method)(f"/api/v1/agents/{ids['agent']}", headers={"Authorization": "Bearer " + env["foreign_jwt"]}, **({"json": body} if body else {}))
        assert response.status_code == 200 and response.json()["retcode"] != 0, response.text
        assert read_dsl(env) == edited
    assert not env["redis"].exists(f"canvas:replica:{ids['agent']}:{ids['foreign']}:{ids['foreign']}")
    env["journal"].event("api-sql-redis-readback", canvas=ids["agent"], numeric_model=314, dataset_bindings=bindings, external_refs_empty_on_create=True, graph_equal=True, foreign_denied=True)

    from agent import canvas as canvas_module
    from agent.component import agent_with_tools, llm
    from agent.tools.retrieval import Retrieval
    from api.db.joint_services import tenant_model_service
    from api.db.services.llm_service import LLMBundle

    calls: list[dict[str, Any]] = []

    class Model(LLMBundle):
        max_length = 16000

        def __init__(self, db: Session, tenant_id: str, config: dict[str, Any], **kwargs: Any) -> None:
            assert tenant_id == ids["owner"] and config["tenant_id"] == tenant_id and config["id"] == 314
            assert config["mdl_type"] == "chat"
            calls.append({"model": config["id"], "tenant": tenant_id})

        async def async_chat(self, system: str, history: list[dict[str, Any]], config: dict[str, Any], **kwargs: Any) -> str:
            if "classification system" in system:
                return history[-1]["content"].rsplit('USER: "', 1)[-1].split('"', 1)[0]
            category = "Product Feature Comparison" if "Specification Comparison Agent" in system else "Product Usage Guide" if "Usage Guide Agent" in system else "Book Installation"
            return "answer:" + category

    async def retrieve(self: Retrieval, query: str) -> None:
        _, selected, rows = await asyncio.to_thread(self._resolve_kbs)
        assert selected == [bindings[self._id]] and len(rows) == 1 and rows[0].tenant_id == ids["owner"]
        calls.append({"dataset": selected[0], "tenant": rows[0].tenant_id})
        self.set_output("formalized_content", "controlled schema")

    for module in (llm, agent_with_tools, tenant_model_service):
        monkeypatch.setattr(module, "LLMBundle", Model)
    monkeypatch.setattr(Retrieval, "_retrieve_kb", retrieve)
    message_ids = iter(env["messages"])
    monkeypatch.setattr(canvas_module, "get_uuid", lambda: next(message_ids))
    with Session(env["engine"]) as db:
        with pytest.raises(LookupError):
            tenant_model_service.get_model_config_by_type_and_name(db, ids["owner"], "chat", "315")
    canvas = canvas_module.Canvas(json.dumps(detail.json()["data"]["dsl"]), tenant_id=ids["owner"], task_id=ids["task"])
    try:
        retrieval = canvas.get_component_obj("Retrieval:EightyDaysHappen")
        before_calls = list(calls)
        selected = retrieval._param.dataset_ids
        retrieval._param.dataset_ids = [ids["foreign_kb"]]
        with pytest.raises(PermissionError):
            retrieval._resolve_kbs()
        assert calls == before_calls and read_dsl(env) == edited
        retrieval._param.dataset_ids = selected

        async def execute(query: str) -> list[dict[str, Any]]:
            return [event async for event in canvas.run(query=query)]

        for category in CATEGORIES:
            events = asyncio.run(execute(category))
            assert events[-1]["event"] == "workflow_finished" and not canvas.error
            answer = "".join(event["data"]["content"] for event in events if event["event"] == "message")
            assert answer == "answer:" + category
            assert canvas.get_variable_value(AGGREGATOR + "@answer") == answer
        assert {call["model"] for call in calls if "model" in call} == {314}
        assert {call["dataset"] for call in calls if "dataset" in call} == set(bindings.values())
        assert read_dsl(env) == edited
        env["journal"].event("canvas-sql-binding-readback", model=314, tenant=ids["owner"], datasets=list(bindings.values()), branches=list(CATEGORIES), source_dsl_unchanged=True)
    finally:
        canvas._thread_pool.shutdown(wait=True)
