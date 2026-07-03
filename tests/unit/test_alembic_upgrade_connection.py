from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

# Ensure project root is importable when pytest adjusts cwd/sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from api.db import db_models


class _FakeConnectionContext:
    def __init__(self, connection: object):
        self._connection = connection

    def __enter__(self) -> object:
        return self._connection

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeEngine:
    def __init__(self, connection: object):
        self._connection = connection

    def connect(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self._connection)

    def begin(self) -> _FakeConnectionContext:
        return _FakeConnectionContext(self._connection)


class _FakeConfig:
    created: list[_FakeConfig] = []

    def __init__(self, ini_path: str):
        self.ini_path = ini_path
        self.options: dict[str, str] = {}
        self.attributes: dict[str, object] = {}
        self.__class__.created.append(self)

    def set_main_option(self, key: str, value: str) -> None:
        self.options[key] = value


def _patch_upgrade_dependencies(monkeypatch: pytest.MonkeyPatch) -> tuple[object, list[tuple[_FakeConfig, str]]]:
    fake_connection = object()
    upgrade_calls: list[tuple[_FakeConfig, str]] = []

    monkeypatch.setattr(db_models, "Config", _FakeConfig)
    monkeypatch.setattr(db_models, "engine", _FakeEngine(fake_connection))
    monkeypatch.setattr(db_models.os.path, "exists", lambda _: True)
    monkeypatch.setattr(
        db_models,
        "MigrationContext",
        SimpleNamespace(configure=lambda connection, opts: SimpleNamespace(get_current_revision=lambda: "base")),
    )
    monkeypatch.setattr(
        db_models,
        "ScriptDirectory",
        SimpleNamespace(from_config=lambda cfg: SimpleNamespace(get_current_head=lambda: "head")),
    )
    monkeypatch.setattr(
        db_models.command,
        "upgrade",
        lambda cfg, revision: upgrade_calls.append((cfg, revision)),
    )
    _FakeConfig.created.clear()
    return fake_connection, upgrade_calls


def test_upgrade_database_tables_uses_existing_connection_for_alembic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_connection, upgrade_calls = _patch_upgrade_dependencies(monkeypatch)

    result = db_models.upgrade_database_tables()

    assert result == "数据库迁移成功完成"
    assert len(_FakeConfig.created) == 1
    cfg = _FakeConfig.created[0]
    assert cfg.options.get("script_location")
    assert "sqlalchemy.url" not in cfg.options
    assert cfg.attributes["connection"] is fake_connection
    assert upgrade_calls == [(cfg, "head")]


def test_upgrade_database_tables_raises_on_migration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_upgrade_dependencies(monkeypatch)
    monkeypatch.setattr(
        db_models.command,
        "upgrade",
        lambda _cfg, _revision: (_ for _ in ()).throw(ValueError("boom")),
    )

    with pytest.raises(RuntimeError, match="数据库迁移过程中发生错误"):
        db_models.upgrade_database_tables()
