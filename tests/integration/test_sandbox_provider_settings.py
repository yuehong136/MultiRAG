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


def test_local_provider_uses_committed_settings_with_environment_override(pg_scratch_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    SystemSettings.__table__.create(pg_scratch_engine)
    sessions = sessionmaker(pg_scratch_engine, expire_on_commit=False)
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
