"""Real main-API login interoperability and rejected-request SQL readback."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session


def verify(engine: sa.Engine, evidence: Path, config: Path, bases: list[str], env: dict[str, str], admin_credentials: tuple[str, str]) -> dict[str, Any]:
    from redis import Redis

    from api.db.db_models import Conversation, Dialog, Tenant, User, UserTenant
    from api.db.services.user_service import UserService
    from api.utils.crypt import crypt

    users = [uuid4().hex for _ in range(4)]
    password = uuid4().hex
    encrypted = crypt(password)
    dialogs = [uuid4().hex for _ in range(2)]
    sessions = [uuid4().hex for _ in range(2)]
    emails = [f"{uid}@hybrid.invalid" for uid in users]
    with Session(engine) as db:
        for uid, email in zip(users, emails, strict=True):
            db.add(User(id=uid, email=email, nickname="Owned hybrid", password=UserService.hash_password(password), access_token=uuid4().hex))
            db.add(Tenant(id=uid, name="Owned hybrid", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(UserTenant(id=uuid4().hex, user_id=uid, tenant_id=uid, role="owner", invited_by=uid))
        for uid, role in zip(users[1:3], ["normal", "invite"], strict=True):
            db.add(UserTenant(id=uuid4().hex, user_id=uid, tenant_id=users[0], role=role, invited_by=users[0]))
        for dialog, session, owner in zip(dialogs, sessions, users[:2], strict=True):
            db.add(Dialog(id=dialog, tenant_id=owner, name="Owned", llm_id=""))
            db.add(Conversation(id=session, dialog_id=dialog, user_id=owner, name="Owned", message=[], reference=[]))
        db.commit()

    from ruamel.yaml import YAML

    with config.open() as source:
        redis_config = YAML(typ="safe").load(source)["redis"]
    redis_host, redis_port = redis_config["host"].rsplit(":", 1)
    redis = Redis(host=redis_host, port=int(redis_port), password=redis_config.get("password") or None, db=int(redis_config.get("db", 0)))

    def snapshot() -> str:
        with engine.connect() as connection:
            state = {
                name: [dict(row) for row in connection.execute(sa.text(f"SELECT * FROM usr_ai.{name} ORDER BY id")).mappings()]
                for name in ["t_ai_users", "t_ai_tenants", "t_ai_user_tenants", "t_ai_dialogs", "t_ai_conversations", "t_ai_files"]
            }
        state["owned_redis"] = [(key.hex(), hashlib.sha256(redis.dump(key) or b"").hexdigest()) for key in sorted(redis.scan_iter())]
        return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()

    def start_go(directory: Path, configuration: Path) -> tuple[subprocess.Popen[bytes], Any]:
        log = (directory / "go.log").open("wb")
        process = subprocess.Popen(
            [os.environ.get("AUTH_SMOKE_GO") or shutil.which("go") or "go", "test", "./internal/handler", "-run", "^TestHybridAuthLiveServer$", "-count=1", "-timeout=6m", "-v"],
            env=dict(env, MULTIRAG_HYBRID_LIVE_CONFIG=str(configuration)),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(120):
            assert process.poll() is None, "Go live process failed; inspect owned log"
            if (directory / "go-base").exists():
                return process, log
            time.sleep(1)
        process.terminate()
        process.wait(timeout=20)
        log.close()
        raise AssertionError("Go live startup timeout")

    def stop_go(process: subprocess.Popen[bytes], log: Any, directory: Path) -> None:
        (directory / "go-stop").touch()
        try:
            assert process.wait(timeout=25) == 0
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=20)
            log.close()

    process, log = start_go(evidence, config)
    result: dict[str, Any] = {}
    try:
        go_base = (evidence / "go-base").read_text()
        response = requests.post(bases[0] + "/api/v1/auth/login", json={"username": emails[0], "password": encrypted}, timeout=15)
        assert response.json()["retcode"] == 0
        python_token = response.headers["Authorization"]
        response = requests.get(go_base + "/api/v1/users/me", headers={"Authorization": "Bearer " + python_token}, timeout=10)
        assert response.json()["code"] == 0 and response.json()["data"]["id"] == users[0]
        assert not {"password", "access_token"} & response.json()["data"].keys()
        result["python_login_go_identity"] = True
        tokens = []
        for uid, email in zip(users, emails, strict=True):
            response = requests.post(go_base + "/api/v1/auth/login", json={"username": email, "password": encrypted}, timeout=10)
            assert response.json()["code"] == 0, response.json().get("message", "Go login failed")
            token = response.headers["Authorization"]
            tokens.append(token)
            response = requests.get(bases[1] + "/api/v1/users/me", headers={"Authorization": "Bearer " + token}, timeout=10)
            assert response.json()["retcode"] == 0 and response.json()["data"]["id"] == uid
        result["go_login_python_identity"] = True
        admin_email, admin_password = admin_credentials
        response = requests.post(go_base + "/api/v1/admin/login", json={"email": admin_email, "password": crypt(admin_password)}, timeout=15)
        assert response.json()["code"] == 0
        admin_token = response.headers["Authorization"]
        assert requests.get(go_base + "/api/v1/admin/auth", headers={"Authorization": "Bearer " + admin_token}, timeout=10).json()["code"] == 0
        control_before = snapshot()
        assert requests.post(go_base + "/owned-admin/mutate", headers={"Authorization": "Bearer " + admin_token}, timeout=10).status_code == 200
        assert snapshot() != control_before
        result["downstream_mutation_positive_control"] = True
        admin_before = snapshot()
        assert requests.get(go_base + "/api/v1/users/me", headers={"Authorization": "Bearer " + admin_token}, timeout=10).status_code == 403
        assert requests.get(bases[0] + "/admin/", headers={"Authorization": "Bearer " + admin_token}, allow_redirects=False, timeout=10).status_code in {302, 303, 307}
        assert snapshot() == admin_before
        result["admin_boundaries_preserved"] = True
        before = snapshot()
        for base in [go_base, bases[0]]:
            for index in range(4):
                response = requests.get(base + f"/api/v1/chats/{dialogs[0]}/sessions", headers={"Authorization": "Bearer " + tokens[index]}, timeout=10)
                if index == 0:
                    assert response.json()["code"] == 0
                    assert [entry["id"] for entry in response.json()["data"]] == [sessions[0]]
                else:
                    assert response.json()["code"] != 0
        result["owner_member_invite_session_scope"] = True
        for index in range(4):
            response = requests.post(go_base + "/owned-admin/mutate", headers={"Authorization": "Bearer " + tokens[index]}, timeout=10)
            assert response.status_code == 403
        assert requests.post(go_base + "/api/v1/admin/login", json={"email": emails[0], "password": encrypted}, timeout=10).json()["code"] != 0
        for base in [go_base, bases[0]]:
            assert requests.get(base + "/api/v1/users/me", headers={"Authorization": "Bearer tampered"}, timeout=10).status_code == 401
        assert snapshot() == before
        result["denied_sql_unchanged"] = True
        with Session(engine) as db:
            db.execute(sa.update(User).where(User.id == users[1]).values(is_active=False))
            db.commit()
        before = snapshot()
        for base in [go_base, bases[0]]:
            assert requests.get(base + "/api/v1/users/me", headers={"Authorization": "Bearer " + tokens[1]}, timeout=10).status_code == 401
        assert snapshot() == before
        result["disabled_user_denied_unchanged"] = True
        assert requests.post(go_base + "/api/v1/auth/logout", headers={"Authorization": "Bearer " + tokens[0]}, timeout=10).json()["code"] == 0
        before = snapshot()
        for base in [go_base, bases[0]]:
            assert requests.get(base + "/api/v1/users/me", headers={"Authorization": "Bearer " + tokens[0]}, timeout=10).status_code == 401
        assert snapshot() == before
        result["logout_both_reject_unchanged"] = True
    finally:
        stop_go(process, log, evidence)
    result["go_process_stopped"] = True
    directory = evidence / "disabled"
    directory.mkdir(mode=0o700)
    configuration = directory / "private.yaml"
    with config.open() as source:
        disabled = YAML(typ="safe").load(source)
    disabled["authentication"] = {"enable_register": False, "disable_password_login": True}
    configuration.touch(mode=0o600)
    with configuration.open("w") as target:
        YAML().dump(disabled, target)
    process, log = start_go(directory, configuration)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    python_log = (directory / "python.log").open("wb")
    python = subprocess.Popen(
        [sys.executable, "-m", "tests.support.shared_secret_live", "serve", str(port)], env=dict(env, MULTIRAG_CONFIG_OVERLAY_FILE=str(configuration)), stdout=python_log, stderr=subprocess.STDOUT
    )
    try:
        disabled_python = f"http://127.0.0.1:{port}"
        for _ in range(90):
            assert python.poll() is None
            try:
                if requests.get(disabled_python + "/api/v1/system/ping", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise AssertionError("disabled Python API startup timed out")
        before = snapshot()
        for base, business_key in [((directory / "go-base").read_text(), "code"), (disabled_python, "retcode")]:
            config_response = requests.get(base + "/api/v1/system/config", timeout=10).json()
            assert config_response["data"]["registerEnabled"] == 0
            assert config_response["data"]["disablePasswordLogin"] is True
            response = requests.post(base + "/api/v1/auth/login", json={"username": emails[3], "password": encrypted}, timeout=10)
            assert response.json()[business_key] != 0
            response = requests.post(base + "/api/v1/users", json={"email": f"{uuid4().hex}@denied.invalid", "password": encrypted, "nickname": "Denied"}, timeout=10)
            assert response.json()[business_key] != 0
        assert requests.post(disabled_python + "/auth/token", data={"username": emails[3], "password": password}, timeout=10).status_code == 403
        assert snapshot() == before
        result["disabled_login_registration_sql_unchanged"] = True
    finally:
        python.terminate()
        python.wait(timeout=20)
        python_log.close()
        stop_go(process, log, directory)
        configuration.unlink()
    result["disabled_processes_stopped"] = True
    (evidence / "hybrid.json").write_text(json.dumps(result, indent=2))
    return result
