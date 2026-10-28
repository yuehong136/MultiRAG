"""Owned PostgreSQL and HTTP evidence for provider-instance model lifecycles."""

import hashlib
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, get_async_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.model_backfill import backfill
from api.db.services import provider_verification
from common import resources, settings
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal

REAL_VERIFY_MODEL = provider_verification.verify_model

pytestmark = pytest.mark.integration


def tenant(identifier: str) -> Tenant:
    return Tenant(id=identifier, name="Provider fixture", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="", status="1")


@pytest.fixture
def owned_engine(tmp_path: Path) -> Iterator[Engine]:
    with scratch_database(tmp_path) as engine:
        journal = ResourceJournal("provider-instance-sql")

        def reserve(_connection: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, _many: bool) -> None:
            if statement.lstrip().split(maxsplit=1)[0].upper() in {"INSERT", "UPDATE", "DELETE", "ALTER", "DROP", "CREATE"}:
                rows = parameters if isinstance(parameters, list) else [parameters]
                identities = [{key: value for key, value in row.items() if key in {"id", "tenant_id", "provider_id", "instance_id", "llm_name", "model_name"}} for row in rows if isinstance(row, dict)]
                journal.event("reserve-sql", database=engine.url.database, statement_sha256=hashlib.sha256(statement.encode()).hexdigest(), identities=identities)

        event.listen(engine, "before_cursor_execute", reserve)
        engine._provider_reserve = reserve
        yield engine
        event.remove(engine, "before_cursor_execute", reserve)


@pytest.fixture
def http(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, Engine, Any]]:
    async def probe_boundary(_provider: str, _key: str, _base: str, declaration: Any) -> dict[str, Any]:
        return {"verified": True, "model_name": declaration.model_name, "model_type": declaration.model_types[0]}

    # Existing cases isolate SQL/lifecycle semantics. Dedicated connection cases
    # below restore the real active adapter and use owned loopback HTTP.
    monkeypatch.setattr(provider_verification, "verify_model", probe_boundary)
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.commit()
    monkeypatch.setitem(resources._state, "secret_key", "test-only-provider-session-key")
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "OpenAI", "llm": [{"llm_name": "catalog-chat", "mdl_type": "chat", "max_tokens": 8192, "is_tools": True, "tags": "LLM,IMAGE2TEXT"}]}])
    from api.apps.restful_apis import provider_api
    from api.utils.api_utils import async_current_user

    engine = create_async_engine(owned_engine.url)
    event.listen(engine.sync_engine, "before_cursor_execute", owned_engine._provider_reserve)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def session() -> Any:
        async with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(provider_api.router, prefix="/api/v1")
    app.dependency_overrides[get_async_db] = session
    app.dependency_overrides[async_current_user] = lambda: SimpleNamespace(id=owner)
    client = TestClient(app)
    try:
        yield client, owner, owned_engine, provider_api
    finally:
        client.close()
        import asyncio

        asyncio.run(engine.dispose())


def create(client: TestClient, name: str = "first", *, models: list[str] | None = None) -> dict[str, Any]:
    provider = client.put("/api/v1/providers", json={"provider_name": "OpenAI"})
    assert provider.status_code in {200, 409}, provider.text
    body = {"instance_name": name, "api_key": "fixture-shared-key", "base_url": f"https://{name}.invalid/v1", "region": ""}
    if models is not None:
        body["model_ids"] = models
    if models == []:
        body["custom_model"] = {"model_name": "verified-fixture-model", "model_types": ["chat"], "max_tokens": 0}
    response = client.post("/api/v1/providers/OpenAI/instances", json=body)
    assert response.status_code == 200, response.text
    if models == []:
        items = client.get(f"/api/v1/providers/OpenAI/instances/{name}/models").json()["data"]["items"]
        assert len(items) == 1
        assert client.delete(f"/api/v1/providers/OpenAI/instances/{name}/models/{items[0]['id']}").status_code == 200
    return response.json()["data"]


