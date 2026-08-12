from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.db import schema_bootstrap


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
