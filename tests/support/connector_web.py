"""Owned Web server for connector consumer acceptance."""

import json
import os
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import requests


@pytest.fixture(scope="module")
def connector_web(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    checkout = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    assert (checkout / "node_modules").is_dir(), "Installed Web checkout is required"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("connector_web") / "vite.log"
    evidence_dir = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web"))
    evidence_dir.mkdir(parents=True, exist_ok=True)
    registration = {"port": port, "checkout": str(checkout), "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(), "pid": None, "listener_closed": False}
    registration_path = evidence_dir / "web-server-resource.json"
    registration_path.write_text(json.dumps(registration))
    config_path = log_path.with_name("vite-owned.mjs")
    cache = log_path.with_name("vite-cache")
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
            ["npm", "run", "dev", "--", "--config", str(config_path), "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=checkout,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            registration["pid"] = process.pid
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
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            with socket.socket() as listener:
                assert listener.connect_ex(("127.0.0.1", port)) != 0
            evidence_dir = Path(os.environ.get("CONNECTOR_WEB_EVIDENCE", ".test-results/connector-web"))
            evidence_dir.mkdir(parents=True, exist_ok=True)
            if cache.exists():
                shutil.rmtree(cache)
            config_path.unlink(missing_ok=True)
            registration.update(cache_removed=not cache.exists(), config_removed=not config_path.exists(), returncode=process.returncode)
            registration["listener_closed"] = True
            registration_path.write_text(json.dumps(registration))
            (evidence_dir / "web-server-cleanup.json").write_text(json.dumps(registration))