def test_instances_preserve_credentials_capabilities_defaults_and_secret_projection(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client, "first")
    create(client, "second")
    items = client.get("/api/v1/models").json()["data"]["items"]
    assert len(items) == 2 and len({item["id"] for item in items}) == 2
    assert {tuple(item["model_types"]) for item in items} == {("chat", "image2text")}
    instances = client.get("/api/v1/providers/OpenAI/instances").json()["data"]
    assert instances["total"] == 2 and "fixture-shared-key" not in json.dumps(instances)
    for item in items:
        with Session(engine) as db:
            config = get_model_config_by_type_and_name(db, owner, "image2text", str(item["id"]))
            assert config["llm_name"] == "catalog-chat"
            assert config["api_base"] == f"https://{item['instance_name']}.invalid/v1"
            assert config["api_key"] == "fixture-shared-key" and config["mdl_type"] == "image2text"
            assert get_model_config_by_id(db, item["id"], tenant_id=owner, model_type="chat")["is_tools"] is True
            with pytest.raises(LookupError, match="capability"):
                get_model_config_by_id(db, item["id"], tenant_id=owner)
            with pytest.raises(LookupError):
                get_model_config_by_id(db, item["id"], allowed_tenant_ids=set(), model_type="chat")
            with pytest.raises(LookupError):
                get_model_config_by_type_and_name(db, uuid4().hex, "chat", str(item["id"]))
    chosen = items[1]
    assert client.patch("/api/v1/models/default", json={"model_type": "image2text", "model_id": chosen["id"]}).status_code == 200
    with Session(engine) as db:
        assert get_tenant_default_model_by_type(db, owner, "image2text")["api_base"] == "https://second.invalid/v1"
    response = client.get("/api/v1/models/default").json()["data"]["models"]
    assert next(item for item in response if item["model_type"] == "image2text")["model_id"] == chosen["id"]
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
    assert client.patch("/api/v1/models/default", json={"model_type": "image2text", "model_id": None}).status_code == 200
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 0


def test_blank_patch_status_and_rejections_have_independent_readback(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client, models=[])
    assert client.get("/api/v1/models").json()["data"]["items"] == []
    registered = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "zero", "model_types": ["ocr", "image2text"], "max_tokens": 0, "is_tools": False})
    assert registered.status_code == 200, registered.text
    identifier = registered.json()["data"]["id"]
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"api_key": "", "region": ""}).status_code == 200
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"clear_api_key": True}).status_code == 400
    assert client.patch("/api/v1/models/default", json={"model_type": "embedding", "model_id": identifier}).status_code == 400
    for query in ["page=0", "page=1&page=2", "page_size=101", "page_size=1.0", "page_size=nan", "active=1", "model_type=unknown"]:
        assert client.get("/api/v1/models?" + query).status_code == 422
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"status": "inactive"}).status_code == 200
    assert client.get("/api/v1/models?active=true").json()["data"]["items"] == []
    with Session(engine) as db:
        instance = db.scalar(select(TenantModelInstance))
        assert instance.api_key == "fixture-shared-key" and instance.region == "" and instance.status == "inactive"
        row = db.get(TenantLLM, identifier)
        assert row.max_tokens == 0
        assert db.get(Tenant, owner).tenant_embd_id is None
        with pytest.raises(LookupError, match="disabled"):
            get_model_config_by_type_and_name(db, owner, "ocr", str(identifier))


def test_atomic_create_failure_and_empty_provider_deletion(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _owner, engine, module = http
    assert client.put("/api/v1/providers", json={"provider_name": "OpenAI"}).status_code == 200
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    assert client.put("/api/v1/providers", json={"provider_name": "OpenAI"}).status_code == 200

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("synthetic storage failure containing fixture-key")

    monkeypatch.setattr(module.service, "add_model", fail)
    response = client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "failed", "api_key": "fixture-key"})
    assert response.status_code == 500 and "fixture-key" not in response.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 1
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 0


