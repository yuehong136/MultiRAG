"""Tests for the process and default-tenant bootstrap entry points."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from api.db import init_data
from api.db.joint_services import user_account_service
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


def test_superuser_probes_models_under_the_initialized_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock(spec=Session)
    tenant = {"tenant_id": "startup-tenant", "llm_id": "configured-chat", "embd_id": "configured-embedding"}
    monkeypatch.setattr(init_data.UserService, "query_user_onlywith_email", lambda *_: None)
    monkeypatch.setattr(init_data.UserService, "save", MagicMock())
    monkeypatch.setattr(user_account_service, "initialize_user_tenant", lambda *_: SimpleNamespace(to_dict=lambda: tenant))
    find_model = MagicMock()
    chat = SimpleNamespace(async_chat=AsyncMock(return_value="ready"))
    embedding = SimpleNamespace(encode=MagicMock(return_value=([0.0], 1)))
    make_bundle = MagicMock(side_effect=[chat, embedding])
    monkeypatch.setattr(init_data, "get_model_config_by_type_and_name", find_model)
    monkeypatch.setattr(init_data, "LLMBundle", make_bundle)

    init_data.init_superuser(db, email="bootstrap@example.test", password="fixture-password")

    db.commit.assert_called_once()
    assert [call.args[1:] for call in find_model.call_args_list] == [
        ("startup-tenant", "chat", "configured-chat"),
        ("startup-tenant", "embedding", "configured-embedding"),
    ]
    assert [call.args[1] for call in make_bundle.call_args_list] == ["startup-tenant", "startup-tenant"]
    chat.async_chat.assert_awaited_once()
    embedding.encode.assert_called_once()
