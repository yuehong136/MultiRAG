"""Shared owned PostgreSQL and HTTP fixtures for provider-instance contracts."""

import hashlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import Tenant, get_async_db
from api.db.model_contract import ModelRegistration
from api.db.services import provider_verification
from common import resources, settings
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal

REAL_VERIFY_MODEL = provider_verification.verify_model


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
def http(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, Engine, ModuleType]]:
    async def probe_boundary(_provider: str, _key: str, _base: str, declaration: ModelRegistration) -> dict[str, Any]:
        return {"verified": True, "model_name": declaration.model_name, "model_type": declaration.model_types[0]}

    # Isolate SQL/lifecycle semantics. Dedicated connection cases restore the
    # real active adapter and use owned loopback HTTP.
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

    async def session() -> AsyncIterator[AsyncSession]:
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
