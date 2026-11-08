"""Explicit cross-stack acceptance; requires the registered Go test binary."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from api.db.db_models import File, FileCommit
from api.db.services.file_commit_service import FileCommitService
from common.config_utils import CONFIGS
from tests.integration.test_file_commits import create
from tests.integration.test_file_commits import objects as objects
from tests.integration.test_file_commits import state as state


async def test_python_go_shared_snapshot_sql_http_objects(state: dict[str, Any]) -> None:
    env = state
    binary = Path(os.environ["MULTIRAG_FILE_COMMIT_BINARY"]).resolve(strict=True)
    first = await create(env, [])
    async with env["sessions"]() as db:
        url = db.bind.url.set(drivername="postgresql").update_query_dict({"search_path": "usr_ai"})
    config_path = Path(os.environ["REST_DOCUMENT_RESOURCE_DIR"]) / (env["folder"] + "-go-config.json")
    env["journal"].event("reserve-go-live-config", path=str(config_path), binary=str(binary), database=url.database, bucket=env["objects"].bucket)
    config = {
        "DSN": url.render_as_string(hide_password=False),
        "User": env["user"],
        "Other": env["foreign"],
        "Tenant": env["tenant"],
        "Folder": env["folder"],
        "File": env["file"],
        "First": first["id"],
        "Minio": {
            "Host": CONFIGS["minio"]["host"],
            "User": CONFIGS["minio"]["user"],
            "Password": str(CONFIGS["minio"]["password"]),
            "Secure": CONFIGS["minio"].get("secure", False),
            "Bucket": env["objects"].bucket,
            "PrefixPath": "",
        },
    }
    process = None
    output_path = config_path.with_suffix(".output")
    try:
        with open(config_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(config, stream)
            stream.flush()
            os.fsync(stream.fileno())
        command = [
            sys.executable,
            "-B",
            "scripts/run_registered.py",
            "--child",
            os.environ["MULTIRAG_RUN_JOURNAL"],
            str(binary),
            "-test.run=^TestFileCommitLiveAcceptance$",
            "-test.v",
            "-test.timeout=90s",
        ]
        env["journal"].event("reserve-go-test-process", command=command, output=str(output_path), parent_pid=os.getpid(), pgid=os.getpgrp())
        with output_path.open("xb") as output:
            process = await asyncio.to_thread(subprocess.Popen, command, env={**os.environ, "MULTIRAG_FILE_COMMIT_CONFIG": str(config_path)}, stdout=output, stderr=subprocess.STDOUT)
            env["journal"].event("go-test-started", pid=process.pid, pgid=os.getpgid(process.pid))
            code = await asyncio.to_thread(process.wait, timeout=100)
        env["journal"].event("go-test-exit", raw_exit=code)
        assert code == 0, output_path.read_text()
        async with env["sessions"]() as reader:
            assert await reader.get(File, env["file"]) is None
            commits = list(await reader.scalars(sa.select(FileCommit).where(FileCommit.folder_id == env["folder"]).order_by(FileCommit.sequence)))
            assert len(commits) == 5
            assert [row.sequence for row in commits] == [1, 2, 3, 4, 5]
            assert [row.parent_id for row in commits] == [None, *[row.id for row in commits[:-1]]]
            svc = FileCommitService(reader, env["objects"])
            assert await svc.content(commits[0], env["file"]) == b"old"
            assert await svc.content(commits[1], env["file"]) == b"go replacement"
            assert await svc.content(commits[3], env["file"]) == b"go replacement"
            assert env["file"] not in commits[-1].tree_state
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=10)
        config_path.unlink(missing_ok=True)
        env["journal"].event("go-config-cleanup-readback", path=str(config_path), absent=not config_path.exists())
        # Discover only objects within the bucket already exclusively reserved by
        # this fixture, then independently verify every Go-created object cleanup.
        for obj in env["objects"].reader.list_objects(env["objects"].bucket, recursive=True):
            env["objects"].keys.add(obj.object_name)
