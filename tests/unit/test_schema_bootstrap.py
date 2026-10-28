from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from api.db import db_models, schema_bootstrap


def _arrange(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_tables: list[str],
) -> list[tuple[str, bool | None]]:
    calls: list[tuple[str, bool | None]] = []
    monkeypatch.setattr(
        schema_bootstrap,
        "sa_inspect",
        lambda _engine: SimpleNamespace(
            get_table_names=lambda *, schema: existing_tables,
        ),
    )
    monkeypatch.setattr(
        schema_bootstrap,
        "init_database_tables",
        lambda: calls.append(("create", None)),
    )
    monkeypatch.setattr(
        schema_bootstrap,
        "upgrade_database_tables",
        lambda *, is_fresh_install: calls.append(
            ("migrate", is_fresh_install),
        ),
    )
    return calls


def test_fresh_database_creates_models_before_stamping_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _arrange(monkeypatch, existing_tables=[])

    schema_bootstrap.bootstrap_database_schema()

    assert calls == [("create", None), ("migrate", True)]


def test_stored_database_migrates_before_model_first_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _arrange(monkeypatch, existing_tables=["t_ai_chat_channels"])

    schema_bootstrap.bootstrap_database_schema()

    assert calls == [("migrate", False), ("create", None)]


def test_stored_database_does_not_create_models_after_failed_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _arrange(monkeypatch, existing_tables=["t_ai_chat_channels"])

    def _fail_migration(*, is_fresh_install: bool) -> None:
        calls.append(("migrate", is_fresh_install))
        raise RuntimeError("migration failed")

    monkeypatch.setattr(
        schema_bootstrap,
        "upgrade_database_tables",
        _fail_migration,
    )

    with pytest.raises(RuntimeError, match="migration failed"):
        schema_bootstrap.bootstrap_database_schema()

    assert calls == [("migrate", False)]


@pytest.mark.parametrize("existing_tables", [[], ["t_ai_chat_channels"]], ids=["fresh", "stored"])
@pytest.mark.parametrize("connection_failure", [False, True], ids=["missing-schema", "connection-error"])
def test_table_initialization_failure_stops_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    existing_tables: list[str],
    connection_failure: bool,
) -> None:
    calls = _arrange(monkeypatch, existing_tables=existing_tables)

    @contextmanager
    def connect() -> Iterator[Any]:
        if connection_failure:
            raise OperationalError("schema-check", {}, RuntimeError("connection unavailable"))
        yield SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchone=lambda: None))

    monkeypatch.setattr(db_models, "engine", SimpleNamespace(connect=connect))
    monkeypatch.setattr(schema_bootstrap, "init_database_tables", db_models.init_database_tables)

    with pytest.raises(RuntimeError, match=r"[Ss]chema|database schema"):
        schema_bootstrap.bootstrap_database_schema()

    assert calls == ([("migrate", False)] if existing_tables else [])
