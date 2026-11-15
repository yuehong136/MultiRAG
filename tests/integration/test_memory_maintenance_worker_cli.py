"""Real standalone worker startup uses only an owned empty scratch SQL queue."""

import asyncio
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from api.db.db_models import Memory
from api.db.memory_execution_models import MemoryMaintenanceExecution
from common.app_config import get_app_config
from common.config_utils import CONFIGS
from tests.support.database import scratch_database
from tests.support.resource_journal import ResourceJournal


async def test_real_worker_cli_initializes_and_closes_an_empty_owned_queue(tmp_path: Path) -> None:
    journal = ResourceJournal("memory_maintenance_worker_cli")

    def retain_output(suffix: str, content: bytes) -> None:
        path = journal.path.with_suffix(suffix)
        journal.event("reserve-retained-output", path=str(path), mode="0600", retained_evidence=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        journal.event("retained-output", path=str(path), sha256=hashlib.sha256(content).hexdigest(), bytes=len(content))

    # Bootstrap may read the existing shared signing key, but this test must not
    # create a key in the borrowed service as a side effect of worker startup.
    configured = get_app_config().multirag.secret_key
    if configured is None or len(configured.get_secret_value()) < 32:
        from core.utils.redis_conn import REDIS_CONN

        assert REDIS_CONN.REDIS.exists("multirag:system:secret_key"), "Borrowed signing key is absent; real CLI startup is not authorized to create it"
    with scratch_database(tmp_path / "worker-cli-database") as engine:
        configuration = tmp_path / "worker-private-config.yaml"
        journal.event("reserve-private-config", path=str(configuration), mode="0600", contains_credentials=True)
        postgres = {**CONFIGS["postgresql"], "dbname": engine.url.database}
        body = json.dumps({"postgresql": postgres}, ensure_ascii=False).encode("utf-8")
        descriptor = os.open(configuration, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            assert stat.S_IMODE(configuration.stat().st_mode) == 0o600
            journal.event("private-config-created", path=str(configuration), sha256=hashlib.sha256(body).hexdigest(), database=engine.url.database)
            child_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("MULTIRAG_POSTGRESQL__")}
            child_env.update(MULTIRAG_CONFIG_OVERLAY_FILE=str(configuration), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1", MODELSCOPE_OFFLINE="1")
            child_env.setdefault("MULTIRAG_RUN_ID", journal.run_id)
            gate_journal = os.environ.get("MULTIRAG_RUN_JOURNAL", str(journal.path))
            command = [
                sys.executable,
                "-B",
                "scripts/run_registered.py",
                "--child",
                gate_journal,
                sys.executable,
                "-B",
                "-m",
                "memory.maintenance.worker",
                "--once",
                "--worker-id",
                "owned-empty-queue-cli",
            ]
            journal.event("reserve-process", identity="real-worker-cli", command=command, database=engine.url.database, inherited_process_group=True)
            process = await asyncio.create_subprocess_exec(*command, cwd=Path(__file__).parents[2], env=child_env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            journal.event("process-started", identity="real-worker-cli", pid=process.pid)
            communication = asyncio.create_task(process.communicate())
            try:
                stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=60)
                assert process.returncode == 0, (stdout + stderr).decode()
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=3)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
                stdout, stderr = await communication
                retain_output(".worker.stdout", stdout)
                retain_output(".worker.stderr", stderr)
                with pytest.raises(ProcessLookupError):
                    os.kill(process.pid, 0)
                journal.event("process-cleanup-readback", identity="real-worker-cli", pid=process.pid, absent=True)
            with engine.connect() as reader:
                assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryMaintenanceExecution)) == 0
                assert reader.scalar(sa.select(sa.func.count()).select_from(Memory)) == 0
                journal.event("empty-queue-readback", database=engine.url.database, execution_count=0, memory_count=0)
        finally:
            configuration.unlink(missing_ok=True)
            assert not configuration.exists()
            journal.event("private-config-cleanup-readback", path=str(configuration), absent=True)
