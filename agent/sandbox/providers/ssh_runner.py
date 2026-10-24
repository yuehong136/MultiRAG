"""Stdlib-only remote entry point, shipped with the current process implementation."""

import fcntl
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .process import ProcessProvider


def main() -> None:
    workspace = Path(sys.argv[1])
    with (workspace / ".lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not workspace.is_dir() or os.stat(workspace / ".lock").st_ino != os.fstat(lock.fileno()).st_ino:
            raise RuntimeError("Remote workspace changed before execution.")
        _execute(workspace)


def _execute(workspace: Path) -> None:
    provider = ProcessProvider()
    instance_id: str | None = None
    response: dict[str, Any]
    try:
        request = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
        config = request["config"]
        config["work_dir"] = str(workspace / "runs")
        provider.initialize(config)
        instance = provider.create_instance(request["language"])
        instance_id = instance.instance_id
        result = provider.execute_code(instance_id, request["code"], request["language"], request["timeout"], request["arguments"])
        response = {"result": asdict(result)}
    except Exception as exc:
        response = {"error": str(exc), "error_type": type(exc).__name__}
    finally:
        # ProcessProvider reaps the process group before collecting any artifacts.
        # The workspace is removed even if the SSH client has disconnected.
        try:
            if instance_id is not None:
                provider.destroy_instance(instance_id)
            shutil.rmtree(workspace)
        except Exception:
            response = {"error": "Remote execution workspace cleanup failed.", "error_type": "RuntimeError"}
    sys.stdout.write(json.dumps(response, ensure_ascii=False, allow_nan=False))
    sys.stdout.flush()
