"""Dialog bindings select a model without requiring a client-supplied name."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db.services import dialog_service
from common.constants import LLMType


@pytest.mark.parametrize(("name", "binding", "reference"), [("", 42, "42"), ("stale-name", 42, "42"), ("42", None, "42"), ("model@provider", None, "model@provider")])
def test_dialog_primary_model_uses_authorized_binding(db: Session, monkeypatch: pytest.MonkeyPatch, name: str, binding: int | None, reference: str) -> None:
    config = {"id": 42, "mdl_type": "chat", "llm_name": "resolved-model"}
    resolve_type = Mock(return_value=["chat", "image2text"])
    resolve_config = Mock(return_value=config)
    default = Mock(side_effect=AssertionError("An explicit model must not fall back to a default"))
    monkeypatch.setattr(dialog_service, "resolve_model_type", resolve_type)
    monkeypatch.setattr(dialog_service, "_resolve_model_config", resolve_config)
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", default)
    dialog = SimpleNamespace(tenant_id="owner", llm_id=name, tenant_llm_id=binding)
    assert dialog_service._resolve_dialog_primary_model_config(db, dialog) == config
    resolve_type.assert_called_once_with(db, "owner", reference)
    resolve_config.assert_called_once_with(db, "owner", binding, "chat", reference)
    default.assert_not_called()


def test_dialog_primary_model_uses_tenant_default_when_unbound(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"id": 42, "mdl_type": "chat", "llm_name": "default-model"}
    default = Mock(return_value=config)
    monkeypatch.setattr(dialog_service, "resolve_model_type", Mock(side_effect=AssertionError("Empty references cannot be resolved")))
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", default)
    dialog = SimpleNamespace(tenant_id="owner", llm_id="", tenant_llm_id=None)
    assert dialog_service._resolve_dialog_primary_model_config(db, dialog) == config
    default.assert_called_once_with(db, "owner", LLMType.CHAT)
    assert dialog.llm_id == "" and dialog.tenant_llm_id is None


@pytest.mark.parametrize("binding", [None, 42])
def test_invalid_explicit_dialog_model_never_uses_a_default(db: Session, monkeypatch: pytest.MonkeyPatch, binding: int | None) -> None:
    default = Mock(side_effect=AssertionError("Invalid explicit models must fail closed"))
    monkeypatch.setattr(dialog_service, "resolve_model_type", Mock(side_effect=LookupError("not authorized")))
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", default)
    dialog = SimpleNamespace(tenant_id="owner", llm_id="unavailable", tenant_llm_id=binding)
    with pytest.raises(LookupError, match="not authorized"):
        dialog_service._resolve_dialog_primary_model_config(db, dialog)
    default.assert_not_called()


def test_unset_tenant_default_remains_a_failure(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    def default(*args: Any) -> dict[str, Any]:
        raise ValueError("No default chat model is set")

    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", default)
    with pytest.raises(ValueError, match="No default"):
        dialog_service._resolve_dialog_primary_model_config(db, SimpleNamespace(tenant_id="owner", llm_id="", tenant_llm_id=None))
