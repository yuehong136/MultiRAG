from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db import init_data
from api.db.db_models import Tenant
from api.db.joint_services import user_account_service
from common.constants import LLMType


@pytest.mark.parametrize("chat_model,embedding_model", [("chat-default", "embed-default"), ("", "")])
def test_superuser_model_probes_use_serialized_tenant_identity(monkeypatch: pytest.MonkeyPatch, chat_model: str, embedding_model: str) -> None:
    db = Mock(spec=Session)
    tenant = Tenant(id="startup-tenant", llm_id=chat_model, embd_id=embedding_model)
    assert "id" not in tenant.to_dict()
    monkeypatch.setattr(init_data.UserService, "query_user_onlywith_email", Mock(return_value=None))
    save = Mock()
    monkeypatch.setattr(init_data.UserService, "save", save)
    monkeypatch.setattr(user_account_service, "initialize_user_tenant", Mock(return_value=tenant))
    resolved: list[tuple[str, str, str]] = []
    probes: list[str] = []

    def resolve(session: Session, tenant_id: str, model_type: str, model_name: str) -> dict[str, Any]:
        assert session is db
        assert db.commit.called
        resolved.append((tenant_id, model_type, model_name))
        return {"model_name": model_name}

    class Bundle:
        def __init__(self, session: Session, tenant_id: str, config: dict[str, Any]) -> None:
            assert session is db
            assert tenant_id == tenant.id
            self.model = str(config["model_name"])

        async def async_chat(self, **kwargs: Any) -> str:
            assert kwargs["history"] == [{"role": "user", "content": "Hello!"}]
            probes.append(self.model)
            return "ready"

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            assert texts == ["Hello!"]
            probes.append(self.model)
            return [[0.0, 1.0]], 1

    monkeypatch.setattr(init_data, "get_model_config_by_type_and_name", resolve)
    monkeypatch.setattr(init_data, "LLMBundle", Bundle)
    init_data.init_superuser(db, email="startup@example.test", password="fixture-password")
    expected = []
    if chat_model:
        expected.append((tenant.id, LLMType.CHAT.value, chat_model))
    if embedding_model:
        expected.append((tenant.id, LLMType.EMBEDDING.value, embedding_model))
    assert resolved == expected
    assert probes == [item[2] for item in expected]
    assert save.call_args.kwargs["commit"] is False
    db.commit.assert_called_once_with()
    db.rollback.assert_not_called()
