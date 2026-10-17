"""Owned full-resource API smoke and SQLAdmin replica-cookie acceptance.

Run with an owned Infinity URI and an evidence directory. No worker lifespan is
started: this exercises real application import, resources, routes and SQL auth.
"""

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4


def serve() -> None:
    import uvicorn

    from api import apps
    from common import resources, settings

    assert resources._state["doc_store"] is not None
    assert apps.manager.secret.secret_for_encode == settings.SECRET_KEY.encode()
    uvicorn.run(apps.app, host="127.0.0.1", port=int(sys.argv[2]), lifespan="off", log_level="error", access_log=False)


def main() -> None:
    import requests
    from minio import Minio
    from ruamel.yaml import YAML
    from sqlalchemy.orm import Session

    from api.db.db_models import User
    from api.db.services.user_service import UserService
    from common.config_utils import CONFIGS
    from tests.support.database import scratch_database

    evidence = Path(sys.argv[2]).resolve()
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    result: dict[str, object] = {"resource_bootstrap": "actual", "worker_lifespan": "off", "replicas_stopped": False}
    root = Path(__file__).resolve().parents[2]
    files = ["api/apps/__init__.py", "common/resources.py", "common/app_config.py", "core/utils/redis_conn.py", "tests/support/shared_secret_live.py"]
    result["source"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    changed = subprocess.check_output(["git", "diff", "--name-only", "HEAD"], text=True).splitlines()
    files.extend(name for name in changed if name.endswith((".py", ".go")) and name not in files)
    result["source"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
    result["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    with ExitStack() as cleanup:
        cleanup.callback(lambda: (evidence / "acceptance.json").write_text(json.dumps(result, indent=2)))
        engine = cleanup.enter_context(scratch_database(evidence))
        bucket = "auth-smoke-" + uuid4().hex[:16]
        mc = {"host": os.environ["AUTH_SMOKE_MINIO"], "user": "owned-smoke-user", "password": "owned-smoke-password", "secure": False, "region": ""}
        secure = str(mc.get("secure", False)).lower() in {"true", "1"}
        client = Minio(mc["host"], access_key=mc["user"], secret_key=mc["password"], secure=secure)

        def remove_bucket() -> None:
            client.remove_bucket(bucket)
            assert not client.bucket_exists(bucket)
            result["bucket_absent"] = True

        assert not client.bucket_exists(bucket)
        client.make_bucket(bucket)
        cleanup.callback(remove_bucket)
        email = f"{uuid4().hex}@auth-smoke.invalid"
        password = uuid4().hex
        with Session(engine) as db:
            db.add(User(id=uuid4().hex, email=email, nickname="Owned smoke", password=UserService.hash_password(password), is_superuser=True, access_token=uuid4().hex))
            db.commit()
        overlay = {
            "redis": {"host": os.environ["AUTH_SMOKE_REDIS"], "password": "", "db": 0},
            "postgresql": dict(CONFIGS["postgresql"], dbname=engine.url.database),
            "minio": dict(mc, bucket=bucket, prefix_path=""),
            "infinity": dict(CONFIGS.get("infinity", {}), uri=sys.argv[1], db_name="auth_smoke_" + uuid4().hex[:12]),
            "multirag": dict(CONFIGS["multirag"], secret_key="owned-smoke-key-" + uuid4().hex),
        }
        config = evidence / "private.yaml"
        config.touch(mode=0o600)
        with config.open("w") as out:
            YAML().dump(overlay, out)
        cleanup.callback(config.unlink)
        env = dict(os.environ, MULTIRAG_CONFIG_OVERLAY_FILE=str(config), DOC_ENGINE="infinity", PYTHONDONTWRITEBYTECODE="1", ADMIN_REQUIRE_SUPERUSER="true")
        processes: list[subprocess.Popen[bytes]] = []

        def stop() -> None:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=20)
            result["replicas_stopped"] = True

        cleanup.callback(stop)
        bases = []
        for index in range(2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            bases.append(f"http://127.0.0.1:{port}")
            log = cleanup.enter_context((evidence / f"replica-{index}.log").open("wb"))
            processes.append(subprocess.Popen([sys.executable, "-m", "tests.support.shared_secret_live", "serve", str(port)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT))
            for _ in range(90):
                assert processes[-1].poll() is None, "replica exited during actual bootstrap; inspect private local log"
                try:
                    if requests.get(bases[-1] + "/api/v1/system/ping", timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("actual API bootstrap timed out")
        for index, base in enumerate(bases):
            smoke_env = dict(env, SMOKE_BASE_URL=base)
            with (evidence / f"smoke-{index}.log").open("wb") as log:
                command = subprocess.run(["make", "smoke", "UV=uv run --no-sync"], cwd=root, env=smoke_env, stdout=log, stderr=subprocess.STDOUT, check=False)
            result[f"make_smoke_{index}_exit"] = command.returncode
            assert command.returncode == 0
            response = requests.get(base + "/api/v1/system/healthz", timeout=30)
            health = {k: v for k, v in response.json().items() if not k.startswith("_")}
            result[f"health_{index}"] = health
            assert response.status_code == 200 and all(v == "ok" for v in health.values())
        for source, target in [(0, 1), (1, 0)]:
            session = requests.Session()
            response = session.post(bases[source] + "/admin/login", data={"username": email, "password": password}, allow_redirects=False, timeout=10)
            assert response.status_code == 302 and session.cookies, "real SQLAdmin login failed"
            assert session.get(bases[target] + "/admin/", allow_redirects=False, timeout=10).status_code == 200
            result[f"cookie_{source}_to_{target}"] = True
            session.cookies.clear()
            assert session.get(bases[target] + "/admin/", allow_redirects=False, timeout=10).status_code in {302, 303, 307}
        result["passed"] = True
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve()
    else:
        main()