def test_backfill_preserves_legacy_ids_divergent_envelopes_and_zero(owned_engine: Engine) -> None:
    owner = uuid4().hex
    credentials = ['{"api_key":"same","is_tools":false,"provider_order":["A"]}', '{"api_key":"other","is_tools":true,"provider_order":["B"]}']
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        for index, credential in enumerate(credentials):
            db.add(
                TenantLLM(
                    tenant_id=owner,
                    llm_factory="OpenAI",
                    llm_name=f"legacy-{index}",
                    mdl_type="chat",
                    api_key=credential,
                    api_base=f"https://legacy-{index}.invalid",
                    max_tokens=0,
                    status="0" if index else "1",
                )
            )
        db.commit()
        before = [(row.id, row.llm_name, row.api_key, row.api_base, row.max_tokens, row.status) for row in db.scalars(select(TenantLLM).order_by(TenantLLM.id))]
    with owned_engine.begin() as connection:
        backfill(connection)
        backfill(connection)
    with Session(owned_engine) as db:
        after = [(row.id, row.llm_name, row.api_key, row.api_base, row.max_tokens, row.status) for row in db.scalars(select(TenantLLM).order_by(TenantLLM.id))]
        assert before == after
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 2
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
        config = get_model_config_by_type_and_name(db, owner, "chat", "legacy-0@OpenAI")
        assert config["id"] == before[0][0] and config["max_tokens"] == 0 and config["is_tools"] is False
        assert json.loads(config["api_key_payload"])["provider_order"] == ["A"]
        with pytest.raises(LookupError, match="disabled"):
            get_model_config_by_type_and_name(db, owner, "chat", "legacy-1@OpenAI")


def test_upgrade_is_additive_idempotent_and_no_destructive_downgrade(owned_engine: Engine) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / "configs/alembic/versions/b8d0f2a4c6e8_add_model_provider_instances.py"
    spec = importlib.util.spec_from_file_location("provider_migration_test", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="legacy", mdl_type="embedding", api_key="fixture", api_base="", max_tokens=0))
        db.commit()
        identity = db.scalar(select(TenantLLM.id))
    with owned_engine.begin() as connection:
        for table in (TenantModel.__table__, TenantModelInstance.__table__, TenantModelProvider.__table__):
            table.drop(connection)
        connection.execute(text("ALTER TABLE usr_ai.t_ai_tenants DROP COLUMN ocr_id, DROP COLUMN tenant_ocr_id"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            with pytest.raises(RuntimeError, match="export"):
                migration.downgrade()
    with Session(owned_engine) as db:
        assert db.get(TenantLLM, identity).max_tokens == 0
        assert db.scalar(select(TenantModel.id)) == identity
        assert db.get(Tenant, owner).ocr_id is None


def test_long_base_url_strict_paths_and_validation_never_echo_secrets(http: tuple[Any, ...]) -> None:
    client, _owner, engine, _module = http
    create(client, models=[])
    registered = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "long-url", "model_types": ["chat"], "max_tokens": 0})
    identifier = registered.json()["data"]["id"]
    long_url = "https://long.invalid/" + "path" * 90
    response = client.patch("/api/v1/providers/OpenAI/instances/first", json={"base_url": long_url})
    assert response.status_code == 200, response.text
    for invalid in ["0", "01", "+1", "1.0", "-1", "9223372036854775808"]:
        for method in (client.patch, client.delete):
            kwargs = {"json": {"status": "inactive"}} if method == client.patch else {}
            assert method(f"/api/v1/providers/OpenAI/instances/first/models/{invalid}", **kwargs).status_code == 400
    secret = "secret-must-never-echo"
    invalid = client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "bad", "api_key": {"secret": secret}})
    assert invalid.status_code == 422 and secret not in invalid.text
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier).api_base == long_url
        assert db.get(TenantLLM, identifier).status == "1"
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 1


def test_reference_checks_use_model_fields_and_owner_scope(http: tuple[Any, ...]) -> None:
    from api.db.db_models import Search, UserCanvas

    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    other = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(other))
        db.add(Search(id=uuid4().hex, tenant_id=other, name="catalog-chat@OpenAI", created_by=other, description="catalog-chat@OpenAI", search_config={"chat_id": "catalog-chat@OpenAI"}))
        canvas = UserCanvas(id=uuid4().hex, user_id=owner, title="Agent", dsl={"components": {"llm": {"params": {"llm_id": str(identifier)}}}})
        db.add(canvas)
        db.commit()
        canvas_id = canvas.id
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier) is not None
        canvas = db.get(UserCanvas, canvas_id)
        canvas.dsl = {"components": {"parser": {"params": {"layout_recognize": str(identifier)}}}}
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        canvas = db.get(UserCanvas, canvas_id)
        canvas.dsl = {}
        db.commit()
    with Session(engine) as db:
        search = Search(id=uuid4().hex, tenant_id=owner, name="Model search", created_by=owner, search_config={"chat_id": str(identifier)})
        db.add(search)
        db.commit()
        search_id = search.id
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        search = db.get(Search, search_id)
        search.search_config = {"chat_id": "catalog-chat@OpenAI"}
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        db.delete(db.get(Search, search_id))
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier) is None
        assert db.scalar(select(func.count()).select_from(Search).where(Search.tenant_id == other)) == 1


