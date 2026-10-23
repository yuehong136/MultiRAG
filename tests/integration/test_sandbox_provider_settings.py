"""Read committed provider settings from an owned scratch PostgreSQL DB."""

import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from agent.sandbox import client
from api.db.db_models import SystemSettings


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
        rows = client.get("/api/v1/admin/variables", headers=headers).json()["data"]
        assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
        assert all(row["setting_type"] == "config" and "source" not in row for row in rows)
        response = client.get("/api/v1/admin/variables", params={"var_name": prefix + "mail."}, headers=headers).json()
        assert response["code"] == 0 and len(response["data"]) == 3
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
