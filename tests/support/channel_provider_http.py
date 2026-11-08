"""Actual Channel handlers and SQL with controlled browser authentication.

Callers supply a scratch engine. Provider traffic and Web authentication are
controlled boundaries; the control service, row-bound cipher and SQL are real.
No business startup, supervisor or provider connection is launched.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.channel_control.callback_inbox import callback_inbox_key
from api.channel_control.dependencies import get_channel_control_service
from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_control.secret_store import AESGCMChannelSecretStore
from api.channel_control.service import ChannelControlService
from api.channel_execution.dependencies import StaticBearerWorkloadAuthenticator, _redis_host_port, get_channel_execution_redis, require_channel_workload
from api.channel_execution.models import WorkloadIdentity
from api.db.db_models import Dialog
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, Principal, TenantMembershipEvidence, build_principal_from_authenticated_actor
from common.app_config import get_app_config
from common.channel_secret_crypto import ChannelSecretCipher
from tests.support.resource_journal import ResourceJournal
from tests.support.uvicorn_runtime import run_server


@contextmanager
def channel_provider_http(engine: sa.Engine, evidence: Path) -> Iterator[dict[str, Any]]:
    from common import resources

    # Importing the router package must not initialize unrelated model/storage
    # services in this deliberately narrow HTTP harness.
    previous_resources = dict(resources._state)
    resources._state.setdefault("secret_key", "channel-controlled-auth")
    from api.apps.restful_apis import channel_callback_api, channel_runtime_api, chat_channel_api
    from api.utils.api_utils import async_current_user

    journal = ResourceJournal("channel-provider-http")
    owners = {name: uuid4().hex for name in ("owner", "other")}
    tokens = {name: "channel-http-" + uuid4().hex for name in owners}
    dialogs = {name: uuid4().hex for name in owners}
    factory_engine = create_async_engine(engine.url)
    factory = async_sessionmaker(factory_engine, expire_on_commit=False)
    cipher = AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32)))
    master = "channel-workload-" + uuid4().hex
    authenticator = StaticBearerWorkloadAuthenticator(SecretStr(master))
    keys: set[str] = set()
    config = get_app_config().redis
    host, port = _redis_host_port(config.host)

    async def service() -> AsyncIterator[ChannelControlService]:
        async with factory() as db:
            yield ChannelControlService(SqlAlchemyChannelRepository(db), cipher)

    async def redis_client() -> AsyncIterator[Redis]:
        client = Redis(host=host, port=port, db=config.db, username=config.username or None, password=config.password or None, decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        try:
            yield client
        finally:
            await client.aclose()

    def user(request: Request) -> Principal:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        name = next((name for name, value in tokens.items() if value == token), None)
        if name is None:
            raise HTTPException(401, "Controlled authentication required")
        actor = owners[name]
        return build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id=actor),
            membership=TenantMembershipEvidence(platform_user_id=actor, tenant_id=actor),
            authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
        )

    async def workload(request: Request) -> WorkloadIdentity:
        return await authenticator.authenticate(request)

    def reserve_inbox(binding_id: str, generation: int, *, owner: str = "owner") -> str:
        key = callback_inbox_key(owners[owner], binding_id, generation)
        journal.event("reserve-redis-key", key=key, tenant=owners[owner], binding=binding_id, generation=generation, borrowed_endpoint={"host": host, "port": port, "db": config.db})
        keys.add(key)
        return key

    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1):[0-9]+", allow_methods=["*"], allow_headers=["*"])
    app.include_router(chat_channel_api.router, prefix="/api/v1")
    app.include_router(channel_callback_api.router, prefix="/api/v1")
    app.include_router(channel_runtime_api.router, prefix="/api/v1")
    app.dependency_overrides.update({get_channel_control_service: service, get_channel_execution_redis: redis_client, async_current_user: user, require_channel_workload: workload})
    with Session(engine) as db:
        for name in owners:
            db.add(Dialog(id=dialogs[name], tenant_id=owners[name], name="channel-target-" + name, llm_id="controlled-unused"))
        db.commit()
    journal.event("reserve-listener", host="127.0.0.1", port=0, pid=os.getpid(), pgid=os.getpgrp())
    listener = socket.socket()
    thread: threading.Thread | None = None
    server: uvicorn.Server | None = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        address = listener.getsockname()
        journal.event("listener-created", host=address[0], port=address[1], pid=os.getpid(), pgid=os.getpgrp())
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, lifespan="off"))
        journal.event("reserve-thread", name="channel-provider-http", pid=os.getpid(), pgid=os.getpgrp())
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True, name="channel-provider-http")
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started and thread.is_alive(), "owned Channel HTTP listener failed to start"
        yield {
            "base": f"http://127.0.0.1:{address[1]}",
            "tokens": tokens,
            "owners": owners,
            "dialogs": dialogs,
            "engine": engine,
            "cipher": cipher,
            "master_token": master,
            "reserve_inbox": reserve_inbox,
            "redis": {"host": host, "port": port, "db": config.db, "username": config.username or None, "password": config.password or None},
            "journal": journal,
        }
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=10)
            assert not thread.is_alive()
        listener.close()
        asyncio.run(factory_engine.dispose())
        from redis import Redis as SyncRedis

        with SyncRedis(host=host, port=port, db=config.db, username=config.username or None, password=config.password or None) as writer:
            if keys:
                writer.delete(*keys)
        with SyncRedis(host=host, port=port, db=config.db, username=config.username or None, password=config.password or None) as observer:
            absent = not keys or observer.exists(*keys) == 0
            journal.event("redis-cleanup-readback", keys=sorted(keys), absent=absent)
            assert absent
        journal.event("listener-cleanup-readback", fd=listener.fileno(), thread_absent=thread is None or not thread.is_alive())
        resources._state.clear()
        resources._state.update(previous_resources)
