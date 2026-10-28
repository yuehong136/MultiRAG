"""Container consumers must start only after authoritative schema bootstrap."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.support.resource_journal import ResourceJournal

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def _run_entrypoint(tmp_path: Path, *, fail: bool, strict: bool) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    journal = ResourceJournal("docker-schema-bootstrap")
    shim = tmp_path / "python-shim"
    calls_file = tmp_path / "python-calls.jsonl"
    journal.event("reserve-entrypoint-probe", shim=str(shim), calls=str(calls_file), process_group="new session; child pid")
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "with open(os.environ['BOOTSTRAP_CALLS'], 'a') as output:\n"
        "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == '-c':\n"
        "    sys.exit(41 if os.environ['BOOTSTRAP_FAIL'] == '1' else 0)\n"
        "time.sleep(30)\n",
    )
    shim.chmod(0o700)
    environment = {
        **os.environ,
        "PYTHON_BIN": str(shim),
        "BOOTSTRAP_CALLS": str(calls_file),
        "BOOTSTRAP_FAIL": str(int(fail)),
        "STRICT_MODE": str(int(strict)),
        "SKIP_CONFIG_GENERATE": "1",
        "MULTIRAG_RENDER_CONFIG_ONLY": "0",
        "USE_DOCLING": "false",
        "ENABLE_WEBSERVER": "0",
        "API_PROXY_SCHEME": "python",
    }
    process = subprocess.Popen(
        ["bash", str(ENTRYPOINT), "--disable-redis", "--disable-server", "--disable-datasync", "--disable-taskexecutor"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    calls: list[list[str]] = []
    try:
        journal.event("entrypoint-spawned", pid=process.pid, pgid=process.pid)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
            if process.poll() is not None or len(calls) > 1:
                break
            time.sleep(0.02)
    finally:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=5)
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            group_absent = True
        else:
            group_absent = False
        journal.event("entrypoint-cleanup", pid=process.pid, exit=process.returncode, process_group_absent=group_absent)
        assert group_absent, "Owned entrypoint process group remains after cleanup"
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()] if calls_file.exists() else []
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr), calls


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
def test_schema_failure_stops_container_before_any_consumer(tmp_path: Path, strict: bool) -> None:
    result, calls = _run_entrypoint(tmp_path, fail=True, strict=strict)

    assert result.returncode == 1, result.stdout + result.stderr
    assert len(calls) == 1, calls
    assert calls[0][0] == "-c"
    assert "bootstrap_database_schema" in calls[0][1]
    assert "数据库引导完成" not in result.stdout


def test_successful_schema_bootstrap_precedes_container_consumers(tmp_path: Path) -> None:
    result, calls = _run_entrypoint(tmp_path, fail=False, strict=False)

    assert len(calls) == 2, (calls, result.stdout, result.stderr)
    assert calls[0][0] == "-c"
    assert "bootstrap_database_schema" in calls[0][1]
    assert calls[1] == ["admin/server/admin_server.py"]
