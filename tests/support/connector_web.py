"""Owned Web server for connector consumer acceptance."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest
import requests

from tests.support.process_group import NEW_PROCESS_GROUP, stop_console_group
from tests.support.resource_journal import ResourceJournal


def _live_group(group: int) -> list[psutil.Process]:
    members = []
    for process in psutil.process_iter():
        try:
            if os.getpgid(process.pid) == group and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
    return members


def stop_owned_web_group(process: subprocess.Popen[Any], *, port: int | None = None, grace_seconds: float = 10, kill_seconds: float = 5) -> bool:
    """Wait for the npm/Vite group, including children outliving the npm leader."""
    if sys.platform == "win32":
        stop_console_group(process)
        return False
    if process.poll() is None:
        assert os.getpgid(process.pid) == process.pid, "Web server must own its process group"
    for signum, timeout in [(signal.SIGTERM, grace_seconds), (signal.SIGKILL, kill_seconds)]:
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + timeout
        while True:
            process.poll()
            closed = True
            if port is not None:
                with socket.socket() as listener:
                    listener.settimeout(0.1)
                    closed = listener.connect_ex(("127.0.0.1", port)) != 0
            if not _live_group(process.pid) and closed:
                process.wait(timeout=1)
                return signum == signal.SIGKILL
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    raise AssertionError("Owned Web process group or listener did not stop after SIGKILL")


@pytest.fixture(scope="module")
def connector_web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    journal = ResourceJournal("connector-web")
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    assert (checkout / "node_modules").is_dir(), "Installed Web checkout is required"
    # A full path also finds npm's npm.cmd shim on Windows, which CreateProcess cannot by bare name.
    npm = shutil.which("npm")
    assert npm is not None, "npm is required to start the Web checkout"
    journal.event("reserve-port-probe", address=["127.0.0.1", 0])
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("connector_web") / "vite.log"
    evidence_dir = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web"))
    journal.event("reserve-evidence-directory", path=str(evidence_dir))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    registration = {"port": port, "checkout": str(checkout), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(), "pid": None, "listener_closed": False}
    registration_path = evidence_dir / "web-server-resource.json"
    journal.event("reserve-web-registration", path=str(registration_path))
    registration_path.write_text(json.dumps(registration))
    config_path = log_path.with_name("vite-owned.mjs")
    cache = log_path.with_name("vite-cache")
    journal.event(
        "reserve-web-server-resources", listener=["127.0.0.1", port], log=str(log_path), config=str(config_path), cache=str(cache), checkout=str(checkout), process_group="new session, pgid=child PID"
    )
    config_path.write_text(
        "import base from "
        + json.dumps((checkout / "vite.config.ts").as_uri())
        + ";\nexport default async env => ({...(typeof base === 'function' ? await base(env) : base), cacheDir: "
        + json.dumps(str(cache))
        + "});\n"
    )
    registration.update(config=str(config_path), cache=str(cache))
    registration_path.write_text(json.dumps(registration))
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [npm, "run", "dev", "--", "--config", str(config_path), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=checkout,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            creationflags=NEW_PROCESS_GROUP,
        )
        try:
            registration["pid"] = process.pid
            journal.event("web-server-started", pid=process.pid, process_group=process.pid)
            registration_path.write_text(json.dumps(registration))
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    if requests.get(base, timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("Owned Web server failed to start")
            yield base
        finally:
            escalated = stop_owned_web_group(process, port=port)
            with socket.socket() as listener:
                assert listener.connect_ex(("127.0.0.1", port)) != 0
            evidence_dir = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web"))
            evidence_dir.mkdir(parents=True, exist_ok=True)
            if cache.exists():
                shutil.rmtree(cache)
            config_path.unlink(missing_ok=True)
            registration.update(cache_removed=not cache.exists(), config_removed=not config_path.exists(), returncode=process.returncode, live_processes_absent=True, kill_escalated=escalated)
            registration["listener_closed"] = True
            registration_path.write_text(json.dumps(registration))
            (evidence_dir / "web-server-cleanup.json").write_text(json.dumps(registration))
            journal.event("web-server-cleanup-readback", **registration)