def test_foreign_owner_denials_have_no_writes(http: tuple[Any, ...]) -> None:
    from api.utils.api_utils import async_current_user

    client, _owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    other = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(other))
        db.commit()
    client.app.dependency_overrides[async_current_user] = lambda: SimpleNamespace(id=other)
    assert client.get("/api/v1/models").json()["data"]["items"] == []
    assert client.get("/api/v1/providers/OpenAI/instances").status_code == 404
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"api_key": "forbidden"}).status_code == 404
    assert client.delete("/api/v1/providers/OpenAI").status_code == 404
    assert client.patch("/api/v1/models/default", json={"model_type": "chat", "model_id": identifier}).status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(TenantModelInstance.api_key)) == "fixture-shared-key"
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 1
        assert db.get(Tenant, other).tenant_llm_id is None


def test_numeric_name_collision_is_rejected(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    with Session(engine) as db:
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name=str(identifier), mdl_type="chat", api_key="legacy-other-key", api_base="https://other.invalid"))
        db.commit()
    with Session(engine) as db:
        with pytest.raises(LookupError, match="ambiguous"):
            get_model_config_by_type_and_name(db, owner, "chat", str(identifier))
        assert get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat")["api_key"] == "fixture-shared-key"


def test_logout_persists_invalidation_and_commit_failure_rolls_back(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from api.db.db_models import APIToken, User

    monkeypatch.setitem(resources._state, "secret_key", "test-only-logout-key")
    from datetime import UTC, datetime

    from api.apps import load_user, manager
    from api.apps.restful_apis.user_api import log_out
    from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor

    owner = uuid4().hex
    email = f"{owner}@fixture.invalid"
    with Session(owned_engine) as db:
        db.add(User(id=owner, nickname="Logout", email=email, access_token="old-access", password="unused"))
        db.add(tenant(owner))
        db.add(APIToken(tenant_id=owner, token="fixture-independent-api-key", name="separate"))
        db.commit()
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=owner, display_name="Logout"),
        membership=TenantMembershipEvidence(platform_user_id=owner, tenant_id=owner),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )
    jwt = manager.create_access_token(data={"sub": email})

    async def invoke(fail: bool = False) -> None:
        async_engine = create_async_engine(owned_engine.url)
        async with AsyncSession(async_engine, expire_on_commit=False) as db:
            if fail:

                async def fail_commit() -> None:
                    raise SQLAlchemyError("fixture storage error")

                monkeypatch.setattr(db, "commit", fail_commit)
            try:
                await log_out(db, principal)
            finally:
                await db.close()
                await async_engine.dispose()

    with pytest.raises(HTTPException, match="500"):
        asyncio.run(invoke(True))
    with Session(owned_engine) as db:
        assert db.get(User, owner).access_token == "old-access"
        assert load_user(email, db) is not None
    asyncio.run(invoke())
    with Session(owned_engine) as db:
        assert db.get(User, owner).access_token.startswith("INVALID_")
        assert load_user(manager._get_payload(jwt)["sub"], db) is None
        assert db.scalar(select(APIToken.token).where(APIToken.tenant_id == owner)) == "fixture-independent-api-key"


@pytest.mark.parametrize("name", ["0", "1.0", "+1", "01"])
def test_exact_legacy_numeric_looking_aliases_survive(owned_engine: Engine, name: str) -> None:
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name=name, mdl_type="chat", api_key="fixture", api_base=""))
        db.commit()
    with Session(owned_engine) as db:
        assert get_model_config_by_type_and_name(db, owner, "chat", name)["llm_name"] == name


