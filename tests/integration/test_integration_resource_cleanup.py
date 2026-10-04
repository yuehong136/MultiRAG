"""Fixture construction failures must clean resources before reaching yield."""

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from minio import Minio

from api.db.db_models import APIToken, Tenant, User, UserTenant
from common.config_utils import CONFIGS
from tests.support import runtime_upload


def test_failed_http_fixture_setup_removes_committed_rows_and_bucket(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fail_engine(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("controlled failure after bucket creation")

    monkeypatch.setattr(runtime_upload, "create_async_engine", fail_engine)
    monkeypatch.setenv("MULTIRAG_343BDA_EVIDENCE_DIR", str(tmp_path))
    fixture = runtime_upload.runtime_upload_api.__wrapped__(bootstrapped_engine, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="controlled failure"):
        next(fixture)
    record = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert record["bucket_removed"] is True
    cfg = CONFIGS["minio"]
    reader = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    assert not reader.bucket_exists(record["bucket"])
    with bootstrapped_engine.connect() as connection:
        for model, column in [(APIToken, APIToken.tenant_id), (UserTenant, UserTenant.user_id), (Tenant, Tenant.id), (User, User.id)]:
            assert connection.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(record["owners"]))) == 0
