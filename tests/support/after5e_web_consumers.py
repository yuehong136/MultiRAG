"""Owned PostgreSQL/JWT/HTTP boundary for the frozen after5e Web consumers."""

import asyncio
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import Tenant, User, UserTenant, get_async_db
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal
from tests.support.uvicorn_runtime import run_server
from tests.support.web_consumer import pinned_web_checkout, private_consumer_file

ROOT = Path(__file__).resolve().parents[2]


def durable_json(path: Path, value: dict[str, Any], journal: ResourceJournal) -> None:
    """Create one immutable, fsynced receipt after reserving its exact identity."""
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    journal.event("reserve-receipt", path=str(path), sha256=hashlib.sha256(payload.encode()).hexdigest())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


@pytest.fixture
def after5e_consumer_http(request: pytest.FixtureRequest, postgres_service: None) -> Iterator[dict[str, Any]]:
    """Serve production routers and authentication against one registered SQL DB."""
    from api.apps import app as production_app
    from api.apps import manager
    from api.apps.restful_apis import agent_api, connector_api

    journal = ResourceJournal(request.node.nodeid)
    parent = Path(os.environ["AFTER5E_EXTRA_CONSUMER_EVIDENCE"]).resolve(strict=True)
    evidence = parent / journal.run_id
    private = evidence / "private"
    journal.event("reserve-directories", evidence=str(evidence), private=str(private), owner="I", external_resources=[])
    evidence.mkdir(mode=0o700)
    private.mkdir(mode=0o700)
    version = 0

    def manifest(state: str, **fields: Any) -> None:
        nonlocal version
        version += 1
        durable_json(evidence / f"manifest-v{version:03}.json", {"schemaVersion": 1, "owner": "I", "state": state, **fields}, journal)

    private_paths: list[Path] = []
    manifest("reserved", journal=str(journal.path), evidence=str(evidence), private=str(private))
    try:
        with scratch_database(evidence) as engine, ExitStack() as cleanup:
            owners = [uuid4().hex, uuid4().hex]
            memberships = [uuid4().hex, uuid4().hex]
            emails = [owner + "@after5e.test" for owner in owners]

            def reserve_sql(_connection: Any, _cursor: Any, statement: str, _parameters: Any, context: Any, _many: bool) -> None:
                if statement.lstrip().split(maxsplit=1)[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
                    rows = getattr(context, "compiled_parameters", [])
                    identifiers = [{key: value for key, value in row.items() if key == "id" or key.endswith(("_id", "_id_1")) or key in {"id_1", "pk_1"}} for row in rows]
                    journal.event("reserve-sql-write", database=engine.url.database, statement_sha256=hashlib.sha256(statement.encode()).hexdigest(), identifiers=identifiers)

            sa.event.listen(engine, "before_cursor_execute", reserve_sql)
            cleanup.callback(sa.event.remove, engine, "before_cursor_execute", reserve_sql)
            journal.event("reserve-auth-rows", database=engine.url.database, owners=owners, memberships=memberships)
            with Session(engine) as db:
                for owner, membership, email in zip(owners, memberships, emails, strict=True):
                    db.add(User(id=owner, email=email, nickname="After5e consumer", password="unused", access_token="active", status="1"))
                    db.add(Tenant(id=owner, name="After5e scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive", status="1"))
                    db.add(UserTenant(id=membership, user_id=owner, tenant_id=owner, role="owner", invited_by=owner, status="1"))
                db.commit()
            tokens = [manager.create_access_token(data={"sub": email}) for email in emails]
            token_files = [private / "owner.token", private / "peer.token"]
            for path, token in zip(token_files, tokens, strict=True):
                private_consumer_file(path, token, journal, private_paths)
            async_engine = create_async_engine(engine.url, poolclass=NullPool)
            sa.event.listen(async_engine.sync_engine, "before_cursor_execute", reserve_sql)
            cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
            sessions = async_sessionmaker(async_engine, expire_on_commit=False)

            async def session() -> AsyncIterator[AsyncSession]:
                async with sessions() as db:
                    yield db

            app = FastAPI()
            app.exception_handlers.update(production_app.exception_handlers)
            app.include_router(agent_api.router, prefix="/api/v1")
            app.include_router(connector_api.router, prefix="/api/v1")
            app.dependency_overrides[get_async_db] = session
            listener = None
            address = ("127.0.0.1", 0)
            for _attempt in range(32):
                address = ("127.0.0.1", 20000 + int(uuid4().hex[:4], 16) % 30000)
                journal.event("reserve-listener", address=list(address), owner="I")
                candidate = socket.socket()
                try:
                    candidate.bind(address)
                except OSError:
                    candidate.close()
                    journal.event("listener-reservation-unavailable", address=list(address))
                else:
                    listener = candidate
                    break
            assert listener is not None, "No owned consumer HTTP listener available"
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", access_log=False))
            thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, name="after5e-http-" + journal.run_id)

            def stop() -> None:
                server.should_exit = True
                if thread.ident is not None:
                    thread.join(timeout=15)
                listener.close()
                with socket.socket() as probe:
                    probe.settimeout(1)
                    closed = probe.connect_ex(address) != 0
                journal.event("http-cleanup-readback", address=list(address), listener_absent=closed, thread_absent=not thread.is_alive())
                assert closed and not thread.is_alive()

            cleanup.callback(stop)
            journal.event("reserve-thread", name=thread.name, address=list(address))
            thread.start()
            deadline = time.monotonic() + 30
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "Owned consumer HTTP app did not start"
            with engine.connect() as reader:
                identity = reader.execute(sa.text("SELECT current_database(), version(), inet_server_addr()::text, inet_server_port()")).one()
            manifest("serving", database=identity[0], database_version=identity[1], database_address=identity[2], database_port=identity[3], owners=owners, address=list(address))
            yield {
                "base": f"http://{address[0]}:{address[1]}",
                "owners": owners,
                "tokens": tokens,
                "token_files": token_files,
                "engine": engine,
                "journal": journal,
                "evidence": evidence,
                "private": private,
                "private_paths": private_paths,
                "manifest": manifest,
            }
            manifest("case-finished", database=engine.url.database)
        manifest("database-context-closed", database_cleanup_journal="scratch_database append-only database-cleanup-readback")
    finally:
        for path in reversed(private_paths):
            path.unlink(missing_ok=True)
        assert not list(private.iterdir()), "Unexpected private payload retained; preserve it for explicit cleanup attribution"
        private.rmdir()
        journal.event("private-cleanup-readback", paths=[str(path) for path in private_paths], payloads_absent=all(not path.exists() for path in private_paths), directory_absent=not private.exists())
        manifest("private-payloads-cleaned", private_absent=not private.exists())