def test_legacy_management_guard_denies_linked_records_before_probing(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from api.apps.llm_app import AddLLMRequest, add_llm

    client, owner, engine, _module = http
    create(client)
    with Session(engine) as db:
        with pytest.raises(HTTPException, match="409"):
            asyncio.run(add_llm(AddLLMRequest(llm_factory="OpenAI", llm_name="catalog-chat", mdl_type="chat", api_key="must-not-probe"), db, SimpleNamespace(id=owner)))
    with Session(engine) as db:
        assert db.scalar(select(TenantModelInstance.api_key)) == "fixture-shared-key"
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 1


def test_real_model_http_and_worker_rebind_keep_instance_identity(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from api.db.joint_services.tenant_model_service import rebind_model_config
    from api.db.services import tenant_llm_service
    from api.db.services.llm_service import LLMBundle

    client, owner, engine, _module = http
    journal = ResourceJournal("model-http-worker-rebind")
    requests_seen: list[tuple[str, str, str]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append((self.path, self.headers["Authorization"], payload["model"]))
            content = json.dumps(
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with ExitStack() as cleanup:
        port = 30000 + int(uuid4().hex[:4], 16) % 20000
        address = ("127.0.0.1", port)
        journal.event("reserve-listener", address=list(address))
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="provider-fixture")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        for name in ("first", "second"):
            create(client, name, models=[])
            assert client.patch(f"/api/v1/providers/OpenAI/instances/{name}", json={"api_key": "fixture-" + name, "base_url": f"http://127.0.0.1:{port}/{name}/v1"}).status_code == 200
            assert client.post(f"/api/v1/providers/OpenAI/instances/{name}/models", json={"model_name": "same-raw-name", "model_types": ["chat"], "max_tokens": 8192}).status_code == 200
        identifiers = [item["id"] for item in client.get("/api/v1/models").json()["data"]["items"]]

        async def infer() -> None:
            import aiohttp
            import httpx
            import litellm
            from litellm.caching.llm_caching_handler import LLMClientCache
            from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

            assert GLOBAL_LOGGING_WORKER._worker_task is None
            owned_cache = LLMClientCache()
            monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", owned_cache)
            async_engine = create_async_engine(engine.url)
            event.listen(async_engine.sync_engine, "before_cursor_execute", engine._provider_reserve)
            monkeypatch.setattr(tenant_llm_service, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False))
            try:
                for identifier in identifiers:
                    with Session(engine) as db:
                        config = get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat")
                        original = LLMBundle(db, owner, config, max_retries=1)
                        original.db = None
                        rebuilt = rebind_model_config(db, original, "chat")
                        assert rebuilt["id"] == identifier and rebuilt["api_base"] == config["api_base"]
                        bundle = LLMBundle(db, owner, rebuilt, max_retries=1)
                        bundle.db = None
                    try:
                        assert await bundle.async_chat("", [{"role": "user", "content": "hello"}], {"temperature": 0}) == "fixture answer"
                    finally:
                        await bundle.aclose()
                        await original.aclose()
            finally:
                await GLOBAL_LOGGING_WORKER.stop()
                await GLOBAL_LOGGING_WORKER.clear_queue()
                await litellm.close_litellm_async_clients()
                seen: set[int] = set()
                sessions: list[Any] = []

                def collect(value: Any) -> None:
                    if id(value) in seen:
                        return
                    seen.add(id(value))
                    if isinstance(value, (aiohttp.ClientSession, httpx.AsyncClient)):
                        sessions.append(value)
                    fields = vars(value) if hasattr(value, "__dict__") else {}
                    for key in ("client", "_client", "client_session", "_transport", "transport"):
                        if key in fields:
                            collect(fields[key])

                for handler in owned_cache.cache_dict.values():
                    collect(handler)
                collect(getattr(litellm, "base_llm_aiohttp_handler", None))
                # This test owns the isolated SDK cache. The installed SDK's
                # cleanup hook misses some wrapped transports, so close the
                # exact remaining sessions and independently read their state.
                for value in sessions:
                    if isinstance(value, aiohttp.ClientSession):
                        if not value.closed:
                            await value.close()
                    elif not value.is_closed:
                        await value.aclose()
                assert all(value.closed if isinstance(value, aiohttp.ClientSession) else value.is_closed for value in sessions)
                assert GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
                assert GLOBAL_LOGGING_WORKER._queue is None or GLOBAL_LOGGING_WORKER._queue.empty()
                journal.event("sdk-clients-cleaned", sessions=len(sessions), closed=True, logging_tasks_stopped=True)
                await async_engine.dispose()

        asyncio.run(infer())
        assert requests_seen == [("/first/v1/chat/completions", "Bearer fixture-first", "same-raw-name"), ("/second/v1/chat/completions", "Bearer fixture-second", "same-raw-name")]
        with Session(engine) as db:
            assert [db.get(TenantLLM, identifier).used_tokens for identifier in identifiers] == [3, 3]
    with socket.socket() as probe:
        assert probe.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-cleaned", address=list(address), absent=True, thread_stopped=True)


def test_readonly_legacy_projection_includes_disabled_models_without_opaque_keys(http: tuple[Any, ...]) -> None:
    from api.apps.llm_app import my_llms

    client, owner, engine, _module = http
    create(client)
    with Session(engine) as db:
        row = db.scalar(select(TenantLLM))
        row.status = "0"
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="legacy", mdl_type="chat", api_key="fixture-legacy", api_base="", max_tokens=0, status="0"))
        db.commit()
    with Session(engine) as db:
        for details in (False, True):
            data = json.loads(my_llms(details, db, SimpleNamespace(id=owner)).body)
            assert data["retcode"] == 0
            assert {item["name"] for item in data["data"]["OpenAI"]["llm"]} == {"catalog-chat@first", "legacy"}
            assert {item["status"] for item in data["data"]["OpenAI"]["llm"]} == {"0"}
            assert "model:" not in json.dumps(data) and "fixture-legacy" not in json.dumps(data) and "fixture-shared-key" not in json.dumps(data)
            if details:
                assert next(item for item in data["data"]["OpenAI"]["llm"] if item["name"] == "legacy")["max_tokens"] == 0


