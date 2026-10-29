"""Pinned Web clients over real JWT/HTTP/SQL; generation and retrieval controlled."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import ExitStack
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from api.db.db_models import Connector, Connector2Kb, Document, File, Knowledgebase, Search, SyncLogs, Task, Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider, get_async_db
from tests.support.resource_journal import ResourceJournal
from tests.support.rest_document_contract import rest_documents as rest_documents
from tests.support.web_consumer import pinned_web_checkout as _pinned_checkout
from tests.support.web_consumer import private_consumer_file as _private_file

pytestmark = pytest.mark.external_consumer
ROOT = Path(__file__).resolve().parents[2]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _sql_state(engine: sa.Engine) -> dict[str, list[dict[str, Any]]]:
    models = (Connector, Connector2Kb, SyncLogs, Task, File, Document, Tenant, Knowledgebase, Search, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider)
    with engine.connect() as reader:
        return {model.__tablename__: [dict(row) for row in reader.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key.columns)).mappings()] for model in models}


def _run_consumer(checkout: Path, script: Path, descriptor: Path, evidence: Path, journal: ResourceJournal) -> dict[str, Any]:
    """Keep the real Node fetch transport behind its own durable process gate."""
    node = shutil.which("node")
    assert node is not None and script.is_file(), "Node and the prepared Web consumer are required"
    runner = Path(os.environ.get("MULTIRAG_CANONICAL_RUNNER", str(ROOT / "scripts/run_registered.py")))
    inputs = [
        runner,
        runner.with_name("resource_journal.py"),
        Path(__file__),
        ROOT / "tests/support/web_consumer.py",
        script,
        descriptor,
        Path(node),
        ROOT / ".github/consumer-web.json",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    for folder in ("api", "common", "core", "agent", "deepdoc"):
        inputs.extend(path for path in (ROOT / folder).rglob("*.py") if path.is_file())
    inputs.extend(path for path in (ROOT / "tests/support").rglob("*.py") if path.is_file())
    configuration_roots = {ROOT / "configs", Path(os.environ.get("RAG_PROJECT_BASE", str(ROOT))) / "configs"}
    for configuration_root in configuration_roots:
        inputs.extend(path for path in configuration_root.rglob("*") if path.is_file() and path.suffix in {".json", ".yaml", ".yml"})
    inputs.extend(path for path in (checkout / "src").rglob("*") if path.is_file() and path.suffix in {".ts", ".tsx", ".json"})
    inputs.extend(checkout / name for name in ("package.json", "package-lock.json", "tsconfig.json", "tsconfig.app.json") if (checkout / name).is_file())
    for package in ("tsx", "esbuild", "@esbuild", "get-tsconfig", "resolve-pkg-maps"):
        inputs.extend(path for path in (checkout / "node_modules" / package).resolve().rglob("*") if path.is_file())
    for package in (checkout / "node_modules/@esbuild").iterdir():
        inputs.extend(path for path in package.resolve().rglob("*") if path.is_file())
    if overlay := os.environ.get("MULTIRAG_CONFIG_OVERLAY_FILE"):
        inputs.append(Path(overlay))
    command = [sys.executable, "-B", str(runner), "--evidence-root", str(evidence), "--label", "search-azure-real-web"]
    for path in dict.fromkeys(inputs):
        command.extend(("--input", str(path)))
    command.extend(("--borrowed", str(checkout / "node_modules"), "--borrowed", node, "--", node, "--import", "tsx", str(script)))
    journal.event(
        "reserve-consumer-command",
        script=str(script),
        script_sha256=hashlib.sha256(script.read_bytes()).hexdigest(),
        descriptor_sha256=hashlib.sha256(descriptor.read_bytes()).hexdigest(),
        cwd=str(checkout),
        input_count=len(set(inputs)),
    )
    process = subprocess.Popen(
        command,
        cwd=checkout,
        env={**os.environ, "WEB_CONSUMER_DESCRIPTOR": str(descriptor), "TSX_TSCONFIG_PATH": str(checkout / "tsconfig.app.json")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    previous: dict[signal.Signals, Any] = {}

    def cancelled(signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt(f"Web consumer interrupted by signal {signum}")

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, cancelled)
        journal.event("consumer-gate-started", pid=process.pid)
        stdout, stderr = process.communicate(timeout=180)
    finally:
        for signum in previous:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=15)
            journal.event("consumer-gate-joined", pid=process.pid, exit_code=process.returncode)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    assert process.returncode == 0, stdout + stderr
    receipt = json.loads(stdout.strip().splitlines()[-1])
    assert receipt["raw_exit"] == receipt["exit_code"] == 0
    output = Path(receipt["journal"]).with_suffix(".output")
    events = [json.loads(line) for line in output.read_text().splitlines() if line.strip()]
    assert events[-1]["event"] == "consumer_complete", events
    journal.event("consumer-result", receipt=receipt, result=events[-1])
    return events[-1]


class _ConnectorReadback:
    """Observe committed SQL before delivering each mutation's final HTTP body."""

    def __init__(self, app: ASGIApp, engine: sa.Engine, reserved: set[str], journal: ResourceJournal, writes: list[dict[str, Any]]) -> None:
        self.app, self.engine, self.reserved, self.journal, self.writes = app, engine, reserved, journal, writes
        self.receipts: list[dict[str, Any]] = []
        self.failures: list[str] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method, path = scope.get("method"), scope.get("path", "")
        observe = scope["type"] == "http" and method in {"POST", "PATCH", "DELETE"} and (path == "/api/v1/connectors" or path.startswith("/api/v1/connectors/"))
        if not observe:
            await self.app(scope, receive, send)
            return
        before = await asyncio.to_thread(_sql_state, self.engine)
        first_write = len(self.writes)
        request_chunks: list[bytes] = []
        response_chunks: list[bytes] = []
        status = 0

        async def observed_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                request_chunks.append(message.get("body", b""))
            return message

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            if message["type"] == "http.response.body":
                response_chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    after = await asyncio.to_thread(_sql_state, self.engine)
                    body = json.loads(b"".join(response_chunks))
                    payload = json.loads(b"".join(request_chunks) or b"{}")
                    code = body.get("retcode", body.get("code"))
                    successful = status == 200 and code == 0
                    try:
                        if not successful:
                            assert after == before, "rejected mutation changed SQL"
                            assert len(self.writes) == first_write, "rejected mutation issued SQL DML"
                        else:
                            assert all(after[key] == value for key, value in before.items() if key != Connector.__tablename__), "mutation changed protected rows"
                            rows = {row["id"]: row for row in after[Connector.__tablename__]}
                            identifier = body["data"]["id"] if method == "POST" else path.rsplit("/", 1)[-1]
                            assert identifier in self.reserved, "mutation escaped reserved IDs"
                            row = rows[identifier]
                            assert row["source"] == "azure_blob" and row["config"] == payload["config"], "persisted Azure configuration differs"
                            assert row["refresh_freq"] == row["prune_freq"] == 0 and row["timeout_secs"] == 60, "schedule changed"
                            assert row["config"]["sync_deleted_files"] is False, "Azure prune was enabled"
                            expected = {
                                "account_key": {"account_name", "account_key", "container_name"},
                                "connection_string": {"connection_string", "container_name"},
                                "sas_token": {"container_url", "sas_token"},
                            }
                            assert set(row["config"]["credentials"]) == expected[row["config"]["auth_mode"]], "inactive credentials were persisted"
                            before_ids = {row["id"] for row in before[Connector.__tablename__]}
                            assert set(rows) == before_ids | ({identifier} if method == "POST" else set()), "unexpected connector identity"
                    except AssertionError as error:
                        self.failures.append(str(error))
                    receipt = {
                        "method": method,
                        "path": path,
                        "code": code,
                        "successful": successful,
                        "before_sha256": _hash(before),
                        "after_sha256": _hash(after),
                        "dml_count": len(self.writes) - first_write,
                    }
                    self.receipts.append(receipt)
                    self.journal.event("independent-http-sql-readback", **receipt)
            await send(message)

        await self.app(scope, observed_receive, observed_send)


