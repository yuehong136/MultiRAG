"""Tests for the unified process bootstrap entry point."""

import pytest

from common import bootstrap


@pytest.fixture
def initialization_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    calls: list[object] = []
    monkeypatch.setattr(bootstrap, "get_app_config", lambda: calls.append("config"))
    monkeypatch.setattr(bootstrap.observability, "init_otel", lambda: calls.append("observability"))
    monkeypatch.setattr(bootstrap.resources, "init_resources", lambda *, force: calls.append(("resources", force)))
    return calls


def test_ensure_initialized_keeps_default_resource_initialization(initialization_calls: list[object]) -> None:
    bootstrap.ensure_initialized()

    assert initialization_calls == ["config", "observability", ("resources", False)]


def test_ensure_initialized_preserves_positional_force(initialization_calls: list[object]) -> None:
    bootstrap.ensure_initialized(True)

    assert initialization_calls == ["config", "observability", ("resources", True)]


def test_ensure_initialized_can_skip_stateful_resources(initialization_calls: list[object]) -> None:
    bootstrap.ensure_initialized(force=True, initialize_resources=False)

    assert initialization_calls == ["config", "observability"]
