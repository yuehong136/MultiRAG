"""Compare actual PostgreSQL, Milvus and Redis restores at one frozen recovery point."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import sqlalchemy as sa
import yaml

from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import private_consumer_file

if TYPE_CHECKING:
    from tests.manual.memory_restore_stores import RestoreStores

ROOT = Path(__file__).resolve().parents[2]
STORES = ("postgresql", "milvus", "redis")


class RecoveryPointMismatch(ValueError):
    """The isolated stacks cannot be admitted together."""


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def archive_hash(path: Path) -> str:
    if not path.is_file():
        raise RecoveryPointMismatch("backup archive missing")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_archives(paths: dict[str, Path], expected_hashes: dict[str, str]) -> None:
    if set(paths) != set(expected_hashes) or set(paths) != set(STORES):
        raise RecoveryPointMismatch("backup set incomplete")
    for store in STORES:
        if archive_hash(paths[store]) != expected_hashes[store]:
            raise RecoveryPointMismatch(f"archive digest mismatch: {store}")


def admit_restored(observed: dict[str, str], expected: dict[str, str]) -> None:
    if set(observed) != set(expected) or set(observed) != set(STORES):
        raise RecoveryPointMismatch("restored state incomplete")
    different = [store for store in STORES if observed[store] != expected[store]]
    if different:
        raise RecoveryPointMismatch("restored state mismatch: " + ",".join(different))


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def redis_scope(bindings: dict[str, Any]) -> dict[str, Any]:
    from core.utils.redis_conn import REDIS_CONN

    redis = REDIS_CONN.REDIS
    queue = bindings["queue"]
    group = bindings["group"]
    rows = [{"id": _text(identity), "payload": {_text(key): _text(value) for key, value in values.items()}} for identity, values in redis.xrange(queue)]
    group_info = next(row for row in redis.xinfo_groups(queue) if _text(row["name"]) == group)
    pending = redis.xpending_range(queue, group, "-", "+", 100)
    assert int(group_info["pending"]) == len(pending)
    counter = redis.get(bindings["counter"])
    assert counter is not None
    return {
        "counter": int(counter),
        "last_delivered_id": _text(group_info["last-delivered-id"]),
        "entries_read": group_info["entries-read"],
        "stream": rows,
        "pending": [{"id": _text(row["message_id"]), "consumer": _text(row["consumer"]), "deliveries": int(row["times_delivered"])} for row in pending],
    }


def snapshots(engine: sa.Engine, bindings: dict[str, Any]) -> dict[str, str]:
    from memory.revisions.storage import ManagedMessageStore
    from tests.manual.memory_compaction_rollout import _snapshot

    async def inventories() -> dict[str, Any]:
        return {name: sorted(await ManagedMessageStore(tenant, identity).inventory(), key=lambda row: row["message_id"]) for name, (tenant, identity) in sorted(bindings["banks"].items())}

    return {"postgresql": fingerprint(_snapshot(engine)), "milvus": fingerprint(asyncio.run(inventories())), "redis": fingerprint(redis_scope(bindings))}


@dataclass(frozen=True)
class Point:
    archives: dict[str, Path]
    archive_hashes: dict[str, str]
    state: dict[str, str]


@dataclass
class Capture:
    stores: RestoreStores
    work: Path
    journal: ResourceJournal
    points: dict[str, Point]
    bindings: dict[str, Any]

    @pytest.fixture
    def same_point_capture(self) -> Capture:
        return self

    def take(self, name: str, engine: sa.Engine) -> None:
        # No request producers or workers run in the seed fixture. Assert the
        # owned listener has drained before sampling or taking storage backups.
        with engine.connect() as conn:
            conn.execute(sa.text("SET TRANSACTION READ ONLY"))
            assert conn.scalar(sa.text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND state <> 'idle'")) == 0
            assert conn.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_memory_writer_admissions WHERE active_token IS NOT NULL OR state IN ('active','unknown')")) == 0
            assert (
                conn.scalar(sa.text("SELECT count(*) FROM usr_ai.t_ai_memory_publication_outbox WHERE lease_token IS NOT NULL OR receipt->'active_attempts' NOT IN ('[]'::jsonb, 'null'::jsonb)")) == 0
            )
        before = snapshots(engine, self.bindings)
        archives = self.stores.backup(self.work / name, self.bindings["collections"])
        assert snapshots(engine, self.bindings) == before
        hashes = {store: archive_hash(archives[store]) for store in STORES}
        if self.points:
            assert all(before[store] != self.points["point-a"].state[store] for store in STORES)
        self.points[name] = Point(archives, hashes, before)
        self.journal.event("same-point-captured", point=name, archive_sha256=hashes, state_sha256=before)


def _child_environment(overlay: Path) -> dict[str, str]:
    sections = ("POSTGRESQL", "REDIS", "DATAV_REDIS", "MILVUS", "MINIO")
    env = {key: value for key, value in os.environ.items() if not key.startswith(tuple(f"MULTIRAG_{name}__" for name in sections))}
    for key in ("ALEMBIC_DATABASE_URL", "DOC_ENGINE", "DB_TYPE", "RAG_PROJECT_BASE", "RAG_DEPLOY_BASE", "PYTHONPATH"):
        env.pop(key, None)
    return {
        **env,
        "DOC_ENGINE": "milvus",
        "DB_TYPE": "postgresql",
        "STORAGE_IMPL": "MINIO",
        "MULTIRAG_CONFIG_OVERLAY_FILE": str(overlay),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "OTEL_SDK_DISABLED": "true",
        "PYTHONPATH": str(ROOT),
    }


def _replay(engine: sa.Engine, bindings: dict[str, Any], journal: ResourceJournal) -> None:
    from sqlalchemy.orm import Session

    from api.db.db_models import Task
    from api.db.services.tenant_llm_service import TenantLLMService
    from common import settings
    from core.svr import task_executor as worker
    from memory.revisions.identifiers import allocate_message_id
    from tests.manual.memory_compaction_rollout import _snapshot

    before = snapshots(engine, bindings)
    sql_before = _snapshot(engine)
    queue_before = redis_scope(bindings)
    assert [row["consumer"] for row in queue_before["pending"]] == [bindings["consumer"], bindings["consumer"]]
    assert [row["id"] for row in queue_before["pending"]] == bindings["pendingIds"]
    worker.CONSUMER_NAME = bindings["consumer"]
    assert worker.SVR_CONSUMER_GROUP_NAME == bindings["group"]
    done_before, failed_before = worker.DONE_TASKS, worker.FAILED_TASKS

    async def collect_bound() -> None:
        with Session(engine) as db:
            terminal = db.get(Task, bindings["terminalTaskId"])
            unknown = db.get(Task, bindings["unknownTaskId"])
            assert terminal is not None and terminal.progress == 1
            assert unknown is not None and unknown.progress == 0
            terminal_before = terminal.to_dict()
            for _ in range(2):
                # Both restored entries must stop in the genuine worker collect
                # path. No admitted task remains to invoke extraction or a model.
                assert await worker.collect(db) == (None, None)
            db.expire_all()
            assert terminal.to_dict() == terminal_before
            assert unknown.progress < 0 and "reconciliation" in (unknown.progress_msg or "")

    def model_forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Restored terminal or uncertain work cannot dispatch a model")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings, "get_svr_queue_names", lambda: [bindings["queue"]])
        patch.setattr(TenantLLMService, "model_instance_from_config", classmethod(model_forbidden))
        asyncio.run(collect_bound())
    assert worker.DONE_TASKS == done_before + 1 and worker.FAILED_TASKS == failed_before + 1
    after = redis_scope(bindings)
    assert after == {**queue_before, "pending": []}
    assert snapshots(engine, bindings)["milvus"] == before["milvus"]
    sql_after = _snapshot(engine)
    assert sql_before["tables"].pop("usr_ai.t_ai_tasks")["count"] == sql_after["tables"].pop("usr_ai.t_ai_tasks")["count"]
    assert sql_before == sql_after
    allocated = asyncio.run(allocate_message_id(minimum_message_id=bindings["maxMessageId"], counter_key=bindings["counter"]))
    assert allocated > max(bindings["maxMessageId"], queue_before["counter"])
    journal.event(
        "same-point-worker-replay", terminal_acknowledged_without_rewrite=True, legacy_unknown_stopped=True, pending_after=0, model_dispatches=0, milvus_unchanged=True, allocated_id=allocated
    )


def _target(bindings_path: Path, probe: Path | None) -> None:
    work = Path(os.environ["TMPDIR"]).resolve() / "memory-same-point"
    assert bindings_path == work / "bindings.json"
    assert Path(os.environ["MULTIRAG_CONFIG_OVERLAY_FILE"]) == work / "overlay.json"
    assert probe is None or probe == work / "observed.json"
    from common.app_config import get_app_config
    from common.bootstrap import ensure_initialized
    from common.config_utils import CONFIGS

    bindings = json.loads(bindings_path.read_text())
    expected_config = bindings.pop("ownedEndpoints")
    pg = CONFIGS["postgresql"]
    assert [pg["host"], int(pg["port"]), pg["dbname"]] == expected_config["postgresql"]
    assert CONFIGS["redis"]["host"] == expected_config["redis"]
    assert CONFIGS["datav_redis"]["host"] == expected_config["redis"]
    assert CONFIGS["milvus"]["hosts"] == expected_config["milvus"]
    assert CONFIGS["minio"]["host"] == expected_config["minio"]
    # A fixed, rehearsal-only signing key keeps resource initialization from
    # creating or replacing a shared Redis secret during a read-only probe.
    secret = get_app_config().multirag.secret_key
    assert secret is not None and secret.get_secret_value() == os.environ["MULTIRAG_MULTIRAG__SECRET_KEY"]
    ensure_initialized()

    from api.db.db_models import engine

    assert [engine.url.host, engine.url.port, engine.url.database] == expected_config["postgresql"]
    journal = ResourceJournal("memory-same-point-target")
    observed = snapshots(engine, bindings)
    if probe is not None:
        private_consumer_file(probe, json.dumps(observed), journal, [])
        return
    admit_restored(observed, bindings.pop("expectedState"))
    from tests.manual.memory_same_point_web import smoke_target

    started = time.monotonic()
    smoke_target(engine, bindings)
    assert snapshots(engine, bindings) == observed
    journal.event("same-point-web-ready", seconds=time.monotonic() - started)
    _replay(engine, bindings, journal)
    after = snapshots(engine, bindings)
    smoke_target(engine, bindings)
    assert snapshots(engine, bindings) == after


def _run_target(stores: RestoreStores, work: Path, bindings: dict[str, Any], point: Point, *, probe: bool, journal: ResourceJournal) -> dict[str, str] | None:
    overlay, private, observed = (work / name for name in ("overlay.json", "bindings.json", "observed.json"))
    config = stores.overlay(yaml.safe_load((ROOT / "configs" / "service_conf.yaml").read_text()))
    postgres = stores.postgres_url
    endpoints = {
        "postgresql": [postgres.host, postgres.port, postgres.database],
        "redis": config["redis"]["host"],
        "milvus": config["milvus"]["hosts"],
        "minio": config["minio"]["host"],
    }
    private_consumer_file(overlay, json.dumps(config), journal, [])
    private_consumer_file(private, json.dumps({**bindings, "expectedState": point.state, "ownedEndpoints": endpoints}), journal, [])
    cmd = [sys.executable, "-B", "-m", "tests.manual.memory_same_point_rollout", "--target", str(private)]
    if probe:
        cmd.extend(["--probe", str(observed)])
    try:
        subprocess.run(cmd, cwd=ROOT, env=_child_environment(overlay), check=True, timeout=200)
        return json.loads(observed.read_text()) if probe else None
    finally:
        for path in (overlay, private, observed):
            path.unlink(missing_ok=True)


def _run() -> None:
    from tests.manual.memory_restore_stores import isolated_stores

    journal = ResourceJournal("memory-same-point-rollout")
    work = Path(os.environ["TMPDIR"]).resolve() / "memory-same-point"
    journal.event("reserve-same-point-work", path=str(work))
    work.mkdir(mode=0o700)
    try:
        with isolated_stores(work, journal, name="source") as source:
            overlay = work / "source.json"
            config = source.overlay(yaml.safe_load((ROOT / "configs" / "service_conf.yaml").read_text()))
            private_consumer_file(overlay, json.dumps(config), journal, [])
            capture = Capture(source, work, journal, {}, {})
            original_env = dict(os.environ)
            source_env = _child_environment(overlay)
            try:
                os.environ.clear()
                os.environ.update(source_env)
                exit_code = pytest.main(
                    ["-o", "addopts=", "--allow-unix-socket", "--allow-hosts=localhost,127.0.0.1,::1", "--tb=short", "-q", str(ROOT / "tests/manual/test_memory_same_point_seed.py")], plugins=[capture]
                )
                assert exit_code == pytest.ExitCode.OK
            finally:
                os.environ.clear()
                os.environ.update(original_env)
                overlay.unlink(missing_ok=True)
        a, b = capture.points["point-a"], capture.points["point-b"]
        check_archives(a.archives, a.archive_hashes)
        check_archives(b.archives, b.archive_hashes)
        missing = {store: path for store, path in a.archives.items() if store != "redis"}
        with pytest.raises(RecoveryPointMismatch, match="backup set incomplete"):
            check_archives(missing, a.archive_hashes)
        journal.event("same-point-negative", reason="backup set incomplete", storage_restore_started=False, application_started=False, worker_started=False)

        mixed = {**a.archives, "redis": b.archives["redis"]}
        with pytest.raises(RecoveryPointMismatch, match="archive digest mismatch: redis"):
            check_archives(mixed, a.archive_hashes)
        # Compose three individually intact archives from different checkpoints.
        # Verify storage itself rejects admission independently of labels/hashes.
        check_archives(mixed, {**a.archive_hashes, "redis": b.archive_hashes["redis"]})
        with isolated_stores(work, journal, name="mixed") as target:
            target.restore(mixed, capture.bindings["collections"])
            mixed_state = _run_target(target, work, capture.bindings, a, probe=True, journal=journal)
            assert mixed_state == {**a.state, "redis": b.state["redis"]}
            with pytest.raises(RecoveryPointMismatch, match="restored state mismatch: redis"):
                admit_restored(mixed_state, a.state)
            journal.event("same-point-negative", reason="restored state mismatch: redis", all_archives_intact=True, state_verified_independently=True, application_started=False, worker_started=False)

        started = time.monotonic()
        with isolated_stores(work, journal, name="same") as target:
            check_archives(a.archives, a.archive_hashes)
            target.restore(a.archives, capture.bindings["collections"])
            observed = _run_target(target, work, capture.bindings, a, probe=True, journal=journal)
            assert observed is not None
            admit_restored(observed, a.state)
            journal.event("same-point-admitted", state_sha256=observed, seconds=time.monotonic() - started)
            _run_target(target, work, capture.bindings, a, probe=False, journal=journal)
        journal.event("same-point-complete", point="point-a", negative_cases=2, source_stack_stopped_before_restore=True)
    finally:
        shutil.rmtree(work)
        assert not work.exists()
        journal.event("same-point-work-cleaned", path=str(work), absent=True)


def main() -> None:
    if not __debug__:
        raise RuntimeError("The rehearsal requires its isolation and admission assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--probe", type=Path)
    args = parser.parse_args()
    if not os.environ.get("MULTIRAG_RUN_ID") or not os.environ.get("MULTIRAG_RUN_JOURNAL"):
        parser.error("Use scripts/run_registered.py")
    if args.target is not None:
        _target(args.target, args.probe)
    elif args.probe is not None:
        parser.error("--probe requires --target")
    else:
        work = Path(os.environ["MULTIRAG_ROLLOUT_WORK"]).resolve(strict=True)
        web = Path(os.environ["MULTIRAG_ROLLOUT_WEB"]).resolve(strict=True)
        assert web.is_relative_to(work) and work.is_relative_to(Path(os.environ["TMPDIR"]).resolve())
        assert re.fullmatch(r"[a-f0-9]{40}", os.environ["MULTIRAG_ROLLOUT_WEB_SHA"])
        os.environ["MULTIRAG_SECRET_KEY"] = os.environ["MULTIRAG_MULTIRAG__SECRET_KEY"] = secrets.token_hex(32)

        def cancel(signum: int, _frame: Any) -> None:
            raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, cancel)
        _run()


if __name__ == "__main__":
    main()
