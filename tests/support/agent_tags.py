"""Real registered Agent HTTP API on a caller-owned scratch database."""

import asyncio
import json
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn
from requests import Session as HTTPSession
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.db.db_models import Tenant, User, UserCanvas, UserTenant, get_async_db, get_db


@contextmanager
def tags_http_api(engine: sa.Engine, evidence: Path) -> Iterator[dict[str, Any]]:
    from api import apps
    from api.db import db_models

    evidence.mkdir(parents=True, exist_ok=True)
    owners = [uuid4().hex for _ in range(4)]
    sessions = sessionmaker(engine, expire_on_commit=False)
    manifest: dict[str, Any] = {"database": engine.url.database, "pid": os.getpid(), "owners": owners, "started": False, "stopped": False}
    path = evidence / "http-resources.json"

    def save() -> None:
        path.write_text(json.dumps(manifest, indent=2))

    with ExitStack() as cleanup, pytest.MonkeyPatch.context() as patch:
        cleanup.callback(save)

        def remove_replicas() -> None:
            # Agent detail reads initialize runtime replicas. Every matching
            # tenant and runtime user below belongs to this scratch fixture.
            from core.utils.redis_conn import REDIS_CONN

            client = REDIS_CONN.REDIS
            assert client is not None
            keys = []
            for owner in owners:
                for prefix in ("canvas:replica", "canvas:replica:lock"):
                    keys.extend(client.scan_iter(match=f"{prefix}:*:{owner}:*"))
            manifest["redis_keys"] = sorted(key.decode() for key in keys)
            if keys:
                client.delete(*keys)
            assert all(not client.exists(key) for key in keys)
            manifest["redis_keys_absent"] = True

        cleanup.callback(remove_replicas)
        with sessions() as db:
            for index, owner in enumerate(owners):
                db.add(User(id=owner, email=f"{owner}@tags.example.com", nickname=f"Tags {index}", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name=f"Tags {index}", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=owner, role="owner", invited_by=owner))
            db.commit()
        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        def scratch_db() -> Iterator[Session]:
            with sessions() as db:
                yield db

        async def scratch_async_db() -> AsyncIterator[AsyncSession]:
            async with async_sessions() as db:
                yield db

        patch.setattr(apps, "SessionLocal", sessions)
        patch.setattr(db_models, "SessionLocal", sessions)
        patch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
        patch.setitem(apps.app.dependency_overrides, get_async_db, scratch_async_db)
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        manifest["port"] = listener.getsockname()[1]
        base = f"http://127.0.0.1:{manifest['port']}"
        save()
        server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop() -> None:
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive()
            manifest["stopped"] = True

        cleanup.callback(stop)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        manifest["started"] = True
        save()
        jwts = [apps.manager.create_access_token(data={"sub": f"{owner}@tags.example.com"}) for owner in owners]
        with HTTPSession() as client:
            client.headers["Authorization"] = f"Bearer {jwts[0]}"
            yield {"base": base, "owners": owners, "jwts": jwts, "client": client, "engine": engine}


def seed_agent(env: dict[str, Any], *, owner: int = 0, tags: str = "", permission: str = "me", category: str = "agent_canvas", title: str = "Tags Agent") -> str:
    agent_id = uuid4().hex
    with Session(env["engine"]) as db:
        db.add(UserCanvas(id=agent_id, user_id=env["owners"][owner], title=title, tags=tags, permission=permission, canvas_category=category, dsl={"components": {}, "history": []}))
        db.commit()
    return agent_id


def read_agent(env: dict[str, Any], agent_id: str) -> dict[str, Any]:
    with Session(env["engine"]) as db:
        row = db.get(UserCanvas, agent_id)
        assert row is not None
        return row.to_dict()
