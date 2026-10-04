"""Fixture construction failures must clean resources before reaching yield."""

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from minio import Minio

from api.db.db_models import APIToken, Tenant, User, UserTenant
from common.config_utils import CONFIGS
from tests.support import agent_update_release, dataset_management_http, document_image_read_service, runtime_upload


@pytest.mark.parametrize("support,fixture_name", [(agent_update_release, "release_api"), (dataset_management_http, "management_api")])
def test_failed_shared_http_setup_removes_committed_rows(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, support: Any, fixture_name: str) -> None:
    models = [APIToken, Tenant, User, UserTenant]

    def rows() -> dict[str, list[Any]]:
        with bootstrapped_engine.connect() as connection:
            return {model.__tablename__: list(connection.execute(sa.select(model.__table__)).mappings()) for model in models}

    before = rows()

    def fail_engine(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("controlled failure after SQL commit")

    monkeypatch.setattr(support, "create_async_engine", fail_engine)
    args = [bootstrapped_engine, monkeypatch]
    if fixture_name == "management_api":
        args.append(tmp_path)
    fixture = getattr(support, fixture_name).__wrapped__(*args)
    with pytest.raises(RuntimeError, match="controlled failure"):
        next(fixture)
    assert rows() == before


def test_image_fixture_cleans_bucket_when_create_acknowledgement_is_lost(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    created: list[str] = []
    original = Minio.make_bucket

    def fail_after_create(self: Minio, bucket_name: str, *args: Any, **kwargs: Any) -> None:
        original(self, bucket_name, *args, **kwargs)
        created.append(bucket_name)
        raise RuntimeError("controlled failure after bucket creation")

    monkeypatch.setattr(Minio, "make_bucket", fail_after_create)
    fixture = document_image_read_service.image_resources.__wrapped__(bootstrapped_engine, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="controlled failure"):
        next(fixture)
    assert len(created) == 1
    cfg = CONFIGS["minio"]
    reader = Minio(cfg["host"], access_key=cfg["user"], secret_key=cfg["password"], secure=str(cfg.get("secure", False)).lower() in {"true", "1", "yes"})
    assert not reader.bucket_exists(created[0])


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


def test_scratch_database_lost_create_reply_still_drops_owned_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.support.database import _pg_url, scratch_database

    execute = sa.Connection.execute
    created: list[str] = []

    def lost_reply(self: sa.Connection, statement: Any, *args: Any, **kwargs: Any) -> Any:
        result = execute(self, statement, *args, **kwargs)
        if str(statement).startswith('CREATE DATABASE "multirag_test_'):
            created.append(str(statement).split('"')[1])
            raise RuntimeError("controlled lost create response")
        return result

    monkeypatch.setattr(sa.Connection, "execute", lost_reply)
    with pytest.raises(RuntimeError, match="controlled lost create"):
        with scratch_database(tmp_path):
            pytest.fail("initialization must fail")
    assert len(created) == 1
    admin = sa.create_engine(_pg_url(CONFIGS["postgresql"]["dbname"]))
    try:
        with admin.connect() as reader:
            assert reader.scalar(sa.text("SELECT count(*) FROM pg_database WHERE datname=:name"), {"name": created[0]}) == 0
    finally:
        admin.dispose()
