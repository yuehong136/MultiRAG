"""Run the actual Web discovery client against an owned authenticated backend."""

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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, User, UserTenant, get_async_db
from api.utils.api_utils import async_current_user
from tests.support.resource_journal import ResourceJournal
from tests.support.uvicorn_runtime import run_server

ROOT = Path(__file__).resolve().parents[2]


def _private_file(path: Path, value: str, journal: ResourceJournal) -> None:
    journal.event("reserve-private-consumer-file", path=str(path), content_sha256=hashlib.sha256(value.encode()).hexdigest())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def _consumer(checkout: Path, descriptor: Path, phase: str, evidence: Path, journal: ResourceJournal) -> dict[str, Any]:
    runner = Path(os.environ.get("MULTIRAG_CANONICAL_RUNNER", str(ROOT / "scripts/run_registered.py")))
    node = shutil.which("node")
    assert node is not None, "Actual Web discovery requires Node.js"
    script = checkout / "scripts/verify-provider-discovery-routes.ts"
    assert script.is_file(), "Actual Web discovery consumer is required"
    inputs = [runner, runner.with_name("resource_journal.py"), Path(__file__), descriptor, script, Path(node)]
    configuration = json.loads(descriptor.read_text())
    inputs.extend(Path(configuration[key]) for key in ("ownerTokenFile", "providerKeyFile", "readbackFile") if Path(configuration[key]).is_file())
    for directory in ("src", "scripts"):
        inputs.extend(path for path in (checkout / directory).rglob("*") if path.is_file() and path.suffix in {".ts", ".tsx", ".json"})
    inputs.extend(path for path in checkout.iterdir() if path.is_file() and (path.suffix in {".json", ".ts"} or path.name in {"pnpm-lock.yaml", "yarn.lock"}))
    for package in ("tsx", "esbuild", "@esbuild", "get-tsconfig", "resolve-pkg-maps"):
        inputs.extend(path for path in (checkout / "node_modules" / package).resolve().rglob("*") if path.is_file())
    if (checkout / "node_modules/@esbuild").is_dir():
        for package in (checkout / "node_modules/@esbuild").iterdir():
            inputs.extend(path for path in package.resolve().rglob("*") if path.is_file())
    inputs.extend(
        ROOT / path
        for path in (
            "api/apps/restful_apis/provider_api.py",
            "api/db/model_contract.py",
            "api/db/provider_catalog.py",
            "api/db/db_models.py",
            "api/db/services/model_provider_service.py",
            "api/db/services/provider_verification.py",
            "api/utils/api_utils.py",
            "api/apps/__init__.py",
            "core/llm/model_meta.py",
            "core/llm/__init__.py",
            "core/llm/chat.py",
            "common/asyncio_utils.py",
            "common/settings.py",
            "common/app_config.py",
            "configs/llm_factories.json",
            "pyproject.toml",
            "uv.lock",
            ".github/consumer-web.json",
            "tests/support/uvicorn_runtime.py",
        )
    )
    if overlay := os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE"):
        inputs.append(Path(overlay))
    command = [sys.executable, "-B", str(runner), "--evidence-root", str(evidence), "--label", "discovery-web-" + phase]
    for path in dict.fromkeys(inputs):
        command.extend(("--input", str(path)))
    command.extend(("--borrowed", str(checkout / "node_modules"), "--", node, "--import", "tsx", str(script)))
    env = {**os.environ, "I_WEB_ACCEPTANCE_DESCRIPTOR": str(descriptor), "I_WEB_ACCEPTANCE_MODE": phase, "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")}
    journal.event("reserve-consumer-command", phase=phase, cwd=str(checkout), command=command, descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest())
    process = subprocess.Popen(command, cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        journal.event("consumer-runner-started", phase=phase, pid=process.pid)
        stdout, stderr = process.communicate(timeout=180)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)
        journal.event("consumer-runner-joined", phase=phase, pid=process.pid, exit_code=process.returncode)
    assert process.returncode == 0, stdout + stderr
    receipt = json.loads(stdout.strip().splitlines()[-1])
    assert receipt["raw_exit"] == receipt["exit_code"] == 0
    result = json.loads(Path(receipt["journal"]).with_suffix(".output").read_text().strip().splitlines()[-1])
    assert result["mode"] == phase and result["status"] == "passed", result
    journal.event("consumer-result", phase=phase, receipt=receipt, result=result)
    return result


