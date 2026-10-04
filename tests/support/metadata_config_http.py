"""Isolated HTTP fixture for typed metadata configuration consumers."""

import asyncio
import copy
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, Knowledgebase, Tenant, User, UserTenant, get_async_db

FIELDS = [{"key": "author", "description": "Author", "enum": ["Alice"], "restrictDefinedValues": True}]

PARSER = {"chunk_token_num": 128, "delimiter": "\n", "raptor": {"use_raptor": True}, "metadata": []}


def sql_state(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Independent connection, complete rows, only this fixture's exact IDs."""
    result = {}
    with env["engine"].connect() as db:
        for model, ids in [(Knowledgebase, env["datasets"]), (Document, env["documents"])]:
            result[model.__tablename__] = {row["id"]: dict(row) for row in db.execute(sa.select(model.__table__).where(model.id.in_(ids))).mappings()}
    return result


@pytest.fixture
def management_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    with ExitStack() as cleanup:
        from api.apps import app, manager

        users = [uuid4().hex for _ in range(3)]
        datasets, documents = ([uuid4().hex for _ in range(3)] for _ in range(2))
        token_names = ["metadata-" + uuid4().hex[:15] for _ in users]
        tokens = ["metadata-key-" + uuid4().hex for _ in users]
        record = {"database": bootstrapped_engine.url.database, "users": users, "datasets": datasets, "documents": documents, "token_names": token_names}
        evidence = Path(os.environ.get("MULTIRAG_METADATA_EVIDENCE_DIR", str(tmp_path)))
        evidence.mkdir(parents=True, exist_ok=True)
        record_path = evidence / (users[0] + ".json")
        record_path.write_text(json.dumps(record))

        def remove_rows() -> None:
            with bootstrapped_engine.begin() as db:
                scopes = [(Document, documents), (Knowledgebase, datasets), (UserTenant, users), (APIToken, token_names), (Tenant, users), (User, users)]
                for model, ids in scopes:
                    column = model.user_id if model is UserTenant else model.name if model is APIToken else model.id
                    db.execute(sa.delete(model).where(column.in_(ids)))
            # Independent readback after the deletion transaction committed.
            with bootstrapped_engine.connect() as db:
                remaining = {}
                for model, ids in scopes:
                    column = model.user_id if model is UserTenant else model.name if model is APIToken else model.id
                    remaining[model.__tablename__] = db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(ids)))
                assert not any(remaining.values()), remaining
            record["remaining"] = remaining
            record_path.write_text(json.dumps(record))

        cleanup.callback(remove_rows)
        with Session(bootstrapped_engine) as db:
            for user, name, token in zip(users, token_names, tokens, strict=True):
                db.add(User(id=user, email=f"{user}@metadata.test", nickname="HTTP scratch", password="unused", access_token="active"))
                db.add(Tenant(id=user, name="HTTP scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, tenant_id=user, user_id=user, role="owner", invited_by=user))
                db.add(APIToken(tenant_id=user, token=token, name=name))
            db.add(UserTenant(id=uuid4().hex, tenant_id=users[0], user_id=users[2], role="admin", invited_by=users[0]))
            for i, (dataset, doc) in enumerate(zip(datasets, documents, strict=True)):
                owner = users[0] if i < 2 else users[1]
                db.add(Knowledgebase(id=dataset, tenant_id=owner, created_by=owner, name="Metadata scratch", embd_id="scratch", parser_id="naive", parser_config=copy.deepcopy(PARSER)))
                db.add(Document(id=doc, kb_id=dataset, created_by=owner, name="source.txt", parser_id="naive", type="txt", parser_config=copy.deepcopy(PARSER)))
            db.flush()
            db.commit()

        engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(engine.dispose()))
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def scratch_db() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        monkeypatch.setitem(app.dependency_overrides, get_async_db, scratch_db)
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        env = {
            "engine": bootstrapped_engine,
            "base": f"http://127.0.0.1:{port}",
            "users": users,
            "datasets": datasets,
            "documents": documents,
            "jwt": [manager.create_access_token(data={"sub": f"{user}@metadata.test"}) for user in users],
            "keys": tokens,
        }
        env["record_path"] = record_path
        record["port"] = port
        record_path.write_text(json.dumps(record))

        def stop_server() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive()
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", port)) != 0
            record["listener_closed"] = True
            record_path.write_text(json.dumps(record))

        cleanup.callback(stop_server)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield env


def request_api(env: dict[str, Any], method: str, suffix: str, *, credential: str | None = None, payload: Any = None, params: Any = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return requests.request(method, env["base"] + "/api/v1" + suffix, headers=headers, json=payload, params=params, timeout=30)


def metadata_path(env: dict[str, Any], document: bool = False) -> str:
    return f"/datasets/{env['datasets'][0]}" + (f"/documents/{env['documents'][0]}" if document else "") + "/metadata/config"
