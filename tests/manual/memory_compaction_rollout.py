"""Rehearse a compaction upgrade and a restore from an owned predecessor backup."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from tests.support.database import _alembic_config, _pg_url, scratch_database
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "fc24e6f8b0d2"
TARGET_REVISION = "0d35f7a9c1e3"
GENERATED = "rollout-owned-generated"
FAILED = "rollout-owned-failed"
ARTIFACT_TABLE = "usr_ai.t_ai_memory_compaction_artifacts"
SCRATCH = re.compile(r"multirag_test_[a-f0-9]{12}")


def _owned_database(engine: sa.Engine) -> str:
    database = engine.url.database or ""
    assert SCRATCH.fullmatch(database)
    with engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT current_database()")) == database
    return database


def _pg_tool(engine: sa.Engine, tool: Path, journal: ResourceJournal, *, arguments: list[str], archive: Path, restore: bool = False) -> None:
    database = _owned_database(engine)
    url = engine.url
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    passfile = archive.with_suffix(".pgpass")
    fields = (str(url.host), str(url.port), database, str(url.username), url.password or "")
    assert not any("\n" in field or "\r" in field for field in fields)
    entry = ":".join(field.replace("\\", "\\\\").replace(":", "\\:") for field in fields)
    journal.event("reserve-private-consumer-file", path=str(passfile))
    with open(passfile, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        stream.write(entry + "\n")
    env.update(PGPASSFILE=str(passfile), PGCONNECT_TIMEOUT="5")
    args = [str(tool), "--host", str(url.host), "--port", str(url.port), "--username", str(url.username), "--dbname", database, "--no-password", *arguments]
    try:
        if restore:
            subprocess.run([*args, str(archive)], env=env, check=True, timeout=120)
        else:
            journal.event("reserve-private-consumer-file", path=str(archive))
            with open(archive, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
                subprocess.run(args, env=env, stdout=stream, check=True, timeout=120)
                stream.flush()
                os.fsync(stream.fileno())
            with archive.open("rb") as stream:
                assert stream.read(5) == b"PGDMP"
                stream.seek(0)
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            journal.event("rollout-backup-written", database=database, size=archive.stat().st_size, sha256=digest)
    finally:
        passfile.unlink()


def _snapshot(engine: sa.Engine, *, ignore_retirement: bool = False) -> dict[str, Any]:
    _owned_database(engine)
    with engine.connect() as conn:
        conn.execute(sa.text("SET TRANSACTION READ ONLY"))
        conn.execute(sa.text("SET LOCAL TIME ZONE 'UTC'"))
        revision = conn.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version"))
        inspector = sa.inspect(conn)
        tables: dict[str, Any] = {}
        sequences: dict[str, Any] = {}
        for schema in ("usr_ai", "public"):
            for name in sorted(inspector.get_table_names(schema=schema)):
                qualified = f"{schema}.{name}"
                if qualified == "usr_ai.alembic_version":
                    continue
                table = conn.dialect.identifier_preparer.quote_schema(schema) + "." + conn.dialect.identifier_preparer.quote(name)
                row = "to_jsonb(t)"
                if ignore_retirement and qualified == ARTIFACT_TABLE:
                    row += " - 'retired_at' - 'retirement'"
                values = list(conn.scalars(sa.text(f"SELECT ({row})::text FROM {table} AS t ORDER BY 1")))
                columns = [
                    (entry["name"], str(entry["type"]), entry["nullable"], entry["default"])
                    for entry in inspector.get_columns(name, schema=schema)
                    if not (ignore_retirement and qualified == ARTIFACT_TABLE and entry["name"] in {"retired_at", "retirement"})
                ]
                tables[qualified] = {
                    "columns": columns,
                    "count": len(values),
                    "rows_sha256": hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest(),
                }
            for name in sorted(inspector.get_sequence_names(schema=schema)):
                identifier = conn.dialect.identifier_preparer.quote_schema(schema) + "." + conn.dialect.identifier_preparer.quote(name)
                sequences[f"{schema}.{name}"] = tuple(conn.execute(sa.text(f"SELECT last_value, is_called FROM {identifier}")).one())
        artifact_columns = {entry["name"] for entry in inspector.get_columns(ARTIFACT_TABLE.split(".")[1], schema="usr_ai")}
        indexes = {entry["name"] for entry in inspector.get_indexes(ARTIFACT_TABLE.split(".")[1], schema="usr_ai")}
        constraints = {entry["name"] for entry in inspector.get_check_constraints(ARTIFACT_TABLE.split(".")[1], schema="usr_ai")}
    return {
        "revision": revision,
        "tables": tables,
        "sequences": sequences,
        "retirement_columns": sorted(artifact_columns & {"retired_at", "retirement"}),
        "retirement_index": "ix_memory_compaction_retention" in indexes,
        "retirement_check": "ck_memory_compaction_retirement" in constraints,
    }


def _overlay(path: Path, engine: sa.Engine, journal: ResourceJournal, owned: list[Path]) -> None:
    from common.config_utils import CONFIGS

    config = {**CONFIGS, "postgresql": {**CONFIGS["postgresql"], "dbname": _owned_database(engine)}}
    private_consumer_file(path, json.dumps(config), journal, owned)


def _child_environment(repo: Path, overlay: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MULTIRAG_POSTGRESQL__")}
    for key in ("ALEMBIC_DATABASE_URL", "DOC_ENGINE", "DB_TYPE", "RAG_PROJECT_BASE", "RAG_DEPLOY_BASE", "PYTHONPATH"):
        env.pop(key, None)
    return {
        **env,
        "DOC_ENGINE": "milvus",
        "DB_TYPE": "postgresql",
        "MULTIRAG_CONFIG_OVERLAY_FILE": str(overlay),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYTHONPATH": str(repo),
    }


@pytest.fixture(scope="session")
def bootstrapped_engine() -> Iterator[sa.Engine]:
    from api.db.db_models import Base
    from common.config_utils import CONFIGS

    engine = sa.create_engine(_pg_url(CONFIGS["postgresql"]["dbname"]))
    try:
        assert _owned_database(engine) == os.environ["MULTIRAG_ROLLOUT_EXPECTED_DB"]
        config = _alembic_config()
        assert ScriptDirectory.from_config(config).get_current_head() == PREVIOUS_REVISION
        with engine.begin() as conn:
            assert not sa.inspect(conn).has_schema("usr_ai")
            conn.execute(sa.text("CREATE SCHEMA usr_ai"))
            Base.metadata.create_all(conn)
            config.attributes["connection"] = conn
            command.stamp(config, "head")
        yield engine
    finally:
        engine.dispose()


def _smoke(repo: Path, engine: sa.Engine, work: Path, bindings: dict[str, Any], mode: str, journal: ResourceJournal) -> None:
    owned: list[Path] = []
    config, overlay = work / f"{mode}-bindings.json", work / f"{mode}-postgres.json"
    try:
        _overlay(overlay, engine, journal, owned)
        private_consumer_file(config, json.dumps({**bindings, "database": _owned_database(engine)}), journal, owned)
        subprocess.run(
            [sys.executable, "-B", "-m", "tests.manual.memory_compaction_rollout", "--smoke", mode, "--bindings", str(config)],
            cwd=repo,
            env=_child_environment(repo, overlay),
            timeout=150,
            check=True,
        )
    finally:
        for path in owned:
            path.unlink(missing_ok=True)


def test_seed_rehearsal(compaction_api: dict[str, Any]) -> None:
    """Run from the archived predecessor; the existing fixture owns live RAW sources."""
    from tests.support.memory_compaction_http import request
    from tests.support.memory_writes_http import api_request, result

    env = compaction_api
    source_engine: sa.Engine = env["engine"]
    work = Path(os.environ["MULTIRAG_ROLLOUT_WORK"])
    target = Path(os.environ["MULTIRAG_ROLLOUT_TARGET"])
    tools = Path(os.environ["MULTIRAG_ROLLOUT_PG_BIN"])
    journal = ResourceJournal("memory-compaction-rollout")
    base = f"/memories/{env['memory_id']}/context-compactions"
    generated = result(api_request(env, "POST", base, json=request(env, GENERATED)))
    env["summary_response"] = ""
    failed = result(api_request(env, "POST", base, json=request(env, FAILED)))
    assert generated["operation"]["status"] == "generated" and generated["artifact"]["package"]
    assert failed["operation"]["status"] == "failed" and failed["artifact"]["package"] is None
    assert len(env["summary_calls"]) == 2
    with source_engine.connect() as conn:
        assert conn.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'")) == 0
        assert conn.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_memory_writer_admissions WHERE active_token IS NOT NULL OR state IN ('active','unknown')")) == 0
        assert conn.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_memory_publication_outbox WHERE lease_token IS NOT NULL OR receipt->'active_attempts' NOT IN ('[]'::jsonb, 'null'::jsonb)")) == 0
        server = int(conn.scalar(sa.text("SHOW server_version_num")) or 0)
    for name in ("pg_dump", "pg_restore"):
        version = subprocess.check_output([str(tools / name), "--version"], text=True).strip()
        assert re.search(rf"\b{server // 10000}\.\d+\b", version), version
        journal.event("rollout-pg-tool", tool=name, version=version, server_version_num=server)
    frozen = _snapshot(source_engine)
    assert frozen["revision"] == PREVIOUS_REVISION and not frozen["retirement_columns"] and not frozen["retirement_index"] and not frozen["retirement_check"]
    assert frozen["tables"][ARTIFACT_TABLE]["count"] == 2
    journal.event("rollout-quiesced-source", database=_owned_database(source_engine), snapshot_sha256=hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest())
    bindings = {
        "baseURL": "",
        "token": env["jwts"][1],
        "memoryId": env["memory_id"],
        "operationId": GENERATED,
        "sourceIds": env["compaction_source_ids"],
        "artifactId": generated["artifact"]["artifact_id"],
    }
    archive = work / "previous.pgcustom"
    _pg_tool(source_engine, tools / "pg_dump", journal, arguments=["--format=custom", "--no-owner", "--no-acl"], archive=archive)
    try:
        with scratch_database(work, bootstrap=False) as upgrade:
            restore_args = ["--single-transaction", "--exit-on-error", "--no-owner", "--no-acl"]
            _pg_tool(upgrade, tools / "pg_restore", journal, arguments=restore_args, archive=archive, restore=True)
            assert _snapshot(upgrade) == frozen
            overlay = work / "migration-postgres.json"
            owned: list[Path] = []
            try:
                _overlay(overlay, upgrade, journal, owned)
                subprocess.run(
                    [sys.executable, "-B", "-m", "tests.manual.memory_compaction_rollout", "--upgrade"],
                    cwd=target,
                    env={**_child_environment(target, overlay), "MULTIRAG_ROLLOUT_EXPECTED_DB": _owned_database(upgrade)},
                    check=True,
                    timeout=90,
                )
            finally:
                for path in owned:
                    path.unlink(missing_ok=True)
            upgraded = _snapshot(upgrade, ignore_retirement=True)
            assert upgraded["revision"] == TARGET_REVISION and upgraded["retirement_columns"] == ["retired_at", "retirement"] and upgraded["retirement_index"] and upgraded["retirement_check"]
            assert upgraded["tables"] == frozen["tables"] and upgraded["sequences"] == frozen["sequences"]
            _smoke(target, upgrade, work, bindings, "upgraded", journal)
            after = _snapshot(upgrade)
            assert after["revision"] == TARGET_REVISION and after["tables"][ARTIFACT_TABLE]["rows_sha256"] != frozen["tables"][ARTIFACT_TABLE]["rows_sha256"]
            journal.event(
                "rollout-upgrade-readback", database=_owned_database(upgrade), revision=upgraded["revision"], old_tables_preserved=len(frozen["tables"]), sequences_preserved=len(frozen["sequences"])
            )
        with scratch_database(work, bootstrap=False) as rollback:
            _pg_tool(rollback, tools / "pg_restore", journal, arguments=restore_args, archive=archive, restore=True)
            restored = _snapshot(rollback)
            assert restored == frozen
            _smoke(ROOT, rollback, work, bindings, "restored", journal)
            assert _snapshot(rollback) == restored and _snapshot(source_engine) == frozen
            journal.event("rollout-rollback-readback", database=_owned_database(rollback), revision=restored["revision"], snapshot_matches=True, original_unchanged=True)
    finally:
        archive.unlink(missing_ok=True)
        assert not archive.exists()
        journal.event("rollout-backup-cleaned", path=str(archive), absent=True)


def _upgrade() -> None:
    from api.db import db_models
    from common.config_utils import CONFIGS

    engine = sa.create_engine(_pg_url(CONFIGS["postgresql"]["dbname"]))
    try:
        assert _owned_database(engine) == db_models.engine.url.database == os.environ["MULTIRAG_ROLLOUT_EXPECTED_DB"]
        assert _snapshot(engine)["revision"] == PREVIOUS_REVISION
        config = _alembic_config()
        assert ScriptDirectory.from_config(config).get_current_head() == TARGET_REVISION
        with engine.connect() as conn:
            config.attributes["connection"] = conn
            command.upgrade(config, "head")
        assert _snapshot(engine)["revision"] == TARGET_REVISION
    finally:
        engine.dispose()


def _serve_smoke(mode: str, config: dict[str, Any], *, read_extra: Callable[[Path, dict[str, Any], ResourceJournal, list[Path]], None] | None = None) -> None:
    import uvicorn

    from api.db.db_models import engine

    database = _owned_database(engine)
    assert database == config.pop("database")

    from api.apps import app
    from tests.support.uvicorn_runtime import run_server

    checkout = Path(os.environ["MULTIRAG_ROLLOUT_WEB"])
    web_sha = os.environ["MULTIRAG_ROLLOUT_WEB_SHA"]
    assert checkout.is_dir() and checkout.is_relative_to(Path(os.environ["MULTIRAG_ROLLOUT_WORK"]))
    journal = ResourceJournal("memory-compaction-rollout-web")
    requests: list[tuple[str, str]] = []
    retirement_path = f"/api/v1/memories/{config['memoryId']}/context-compactions/{config['artifactId']}/retirements"

    async def guard(scope: Any, receive: Any, send: Any) -> None:
        method = scope["method"]
        path = scope["path"]
        requests.append((method, path))
        assert method == "GET" or (mode == "upgraded" and method == "POST" and path == retirement_path)
        await app(scope, receive, send)

    with socket.socket() as listener:
        for _attempt in range(32):
            port = 20000 + secrets.randbelow(40000)
            journal.event("reserve-listener", host="127.0.0.1", port=port)
            try:
                listener.bind(("127.0.0.1", port))
                break
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        else:
            raise RuntimeError("No owned loopback listener available")
        journal.event("listener-bound", host="127.0.0.1", port=port)
        server = uvicorn.Server(uvicorn.Config(guard, lifespan="off", log_level="error"))
        thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, daemon=True)
        journal.event("reserve-server-thread", pid=os.getpid(), listener_port=port)
        thread.start()
        owned: list[Path] = []
        try:
            deadline = time.monotonic() + 25
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started
            config["baseURL"] = f"http://127.0.0.1:{port}"

            def consume(script: str, **settings: Any) -> dict[str, Any]:
                path = Path(os.environ["TMPDIR"]) / f"{mode}-{script}.json"
                private_consumer_file(path, json.dumps({**config, **settings, "ownedScratchConfirmed": True}), journal, owned)
                proc = subprocess.run(
                    ["node", "--import", "tsx", f"scripts/consumers/{script}.ts", str(path)],
                    cwd=checkout,
                    env={**os.environ, "TSX_TSCONFIG_PATH": "tsconfig.app.json"},
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                assert proc.returncode == 0, proc.stderr
                receipt = json.loads(proc.stdout)
                assert receipt["passed"] and receipt["generationRequests"] == receipt["applicationRequests"] == 0
                return receipt

            before = _snapshot(engine)
            read = consume("memory-compaction", mode="read", expectedState="generated", prefixCount=1)
            assert read["artifactId"] == config["artifactId"] and _snapshot(engine) == before and requests and all(method == "GET" for method, _ in requests)
            if read_extra is not None:
                assert mode == "restored"
                read_extra(checkout, config, journal, owned)
                assert _snapshot(engine) == before and all(method == "GET" for method, _ in requests)
            if mode == "upgraded":
                retirement = consume("memory-compaction-retention", mode="retire")
                assert retirement["retirementRequests"] == 2
                assert retirement["retainedBefore"] == 2 and retirement["retainedAfter"] == 1
                assert sum(method == "POST" for method, _ in requests) == 2
            journal.event("rollout-web-readback", database=database, phase=mode, web_sha=web_sha, read_only_smoke=True, http_requests=len(requests))
        finally:
            server.should_exit = True
            thread.join(20)
            assert not thread.is_alive()
            for path in owned:
                path.unlink(missing_ok=True)
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    journal.event("listener-cleanup-readback", host="127.0.0.1", port=port, absent=True)


def _checkout(repo: Path, stage: Path, commit: str, journal: ResourceJournal) -> None:
    assert re.fullmatch(r"[a-f0-9]{40}", commit)
    assert subprocess.check_output(["git", "rev-parse", commit + "^{commit}"], cwd=repo, text=True).strip() == commit
    tar = stage.with_suffix(".tar")
    journal.event("reserve-rollout-source-archive", directory=str(stage), archive=str(tar), commit=commit)
    stage.mkdir(mode=0o700)
    with open(tar, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        subprocess.run(["git", "archive", "--format=tar", commit], cwd=repo, stdout=stream, check=True, timeout=90)
    with tar.open("rb") as stream:
        archive_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    journal.event("rollout-source-archive", stage=stage.name, commit=commit, sha256=archive_sha, bytes=tar.stat().st_size)
    with tarfile.open(tar) as archive:
        archive.extractall(stage, filter="data")
    tar.unlink()


def _rehearse(previous_ref: str, target_ref: str, pg_tools: Path) -> None:
    assert os.environ.get("MULTIRAG_RUN_ID") and os.environ.get("MULTIRAG_RUN_JOURNAL"), "Use the registered runner"
    root = Path(os.environ["TMPDIR"]).resolve()
    web = Path(os.environ["WEB_COMPLETION_CHECKOUT"]).resolve(strict=True)
    journal = ResourceJournal("memory-compaction-rollout-reservation")
    work = root / "compaction-rollout"
    previous = work / "previous"
    target = work / "target"
    consumer = work / "web"
    journal.event("reserve-rollout-work", directory=str(work), previous_commit=previous_ref, target_commit=target_ref, web_checkout=str(web))
    work.mkdir(mode=0o700)
    try:
        _checkout(ROOT, previous, previous_ref, journal)
        _checkout(ROOT, target, target_ref, journal)
        sha = json.loads((target / ".github/consumer-web.json").read_text())["ref"]
        _checkout(web, consumer, sha, journal)
        dependency_dir = (web / "node_modules").resolve(strict=True)
        journal.event("reserve-rollout-dependency-link", path=str(consumer / "node_modules"), borrowed=str(dependency_dir))
        (consumer / "node_modules").symlink_to(dependency_dir, target_is_directory=True)
        for stage in (previous, target):
            for relative in ("tests/manual/memory_compaction_rollout.py", "tests/support/database.py"):
                path = stage / relative
                source_file = ROOT / relative
                journal.event("reserve-rollout-harness", path=str(path), sha256=hashlib.sha256(source_file.read_bytes()).hexdigest())
                shutil.copyfile(source_file, path)
            assets = ROOT / "core/res/deepdoc"
            for name in ("det.onnx", "rec.onnx", "ocr.res", "updown_concat_xgb.model", "layout.onnx", "layout.laws.onnx", "layout.manual.onnx", "layout.paper.onnx", "tsr.onnx"):
                model = assets / name
                path = stage / "core/res/deepdoc" / name
                journal.event("reserve-rollout-runtime-asset", path=str(path), borrowed=str(model), sha256=hashlib.sha256(model.read_bytes()).hexdigest())
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(model, path)
        seed = previous / "tests/integration/test_memory_compaction_rollout_owned.py"
        private_consumer_file(
            seed,
            "from api.db import db_models as db_models\n"
            "from tests.manual.memory_compaction_rollout import bootstrapped_engine, test_seed_rehearsal\n"
            "from tests.support.memory_compaction_http import compaction_api, release_api, writes_api\n",
            journal,
            [],
        )
        with scratch_database(work, bootstrap=False) as source:
            overlay = work / "previous-postgres.json"
            owned: list[Path] = []
            try:
                _overlay(overlay, source, journal, owned)
                key = secrets.token_hex(32)
                env = {
                    **_child_environment(previous, overlay),
                    "MULTIRAG_ROLLOUT_WORK": str(work),
                    "MULTIRAG_ROLLOUT_TARGET": str(target),
                    "MULTIRAG_ROLLOUT_PG_BIN": str(pg_tools),
                    "MULTIRAG_ROLLOUT_EXPECTED_DB": _owned_database(source),
                    "MULTIRAG_ROLLOUT_WEB_SHA": sha,
                    "MULTIRAG_ROLLOUT_WEB": str(consumer),
                    "MULTIRAG_SECRET_KEY": key,
                    "MULTIRAG_MULTIRAG__SECRET_KEY": key,
                }
                subprocess.run(
                    [sys.executable, "-B", "-m", "pytest", "-q", "--disable-warnings", "tests/integration/test_memory_compaction_rollout_owned.py::test_seed_rehearsal"],
                    cwd=previous,
                    env=env,
                    check=True,
                    timeout=580,
                )
            finally:
                for path in owned:
                    path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(work)
        assert not work.exists()
        journal.event("rollout-work-cleaned", path=str(work), absent=True)


def main() -> None:
    if not __debug__:
        raise RuntimeError("The rollout requires its database and identity assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-ref")
    parser.add_argument("--target-ref")
    parser.add_argument("--pg-bin-dir", type=Path)
    parser.add_argument("--upgrade", action="store_true")
    parser.add_argument("--smoke", choices=["upgraded", "restored"])
    parser.add_argument("--bindings", type=Path)
    args = parser.parse_args()
    if not os.environ.get("MULTIRAG_RUN_ID") or not os.environ.get("MULTIRAG_RUN_JOURNAL"):
        parser.error("Use scripts/run_registered.py")

    def cancel(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, cancel)
    if args.upgrade and os.environ.get("MULTIRAG_ROLLOUT_WORK"):
        _upgrade()
    elif args.smoke and args.bindings and os.environ.get("MULTIRAG_ROLLOUT_WORK"):
        _serve_smoke(args.smoke, json.loads(args.bindings.read_text()))
    elif args.previous_ref and args.target_ref and args.pg_bin_dir:
        _rehearse(args.previous_ref, args.target_ref, args.pg_bin_dir.resolve(strict=True))
    else:
        parser.error("--previous-ref, --target-ref and --pg-bin-dir are required")


if __name__ == "__main__":
    main()