def test_default_ocr_and_parser_numeric_reference_preserve_model_tool_override(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.joint_services.tenant_model_service import resolve_parser_reference

    client, owner, engine, _module = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "PaddleOCR", "llm": []}])
    assert client.put("/api/v1/providers", json={"provider_name": "PaddleOCR"}).status_code == 200
    key = json.dumps({"api_key": "fixture-ocr", "is_tools": True, "PADDLEOCR_URL": "https://ocr.invalid", "PADDLEOCR_API_TOKEN": "fixture-token"})
    response = client.post(
        "/api/v1/providers/PaddleOCR/instances",
        json={"instance_name": "local", "api_key": key, "model_ids": [], "custom_model": {"model_name": "ocr-model", "model_types": ["ocr"], "max_tokens": 0, "is_tools": False}},
    )
    assert response.status_code == 200, response.text
    model = client.get("/api/v1/providers/PaddleOCR/instances/local/models").json()["data"]["items"][0]
    assert client.patch("/api/v1/models/default", json={"model_type": "ocr", "model_id": model["id"]}).status_code == 200
    with Session(engine) as db:
        config = get_tenant_default_model_by_type(db, owner, "ocr")
        assert config["mdl_type"] == "ocr" and config["max_tokens"] == 0 and config["is_tools"] is False
        assert json.loads(config["api_key_payload"])["PADDLEOCR_API_TOKEN"] == "fixture-token"
        assert resolve_parser_reference(db, owner, str(model["id"])) == ("PaddleOCR", str(model["id"]))
    assert client.delete("/api/v1/providers/PaddleOCR").status_code == 409


