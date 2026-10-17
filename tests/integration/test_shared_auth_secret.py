"""Owned Redis concurrency evidence for the process-wide authentication key."""

import hashlib
import json
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.utils.redis_conn import REDIS_CONN


def _initialize_worker(index: int, key_name: str) -> dict[str, Any]:
    from types import SimpleNamespace

    from common import resources
    from core.utils.redis_conn import REDIS_CONN

    original = REDIS_CONN.get_or_create_secret_key

    def owned_lookup(requested_key: str, candidate: Any) -> str:
        assert requested_key == "multirag:system:secret_key"
        return original(key_name, candidate)

    import pytest

    with pytest.MonkeyPatch.context() as patch:
        # Predictable test material, never a credential usable by a real identity.
        patch.setattr(resources, "get_app_config", lambda: SimpleNamespace(multirag=SimpleNamespace(secret_key=None)))
        patch.setattr(resources.secrets, "token_hex", lambda count: f"test-only-worker-{index}-" + "x" * 48)
        patch.setattr(REDIS_CONN, "get_or_create_secret_key", owned_lookup)
        key = resources._create_secret_key()
        return {"digest": hashlib.sha256(key.encode()).hexdigest(), "source": str(Path(resources.__file__).resolve())}


def test_independent_processes_share_one_persistent_redis_winner(tmp_path: Path) -> None:
    key_name = f"auth-secret-test:{uuid4().hex}:key"
    client = REDIS_CONN.REDIS
    assert client is not None and not client.exists(key_name)
    evidence: dict[str, Any] = {"redis_key": key_name, "cleanup": False}
    try:
        with ProcessPoolExecutor(max_workers=6, mp_context=multiprocessing.get_context("spawn")) as workers:
            futures = [workers.submit(_initialize_worker, i, key_name) for i in range(12)]
            results = [future.result(timeout=90) for future in futures]
        stored = client.get(key_name)
        assert stored and client.ttl(key_name) == -1
        digest = hashlib.sha256(stored.encode()).hexdigest()
        assert {result["digest"] for result in results} == {digest}
        assert {Path(result["source"]) for result in results} == {Path(__file__).resolve().parents[2] / "common/resources.py"}
        evidence.update(processes=6, initializations=12, one_winner=True, persistent=True, source_root=str(Path(__file__).resolve().parents[2]))
    finally:
        client.delete(key_name)
        assert not client.exists(key_name)
        evidence["cleanup"] = True
        (tmp_path / "auth-secret-concurrency.json").write_text(json.dumps(evidence, indent=2))


def test_independent_api_processes_share_configured_jwt_and_session_cookie(tmp_path: Path) -> None:
    import os
    import signal
    import socket
    import subprocess
    import sys
    import time
    from contextlib import ExitStack

    import requests

    root = Path(__file__).resolve().parents[2]
    key = "test-only-cold-start-shared-auth-key-" + "x" * 32
    evidence: dict[str, Any] = {"source_root": str(root), "replicas": [], "cleanup": False}
    env = dict(os.environ, MULTIRAG_MULTIRAG__SECRET_KEY=key)
    with ExitStack() as cleanup:
        bases: list[str] = []
        for i in range(2):
            log = (tmp_path / f"api-{i}.stdout.log").open("w+")
            error = (tmp_path / f"api-{i}.stderr.log").open("w+")
            cleanup.callback(log.close)
            cleanup.callback(error.close)
            process = subprocess.Popen([sys.executable, "-m", "tests.support.shared_secret_probe", "serve"], cwd=root, env=env, stdout=log, stderr=error, text=True)

            def stop(proc: subprocess.Popen[str] = process) -> None:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
                assert proc.returncode in {0, -signal.SIGTERM}

            cleanup.callback(stop)
            deadline = time.monotonic() + 90
            base = None
            while base is None:
                assert process.poll() is None, "owned API process exited before startup"
                assert time.monotonic() < deadline, "owned API startup timed out"
                log.flush()
                log.seek(0)
                for line in log:
                    try:
                        state = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(state, dict) and state.get("base"):
                        assert Path(state["api_root"]).resolve() == root / "api/apps"
                        base = state["base"]
                time.sleep(0.05)
            bases.append(base)
            evidence["replicas"].append({"pid": process.pid, "base": base, "signers_initialized": True})
            while True:
                try:
                    response = requests.get(base + "/api/v1/system/ping", timeout=2)
                    assert response.status_code == 200
                    break
                except requests.ConnectionError:
                    assert time.monotonic() < deadline, "owned API listener never became ready"
                    time.sleep(0.05)
        for issuer, verifier in [(0, 1), (1, 0)]:
            with requests.Session() as client:
                issued = client.post(bases[issuer] + "/__secret_probe/session", timeout=10)
                assert issued.status_code == 200
                token = issued.json()["token"]
                assert "multirag_session" in client.cookies
                checked = client.get(bases[verifier] + "/__secret_probe/check", headers={"Authorization": f"Bearer {token}"}, timeout=10)
                assert checked.status_code == 200 and checked.json() == {"jwt": True, "cookie": True}
                rejected = client.get(bases[verifier] + "/__secret_probe/check", headers={"Authorization": "Bearer tampered"}, timeout=10)
                assert rejected.status_code == 401
                client.cookies.clear()
                checked = client.get(bases[verifier] + "/__secret_probe/check", headers={"Authorization": f"Bearer {token}"}, timeout=10)
                assert checked.json() == {"jwt": True, "cookie": False}
        evidence.update(jwt_both_directions=True, cookie_both_directions=True, tampered_token_denied=True)
    for base in bases:
        port = int(base.rsplit(":", 1)[1])
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
    evidence["cleanup"] = True
    (tmp_path / "auth-signers-http.json").write_text(json.dumps(evidence, indent=2))
