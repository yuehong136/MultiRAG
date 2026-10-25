"""Read committed provider settings from an owned scratch PostgreSQL DB."""

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from agent.sandbox import client
from api.db.db_models import SystemSettings
from tests.support.web_consumer import tsx_runner


def test_local_provider_uses_committed_settings_with_environment_override(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    saved = {"python_bin": sys.executable, "work_dir": str(tmp_path / "runs"), "timeout": 9, "max_artifacts": 2}
    with sessions() as db:
        db.add_all(
            [
                SystemSettings(name="sandbox.provider_type", source="config", data_type="string", value="local"),
                SystemSettings(name="sandbox.local", source="config", data_type="json", value=json.dumps(saved)),
            ]
        )
        db.commit()
    for name in list(os.environ):
        if name.startswith("SANDBOX_LOCAL_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(client, "SessionLocal", sessions)
    monkeypatch.setattr(client, "_provider_manager", None)
    monkeypatch.delenv("SANDBOX_PROVIDER_TYPE", raising=False)
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_LOCAL_TIMEOUT", "3")
    try:
        manager = client.get_provider_manager()
        assert manager.get_provider_name() == "local"
        provider = manager.get_provider()
        assert provider is not None
        assert provider.timeout == 3
        assert provider.max_artifacts == 2
        result = client.execute_code('def main(value): return {"value": value}', arguments={"value": False})
        assert result.exit_code == 0, result.stderr
        assert result.metadata["result_value"] == {"value": False}
        assert list((tmp_path / "runs").iterdir()) == []
        # Independent session: selection and overrides did not mutate saved config.
        with sessions() as db:
            readback = db.scalars(select(SystemSettings).where(SystemSettings.name == "sandbox.local")).one()
            assert json.loads(readback.value) == saved
    finally:
        client.reload_provider()
        with sessions() as db:
            for setting in db.scalars(select(SystemSettings).where(SystemSettings.name.in_(("sandbox.provider_type", "sandbox.local")))):
                db.delete(setting)
            db.commit()


def test_admin_variables_cli_routes_and_committed_sql(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run both CLI source and registered admin routes against the scratch DB."""
    import importlib
    import socket
    import subprocess
    import threading
    import time
    import uuid
    from collections.abc import Iterator

    import httpx
    import uvicorn
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from api.db.db_models import User, get_db
    from api.db.services.user_service import UserService
    from common import settings

    repo = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo / "admin/server"))
    monkeypatch.setattr(settings, "SECRET_KEY", "owned-admin-vars-secret")
    auth = importlib.import_module("auth")
    routes = importlib.import_module("routes")
    sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    monkeypatch.setattr(auth, "SessionLocal", sessions)
    admin_id, user_id = uuid.uuid4().hex, uuid.uuid4().hex
    email = f"{admin_id}@example.invalid"
    password = "scratch-variable-password"
    prefix = "batch18."
    with sessions() as db:
        UserService.save(db, id=admin_id, email=email, nickname="variable-admin", password=password, is_superuser=True, is_active=True, access_token="active", status="1")
        db.add(User(id=user_id, email=f"{user_id}@example.invalid", nickname="normal", is_superuser=False, is_active=True, access_token="active", status="1"))
        db.add_all(
            [
                SystemSettings(name=prefix + name, source="admin", data_type=kind, value=value)
                for name, kind, value in [
                    ("mail.port", "integer", "15"),
                    ("mail.server", "string", "before"),
                    ("mail.use_tls", "boolean", "true"),
                    ("json", "json", "{}"),
                    ("unsupported", "float", "1.5"),
                    ("literal_%a", "string", "literal"),
                    ("literalXXa", "string", "wildcard"),
                ]
            ]
        )
        db.commit()

    def get_scratch_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    app = FastAPI()
    app.include_router(routes.admin_router, prefix="/api/v1/admin")
    app.dependency_overrides[get_db] = get_scratch_db
    token = auth.admin_manager.create_access_token(data={"sub": email})
    headers = {"Authorization": f"Bearer {token}"}

    def snapshot() -> str:
        with bootstrapped_engine.connect() as reader:
            return reader.execute(text("SELECT jsonb_agg(to_jsonb(s) ORDER BY name)::text FROM usr_ai.t_ai_system_settings s WHERE name LIKE 'batch18.%' OR name LIKE 'sandbox.batch18%'")).scalar_one()

    with TestClient(app) as client:
        # Names follow the database collation, which can differ from Python's
        # Unicode ordering (for example literal_%a versus literalXXa in en_US).
        with bootstrapped_engine.connect() as reader:
            ordered_names = list(reader.scalars(text("SELECT name FROM usr_ai.t_ai_system_settings ORDER BY name ASC")))
        response = client.get("/api/v1/admin/variables", headers=headers)
        assert response.status_code == 200 and response.json()["code"] == 0
        rows = response.json()["data"]
        assert [row["name"] for row in rows] == ordered_names
        assert all(row["setting_type"] == "config" and "source" not in row for row in rows)
        response = client.get("/api/v1/admin/variables", params={"var_name": prefix + "mail."}, headers=headers).json()
        assert response["code"] == 0 and len(response["data"]) == 3
        assert [row["name"] for row in response["data"]] == [name for name in ordered_names if name.startswith(prefix + "mail.")]
        response = client.get("/api/v1/admin/variables", params={"var_name": prefix + "literal"}, headers=headers).json()
        assert response["code"] == 0 and [row["name"] for row in response["data"]] == [name for name in ordered_names if name.startswith(prefix + "literal")]
        response = client.get("/api/v1/admin/variables", params={"var_name": prefix + "literal_%"}, headers=headers).json()
        assert [row["name"] for row in response["data"]] == [prefix + "literal_%a"]
        for name, value in [
            (prefix + "mail.port", "0"),
            (prefix + "mail.use_tls", "false"),
            (prefix + "mail.server", ""),
            (prefix + "mail", "exact"),
            ("sandbox.batch18", '{"enabled":false}'),
            (prefix + "feature.enabled", "false"),
        ]:
            assert client.put("/api/v1/admin/variables", json={"var_name": name, "var_value": value}, headers=headers).json()["code"] == 0
            with sessions() as reader:
                assert reader.get(SystemSettings, name).value == value
        exact = client.get("/api/v1/admin/variables", params={"var_name": prefix + "mail"}, headers=headers).json()["data"]
        assert [row["name"] for row in exact] == [prefix + "mail"]
        before = snapshot()
        bad = [
            None,
            {},
            {"var_name": prefix + "mail.port"},
            {"var_name": "", "var_value": "x"},
            {"var_name": prefix + "mail.port", "var_value": "1.5"},
            {"var_name": prefix + "mail.use_tls", "var_value": "0"},
            {"var_name": prefix + "json", "var_value": "NaN"},
            {"var_name": prefix + "unsupported", "var_value": "1.0"},
            {"var_name": "sandbox.batch18_new", "var_value": "{"},
            {"var_name": prefix + "new.enabled", "var_value": "yes"},
            {"var_name": prefix + "mail.port", "var_value": 0},
            {"var_name": prefix + "mail.use_tls", "var_value": False},
            {"var_name": prefix + "mail.server", "var_value": None},
        ]
        for payload in bad:
            response = client.put("/api/v1/admin/variables", json=payload, headers=headers)
            assert response.status_code == 422 or response.json()["code"] != 0
            assert snapshot() == before
        normal = auth.admin_manager.create_access_token(data={"sub": f"{user_id}@example.invalid"})
        for denied in ({}, {"Authorization": "Bearer invalid"}, {"Authorization": f"Bearer {normal}"}):
            response = client.put("/api/v1/admin/variables", json={"var_name": prefix + "mail.port", "var_value": "42"}, headers=denied)
            assert response.status_code in {401, 403}
            assert snapshot() == before

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        for command, success in [
            ("SET VAR batch18.mail.port 0;", True),
            ("SET VAR batch18.mail.use_tls false;", True),
            ("SET VAR batch18.mail.server '';", True),
            ("SET VAR sandbox.batch18 '{\"enabled\":false}';", True),
            ("SHOW VAR batch18.mail.;", True),
            ("LIST VARS;", True),
            ("SET VAR batch18.mail.port invalid;", False),
            ("SHOW VAR missing_batch18;", False),
            ("SET VAR batch18.mail.port;", False),
        ]:
            before = snapshot()
            result = subprocess.run(
                [sys.executable, str(repo / "admin/client/multirag_cli.py"), "-h", "127.0.0.1", "-p", str(port), "-u", email, "-w", password, command],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert (result.returncode == 0) == success, (command, result.returncode, result.stdout, result.stderr)
            if command == "LIST VARS;":
                with bootstrapped_engine.connect() as reader:
                    ordered_names = list(reader.scalars(text("SELECT name FROM usr_ai.t_ai_system_settings ORDER BY name ASC")))
                table = [[cell.strip() for cell in line.split("|")[1:-1]] for line in result.stdout.splitlines() if line.startswith("|")]
                name_column = table[0].index("name")
                assert [row[name_column] for row in table[1:]] == ordered_names
            if not success:
                assert snapshot() == before
        # Live HTTP readback is separate from the CLI process and its login session.
        with httpx.Client() as client:
            rows = client.get(f"http://127.0.0.1:{port}/api/v1/admin/variables", params={"var_name": prefix + "mail.server"}, headers=headers).json()["data"]
            assert rows[0]["value"] == ""
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
        with sessions() as db:
            db.execute(text("DELETE FROM usr_ai.t_ai_system_settings WHERE name LIKE 'batch18.%' OR name LIKE 'sandbox.batch18%'"))
            for user in db.scalars(select(User).where(User.id.in_((admin_id, user_id)))):
                db.delete(user)
            db.commit()


@pytest.fixture
def sandbox_admin_runtime(postgres_service: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Any, dict[str, dict[str, str]], Any, Engine]]:
    """Registered admin routes and independent SQL readers in a manifested DB."""
    import importlib
    from collections.abc import Iterator

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from api.db.db_models import User, get_db
    from common import settings
    from tests.support.database import scratch_database

    repo = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(repo / "admin/server"))
    monkeypatch.setattr(settings, "SECRET_KEY", "owned-sandbox-admin-secret")
    auth = importlib.import_module("auth")
    routes = importlib.import_module("routes")
    evidence = Path(os.environ.get("SANDBOX_VALIDATION_EVIDENCE", str(tmp_path))) / "sql"
    with scratch_database(evidence) as engine:
        sessions = sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(auth, "SessionLocal", sessions)
        monkeypatch.setattr(client, "SessionLocal", sessions)
        for name in list(os.environ):
            if name.startswith("SANDBOX_LOCAL_") or name == "SANDBOX_PROVIDER_TYPE":
                monkeypatch.delenv(name)
        with sessions() as db:
            for role in ("admin", "user"):
                db.add(User(id=f"owned-sandbox-{role}", email=f"{role}@sandbox.invalid", nickname=role, is_superuser=role == "admin", is_active=True, access_token="owned", status="1"))
            db.commit()

        def get_scratch_db() -> Iterator[Session]:
            with sessions() as db:
                yield db

        app = FastAPI()
        app.include_router(routes.admin_router, prefix="/api/v1/admin")
        app.dependency_overrides[get_db] = get_scratch_db
        headers = {role: {"Authorization": "Bearer " + auth.admin_manager.create_access_token(data={"sub": f"{role}@sandbox.invalid"})} for role in ("admin", "user")}
        with TestClient(app) as http:
            try:
                yield http, headers, sessions, engine
            finally:
                client.reload_provider()


def test_admin_sandbox_identity_strict_types_defaults_and_no_denied_effects(
    sandbox_admin_runtime: tuple[Any, dict[str, dict[str, str]], Any, Engine], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    http, headers, sessions, _engine = sandbox_admin_runtime

    def snapshot() -> list[tuple[str, str]]:
        with sessions() as reader:
            return list(reader.execute(select(SystemSettings.name, SystemSettings.value).order_by(SystemSettings.name)))

    base = "/api/v1/admin/sandbox"
    before = snapshot()
    for denied in ({}, headers["user"], {"Authorization": "Bearer ordinary-user-access-token"}):
        for method, path, body in [
            ("GET", "/providers", None),
            ("GET", "/providers/ssh/schema", None),
            ("GET", "/config", None),
            ("POST", "/config", {"provider_type": "local", "config": {}, "set_active": True}),
            ("POST", "/test", {"provider_type": "local", "config": {"work_dir": str(tmp_path / "forbidden")}}),
        ]:
            response = http.request(method, base + path, headers=denied, **({"json": body} if body is not None else {}))
            assert response.status_code in {401, 403}
            assert snapshot() == before
            assert not (tmp_path / "forbidden").exists()
    providers = http.get(base + "/providers", headers=headers["admin"]).json()["data"]
    assert {item["id"] for item in providers} == {"local", "ssh", "self_managed", "aliyun_codeinterpreter", "e2b"}
    assert next(item for item in providers if item["id"] == "local")["available"] is False
    defaults = http.get(base + "/config", headers=headers["admin"]).json()["data"]
    assert defaults["config"]["endpoint"] == "http://sandbox-executor-manager:9385"
    assert "enable_seccomp" not in defaults["config"]
    schema = http.get(base + "/providers/self_managed/schema", headers=headers["admin"]).json()["data"]
    assert schema["enable_seccomp"]["type"] == "boolean" and schema["enable_seccomp"]["readonly"]
    disabled = http.post(base + "/config", headers=headers["admin"], json={"provider_type": "local", "config": {}})
    assert disabled.json()["code"] != 0 and snapshot() == before
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    for payload in [
        {"provider_type": "local", "config": {"timeout": True}},
        {"provider_type": "local", "config": {"timeout": 1.5}},
        {"provider_type": "local", "config": {"max_artifacts": -1}},
        {"provider_type": "local", "config": {"local_enabled": True}},
        {"provider_type": "local", "config": {"unknown": 1}},
        {"provider_type": "self_managed", "config": {"enable_seccomp": True}},
        {"provider_type": "local", "config": {}, "set_active": "false"},
        {"provider_type": "local", "config": []},
    ]:
        response = http.post(base + "/config", headers=headers["admin"], json=payload)
        assert response.status_code == 422 or response.json()["code"] != 0
        assert snapshot() == before
    local = {"python_bin": sys.executable, "work_dir": str(tmp_path / "runs"), "max_artifacts": 0, "timeout": 5}
    response = http.post(base + "/config", headers=headers["admin"], json={"provider_type": "local", "config": local})
    assert response.json()["code"] == 0, response.text
    with sessions() as reader:
        assert reader.get(SystemSettings, "sandbox.provider_type").value == "local"
        assert json.loads(reader.get(SystemSettings, "sandbox.local").value)["max_artifacts"] == 0
    result = http.post(base + "/test", headers=headers["admin"], json={"provider_type": "local", "config": local})
    assert result.json()["code"] == 0 and result.json()["data"]["success"] is True, result.text
    assert list((tmp_path / "runs").iterdir()) == []
    assert client.execute_code("def main(): return False").metadata["result_value"] is False
    cached = client.get_provider_manager()
    # Simulates another process committing configuration without touching this cache.
    with sessions() as writer:
        row = writer.get(SystemSettings, "sandbox.local")
        value = json.loads(row.value)
        value["timeout"] = 7
        row.value = json.dumps(value)
        writer.commit()
    fresh = client.get_provider_manager()
    assert fresh is not cached and fresh.get_provider().timeout == 7
    before = snapshot()
    monkeypatch.setenv("SANDBOX_LOCAL_TIMEOUT", "2")
    effective = http.get(base + "/config", headers=headers["admin"]).json()["data"]
    assert effective["config"]["timeout"] == 7 and "timeout" in effective["environment_overrides"]
    assert client.get_provider_manager().get_provider().timeout == 2
    assert snapshot() == before


def test_admin_sandbox_secret_redaction_inactive_save_and_atomic_rollback(sandbox_admin_runtime: tuple[Any, dict[str, dict[str, str]], Any, Engine], monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    http, headers, sessions, _engine = sandbox_admin_runtime
    base = "/api/v1/admin/sandbox"
    payload = {"provider_type": "ssh", "set_active": False, "config": {"host": "owned.invalid", "username": "runner", "password": "owned-only-secret", "max_artifacts": 0}}
    response = http.post(base + "/config", headers=headers["admin"], json=payload)
    assert response.json()["code"] == 0, response.text
    assert "owned-only-secret" not in response.text
    assert response.json()["data"]["active_provider_type"] == "self_managed"
    for path in (base + "/config?provider_type=ssh", "/api/v1/admin/variables?var_name=sandbox.ssh"):
        response = http.get(path, headers=headers["admin"])
        assert "owned-only-secret" not in response.text
    for missing_secret in (True, False):
        config = {"host": "owned.invalid", "username": "runner", "private_key": "owned-new-key"} if not missing_secret else {"host": "owned.invalid", "username": "runner"}
        if not missing_secret:
            config["password"] = ""
        response = http.post(base + "/config", headers=headers["admin"], json={"provider_type": "ssh", "config": config, "set_active": False})
        assert response.json()["code"] == 0, response.text
        with sessions() as reader:
            saved = json.loads(reader.get(SystemSettings, "sandbox.ssh").value)
            assert saved["password"] == ("owned-only-secret" if missing_secret else "")
            if not missing_secret:
                assert saved["private_key"] == "owned-new-key"
            assert reader.get(SystemSettings, "sandbox.provider_type") is None
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")

    def fail_selector(session: Session, *_args: Any) -> None:
        if any(isinstance(row, SystemSettings) and row.name == "sandbox.provider_type" for row in session.new):
            raise RuntimeError("Owned failure before selector commit")

    event.listen(Session, "before_flush", fail_selector)
    try:
        response = http.post(base + "/config", headers=headers["admin"], json={"provider_type": "local", "config": {}})
        assert response.json()["code"] != 0
    finally:
        event.remove(Session, "before_flush", fail_selector)
    with sessions() as reader:
        assert reader.get(SystemSettings, "sandbox.local") is None
        assert reader.get(SystemSettings, "sandbox.provider_type") is None


def test_admin_sandbox_test_failure_always_destroys_and_does_not_write(sandbox_admin_runtime: tuple[Any, dict[str, dict[str, str]], Any, Engine], monkeypatch: pytest.MonkeyPatch) -> None:
    import sandbox_service

    http, headers, sessions, _engine = sandbox_admin_runtime
    destroyed: list[str] = []
    executed: list[bool] = []

    class Provider:
        def initialize(self, config: dict[str, Any]) -> bool:
            return True

        def create_instance(self, template: str) -> Any:
            from agent.sandbox.providers.base import SandboxInstance

            return SandboxInstance("owned-instance", "e2b", "running", {})

        def execute_code(self, *_args: Any, **_kwargs: Any) -> Any:
            executed.append(True)
            raise TimeoutError("Owned test timeout")

        def destroy_instance(self, instance_id: str) -> bool:
            destroyed.append(instance_id)
            return True

        @staticmethod
        def get_config_schema() -> dict[str, Any]:
            return {}

        def validate_config(self, config: dict[str, Any]) -> tuple[bool, str | None]:
            return True, None

    monkeypatch.setitem(sandbox_service.SandboxMgr.PROVIDER_CLASSES, "e2b", Provider)
    response = http.post("/api/v1/admin/sandbox/test", headers=headers["admin"], json={"provider_type": "e2b", "config": {}})
    assert response.json()["code"] != 0
    assert executed == [True] and destroyed == ["owned-instance"]
    with sessions() as reader:
        assert list(reader.scalars(select(SystemSettings))) == []


@pytest.mark.external_consumer
def test_actual_web_sandbox_admin_client(sandbox_admin_runtime: tuple[Any, dict[str, dict[str, str]], Any, Engine], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import socket
    import subprocess
    import threading
    import time

    import uvicorn

    http, headers, sessions, engine = sandbox_admin_runtime
    checkout = Path(os.environ.get("WEB_SANDBOX_CHECKOUT", "/Users/xldu/project/web"))
    runner = tsx_runner(checkout)
    assert runner.is_file(), "The fixed Web consumer and dependencies are required."
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    evidence = Path(os.environ.get("SANDBOX_VALIDATION_EVIDENCE", str(tmp_path)))
    evidence.mkdir(exist_ok=True, parents=True)
    resource = evidence / "web-consumer-listener.json"
    resource.write_text(json.dumps({"host": "127.0.0.1", "port": port, "pid": os.getpid(), "closed": False}))
    server = uvicorn.Server(uvicorn.Config(http.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    local_root = tmp_path / "web-runs"
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        result = subprocess.run(
            [str(runner), "--tsconfig", str(checkout / "tsconfig.app.json"), str(Path(__file__).resolve().parents[1] / "support/sandbox_admin_web_consumer.mts")],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
            env={
                **os.environ,
                "WEB_SANDBOX_CHECKOUT": str(checkout),
                "SANDBOX_ADMIN_BASE": f"http://127.0.0.1:{port}",
                "SANDBOX_ADMIN_TOKEN": headers["admin"]["Authorization"].removeprefix("Bearer "),
                "SANDBOX_NORMAL_TOKEN": headers["user"]["Authorization"].removeprefix("Bearer "),
                "SANDBOX_PYTHON_BIN": sys.executable,
                "SANDBOX_LOCAL_ROOT": str(local_root),
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout.splitlines()[-1])
        assert receipt["passed"] and receipt["actualLocalProbe"] and receipt["ordinaryUserDenied"]
        with sessions() as reader:
            saved = json.loads(reader.get(SystemSettings, "sandbox.local").value)
            assert saved["timeout"] == 4 and saved["max_artifacts"] == 0
            assert reader.get(SystemSettings, "sandbox.provider_type").value == "local"
        assert list(local_root.iterdir()) == []
        receipt["web_sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
        (evidence / "web-consumer-receipt.json").write_text(json.dumps(receipt, indent=2))
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        assert not thread.is_alive()
        probe = socket.socket()
        assert probe.connect_ex(("127.0.0.1", port)) != 0
        probe.close()
        resource.write_text(json.dumps({"host": "127.0.0.1", "port": port, "pid": os.getpid(), "closed": True}))