def test_workflow_embedding_consumer_selects_owner_and_capability(http: tuple[Any, ...]) -> None:
    from workflow_v2.component.knowledge_base_search_component import KnowledgeBaseSearchComponent

    client, owner, engine, _module = http
    create(client, models=[])
    response = client.post(
        "/api/v1/providers/OpenAI/instances/first/models",
        json={
            "model_name": "shared-embedding",
            "model_types": ["rerank", "embedding"],
            "max_tokens": 0,
            "is_tools": False,
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["id"]
    with Session(engine) as db:
        component = object.__new__(KnowledgeBaseSearchComponent)
        component.db = db
        kb = SimpleNamespace(tenant_id=owner, tenant_embd_id=identifier, embd_id="ignored-legacy-name")
        bundle = component._build_embedding_model([kb])
        assert bundle.llm_type == "embedding" and bundle.llm_name == "shared-embedding"
        assert bundle.tenant_id == owner
        foreign = SimpleNamespace(tenant_id=uuid4().hex, tenant_embd_id=identifier, embd_id="ignored")
        with pytest.raises(LookupError, match="not found"):
            component._build_embedding_model([foreign])


def test_async_construction_detaches_before_callback_returns(http: tuple[Any, ...]) -> None:
    import asyncio

    from api.db.joint_services.tenant_model_service import detached_bundle

    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]

    async def check() -> None:
        async_engine = create_async_engine(engine.url)
        try:
            async with AsyncSession(async_engine) as db:

                def construct(session: Session) -> Any:
                    config = get_model_config_by_id(session, identifier, tenant_id=owner, model_type="chat")
                    bundle = detached_bundle(session, owner, config)
                    assert bundle.db is None
                    return bundle

                bundle = await db.run_sync(construct)
                assert bundle.db is None
                assert await db.scalar(select(Tenant.id).where(Tenant.id == owner)) == owner
        finally:
            await async_engine.dispose()

    asyncio.run(check())


def test_backfill_parameter_binding_quotes_injection_and_idempotency(owned_engine: Engine) -> None:
    owner = "tenant-'quoted"
    factory = "provider'); DROP TABLE usr_ai.t_ai_tenant_models; --"
    credential = "private-'credential"
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory=factory, llm_name="model-'quoted", mdl_type="chat", api_key=credential, api_base="https://fixture.invalid/'path", max_tokens=0))
        db.commit()
    with owned_engine.begin() as connection:
        backfill(connection, owner)
        backfill(connection, owner)
    with Session(owned_engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 1
        provider = db.scalars(select(TenantModelProvider)).one()
        assert provider.tenant_id == owner and provider.provider_name == factory
        instance = db.scalars(select(TenantModelInstance)).one()
        assert instance.api_key == credential and instance.base_url == "https://fixture.invalid/'path"
        assert db.scalars(select(TenantModel)).one().model_name == "model-'quoted"


def test_backfill_failure_rolls_back_provider_instance_and_model_writes(owned_engine: Engine) -> None:
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="invalid-capability", mdl_type="invalid", api_key="private-fixture"))
        db.commit()
    with pytest.raises(RuntimeError, match="explicit repair"), owned_engine.begin() as connection:
        backfill(connection, owner)
    # A new session observes the failed transaction independently.
    with Session(owned_engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 1
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 0


def test_provider_connection_and_atomic_create_use_real_adapter_and_zero_write_failures(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    client, owner, engine, _module = http
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    journal = ResourceJournal("provider-connection-atomic-http")
    requests_seen: list[str] = []
    state = {"failure": False, "malformed": False}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append(payload["model"])
            if state["failure"]:
                status, body = 401, {"error": {"message": "probe-SECRET-key", "type": "authentication_error"}}
            elif state["malformed"]:
                status, body = 200, {"choices": []}
            else:
                status, body = (
                    200,
                    {
                        "id": "chatcmpl-probe",
                        "object": "chat.completion",
                        "created": 1,
                        "model": payload["model"],
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    },
                )
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    def counts() -> tuple[int, ...]:
        with Session(engine) as db:
            return tuple(db.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM))

    with ExitStack() as cleanup:
        address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address))
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="owned-provider-probe")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        candidate = {
            "api_key": "probe-SECRET-key",
            "base_url": f"http://127.0.0.1:{address[1]}/v1",
            "region": "",
            "model_ids": [],
            "custom_model": {"model_name": "custom-zero", "model_types": ["chat", "image2text"], "max_tokens": 0, "is_tools": False},
        }
        endpoint = "/api/v1/providers/OpenAI"
        assert counts() == (0, 0, 0, 0)
        verified = client.post(endpoint + "/connection", json=candidate)
        assert verified.status_code == 200, verified.text
        assert verified.json()["data"] == {"verified": True, "model_name": "custom-zero", "model_type": "chat"}
        assert counts() == (0, 0, 0, 0)
        state["failure"] = True
        rejected = client.post(endpoint + "/instances", json={"instance_name": "first", **candidate})
        assert rejected.status_code == 400 and "probe-SECRET-key" not in rejected.text
        assert counts() == (0, 0, 0, 0)
        state["failure"], state["malformed"] = False, True
        assert client.post(endpoint + "/instances", json={"instance_name": "first", **candidate}).status_code == 400
        assert counts() == (0, 0, 0, 0)
        state["malformed"] = False
        for name in ("first", "second"):
            saved = client.post(endpoint + "/instances", json={"instance_name": name, **candidate})
            assert saved.status_code == 200, saved.text
            assert "probe-SECRET-key" not in saved.text
        assert counts() == (1, 2, 2, 2)
        assert requests_seen == ["custom-zero"] * 5  # Verify + two failed creates + two actual revalidations.
        before = len(requests_seen)
        assert client.post(endpoint + "/instances", json={"instance_name": "first", **candidate}).status_code == 409
        assert client.post(endpoint + "/instances", json={"instance_name": "empty", **candidate, "custom_model": None}).status_code == 400
        assert len(requests_seen) == before and counts() == (1, 2, 2, 2)
        with Session(engine) as db:
            rows = db.scalars(select(TenantModelInstance)).all()
            assert all(row.api_key == "probe-SECRET-key" for row in rows)
            assert all(row.max_tokens == 0 for row in db.scalars(select(TenantLLM)))
            assert all(row.model_types == ["chat", "image2text"] for row in db.scalars(select(TenantModel)))
        assert "probe-SECRET-key" not in caplog.text
        assert client.delete(endpoint).status_code == 200
        assert counts() == (0, 0, 0, 0)
        journal.event("sql-independent-readback", tenant=owner, counts=list(counts()), provider_calls=len(requests_seen))
    with socket.socket() as reader:
        assert reader.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-independent-readback", address=list(address), absent=True, thread_absent=True)