def run_consumer(env: dict[str, Any], script_name: str, descriptor: dict[str, Any], sql_readback: Callable[[], dict[str, Any] | None]) -> dict[str, Any]:
    """Run pinned Web; write its independently observed SQL sidecar only once."""
    checkout, web_sha = pinned_web_checkout()
    journal: ResourceJournal = env["journal"]
    evidence: Path = env["evidence"]
    script = checkout / "scripts" / script_name
    assert script.is_file(), "Pinned Web consumer script is required: " + str(script)
    configuration = {
        "schemaVersion": 1,
        "ownedOrigin": env["base"],
        "ownerTokenFile": str(env["token_files"][0]),
        "peerTokenFile": str(env["token_files"][1]),
        "readbackFile": str(env["private"] / "sql-readback.json"),
        "evidenceRoot": str(evidence),
        "webSourceRoot": str(checkout),
        "webSha": web_sha,
        "backendSha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "backendVersion": tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"],
        **descriptor,
    }
    config = env["private"] / "descriptor.json"
    private_consumer_file(config, json.dumps(configuration), journal, env["private_paths"])
    readback = Path(configuration["readbackFile"])
    journal.event("reserve-sql-sidecar", path=str(readback), writer="I independent SQLAlchemy reader", reader=script_name)
    node = shutil.which("node")
    assert node is not None, "Actual Web consumer requires Node.js"
    runner = ROOT / "scripts/run_registered.py"
    inputs = [runner, runner.with_name("resource_journal.py"), Path(__file__), config, *env["token_files"], Path(node), ROOT / "pyproject.toml", ROOT / "uv.lock", ROOT / ".github/consumer-web.json"]
    for source in (ROOT / "api", ROOT / "common", ROOT / "core", ROOT / "tests", checkout / "src", checkout / "scripts"):
        inputs.extend(path for path in source.rglob("*") if path.is_file() and path.suffix in {".py", ".ts", ".tsx", ".json", ".yaml"})
    inputs.extend(path for path in checkout.iterdir() if path.is_file() and (path.suffix in {".json", ".ts"} or path.name in {"pnpm-lock.yaml", "yarn.lock"}))
    for package in ("tsx", "esbuild", "@esbuild", "get-tsconfig", "resolve-pkg-maps"):
        inputs.extend(path for path in (checkout / "node_modules" / package).resolve().rglob("*") if path.is_file())
    if overlay := os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE"):
        inputs.append(Path(overlay))
    command = [sys.executable, "-B", str(runner), "--evidence-root", str(evidence), "--label", script.stem]
    for path in dict.fromkeys(inputs):
        command.extend(("--input", str(path)))
    command.extend(("--borrowed", str(checkout / "node_modules"), "--", node, "--import", "tsx", str(script), str(config)))
    output_path = evidence / "web-runner.output"
    journal.event("reserve-consumer-command", command=command, output=str(output_path), web_sha=web_sha, backend_sha=configuration["backendSha"])
    process = None
    try:
        with output_path.open("x") as output:
            process = subprocess.Popen(
                command, cwd=checkout, env={**os.environ, "I_WEB_ACCEPTANCE_DESCRIPTOR": str(config), "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")}, stdout=output, stderr=subprocess.STDOUT
            )
            journal.event("consumer-runner-started", pid=process.pid, script=script_name)
            deadline = time.monotonic() + 180
            while process.poll() is None:
                if not readback.exists() and (observed := sql_readback()) is not None:
                    private_consumer_file(readback, json.dumps({"schemaVersion": 1, **observed}), journal, env["private_paths"])
                if time.monotonic() >= deadline:
                    raise TimeoutError("Registered Web consumer timed out")
                time.sleep(0.05)
            if not readback.exists() and (observed := sql_readback()) is not None:
                private_consumer_file(readback, json.dumps({"schemaVersion": 1, **observed}), journal, env["private_paths"])
            assert process.returncode == 0, output_path.read_text()
        assert readback.is_file(), "Web consumer did not reach independently read-back SQL state"
        receipt = json.loads(output_path.read_text().strip().splitlines()[-1])
        assert receipt["raw_exit"] == receipt["exit_code"] == 0
        journal.event("consumer-receipt", script=script_name, receipt=receipt, web_sha=web_sha, sql_sidecar=str(readback))
        return {"web_sha": web_sha, "runner": receipt, "readback": json.loads(readback.read_text())}
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=30)
        journal.event(
            "consumer-runner-cleanup-readback",
            pid=process.pid if process is not None else None,
            joined=process is None or process.poll() is not None,
            exit_code=process.returncode if process is not None else None,
        )