def test_actual_web_search_defaults_and_azure_static_crud(
    rest_documents: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request: pytest.FixtureRequest, record_property: Callable[[str, object], None]
) -> None:
    from api import apps
    from api.apps.services import connector_api_service
    from api.db.joint_services import tenant_model_service
    from api.db.services.llm_service import LLMBundle
    from common import resources, settings
    from common.data_source import azure_blob_connector
    from core.utils.redis_conn import REDIS_CONN

    env, journal = rest_documents, rest_documents["journal"]
    checkout, web_sha = _pinned_checkout()
    script = Path(os.environ.get("WEB_SEARCH_CONNECTOR_CONSUMER", str(Path(__file__).parents[1] / "support/search_connector_web_consumer.ts")))
    private = tmp_path / "search-azure-private"
    evidence_parent = Path(os.environ.get("WEB_SEARCH_CONNECTOR_EVIDENCE", str(journal.path.parent)))
    assert evidence_parent.is_dir(), "The registered consumer evidence parent must exist"
    evidence = evidence_parent / (journal.run_id + "-search-azure-web")
    assert not private.exists() and not evidence.exists(), "Consumer paths must be exclusively owned"
    owned_files: list[Path] = []
    descriptor = private / "descriptor.json"
    cleanup = ExitStack()
    request.addfinalizer(cleanup.close)

    def remove_private() -> None:
        for path in owned_files:
            path.unlink(missing_ok=True)
        if private.exists():
            private.rmdir()
        journal.event("consumer-private-cleanup", descriptor_absent=not descriptor.exists(), directory_absent=not private.exists())

    cleanup.callback(remove_private)
    journal.event("reserve-consumer-directories", private=str(private), evidence=str(evidence))
    private.mkdir(mode=0o700)
    evidence.mkdir(mode=0o700)
    owners = env["owners"]
    numeric_ids = [17001, 17002]
    provider_ids, instance_ids = [uuid4().hex for _ in owners], [uuid4().hex for _ in owners]
    search_id = uuid4().hex
    connectors = {mode: {"id": uuid4().hex, "name": "after67-w-" + uuid4().hex} for mode in ("account_key", "connection_string", "sas_token")}
    connector_ids = {row["id"] for row in connectors.values()}
    tei_model = "after67/controlled-embedding"
    database_endpoint = {"host": env["engine"].url.host, "port": env["engine"].url.port, "database": env["engine"].url.database}
    assert database_endpoint["host"] in {"127.0.0.1", "localhost", "::1"}, "Use one explicit scratch TCP endpoint"
    journal.event(
        "reserve-search-azure-scope",
        endpoint=database_endpoint,
        owners=owners,
        datasets=env["datasets"],
        models=numeric_ids,
        providers=provider_ids,
        instances=instance_ids,
        search=search_id,
        connectors=connectors,
        controlled_uuid_source="connector_api_service.get_uuid only",
    )
    async_engine = create_async_engine(env["engine"].url, poolclass=NullPool)

    def dispose_async_engine() -> None:
        asyncio.run(async_engine.dispose())

    cleanup.callback(dispose_async_engine)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False)
    phase = {"value": "seed"}
    writes: list[dict[str, Any]] = []
    attempted_effects: list[str] = []
    model_calls: list[dict[str, Any]] = []
    retrieval_calls: list[dict[str, Any]] = []
    generation_calls: list[str] = []

    def reserve_sql(_connection: sa.Connection, _cursor: Any, statement: str, _parameters: Any, context: Any, _many: bool) -> None:
        if not any(getattr(context, name, False) for name in ("isinsert", "isupdate", "isdelete")):
            return
        table = context.compiled.statement.table.name
        identities = [{key: value for key, value in row.items() if key == "id" or "_id" in key or key.startswith("id_")} for row in context.compiled_parameters]
        event = {"table": table, "phase": phase["value"], "identities": identities, "statement_sha256": hashlib.sha256(statement.encode()).hexdigest()}
        journal.event("reserve-sql-dml", endpoint=database_endpoint, **event)
        writes.append(event)
        if phase["value"] == "consumer":
            assert table in {Connector.__tablename__, SyncLogs.__tablename__}, "Unexpected consumer SQL write"

    def reserve_orm(db: Session, _context: Any, _instances: Any) -> None:
        if db.get_bind() not in {env["engine"], async_engine.sync_engine}:
            return
        for row in db.new:
            journal.event("reserve-orm-row", endpoint=database_endpoint, table=row.__tablename__, id=getattr(row, "id", None))

    def remove_observer(target: Any, event_name: str, observer: Callable[..., Any]) -> None:
        if sa.event.contains(target, event_name, observer):
            sa.event.remove(target, event_name, observer)

    for target, event_name, observer in (
        (env["engine"], "before_cursor_execute", reserve_sql),
        (async_engine.sync_engine, "before_cursor_execute", reserve_sql),
        (Session, "before_flush", reserve_orm),
    ):
        cleanup.callback(remove_observer, target, event_name, observer)
        sa.event.listen(target, event_name, observer)
    original_state = _sql_state(env["engine"])

    async def scratch_async_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    class ControlledBundle(LLMBundle):
        def __init__(self, db: Session, tenant_id: str, config: dict[str, Any], **_kwargs: Any) -> None:
            # Preserve the real helper's runtime return type while controlling
            # client construction; no provider client is initialized here.
            self.db: Session | None = db
            self.max_length = 4096
            if config["mdl_type"] == "embedding":
                assert config["id"] is None and config["llm_factory"] == "Builtin" and config["llm_name"] == tei_model
            else:
                assert tenant_id == owners[0] and config["id"] == numeric_ids[0] and config["tenant_id"] == owners[0]
                assert config["api_base"] == "https://controlled.invalid/v1" and config["max_tokens"] == 0
            model_calls.append({"model_type": config["mdl_type"], "id": config["id"], "tenant_id": tenant_id, "max_tokens": config["max_tokens"]})

        async def async_chat_streamly_delta(self, _system: str, _messages: list[dict[str, Any]], _config: dict[str, Any]) -> AsyncIterator[str]:
            assert self.db is None, "The run_sync facade escaped detached_bundle"
            generation_calls.append(search_id)
            yield "<think>controlled</think>"
            yield "Visible controlled answer"

    class ControlledRetriever:
        async def retrieval(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["kb_ids"] == [env["datasets"][0]] and kwargs["doc_ids"] == []
            retrieval_calls.append({"kb_ids": kwargs["kb_ids"], "doc_ids": kwargs["doc_ids"]})
            return {"chunks": [], "doc_aggs": []}

        def insert_citations(self, answer: str, *_args: Any, **_kwargs: Any) -> tuple[str, set[str]]:
            return answer, set()

    def forbidden_effect(name: str) -> Callable[..., Any]:
        def rejected(*_args: Any, **_kwargs: Any) -> Any:
            attempted_effects.append(name)
            journal.event("forbidden-side-effect-attempt", boundary=name)
            raise AssertionError("Unexpected external write or Azure SDK construction")

        return rejected

    try:
        with Session(env["engine"]) as db:
            for index, owner_id in enumerate(owners):
                db.add(TenantModelProvider(id=provider_ids[index], tenant_id=owner_id, provider_name="OpenAI", status="active"))
                db.flush()
                db.add(
                    TenantModelInstance(
                        id=instance_ids[index],
                        provider_id=provider_ids[index],
                        instance_name="controlled",
                        api_key="controlled-test-only",
                        base_url="https://controlled.invalid/v1",
                        region="",
                        status="active",
                    )
                )
                db.add(
                    TenantLLM(
                        id=numeric_ids[index],
                        tenant_id=owner_id,
                        llm_factory="OpenAI",
                        llm_name="controlled-chat",
                        mdl_type="chat",
                        api_key="controlled-test-only",
                        api_base="https://controlled.invalid/v1",
                        max_tokens=0,
                        status="1",
                    )
                )
                db.flush()
                db.add(TenantModel(id=numeric_ids[index], instance_id=instance_ids[index], model_name="controlled-chat", model_types=["chat"], is_tools=False))
            owner = db.get(Tenant, owners[0])
            assert owner is not None
            owner.llm_id, owner.tenant_llm_id = str(numeric_ids[0]), numeric_ids[0]
            owner.embd_id, owner.tenant_embd_id = tei_model + "@Builtin", None
            dataset = db.get(Knowledgebase, env["datasets"][0])
            assert dataset is not None
            dataset.embd_id, dataset.tenant_embd_id = tei_model + "@Builtin", None
            db.add(Search(id=search_id, tenant_id=owners[0], created_by=owners[0], name="Controlled saved search", search_config={"kb_ids": [dataset.id], "doc_ids": []}, status="1"))
            db.commit()
        seeded = _sql_state(env["engine"])
        for index, identifier in enumerate(numeric_ids):
            row = next(item for item in seeded[TenantLLM.__tablename__] if item["id"] == identifier)
            assert row["tenant_id"] == owners[index]
        assert not any(row["llm_factory"] == "Builtin" for row in seeded[TenantLLM.__tablename__])
        assert not any(seeded[model.__tablename__] for model in (Connector, Connector2Kb, SyncLogs, Task, File, Document))
        prepared = {
            "webRoot": str(checkout),
            "baseURL": env["base"],
            "token": env["jwt"][0],
            "searchId": search_id,
            "kbIds": [env["datasets"][0]],
            "teiModel": tei_model,
            "modelIds": [numeric_ids[0]],
            "forbiddenModelIds": [numeric_ids[1]],
            "connectors": connectors,
        }
        _private_file(descriptor, json.dumps(prepared), journal, owned_files)
        with monkeypatch.context() as patch:
            patch.setenv("COMPOSE_PROFILES", "milvus,tei-cpu")
            patch.setenv("TEI_MODEL", tei_model)
            patch.setattr(settings, "FACTORY_LLM_INFOS", [*settings.FACTORY_LLM_INFOS, {"name": "Builtin", "llm": []}])
            patch.setattr(settings, "EMBEDDING_CFG", {"api_key": "", "base_url": "https://tei.invalid", "max_tokens": 0})
            journal.event(
                "controlled-generation-configuration",
                tei_model=tei_model,
                compose_profiles=os.environ["COMPOSE_PROFILES"],
                embedding_config_sha256=_hash(settings.EMBEDDING_CFG),
                real_model_resolution=True,
                controlled_boundaries=["LLMBundle construction/generation", "retrieval", "connector UUID source"],
            )
            patch.setitem(apps.app.dependency_overrides, get_async_db, scratch_async_db)
            patch.setattr(tenant_model_service, "LLMBundle", ControlledBundle)
            patch.setitem(resources._state, "retriever", ControlledRetriever())
            patch.setattr(azure_blob_connector, "ContainerClient", forbidden_effect("azure-sdk-constructor"))
            reserved_ids = iter(row["id"] for row in connectors.values())

            def reserved_uuid() -> str:
                return next(reserved_ids)

            patch.setattr(connector_api_service, "get_uuid", reserved_uuid)
            adapter = settings.STORAGE_IMPL
            for name in ("put", "rm", "remove_bucket"):
                if hasattr(adapter, name):
                    patch.setattr(adapter, name, forbidden_effect("storage-" + name))
            store = settings.docStoreConn
            for name in ("insert", "update", "delete", "create_idx", "delete_idx"):
                if hasattr(store, name):
                    patch.setattr(store, name, forbidden_effect("index-" + name))
            redis = REDIS_CONN.REDIS
            assert redis is not None
            redis_execute = redis.execute_command
            redis_endpoint = {key: redis.connection_pool.connection_kwargs.get(key) for key in ("host", "port", "db")}
            journal.event("reserve-readonly-redis-observer", endpoint=redis_endpoint, write_policy="reject before execution")

            def readonly_redis(*args: Any, **kwargs: Any) -> Any:
                command = args[0].decode() if isinstance(args[0], bytes) else str(args[0])
                if command.upper() not in {"GET", "MGET", "EXISTS", "SCAN", "HGET", "HGETALL", "TTL", "PTTL", "PING", "TYPE", "LLEN", "XRANGE", "XINFO"}:
                    return forbidden_effect("redis-" + command.upper())()
                return redis_execute(*args, **kwargs)

            patch.setattr(redis, "execute_command", readonly_redis)
            patch.setattr(redis, "pipeline", forbidden_effect("redis-pipeline"))
            middleware = apps.app.middleware_stack or apps.app.build_middleware_stack()
            observer = _ConnectorReadback(middleware, env["engine"], connector_ids, journal, writes)
            patch.setattr(apps.app, "middleware_stack", observer)
            phase["value"] = "consumer"
            result = _run_consumer(checkout, script, descriptor, evidence, journal)
            assert not observer.failures, observer.failures
            assert len(observer.receipts) == 9 and sum(row["successful"] for row in observer.receipts) == 6
            assert len(generation_calls) == len(retrieval_calls) == 1
            assert [row["model_type"] for row in model_calls] == ["embedding", "chat"]
            after = _sql_state(env["engine"])
            assert all(after[key] == value for key, value in seeded.items() if key != Connector.__tablename__)
            assert {row["id"] for row in after[Connector.__tablename__]} == connector_ids
            before_denial = _sql_state(env["engine"])
            foreign = requests.patch(
                env["base"] + "/api/v1/connectors/" + next(iter(connector_ids)), headers={"Authorization": "Bearer " + env["jwt"][1]}, json={"config": {"prefix": "foreign"}}, timeout=15
            )
            assert foreign.status_code == 200 and foreign.json()["retcode"] != 0
            assert _sql_state(env["engine"]) == before_denial and not observer.failures
            assert not attempted_effects
            assert not list(env["storage"].list_objects(env["bucket"], recursive=True))
            assert not redis.exists(env["queue"])
            assert all(not settings.docStoreConn.has_collection(name) for name in env["collections"])
            journal.event(
                "search-azure-independent-readback",
                web_sha=web_sha,
                endpoint=database_endpoint,
                selected_models=model_calls,
                retrieval=retrieval_calls,
                generation_count=len(generation_calls),
                connectors=sorted(connector_ids),
                http_sql_receipts=observer.receipts,
                protected_rows_unchanged=True,
                attempted_external_effects=attempted_effects,
                object_bucket_empty=True,
                queue_absent=True,
            )
            record_property(
                "actual_web_search_azure_sql",
                json.dumps(
                    {
                        "web_sha": web_sha,
                        "result": result,
                        "generation_count": len(generation_calls),
                        "mutation_readbacks": len(observer.receipts),
                        "controlled_boundaries": ["LLMBundle construction/generation", "retrieval", "connector UUID source"],
                    }
                ),
            )
    finally:
        phase["value"] = "cleanup"
        try:
            with Session(env["engine"]) as db:
                for original in original_state[Tenant.__tablename__]:
                    if original["id"] == owners[0]:
                        db.execute(sa.update(Tenant).where(Tenant.id == owners[0]).values(**{key: original[key] for key in ("llm_id", "tenant_llm_id", "embd_id", "tenant_embd_id")}))
                for original in original_state[Knowledgebase.__tablename__]:
                    if original["id"] == env["datasets"][0]:
                        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == original["id"]).values(embd_id=original["embd_id"], tenant_embd_id=original["tenant_embd_id"]))
                for model, condition in (
                    (SyncLogs, SyncLogs.connector_id.in_(connector_ids)),
                    (Connector2Kb, Connector2Kb.connector_id.in_(connector_ids)),
                    (Connector, Connector.id.in_(connector_ids)),
                    (Search, Search.id == search_id),
                    (TenantModel, TenantModel.id.in_(numeric_ids)),
                    (TenantLLM, TenantLLM.id.in_(numeric_ids)),
                    (TenantModelInstance, TenantModelInstance.id.in_(instance_ids)),
                    (TenantModelProvider, TenantModelProvider.id.in_(provider_ids)),
                ):
                    db.execute(sa.delete(model).where(condition))
                db.commit()
            final = _sql_state(env["engine"])
            for model, identifiers in (
                (Connector, connector_ids),
                (Search, {search_id}),
                (TenantModel, set(numeric_ids)),
                (TenantLLM, set(numeric_ids)),
                (TenantModelInstance, set(instance_ids)),
                (TenantModelProvider, set(provider_ids)),
            ):
                assert not any(row["id"] in identifiers for row in final[model.__tablename__]), "Owned SQL resource survived cleanup"
            assert not any(row["connector_id"] in connector_ids for model in (Connector2Kb, SyncLogs) for row in final[model.__tablename__])
            journal.event(
                "search-azure-sql-cleanup-readback", endpoint=database_endpoint, connector_ids=sorted(connector_ids), model_ids=numeric_ids, search_id=search_id, explicit_owned_rows_absent=True
            )
        finally:
            cleanup.close()