def test_region_catalog_effective_status_order_and_legacy_key_readback(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.joint_services.tenant_model_service import resolve_model_type

    client, owner, engine, _module = http
    factories = [
        {"name": "SILICONFLOW", "rank": "950", "llm": [{"llm_name": "CN_only", "mdl_type": "embedding", "max_tokens": 0}]},
        {"name": "siliconflow_intl", "llm": [{"llm_name": "intl_only", "mdl_type": "chat", "max_tokens": 0}]},
        {"name": "LocalProvider", "llm": []},
        {"name": "OpenAI", "rank": "999", "llm": [{"llm_name": "chat", "mdl_type": "chat", "max_tokens": 0}]},
    ]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    candidate = {"api_key": "shared", "region": "intl", "model_ids": ["intl_only"]}
    for name in ("z_last", "a_first"):
        response = client.post("/api/v1/providers/SILICONFLOW/instances", json={"instance_name": name, **candidate})
        assert response.status_code == 200, response.text
    assert client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "other", "api_key": "shared"}).status_code == 200
    assert client.put("/api/v1/providers", json={"provider_name": "LocalProvider"}).status_code == 200
    providers = client.get("/api/v1/providers").json()["data"]["items"]
    assert [row["name"] for row in providers] == ["OpenAI", "SILICONFLOW", "LocalProvider"]
    assert "siliconflow_intl" not in {row["name"] for row in client.get("/api/v1/providers?available=true").json()["data"]["items"]}
    supported = client.get("/api/v1/providers/SILICONFLOW/instances/a_first/models?supported=true").json()["data"]["items"]
    assert [row["model_name"] for row in supported] == ["intl_only"]
    assert client.get("/api/v1/providers/SILICONFLOW/models?region=intl").json()["data"]["items"][0]["model_name"] == "intl_only"
    items = client.get("/api/v1/models").json()["data"]["items"]
    assert [(row["provider_name"], row["instance_name"]) for row in items] == [("OpenAI", "other"), ("SILICONFLOW", "a_first"), ("SILICONFLOW", "z_last")]
    identifier = items[1]["id"]
    assert client.patch("/api/v1/providers/SILICONFLOW/instances/a_first", json={"status": "inactive"}).status_code == 200
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier).status == "1"
        with pytest.raises(LookupError):
            resolve_model_type(db, owner, str(identifier))
        with pytest.raises(LookupError):
            resolve_model_type(db, uuid4().hex, str(items[0]["id"]))
        other = db.scalar(select(TenantModelInstance).where(TenantModelInstance.instance_name == "z_last"))
        assert other is not None
        other.api_key = json.dumps({"api_key": "historical-key", "source_fid": "siliconflow_intl"})
        other.region = ""
        db.commit()
    with Session(engine) as db:
        config = get_model_config_by_id(db, items[2]["id"], tenant_id=owner, model_type="chat")
        assert config["api_key"] == "historical-key" and config["region"] == "intl" and config["api_base"] == "https://api.siliconflow.com/v1"
    item = next(row for row in client.get("/api/v1/models").json()["data"]["items"] if row["id"] == identifier)
    assert item["status"] == "active" and item["effective_status"] == "inactive"