def run_web_discovery(client: TestClient, owner: str, engine: Engine, base_url: str, journal: ResourceJournal, checkout: Path, tmp_path: Path) -> None:
    from api.apps import manager

    private = tmp_path / "discovery-web-private"
    evidence = journal.path.parent / (journal.run_id + "-web-consumer")
    files = [private / name for name in ("owner.token", "provider.key", "descriptor.json", "readback.json")]
    journal.event("reserve-consumer-directories", private=str(private), evidence=str(evidence), private_paths=[str(path) for path in files])
    assert not private.exists() and not evidence.exists()
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    membership = uuid4().hex
    email = owner + "@discovery.test"
    journal.event("reserve-web-auth", database=engine.url.database, owner=owner, membership=membership)
    with Session(engine) as db:
        assert db.get(User, owner) is None
        assert db.scalar(select(UserTenant.id).where(UserTenant.user_id == owner, UserTenant.tenant_id == owner)) is None
        assert db.scalar(select(func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id == owner)) == 0
        db.add(User(id=owner, email=email, nickname="Discovery Web contract", password="unused", access_token="active"))
        db.add(UserTenant(id=membership, user_id=owner, tenant_id=owner, role="owner", invited_by=owner, status="1"))
        db.commit()
    async_engine = create_async_engine(engine.url, poolclass=NullPool)
    reserve = getattr(engine, "_provider_reserve", None)
    if reserve is not None:
        event.listen(async_engine.sync_engine, "before_cursor_execute", reserve)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)

    async def session() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    sentinel = object()
    previous_auth = client.app.dependency_overrides.pop(async_current_user, sentinel)
    previous_db = client.app.dependency_overrides.get(get_async_db, sentinel)
    client.app.dependency_overrides[get_async_db] = session
    listener: socket.socket | None = None
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    address = ("127.0.0.1", 0)
    origin = ""
    token = manager.create_access_token(data={"sub": email})
    try:
        for _attempt in range(32):
            address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
            journal.event("reserve-listener", address=list(address), purpose="actual-web-discovery-backend")
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind(address)
                break
            except OSError:
                listener.close()
                listener = None
        assert listener is not None, "No owned backend listener available"
        server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_level="error", access_log=False))
        name = "web-discovery-" + journal.run_id[:12]
        journal.event("reserve-thread", name=name, listener=list(address))
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, name=name, daemon=True)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Owned backend failed to start"
        origin = f"http://127.0.0.1:{address[1]}"
        descriptor = {
            "origin": origin,
            "ownerTokenFile": str(files[0]),
            "providerKeyFile": str(files[1]),
            "providerName": "Ollama",
            "discovery": {"base_url": base_url, "region": ""},
            "instanceName": "web-discovery-" + journal.run_id[:12],
            "readbackFile": str(files[3]),
            "scratchDatabase": engine.url.database,
            "ownerId": owner,
            "invalidBaseURL": "https://arbitrary.invalid",
        }
        journal.event("reserve-web-model-resources", database=engine.url.database, owner=owner, provider="Ollama", instance=descriptor["instanceName"], models=["first", "second"])
        _private_file(files[0], token, journal)
        _private_file(files[1], "fixture-discovery-key", journal)
        _private_file(files[2], json.dumps(descriptor), journal)
        journal.event("reserve-consumer-readback", path=str(files[3]))
        result = _consumer(checkout, files[2], "owner", evidence, journal)
        readback = json.loads(files[3].read_text())
        identifiers = readback["modelIds"]
        assert identifiers == result["modelIds"] and identifiers and all(type(value) is int and value > 0 for value in identifiers)
        with Session(engine) as reader:
            provider = reader.scalars(select(TenantModelProvider).where(TenantModelProvider.tenant_id == owner)).one()
            assert provider.provider_name == "Ollama"
            instance = reader.scalars(select(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id)).one()
            assert instance.instance_name == descriptor["instanceName"] and instance.base_url == base_url and instance.api_key == "fixture-discovery-key" and instance.status == "active"
            models = reader.scalars(select(TenantModel).where(TenantModel.instance_id == instance.id)).all()
            assert {model.id for model in models} == set(identifiers)
            assert {model.model_name for model in models} == ({"first", "second"} if len(identifiers) == 2 else {"first"})
            for model in models:
                row = reader.get(TenantLLM, model.id)
                assert row is not None and row.tenant_id == owner and row.max_tokens == 0 and row.status == "1"
                assert model.model_types == ["chat", "image2text"] and model.is_tools is True
        journal.event("web-model-independent-readback", database=engine.url.database, owner=owner, numeric_model_ids=identifiers, multi_capability=True, max_tokens=0)
        _consumer(checkout, files[2], "cleanup", evidence, journal)
        with Session(engine) as reader:
            assert reader.scalar(select(func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id == owner)) == 0
            assert reader.scalar(select(func.count()).select_from(TenantLLM).where(TenantLLM.tenant_id == owner)) == 0
            assert all(reader.get(TenantModel, value) is None for value in identifiers)
            assert reader.get(TenantModelInstance, instance.id) is None
        journal.event("web-cleanup-independent-readback", database=engine.url.database, provider_instance_models_absent=True, numeric_model_ids=identifiers)
    except BaseException:
        if origin and server is not None and server.started:
            try:
                response = httpx.delete(origin + "/api/v1/providers/Ollama", headers={"Authorization": "Bearer " + token}, timeout=10)
                journal.event("web-failure-fallback-cleanup", status=response.status_code, provider="Ollama", owner=owner)
            except Exception as error:
                journal.event("web-failure-fallback-cleanup-failed", error_type=type(error).__name__)
        raise
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.ident is not None:
            thread.join(timeout=15)
        if listener is not None:
            listener.close()
        with socket.socket() as probe:
            probe.settimeout(1)
            absent = probe.connect_ex(address) != 0
        journal.event("listener-independent-readback", address=list(address), absent=absent, thread_absent=thread is None or not thread.is_alive())
        if previous_auth is not sentinel:
            client.app.dependency_overrides[async_current_user] = previous_auth
        if previous_db is sentinel:
            client.app.dependency_overrides.pop(get_async_db, None)
        else:
            client.app.dependency_overrides[get_async_db] = previous_db
        asyncio.run(async_engine.dispose())
        with Session(engine) as db:
            db.execute(delete(UserTenant).where(UserTenant.id == membership))
            db.execute(delete(User).where(User.id == owner))
            db.commit()
        for path in files:
            if path.exists():
                path.unlink()
        private.rmdir()
        journal.event("consumer-private-cleanup", files_absent=all(not path.exists() for path in files), directory_absent=not private.exists())
        assert absent and (thread is None or not thread.is_alive())
